"""Executable contract checks using in-memory fakes, not an agent profile.

No data validation rules, external tools, or model integrations live here.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from typing import Callable

from agent_runtime import (
    ActionError, ActionRequest, ActionResult, AgentRuntime, Check, Context,
    Goal, GoalAssessment, GoalStatus, IntentionStatus, Plan, PlanningGap,
    PlanProposal, RuntimeLimits, RuntimeStatus, Step, Task,
)


TASK = Task("test-task", "Exercise runtime contracts")


@dataclass(frozen=True)
class FakeSnapshot:
    revision: str
    achieved: frozenset[str]
    impossible: frozenset[str]


class FakeProfile:
    """A controllable test double, with no real beliefs or domain predicates."""

    def __init__(self, goals=(), plans=()):
        self.current_goals = list(goals)
        self.library = list(plans)
        self.achieved: set[str] = set()
        self.impossible: set[str] = set()
        self.version = 0
        self.plan_check: Callable[[Plan, Context], Check] = lambda p, c: Check(True)
        self.step_check: Callable[[Step, Context], Check] = lambda s, c: Check(True)
        self.proposal_check = Check(True)
        self.integration_error: Exception | None = None
        self.integrations: list[ActionRequest] = []
        self.validations: list[PlanProposal] = []

    async def observe(self, task):
        return FakeSnapshot(str(self.version), frozenset(self.achieved),
                            frozenset(self.impossible))

    async def goals(self, task, beliefs):
        return tuple(self.current_goals)

    async def assess_goal(self, context):
        if context.goal.id in context.beliefs.impossible:
            return GoalAssessment(GoalStatus.IMPOSSIBLE, "Test marked impossible")
        if context.goal.id in context.beliefs.achieved:
            return GoalAssessment(GoalStatus.ACHIEVED)
        return GoalAssessment(GoalStatus.PENDING)

    async def plans(self, context):
        return tuple(p for p in self.library if p.goal_id == context.goal.id)

    async def plan_applicable(self, plan, context):
        return self.plan_check(plan, context)

    async def step_applicable(self, step, context):
        return self.step_check(step, context)

    async def integrate(self, request, result):
        self.integrations.append(request)
        if self.integration_error is not None:
            raise self.integration_error
        completed = result.observations.get("complete_goal")
        if isinstance(completed, str):
            self.achieved.add(completed)
        self.version += 1

    async def validate_proposal(self, proposal, context):
        self.validations.append(proposal)
        return self.proposal_check


def step(name: str, *, complete=False, **kwargs):
    return Step(name, name, {"complete_goal": complete}, **kwargs)


def plan(name: str, goal="g", *, steps=None, priority=0):
    return Plan(name, goal, tuple(steps or [step(name, complete=True)]), priority)


class RecordingExecutor:
    def __init__(self, outcomes=(), allowed=None):
        self.calls: list[ActionRequest] = []
        self.outcomes = list(outcomes)
        self.allowed = allowed

    def supports(self, action):
        return self.allowed is None or action in self.allowed

    async def execute(self, request):
        self.calls.append(request)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                return outcome
        if request.step.parameters.get("complete_goal"):
            return ActionResult({"complete_goal": request.context.goal.id})
        return ActionResult()


class BlockingExecutor(RecordingExecutor):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, request):
        self.started.set()
        await self.release.wait()
        return await super().execute(request)


class FakeProposals:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls: list[PlanningGap] = []

    async def propose(self, gap):
        self.calls.append(gap)
        return self.proposal


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def finish(self, runtime, *, budget=30):
        for _ in range(budget):
            if runtime.state != RuntimeStatus.RUNNING:
                return runtime.result()
            await runtime.tick()
        self.fail("Runtime did not terminate within the fixture's tick budget")

    async def test_goals_use_priority_then_identifier(self):
        profile = FakeProfile([Goal("b", 2), Goal("z", 9), Goal("a", 2)],
                              [plan("pb", "b"), plan("pz", "z"), plan("pa", "a")])
        executor = RecordingExecutor()
        result = await AgentRuntime(profile, executor).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual([r.context.goal.id for r in executor.calls], ["z", "a", "b"])

    async def test_plans_use_priority_then_identifier(self):
        profile = FakeProfile([Goal("g")], [plan("low"), plan("b", priority=7),
                                            plan("a", priority=7)])
        executor = RecordingExecutor()
        result = await AgentRuntime(profile, executor).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual([r.plan_id for r in executor.calls], ["a"])

    async def test_exhausted_steps_do_not_prove_goal_achievement(self):
        profile = FakeProfile([Goal("g")], [plan("p", steps=[step("only")])])
        executor = RecordingExecutor()
        result = await AgentRuntime(profile, executor).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.FAILED)
        self.assertEqual(len(executor.calls), 1)
        self.assertNotIn("g", profile.achieved)

    async def test_already_achieved_goal_executes_nothing(self):
        profile = FakeProfile([Goal("g")], [plan("p")])
        profile.achieved.add("g")
        executor = RecordingExecutor()
        result = await AgentRuntime(profile, executor).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual(executor.calls, [])

    async def test_no_goals_is_blocked(self):
        result = await AgentRuntime(FakeProfile(), RecordingExecutor()).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.BLOCKED)
        self.assertEqual(result.actions, 0)

    async def test_no_plans_is_blocked(self):
        result = await AgentRuntime(FakeProfile([Goal("g")]), RecordingExecutor()).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.BLOCKED)
        self.assertEqual(result.actions, 0)

    async def test_plan_precondition_rechecked_before_next_step(self):
        profile = FakeProfile([Goal("g")], [
            plan("first", steps=[step("begin"), step("obsolete", complete=True)], priority=5),
            plan("fallback"),
        ])
        executor = RecordingExecutor()
        runtime = AgentRuntime(profile, executor)
        runtime.start(TASK)
        await runtime.tick()
        profile.plan_check = lambda p, c: Check(p.id != "first", "Test invalidation")
        profile.version += 1
        result = await self.finish(runtime)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual([r.step.id for r in executor.calls], ["begin", "fallback"])

    async def test_step_precondition_rechecked_before_next_step(self):
        profile = FakeProfile([Goal("g")], [
            plan("first", steps=[step("begin"), step("obsolete", complete=True)], priority=5),
            plan("fallback"),
        ])
        executor = RecordingExecutor()
        runtime = AgentRuntime(profile, executor)
        runtime.start(TASK)
        await runtime.tick()
        profile.step_check = lambda s, c: Check(s.id != "obsolete", "Test invalidation")
        profile.version += 1
        result = await self.finish(runtime)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual([r.step.id for r in executor.calls], ["begin", "fallback"])

    async def test_revision_change_during_selection_defers_execution(self):
        profile = FakeProfile([Goal("g")], [plan("p")])
        changed = False

        def change_once(selected_step, context):
            nonlocal changed
            if not changed:
                profile.version += 1
                changed = True
            return Check(True)

        profile.step_check = change_once
        executor = RecordingExecutor()
        runtime = AgentRuntime(profile, executor)
        runtime.start(TASK)
        status = await runtime.tick()
        self.assertEqual(status, RuntimeStatus.RUNNING)
        self.assertEqual(executor.calls, [])
        result = await self.finish(runtime)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual(executor.calls[0].context.beliefs.revision, "1")

    async def test_unresolved_gap_rechecks_revision_before_becoming_blocked(self):
        profile = FakeProfile([Goal("g")])

        class ChangingProvider(FakeProposals):
            async def propose(self, gap):
                self.calls.append(gap)
                profile.achieved.add("g")
                profile.version += 1
                await asyncio.sleep(0)
                return None

        executor = RecordingExecutor()
        runtime = AgentRuntime(profile, executor, proposals=ChangingProvider(None))
        runtime.start(TASK)
        self.assertEqual(await runtime.tick(), RuntimeStatus.RUNNING)
        self.assertEqual(executor.calls, [])
        self.assertEqual(await runtime.tick(), RuntimeStatus.SUCCEEDED)

    async def test_cancel_scheduled_by_assessment_wins_over_success(self):
        class CancellingProfile(FakeProfile):
            async def assess_goal(self, context):
                asyncio.get_running_loop().call_soon(runtime.cancel)
                return GoalAssessment(GoalStatus.ACHIEVED)

        profile = CancellingProfile([Goal("g")])
        executor = RecordingExecutor()
        runtime = AgentRuntime(profile, executor)
        result = await runtime.run(TASK)
        self.assertEqual(result.status, RuntimeStatus.CANCELLED)
        self.assertEqual(executor.calls, [])

    async def test_stale_achievement_is_reassessed_before_success(self):
        class ChangingProfile(FakeProfile):
            changed = False

            async def assess_goal(self, context):
                if not self.changed:
                    self.changed = True
                    self.achieved.remove("g")
                    self.version += 1
                return await super().assess_goal(context)

        profile = ChangingProfile([Goal("g")], [plan("p")])
        profile.achieved.add("g")
        executor = RecordingExecutor()
        runtime = AgentRuntime(profile, executor)
        runtime.start(TASK)
        self.assertEqual(await runtime.tick(), RuntimeStatus.RUNNING)
        self.assertEqual(executor.calls, [])
        result = await self.finish(runtime)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0].context.beliefs.revision, "1")

    async def test_removed_support_for_completed_step_does_not_discard_cursor(self):
        profile = FakeProfile([Goal("g")], [
            plan("p", steps=[step("first"), step("second", complete=True)])
        ])
        executor = RecordingExecutor(allowed={"first", "second"})
        runtime = AgentRuntime(profile, executor)
        runtime.start(TASK)
        await runtime.tick()
        original_intention = executor.calls[0].intention_id
        executor.allowed.remove("first")
        profile.version += 1
        result = await self.finish(runtime)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual([r.step.id for r in executor.calls], ["first", "second"])
        self.assertEqual(executor.calls[1].intention_id, original_intention)
        self.assertEqual(result.replans, 0)
        self.assertEqual(result.intentions[0].next_step, 2)

    async def test_action_error_selects_fallback_without_replaying_failed_plan(self):
        profile = FakeProfile([Goal("g")], [plan("first", priority=5), plan("fallback")])
        executor = RecordingExecutor([ActionError("First plan failed")])
        result = await AgentRuntime(profile, executor).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual([r.plan_id for r in executor.calls], ["first", "fallback"])

    async def test_retry_requires_explicit_safety_and_keeps_idempotency_key(self):
        with self.assertRaises(ValueError):
            step("unsafe", max_retries=1)
        retry_step = step("retry", complete=True, retry_safe=True, max_retries=1)
        profile = FakeProfile([Goal("g")], [plan("p", steps=[retry_step])])
        executor = RecordingExecutor([ActionError("Transient", retryable=True)])
        result = await AgentRuntime(profile, executor).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(executor.calls[0].idempotency_key, executor.calls[1].idempotency_key)
        self.assertEqual(executor.calls[0].intention_id, executor.calls[1].intention_id)
        self.assertEqual(executor.calls[1].attempt, executor.calls[0].attempt + 1)
        self.assertEqual(len(profile.integrations), 1)

    async def test_nonretryable_action_error_is_not_retried(self):
        profile = FakeProfile([Goal("g")], [
            plan("p", steps=[step("s", complete=True, retry_safe=True, max_retries=3)])
        ])
        executor = RecordingExecutor([ActionError("Permanent", retryable=False)])
        result = await AgentRuntime(profile, executor).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.FAILED)
        self.assertEqual(len(executor.calls), 1)

    async def test_action_timeout_cancels_unsafe_step_and_selects_fallback(self):
        class TimeoutExecutor(RecordingExecutor):
            def __init__(self):
                super().__init__()
                self.block = asyncio.Event()
                self.slow_cancelled = False

            async def execute(self, request):
                if request.step.id == "slow":
                    self.calls.append(request)
                    try:
                        await self.block.wait()
                    finally:
                        self.slow_cancelled = True
                return await super().execute(request)

        profile = FakeProfile([Goal("g")], [
            plan("first", steps=[step("slow", timeout_seconds=0.01)], priority=5),
            plan("fallback"),
        ])
        executor = TimeoutExecutor()
        result = await AgentRuntime(profile, executor).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertTrue(executor.slow_cancelled)
        self.assertEqual([r.step.id for r in executor.calls], ["slow", "fallback"])
        self.assertEqual(len(profile.integrations), 1)

    async def test_observe_timeout_fails_without_dispatching_action(self):
        class SlowProfile(FakeProfile):
            def __init__(self):
                super().__init__([Goal("g")], [plan("p")])
                self.block = asyncio.Event()
                self.observe_cancelled = False

            async def observe(self, task):
                try:
                    await self.block.wait()
                finally:
                    self.observe_cancelled = True

        profile = SlowProfile()
        executor = RecordingExecutor()
        result = await AgentRuntime(profile, executor,
            limits=RuntimeLimits(hook_timeout_seconds=0.01)).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.FAILED)
        self.assertTrue(profile.observe_cancelled)
        self.assertEqual(result.actions, 0)
        self.assertEqual(executor.calls, [])

    async def test_integration_exception_does_not_repeat_completed_action(self):
        profile = FakeProfile([Goal("g")], [plan("first", priority=5), plan("fallback")])
        profile.integration_error = RuntimeError("Cannot persist observation")
        executor = RecordingExecutor()
        result = await AgentRuntime(profile, executor).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.FAILED)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(len(profile.integrations), 1)

    async def test_action_limit_counts_attempts(self):
        profile = FakeProfile([Goal("g")], [
            plan("p", steps=[step("s", complete=True, retry_safe=True, max_retries=3)])
        ])
        executor = RecordingExecutor([ActionError("Transient", retryable=True)] * 3)
        result = await AgentRuntime(profile, executor, limits=RuntimeLimits(max_actions=2)).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.FAILED)
        self.assertEqual(result.actions, 2)
        self.assertEqual(len(executor.calls), 2)

    async def test_cycle_limit_prevents_another_action(self):
        profile = FakeProfile([Goal("g")], [
            plan("p", steps=[step("first"), step("second", complete=True)])
        ])
        executor = RecordingExecutor()
        result = await AgentRuntime(profile, executor, limits=RuntimeLimits(max_cycles=1)).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.FAILED)
        self.assertEqual(result.cycles, 1)
        self.assertEqual(len(executor.calls), 1)

    async def test_final_allowed_action_can_succeed_at_both_limits(self):
        profile = FakeProfile([Goal("g")], [plan("p")])
        executor = RecordingExecutor()
        result = await AgentRuntime(profile, executor,
            limits=RuntimeLimits(max_actions=1, max_cycles=1)).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual(result.actions, 1)
        self.assertEqual(result.cycles, 1)
        self.assertEqual(len(executor.calls), 1)

    async def test_zero_replans_allows_initial_plan_but_no_fallback(self):
        profile = FakeProfile([Goal("g")], [plan("first", priority=5), plan("fallback")])
        executor = RecordingExecutor([ActionError("Test failure")])
        result = await AgentRuntime(profile, executor, limits=RuntimeLimits(max_replans=0)).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.FAILED)
        self.assertEqual([r.plan_id for r in executor.calls], ["first"])

    async def test_withdrawn_goal_drops_intention(self):
        profile = FakeProfile([Goal("g")], [
            plan("p", steps=[step("first"), step("second", complete=True)])
        ])
        executor = RecordingExecutor()
        runtime = AgentRuntime(profile, executor)
        runtime.start(TASK)
        await runtime.tick()
        profile.current_goals.clear()
        profile.version += 1
        result = await self.finish(runtime)
        self.assertEqual(result.status, RuntimeStatus.BLOCKED)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(result.intentions[0].status, IntentionStatus.DROPPED)

    async def test_impossible_goal_is_not_continued(self):
        profile = FakeProfile([Goal("g")], [
            plan("p", steps=[step("first"), step("second", complete=True)])
        ])
        executor = RecordingExecutor()
        runtime = AgentRuntime(profile, executor)
        runtime.start(TASK)
        await runtime.tick()
        profile.impossible.add("g")
        profile.version += 1
        result = await self.finish(runtime)
        self.assertEqual(result.status, RuntimeStatus.FAILED)
        self.assertEqual(len(executor.calls), 1)
        self.assertNotEqual(result.intentions[0].status, IntentionStatus.ACTIVE)

    async def test_higher_priority_goal_preempts_then_resumes_saved_cursor(self):
        profile = FakeProfile([Goal("low")], [
            plan("low-plan", "low", steps=[step("low-first"), step("low-last", complete=True)]),
            plan("high-plan", "high"),
        ])
        executor = RecordingExecutor()
        runtime = AgentRuntime(profile, executor)
        runtime.start(TASK)
        await runtime.tick()
        low_id = executor.calls[0].intention_id
        profile.current_goals.append(Goal("high", 10))
        profile.version += 1
        await runtime.tick()
        result = await self.finish(runtime)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual([r.step.id for r in executor.calls], ["low-first", "high-plan", "low-last"])
        self.assertEqual(executor.calls[-1].intention_id, low_id)
        low = next(i for i in result.intentions if i.id == low_id)
        self.assertEqual(low.next_step, 2)

    async def test_validated_proposal_can_fill_missing_plan(self):
        profile = FakeProfile([Goal("g")])
        proposal = PlanProposal(plan("suggested"), "Fixture suggestion")
        provider = FakeProposals(proposal)
        executor = RecordingExecutor()
        result = await AgentRuntime(profile, executor, proposals=provider).run(TASK)
        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual(profile.validations, [proposal])
        self.assertEqual([r.plan_id for r in executor.calls], ["suggested"])
        self.assertEqual(len(provider.calls), 1)

    async def test_rejected_or_wrong_goal_proposal_never_executes(self):
        for wrong_goal in (False, True):
            with self.subTest(wrong_goal=wrong_goal):
                profile = FakeProfile([Goal("g")])
                if not wrong_goal:
                    profile.proposal_check = Check(False, "Rejected by profile")
                proposal = PlanProposal(plan("suggested", "other" if wrong_goal else "g"))
                provider = FakeProposals(proposal)
                executor = RecordingExecutor()
                result = await AgentRuntime(profile, executor, proposals=provider,
                    limits=RuntimeLimits(max_gap_requests=1)).run(TASK)
                self.assertEqual(result.status, RuntimeStatus.BLOCKED)
                self.assertEqual(executor.calls, [])
                self.assertEqual(len(provider.calls), 1)

    async def test_gap_request_limit_is_global_across_pending_goals(self):
        for maximum in (0, 1):
            with self.subTest(max_gap_requests=maximum):
                profile = FakeProfile([Goal("c"), Goal("a"), Goal("b")])
                provider = FakeProposals(None)
                executor = RecordingExecutor()
                result = await AgentRuntime(profile, executor, proposals=provider,
                    limits=RuntimeLimits(max_gap_requests=maximum)).run(TASK)
                self.assertEqual(result.status, RuntimeStatus.BLOCKED)
                self.assertEqual(len(provider.calls), maximum)
                self.assertEqual(executor.calls, [])
                if maximum:
                    self.assertEqual(provider.calls[0].context.goal.id, "a")

    async def test_allowlist_applies_to_library_and_proposed_plans(self):
        for proposed in (False, True):
            with self.subTest(proposed=proposed):
                forbidden = plan("forbidden")
                profile = FakeProfile([Goal("g")], [] if proposed else [forbidden])
                provider = FakeProposals(PlanProposal(forbidden)) if proposed else None
                executor = RecordingExecutor(allowed={"allowed"})
                result = await AgentRuntime(profile, executor, proposals=provider,
                    limits=RuntimeLimits(max_gap_requests=1)).run(TASK)
                self.assertEqual(result.status, RuntimeStatus.BLOCKED)
                self.assertEqual(executor.calls, [])

    async def test_explicit_cancel_stops_inflight_execution(self):
        profile = FakeProfile([Goal("g")], [plan("p")])
        executor = BlockingExecutor()
        runtime = AgentRuntime(profile, executor)
        running = asyncio.create_task(runtime.run(TASK))
        await asyncio.wait_for(executor.started.wait(), 1)
        runtime.cancel()
        result = await asyncio.wait_for(running, 1)
        self.assertEqual(result.status, RuntimeStatus.CANCELLED)
        self.assertEqual(profile.integrations, [])

    async def test_cancelling_run_task_records_cancelled_and_propagates(self):
        profile = FakeProfile([Goal("g")], [plan("p")])
        executor = BlockingExecutor()
        runtime = AgentRuntime(profile, executor)
        running = asyncio.create_task(runtime.run(TASK))
        await asyncio.wait_for(executor.started.wait(), 1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(runtime.result().status, RuntimeStatus.CANCELLED)
        self.assertEqual(profile.integrations, [])

    async def test_concurrent_run_cannot_reset_an_active_runtime(self):
        profile = FakeProfile([Goal("g")], [plan("p")])
        executor = BlockingExecutor()
        runtime = AgentRuntime(profile, executor)
        running = asyncio.create_task(runtime.run(TASK))
        try:
            await asyncio.wait_for(executor.started.wait(), 1)
            with self.assertRaises(RuntimeError):
                await runtime.run(Task("other", "Concurrent invocation"))
            self.assertEqual(runtime.result().task.id, TASK.id)
        finally:
            runtime.cancel()
            await asyncio.wait_for(running, 1)


if __name__ == "__main__":
    unittest.main()
