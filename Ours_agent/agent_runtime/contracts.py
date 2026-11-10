"""Integration points for the other three team members. All I/O is async."""

from typing import Protocol, Sequence

from .models import (
    ActionRequest, ActionResult, BeliefSnapshot, Check, Context, Goal,
    GoalAssessment, Plan, PlanningGap, PlanProposal, Step, Task,
)


class AgentProfile(Protocol):
    """Person 2 owns facts, predicates, goals and the plan library.

    All methods except integrate must be side-effect free. Goal/plan/step IDs
    retain their meaning for a task. Snapshots must remain immutable while used.
    Revision changes cover all inputs to predicates, including goals and plans.
    """

    async def observe(self, task: Task) -> BeliefSnapshot: ...

    async def goals(self, task: Task, beliefs: BeliefSnapshot) -> Sequence[Goal]:
        """Authoritative current goals; omitting a previous ID withdraws it.

        Every returned goal is required. Return achieved goals as well; return an
        empty sequence to signal that no current goal can be pursued (BLOCKED).
        """
        ...

    async def assess_goal(self, context: Context) -> GoalAssessment: ...

    async def plans(self, context: Context) -> Sequence[Plan]: ...

    async def plan_applicable(self, plan: Plan, context: Context) -> Check:
        """Conditions that must hold both at adoption and during continuation.

        Entry-only requirements belong in the first step's preconditions.
        """
        ...

    async def step_applicable(self, step: Step, context: Context) -> Check: ...

    async def integrate(self, request: ActionRequest, result: ActionResult) -> None: ...

    async def validate_proposal(self, proposal: PlanProposal, context: Context) -> Check: ...


class ActionExecutor(Protocol):
    """Person 4 owns the action allowlist and actual tools.

    execute must be cancellable async I/O; a blocking call needs an adapter.
    Cancellation/timeout does not prove a remote side effect was rolled back.
    """

    def supports(self, action: str) -> bool: ...

    async def execute(self, request: ActionRequest) -> ActionResult: ...


class ProposalProvider(Protocol):
    """Optional module from person 3. It proposes data, never executable code."""

    async def propose(self, gap: PlanningGap) -> PlanProposal | None: ...
