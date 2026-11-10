"""A bounded, sequential BDI interpreter with injected domain adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Awaitable, TypeVar
from uuid import uuid4

from .contracts import ActionExecutor, AgentProfile, ProposalProvider
from .models import (
    ActionError, ActionRequest, ActionResult, BeliefSnapshot, Check, Context,
    Event, Goal, GoalAssessment, GoalStatus, Intention, IntentionStatus, Plan,
    PlanningGap, PlanProposal, RunResult, RuntimeLimits, RuntimeStatus, Task,
)

T = TypeVar("T")


@dataclass
class _Execution:
    plan: Plan
    record_index: int


class AgentRuntime:
    """One task at a time; independent instances can run concurrently.

    Use run(task) for automatic driving, or start(task)/tick()/result() for
    external scheduling. A tick dispatches at most one action. Concurrent drivers
    are rejected, while cancel() can be called by another coroutine.
    """

    def __init__(
        self,
        profile: AgentProfile,
        executor: ActionExecutor,
        proposals: ProposalProvider | None = None,
        limits: RuntimeLimits | None = None,
    ) -> None:
        self._profile = profile
        self._executor = executor
        self._proposals = proposals
        self._limits = limits or RuntimeLimits()
        self._state = RuntimeStatus.IDLE
        self._busy = False
        self._current_call: asyncio.Future[object] | None = None
        self._task: Task | None = None
        self._events: list[Event] = []
        self._records: list[Intention] = []

    @property
    def state(self) -> RuntimeStatus:
        return self._state

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def intentions(self) -> tuple[Intention, ...]:
        return tuple(self._records)

    def start(self, task: Task) -> None:
        """Start a fresh in-memory session; previous results remain immutable."""
        if self._busy or self._state == RuntimeStatus.RUNNING:
            raise RuntimeError("this runtime already has a running task")
        if not isinstance(task, Task):
            raise TypeError("task must be a Task")
        self._task = task
        self._state = RuntimeStatus.RUNNING
        self._reason = ""
        self._beliefs: BeliefSnapshot | None = None
        self._events = []
        self._records = []
        self._executions: dict[str, _Execution] = {}
        self._active_goal: str | None = None
        self._failed_plans: set[tuple[str, str]] = set()
        self._invalid_plans: set[tuple[str, str, str]] = set()
        self._known_plans: dict[tuple[str, str], Plan] = {}
        self._requested_gaps: set[tuple[str, str]] = set()
        self._cycles = self._actions = self._replans = self._gap_requests = 0
        self._cancel_requested = False
        self._emit("task_started", task_id=task.id)

    async def run(self, task: Task) -> RunResult:
        """Run until success, failure, an unresolved gap, or cancellation."""
        self.start(task)
        self._busy = True
        try:
            while self._state == RuntimeStatus.RUNNING:
                await self._safe_tick()
            return self.result()
        finally:
            self._busy = False

    async def tick(self) -> RuntimeStatus:
        if self._busy:
            raise RuntimeError("another driver is already advancing this runtime")
        if self._task is None:
            raise RuntimeError("call start(task) first")
        if self._state != RuntimeStatus.RUNNING:
            return self._state
        self._busy = True
        try:
            await self._safe_tick()
            return self._state
        finally:
            self._busy = False

    def cancel(self) -> None:
        """Request cooperative cancellation, including the current adapter call."""
        if self._state == RuntimeStatus.RUNNING:
            self._cancel_requested = True
            if self._current_call is not None:
                self._current_call.cancel()

    def result(self) -> RunResult:
        """An immutable report, also available during manual execution."""
        if self._task is None:
            raise RuntimeError("no task has been started")
        return RunResult(
            task=self._task,
            status=self._state,
            reason=self._reason,
            belief_revision=self._beliefs.revision if self._beliefs is not None else None,
            cycles=self._cycles,
            actions=self._actions,
            replans=self._replans,
            events=self.events,
            intentions=self.intentions,
        )

    async def _call(self, awaitable: Awaitable[T], timeout: float | None = None) -> T:
        # Keep a child task so cancel() cannot cancel the caller's unrelated work.
        pending = asyncio.ensure_future(awaitable)
        self._current_call = pending
        if self._cancel_requested:
            pending.cancel()
        try:
            result = await asyncio.wait_for(
                pending,
                timeout=self._limits.hook_timeout_seconds if timeout is None else timeout,
            )
            # The child may have completed just before cancel() was scheduled;
            # cancelling an already completed child alone cannot stop the driver.
            if self._cancel_requested:
                raise asyncio.CancelledError
            return result
        finally:
            self._current_call = None

    async def _safe_tick(self) -> None:
        try:
            if self._cancel_requested:
                self._finish(RuntimeStatus.CANCELLED, "cancellation requested")
            else:
                await self._tick()
        except asyncio.CancelledError:
            self._finish(RuntimeStatus.CANCELLED, "execution cancelled")
            if not self._cancel_requested:
                raise
        except Exception as exc:
            # In particular, an integrate failure must never replay the action.
            self._emit("adapter_error", error_type=type(exc).__name__, error=str(exc))
            self._finish(RuntimeStatus.FAILED, f"adapter error: {type(exc).__name__}: {exc}")

    async def _observe(self) -> BeliefSnapshot:
        assert self._task is not None
        beliefs = await self._call(self._profile.observe(self._task))
        if not isinstance(beliefs.revision, str) or not beliefs.revision:
            raise ValueError("snapshot.revision must be a non-empty string")
        old_revision = self._beliefs.revision if self._beliefs is not None else None
        self._beliefs = beliefs
        if old_revision != beliefs.revision:
            self._emit("beliefs_observed", revision=beliefs.revision)
        return beliefs

    async def _tick(self) -> None:
        assert self._task is not None
        beliefs = await self._observe()
        goals = tuple(await self._call(self._profile.goals(self._task, beliefs)))
        if any(not isinstance(goal, Goal) for goal in goals):
            raise TypeError("profile.goals must return Goal objects")
        if len({goal.id for goal in goals}) != len(goals):
            raise ValueError("goal ids must be unique")
        assessments: dict[str, GoalAssessment] = {}
        for goal in goals:
            assessment = await self._call(
                self._profile.assess_goal(Context(self._task, beliefs, goal))
            )
            if not isinstance(assessment, GoalAssessment):
                raise TypeError("assess_goal must return GoalAssessment")
            assessments[goal.id] = assessment
            self._emit("goal_assessed", goal_id=goal.id, status=assessment.status.value,
                       reason=assessment.reason)
        impossible = [goal for goal in goals
                      if assessments[goal.id].status == GoalStatus.IMPOSSIBLE]
        pending = [goal for goal in goals
                   if assessments[goal.id].status == GoalStatus.PENDING]
        if not goals or impossible or not pending:
            latest = await self._observe()
            if latest.revision != beliefs.revision:
                if self._cycles >= self._limits.max_cycles:
                    self._finish(RuntimeStatus.FAILED, "cycle limit exhausted")
                else:
                    self._cycles += 1
                    self._emit("decision_stale", old_revision=beliefs.revision,
                               revision=latest.revision)
                return
        self._reconcile(assessments)
        if not goals:
            self._finish(RuntimeStatus.BLOCKED, "profile supplied no goals")
            return
        if impossible:
            self._finish(RuntimeStatus.FAILED, f"goal impossible: {impossible[0].id}")
            return
        if not pending:
            self._finish(RuntimeStatus.SUCCEEDED, "all current goals are achieved")
            return
        # Evaluate achievement before enforcing limits: the final permitted action
        # may have completed the goal, which must still count as success.
        if self._cycles >= self._limits.max_cycles:
            self._finish(RuntimeStatus.FAILED, "cycle limit exhausted")
            return
        if self._actions >= self._limits.max_actions:
            self._finish(RuntimeStatus.FAILED, "action limit exhausted")
            return
        if self._replan_limit_exceeded():
            return
        self._cycles += 1
        selected = await self._select(pending, beliefs)
        if self._state != RuntimeStatus.RUNNING:
            return
        # Slow deliberation (including an LLM request) may have used an old view.
        # Check both action dispatch and a decision that no plan exists.
        latest = await self._observe()
        if latest.revision != beliefs.revision:
            self._emit("decision_stale", old_revision=beliefs.revision,
                       revision=latest.revision)
            return
        if selected is None:
            has_failed = any(item.status == IntentionStatus.FAILED for item in self._records)
            self._finish(
                RuntimeStatus.FAILED if has_failed else RuntimeStatus.BLOCKED,
                "no applicable plan for pending goals",
            )
            return
        goal, execution = selected
        self._activate(goal.id, execution)
        await self._execute(goal, execution, latest)

    def _reconcile(self, assessments: dict[str, GoalAssessment]) -> None:
        for goal_id in tuple(self._executions):
            assessment = assessments.get(goal_id)
            if assessment is None:
                self._retire(goal_id, IntentionStatus.DROPPED, "goal withdrawn")
            elif assessment.status == GoalStatus.ACHIEVED:
                self._retire(goal_id, IntentionStatus.SUCCEEDED, assessment.reason or "goal achieved")
            elif assessment.status == GoalStatus.IMPOSSIBLE:
                self._retire(goal_id, IntentionStatus.FAILED, assessment.reason or "goal impossible")

    async def _select(
        self, goals: list[Goal], beliefs: BeliefSnapshot,
    ) -> tuple[Goal, _Execution] | None:
        assert self._task is not None
        for goal in sorted(goals, key=lambda item: (-item.priority, item.id)):
            context = Context(self._task, beliefs, goal)
            execution = self._executions.get(goal.id)
            if execution is not None:
                record = self._records[execution.record_index]
                if record.next_step >= len(execution.plan.steps):
                    self._discard(goal.id, failed=True, reason="plan exhausted without achieving goal")
                else:
                    check = await self._applicable(execution.plan, context, record.next_step)
                    if check.allowed:
                        return goal, execution
                    self._discard(goal.id, failed=False, reason=check.reason or "precondition changed")
                if self._replan_limit_exceeded():
                    return None
            candidates = tuple(await self._call(self._profile.plans(context)))
            if any(not isinstance(plan, Plan) or plan.goal_id != goal.id for plan in candidates):
                raise ValueError("profile plans must be Plan objects for the requested goal")
            if len({plan.id for plan in candidates}) != len(candidates):
                raise ValueError("plan ids must be unique within a goal")
            rejected: list[str] = []
            for plan in sorted(candidates, key=lambda item: (-item.priority, item.id)):
                self._remember_plan(plan)
                if self._excluded(plan, beliefs.revision):
                    rejected.append(f"{plan.id}: previously rejected or failed")
                    continue
                check = await self._applicable(plan, context)
                if check.allowed:
                    return goal, self._adopt(plan)
                rejected.append(f"{plan.id}: {check.reason}")
                self._emit("plan_rejected", goal_id=goal.id, plan_id=plan.id, reason=check.reason)
            proposal = await self._resolve_gap(context, tuple(rejected))
            if proposal is not None:
                return goal, self._adopt(proposal)
        return None

    def _remember_plan(self, plan: Plan) -> None:
        key = (plan.goal_id, plan.id)
        previous = self._known_plans.get(key)
        if previous is not None and previous != plan:
            raise ValueError(f"plan {plan.id!r} changed definition; use a new plan id")
        self._known_plans[key] = plan

    def _excluded(self, plan: Plan, revision: str) -> bool:
        return ((plan.goal_id, plan.id) in self._failed_plans
                or (plan.goal_id, plan.id, revision) in self._invalid_plans)

    async def _applicable(self, plan: Plan, context: Context, cursor: int = 0) -> Check:
        for step in plan.steps[cursor:]:
            if not self._executor.supports(step.action):
                return Check(False, f"unsupported action: {step.action}")
        check = await self._call(self._profile.plan_applicable(plan, context))
        if not isinstance(check, Check):
            raise TypeError("plan_applicable must return Check")
        if not check.allowed:
            return check
        check = await self._call(self._profile.step_applicable(plan.steps[cursor], context))
        if not isinstance(check, Check):
            raise TypeError("step_applicable must return Check")
        return check

    async def _resolve_gap(self, context: Context, rejected: tuple[str, ...]) -> Plan | None:
        key = (context.goal.id, context.beliefs.revision)
        if (self._proposals is None or key in self._requested_gaps
                or self._gap_requests >= self._limits.max_gap_requests):
            return None
        self._requested_gaps.add(key)
        self._gap_requests += 1
        self._emit("gap_requested", goal_id=context.goal.id, revision=context.beliefs.revision)
        proposal = await self._call(self._proposals.propose(PlanningGap(
            context, rejected, "no applicable plan in the supplied library",
        )))
        if proposal is None:
            self._emit("gap_unresolved", goal_id=context.goal.id)
            return None
        if not isinstance(proposal, PlanProposal) or not isinstance(proposal.plan, Plan):
            self._emit("proposal_rejected", goal_id=context.goal.id, reason="invalid proposal type")
            return None
        plan = proposal.plan
        reason = ""
        if plan.goal_id != context.goal.id:
            reason = "proposal targets a different goal"
        elif self._excluded(plan, context.beliefs.revision):
            reason = "proposal reuses an excluded plan"
        elif ((plan.goal_id, plan.id) in self._known_plans
              and self._known_plans[plan.goal_id, plan.id] != plan):
            reason = "proposal changes an existing plan id"
        if reason:
            self._emit("proposal_rejected", goal_id=context.goal.id, reason=reason)
            return None
        validation = await self._call(self._profile.validate_proposal(proposal, context))
        if not isinstance(validation, Check):
            raise TypeError("validate_proposal must return Check")
        if not validation.allowed:
            self._emit("proposal_rejected", goal_id=context.goal.id, reason=validation.reason)
            return None
        check = await self._applicable(plan, context)
        if not check.allowed:
            self._emit("proposal_rejected", goal_id=context.goal.id, reason=check.reason)
            return None
        self._remember_plan(plan)
        self._emit("proposal_accepted", goal_id=context.goal.id, plan_id=plan.id)
        return plan

    def _adopt(self, plan: Plan) -> _Execution:
        record = Intention(uuid4().hex, plan.goal_id, plan.id, IntentionStatus.SUSPENDED)
        execution = _Execution(plan, len(self._records))
        self._records.append(record)
        self._executions[plan.goal_id] = execution
        self._emit("intention_created", intention_id=record.id,
                   goal_id=plan.goal_id, plan_id=plan.id)
        return execution

    def _activate(self, goal_id: str, execution: _Execution) -> None:
        if self._active_goal is not None and self._active_goal != goal_id:
            old = self._executions[self._active_goal]
            self._update(old, status=IntentionStatus.SUSPENDED, reason="higher-priority applicable goal")
            self._emit("intention_suspended", goal_id=self._active_goal)
        record = self._records[execution.record_index]
        if record.status != IntentionStatus.ACTIVE:
            self._update(execution, status=IntentionStatus.ACTIVE, reason="")
            self._emit("intention_activated", intention_id=record.id, goal_id=goal_id,
                       plan_id=execution.plan.id, next_step=record.next_step)
        self._active_goal = goal_id

    async def _execute(self, goal: Goal, execution: _Execution, beliefs: BeliefSnapshot) -> None:
        assert self._task is not None
        record = self._records[execution.record_index]
        step = execution.plan.steps[record.next_step]
        attempt = record.attempts + 1
        self._update(execution, attempts=attempt)
        request = ActionRequest(
            context=Context(self._task, beliefs, goal),
            intention_id=record.id,
            plan_id=execution.plan.id,
            step=step,
            attempt=attempt,
            idempotency_key=f"{record.id}:{step.id}",
        )
        self._actions += 1
        self._emit("action_started", intention_id=record.id, goal_id=goal.id,
                   plan_id=execution.plan.id, step_id=step.id, attempt=attempt)
        error: ActionError | None = None
        try:
            result = await self._call(
                self._executor.execute(request),
                timeout=step.timeout_seconds or self._limits.action_timeout_seconds,
            )
        except TimeoutError:
            error = ActionError("action timeout; external outcome may be unknown", retryable=True)
        except ActionError as exc:
            error = exc
        except Exception as exc:
            error = ActionError(f"{type(exc).__name__}: {exc}")
        if error is not None:
            self._emit("action_failed", intention_id=record.id, step_id=step.id,
                       error=str(error), retryable=error.retryable)
            if step.retry_safe and error.retryable and attempt <= step.max_retries:
                self._emit("retry_scheduled", intention_id=record.id, step_id=step.id,
                           next_attempt=attempt + 1)
                if step.retry_delay_seconds:
                    await self._call(asyncio.sleep(step.retry_delay_seconds),
                                     timeout=step.retry_delay_seconds + self._limits.hook_timeout_seconds)
            else:
                self._discard(goal.id, failed=True, reason=str(error))
            return
        if not isinstance(result, ActionResult):
            raise TypeError("executor.execute must return ActionResult")
        # Tool success is not goal success. Only the profile can interpret this
        # evidence, and assess_goal on the next cycle confirms achievement.
        await self._call(self._profile.integrate(request, result))
        self._update(execution, next_step=record.next_step + 1, attempts=0)
        self._emit("action_completed", intention_id=record.id, step_id=step.id)

    def _discard(self, goal_id: str, *, failed: bool, reason: str) -> None:
        execution = self._executions[goal_id]
        if failed:
            self._failed_plans.add((goal_id, execution.plan.id))
        else:
            assert self._beliefs is not None
            self._invalid_plans.add((goal_id, execution.plan.id, self._beliefs.revision))
        self._replans += 1
        self._retire(goal_id, IntentionStatus.FAILED if failed else IntentionStatus.DROPPED, reason)
        self._emit("reconsideration", goal_id=goal_id, reason=reason, replans=self._replans)

    def _retire(self, goal_id: str, status: IntentionStatus, reason: str) -> None:
        execution = self._executions.pop(goal_id)
        self._update(execution, status=status, reason=reason)
        self._emit("intention_finished", intention_id=self._records[execution.record_index].id,
                   goal_id=goal_id, plan_id=execution.plan.id, status=status.value, reason=reason)
        if self._active_goal == goal_id:
            self._active_goal = None

    def _update(self, execution: _Execution, **changes: object) -> None:
        index = execution.record_index
        self._records[index] = replace(self._records[index], **changes)

    def _replan_limit_exceeded(self) -> bool:
        if self._replans > self._limits.max_replans:
            self._finish(RuntimeStatus.FAILED, "replan limit exhausted")
            return True
        return False

    def _finish(self, state: RuntimeStatus, reason: str) -> None:
        for goal_id in tuple(self._executions):
            self._retire(
                goal_id,
                IntentionStatus.CANCELLED if state == RuntimeStatus.CANCELLED else IntentionStatus.DROPPED,
                reason,
            )
        self._state = state
        self._reason = reason
        self._emit("task_finished", status=state.value, reason=reason)

    def _emit(self, kind: str, **details: object) -> None:
        self._events.append(Event(len(self._events) + 1, kind, details))
