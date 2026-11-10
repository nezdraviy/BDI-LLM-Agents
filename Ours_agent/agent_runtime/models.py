"""Общие сообщения и состояния runtime; предметных правил здесь нет."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Protocol


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _freeze(values: Mapping[str, object]) -> Mapping[str, object]:
    # Only the envelope is copied. Nested values belong to the supplying adapter.
    return MappingProxyType(dict(values))


class BeliefSnapshot(Protocol):
    """Read-only view supplied by person 2. A change requires a new revision.

    The runtime never reads domain facts. An implementation can expose additional
    typed fields or a fact mapping to its own predicates and tool adapters.
    """

    @property
    def revision(self) -> str: ...


class RuntimeStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GoalStatus(StrEnum):
    PENDING = "pending"
    ACHIEVED = "achieved"
    IMPOSSIBLE = "impossible"


class IntentionStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DROPPED = "dropped"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Task:
    id: str
    description: str
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.id, "task.id")
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True)
class Goal:
    id: str
    priority: int = 0

    def __post_init__(self) -> None:
        _identifier(self.id, "goal.id")
        if type(self.priority) is not int:
            raise ValueError("goal.priority must be an integer")


@dataclass(frozen=True)
class Check:
    allowed: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ValueError("check.allowed must be a bool")


@dataclass(frozen=True)
class GoalAssessment:
    status: GoalStatus
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, GoalStatus):
            raise ValueError("assessment.status must be a GoalStatus")


@dataclass(frozen=True)
class Step:
    id: str
    action: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    timeout_seconds: float | None = None
    max_retries: int = 0
    retry_safe: bool = False
    retry_delay_seconds: float = 0

    def __post_init__(self) -> None:
        _identifier(self.id, "step.id")
        _identifier(self.action, "step.action")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if type(self.retry_safe) is not bool:
            raise ValueError("retry_safe must be a bool")
        if self.max_retries and not self.retry_safe:
            raise ValueError("retries require retry_safe=True")
        if self.timeout_seconds is not None:
            _positive(self.timeout_seconds, "timeout_seconds")
        if not isfinite(self.retry_delay_seconds) or self.retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be finite and non-negative")
        object.__setattr__(self, "parameters", _freeze(self.parameters))


@dataclass(frozen=True)
class Plan:
    id: str
    goal_id: str
    steps: tuple[Step, ...]
    priority: int = 0

    def __post_init__(self) -> None:
        _identifier(self.id, "plan.id")
        _identifier(self.goal_id, "plan.goal_id")
        if type(self.priority) is not int:
            raise ValueError("plan.priority must be an integer")
        steps = tuple(self.steps)
        if not steps or any(not isinstance(step, Step) for step in steps):
            raise ValueError("a plan must contain at least one Step")
        if len({step.id for step in steps}) != len(steps):
            raise ValueError("step ids must be unique within a plan")
        object.__setattr__(self, "steps", steps)


@dataclass(frozen=True)
class Context:
    task: Task
    beliefs: BeliefSnapshot
    goal: Goal


@dataclass(frozen=True)
class ActionRequest:
    context: Context
    intention_id: str
    plan_id: str
    step: Step
    attempt: int
    idempotency_key: str


@dataclass(frozen=True)
class ActionResult:
    """Observations interpreted and stored by the profile, never by the runtime."""

    observations: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", _freeze(self.observations))


class ActionError(Exception):
    """An executor failure. Retrying also requires Step.retry_safe."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        if type(retryable) is not bool:
            raise ValueError("retryable must be a bool")
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class PlanningGap:
    context: Context
    rejected_plans: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class PlanProposal:
    plan: Plan
    explanation: str = ""


@dataclass(frozen=True)
class RuntimeLimits:
    max_cycles: int = 100
    max_actions: int = 100
    max_replans: int = 20
    max_gap_requests: int = 2
    hook_timeout_seconds: float = 10
    action_timeout_seconds: float = 30

    def __post_init__(self) -> None:
        for name in ("max_cycles", "max_actions"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_replans", "max_gap_requests"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        _positive(self.hook_timeout_seconds, "hook_timeout_seconds")
        _positive(self.action_timeout_seconds, "action_timeout_seconds")


def _positive(value: float, name: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class Event:
    sequence: int
    kind: str
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _freeze(self.details))


@dataclass(frozen=True)
class Intention:
    id: str
    goal_id: str
    plan_id: str
    status: IntentionStatus
    next_step: int = 0
    attempts: int = 0
    reason: str = ""


@dataclass(frozen=True)
class RunResult:
    task: Task
    status: RuntimeStatus
    reason: str
    belief_revision: str | None
    cycles: int
    actions: int
    replans: int
    events: tuple[Event, ...]
    intentions: tuple[Intention, ...]
