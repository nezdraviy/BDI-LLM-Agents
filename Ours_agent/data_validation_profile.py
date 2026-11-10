"""Domain profile for the first geological data-validation agent.

Place this file beside the ``agent_runtime`` package.

The action names and observation schemas in this module are a proposed v1
contract for coordination with the ActionExecutor (team member 4). They are
not implemented by AgentRuntime itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Sequence

from agent_runtime import (
    ActionRequest,
    ActionResult,
    BeliefSnapshot,
    Check,
    Context,
    Goal,
    GoalAssessment,
    GoalStatus,
    Plan,
    PlanProposal,
    Step,
    Task,
)


# Bump this when goal definitions, priorities, plan IDs, or predicate semantics change.
PROFILE_VERSION = "data-validation-profile-v1"

INVENTORY_GOAL = "inventory_complete"
VALIDATION_GOAL = "validation_complete"
REPORT_GOAL = "report_saved"

SCAN_ACTION = "scan_source_directory"
PARSE_ACTION = "parse_input_files"
VALIDATE_ACTION = "validate_input_data"
BUILD_REPORT_ACTION = "build_validation_report"
SAVE_REPORT_ACTION = "save_validation_report"

_ALLOWED_ACTIONS = {
    INVENTORY_GOAL: frozenset({SCAN_ACTION}),
    VALIDATION_GOAL: frozenset({PARSE_ACTION, VALIDATE_ACTION}),
    REPORT_GOAL: frozenset({BUILD_REPORT_ACTION, SAVE_REPORT_ACTION}),
}
_SEVERITIES = frozenset({"info", "warning", "error", "critical"})
_FORMATS = frozenset({"las", "csv"})


@dataclass(frozen=True)
class FileBelief:
    """Small immutable metadata record; raw log arrays stay with the executor."""

    path: str
    format: str
    parse_status: str = "not_parsed"
    fields: tuple[str, ...] = ()
    row_count: int | None = None
    parse_error: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.path, "file.path")
        normalized_format = self.format.strip().lower().lstrip(".")
        if normalized_format not in _FORMATS and normalized_format != "unknown":
            normalized_format = "unknown"
        object.__setattr__(self, "format", normalized_format)

        allowed_statuses = {"not_parsed", "parsed", "failed", "unsupported"}
        if self.parse_status not in allowed_statuses:
            raise ValueError(f"unsupported parse_status: {self.parse_status!r}")

        fields = tuple(_non_empty(item, "field name") for item in self.fields)
        object.__setattr__(self, "fields", fields)

        if self.row_count is not None:
            if type(self.row_count) is not int or self.row_count < 0:
                raise ValueError("row_count must be a non-negative integer or None")
        if self.parse_error is not None and not isinstance(self.parse_error, str):
            raise ValueError("parse_error must be a string or None")


@dataclass(frozen=True)
class ValidationFinding:
    """A data-quality result, not a technical exception raised by a tool."""

    file_path: str | None
    check_id: str
    severity: str
    message: str

    def __post_init__(self) -> None:
        if self.file_path is not None:
            _non_empty(self.file_path, "finding.file_path")
        _non_empty(self.check_id, "finding.check_id")
        if self.severity not in _SEVERITIES:
            raise ValueError(f"severity must be one of {sorted(_SEVERITIES)}")
        _non_empty(self.message, "finding.message")


@dataclass(frozen=True)
class DataValidationBeliefs:
    """Immutable domain-specific BeliefSnapshot."""

    revision: str
    task_id: str
    source_path: str | None = None
    recursive: bool = True
    requested_report_path: str | None = None
    report_format: str = "json"

    inventory_completed: bool = False
    files: tuple[FileBelief, ...] = ()

    parsing_completed: bool = False
    validation_completed: bool = False
    findings: tuple[ValidationFinding, ...] = ()

    report_built: bool = False
    report_saved: bool = False
    report_path: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.revision, "beliefs.revision")
        _non_empty(self.task_id, "beliefs.task_id")
        if self.source_path is not None:
            _non_empty(self.source_path, "beliefs.source_path")
        if type(self.recursive) is not bool:
            raise ValueError("beliefs.recursive must be bool")
        if self.requested_report_path is not None:
            _non_empty(self.requested_report_path, "beliefs.requested_report_path")
        _non_empty(self.report_format, "beliefs.report_format")

        files = tuple(self.files)
        if any(not isinstance(item, FileBelief) for item in files):
            raise TypeError("files must contain FileBelief objects")
        if len({item.path for item in files}) != len(files):
            raise ValueError("file paths must be unique")
        object.__setattr__(self, "files", files)

        findings = tuple(self.findings)
        if any(not isinstance(item, ValidationFinding) for item in findings):
            raise TypeError("findings must contain ValidationFinding objects")
        object.__setattr__(self, "findings", findings)

        for name in (
            "inventory_completed",
            "parsing_completed",
            "validation_completed",
            "report_built",
            "report_saved",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be bool")

        if not self.inventory_completed and self.files:
            raise ValueError("files cannot be populated before inventory completes")
        if self.parsing_completed and not self.inventory_completed:
            raise ValueError("parsing cannot complete before inventory")
        if self.validation_completed and not self.parsing_completed:
            raise ValueError("validation cannot complete before parsing")
        if self.report_built and not self.validation_completed:
            raise ValueError("report cannot be built before validation")
        if self.report_saved and (not self.report_built or not self.report_path):
            raise ValueError("a saved report requires report_built and report_path")
        if self.report_path is not None:
            _non_empty(self.report_path, "beliefs.report_path")


@dataclass(frozen=True)
class ValidationPolicy:
    """Optional project-specific checks passed to the executor.

    Empty required_fields_by_format and numeric_ranges are intentional defaults:
    the geological project must decide which curves/columns and value ranges are
    mandatory. No GR/RHOB/etc. requirement is assumed by this generic profile.
    """

    accepted_formats: tuple[str, ...] = ("las", "csv")
    required_fields_by_format: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    numeric_ranges: Mapping[str, tuple[float | None, float | None]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        formats = tuple(dict.fromkeys(
            str(item).strip().lower().lstrip(".") for item in self.accepted_formats
        ))
        if not formats or any(item not in _FORMATS for item in formats):
            raise ValueError("accepted_formats must contain las and/or csv")
        object.__setattr__(self, "accepted_formats", formats)

        required: dict[str, tuple[str, ...]] = {}
        for fmt, fields in self.required_fields_by_format.items():
            fmt = str(fmt).strip().lower().lstrip(".")
            if fmt not in formats:
                raise ValueError(f"required fields configured for unsupported format: {fmt}")
            normalized_fields = tuple(dict.fromkeys(
                str(item).strip().upper() for item in fields
            ))
            if any(not item for item in normalized_fields):
                raise ValueError("required field names must be non-empty")
            required[fmt] = normalized_fields
        object.__setattr__(self, "required_fields_by_format", MappingProxyType(required))

        ranges: dict[str, tuple[float | None, float | None]] = {}
        for field_name, bounds in self.numeric_ranges.items():
            name = str(field_name).strip().upper()
            if not name:
                raise ValueError("numeric range field names must be non-empty")
            if len(bounds) != 2:
                raise ValueError(f"numeric range for {name} must be (minimum, maximum)")
            lower, upper = bounds
            for bound in (lower, upper):
                if bound is not None and (not isinstance(bound, (int, float)) or not isfinite(bound)):
                    raise ValueError(f"numeric range bounds for {name} must be finite numbers or None")
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"minimum exceeds maximum for {name}")
            ranges[name] = (lower, upper)
        object.__setattr__(self, "numeric_ranges", MappingProxyType(ranges))

    def as_revision_payload(self) -> dict[str, object]:
        return {
            "accepted_formats": self.accepted_formats,
            "required_fields_by_format": tuple(sorted(self.required_fields_by_format.items())),
            "numeric_ranges": tuple(
                (name, bounds[0], bounds[1])
                for name, bounds in sorted(self.numeric_ranges.items())
            ),
        }

    def as_step_payload(self) -> dict[str, object]:
        # Tuples keep nested parameter values immutable for Step.parameters.
        return {
            "accepted_formats": self.accepted_formats,
            "required_fields_by_format": tuple(sorted(self.required_fields_by_format.items())),
            "numeric_ranges": tuple(
                (name, bounds[0], bounds[1])
                for name, bounds in sorted(self.numeric_ranges.items())
            ),
        }


class DataValidationProfile:
    """AgentProfile implementation for inventory, validation and reporting."""

    def __init__(self, policy: ValidationPolicy | None = None) -> None:
        self.policy = policy or ValidationPolicy()
        # State is partitioned by Task.id, so distinct tasks do not share beliefs.
        self._states: dict[str, DataValidationBeliefs] = {}
        self._goals = (
            Goal(INVENTORY_GOAL, priority=30),
            Goal(VALIDATION_GOAL, priority=20),
            Goal(REPORT_GOAL, priority=10),
        )

    async def observe(self, task: Task) -> DataValidationBeliefs:
        """Return the current immutable snapshot without mutating profile state."""
        task_config = _task_config(task)
        current = self._states.get(task.id)
        if current is not None and _belief_task_config(current) == task_config:
            return current
        return self._initial_beliefs(task.id, task_config)

    async def goals(
        self, task: Task, beliefs: BeliefSnapshot
    ) -> Sequence[Goal]:
        return self._goals

    async def assess_goal(self, context: Context) -> GoalAssessment:
        beliefs = _as_beliefs(context.beliefs)

        if context.goal.id == INVENTORY_GOAL:
            if beliefs.source_path is None:
                return GoalAssessment(
                    GoalStatus.IMPOSSIBLE,
                    "Task.payload must contain a non-empty source_path",
                )
            if beliefs.inventory_completed:
                return GoalAssessment(GoalStatus.ACHIEVED, "Input inventory completed")
            return GoalAssessment(GoalStatus.PENDING, "Input files have not been inventoried")

        if context.goal.id == VALIDATION_GOAL:
            if beliefs.validation_completed:
                # Findings describe data quality; they do not mean the agent failed.
                return GoalAssessment(
                    GoalStatus.ACHIEVED,
                    f"Validation completed; findings: {len(beliefs.findings)}",
                )
            if not beliefs.inventory_completed:
                return GoalAssessment(GoalStatus.PENDING, "Inventory is required first")
            return GoalAssessment(GoalStatus.PENDING, "Data validation has not completed")

        if context.goal.id == REPORT_GOAL:
            if beliefs.report_saved:
                return GoalAssessment(GoalStatus.ACHIEVED, "Validation report saved")
            if not beliefs.validation_completed:
                return GoalAssessment(GoalStatus.PENDING, "Validation is required before reporting")
            return GoalAssessment(GoalStatus.PENDING, "Validation report has not been saved")

        return GoalAssessment(GoalStatus.IMPOSSIBLE, f"Unknown goal: {context.goal.id}")

    async def plans(self, context: Context) -> Sequence[Plan]:
        beliefs = _as_beliefs(context.beliefs)
        task = context.task

        if context.goal.id == INVENTORY_GOAL:
            return (
                Plan(
                    id="inventory-v1",
                    goal_id=INVENTORY_GOAL,
                    priority=10,
                    steps=(
                        Step(
                            id="scan-source",
                            action=SCAN_ACTION,
                            parameters={
                                "source_path": beliefs.source_path or "",
                                "recursive": beliefs.recursive,
                            },
                        ),
                    ),
                ),
            )

        if context.goal.id == VALIDATION_GOAL:
            parse_parameters = self.policy.as_step_payload()
            validate_parameters = self.policy.as_step_payload()
            return (
                Plan(
                    id="parse-and-validate-v1",
                    goal_id=VALIDATION_GOAL,
                    priority=10,
                    steps=(
                        Step(
                            id="parse-input-files",
                            action=PARSE_ACTION,
                            parameters=parse_parameters,
                        ),
                        Step(
                            id="validate-input-data",
                            action=VALIDATE_ACTION,
                            parameters=validate_parameters,
                        ),
                    ),
                ),
            )

        if context.goal.id == REPORT_GOAL:
            save_parameters: dict[str, object] = {}
            if beliefs.requested_report_path is not None:
                save_parameters["output_path"] = beliefs.requested_report_path
            return (
                Plan(
                    id="build-and-save-report-v1",
                    goal_id=REPORT_GOAL,
                    priority=10,
                    steps=(
                        Step(
                            id="build-report",
                            action=BUILD_REPORT_ACTION,
                            parameters={"format": beliefs.report_format},
                        ),
                        Step(
                            id="save-report",
                            action=SAVE_REPORT_ACTION,
                            parameters=save_parameters,
                        ),
                    ),
                ),
            )

        return ()

    async def plan_applicable(self, plan: Plan, context: Context) -> Check:
        beliefs = _as_beliefs(context.beliefs)
        if plan.goal_id != context.goal.id:
            return Check(False, "Plan goal_id does not match context.goal.id")

        if plan.goal_id == INVENTORY_GOAL:
            if beliefs.source_path is None:
                return Check(False, "source_path is missing")
            return Check(True)

        if plan.goal_id == VALIDATION_GOAL:
            if not beliefs.inventory_completed:
                return Check(False, "Input inventory must be completed before validation")
            return Check(True)

        if plan.goal_id == REPORT_GOAL:
            if not beliefs.validation_completed:
                return Check(False, "Data validation must be completed before reporting")
            return Check(True)

        return Check(False, f"Unknown goal: {plan.goal_id}")

    async def step_applicable(self, step: Step, context: Context) -> Check:
        beliefs = _as_beliefs(context.beliefs)

        if step.action == SCAN_ACTION:
            if beliefs.source_path is None:
                return Check(False, "source_path is missing")
            if beliefs.inventory_completed:
                return Check(False, "Input inventory is already complete")
            return Check(True)

        if step.action == PARSE_ACTION:
            if not beliefs.inventory_completed:
                return Check(False, "Input inventory must be completed before parsing")
            if beliefs.parsing_completed:
                return Check(False, "Input parsing is already complete")
            return Check(True)

        if step.action == VALIDATE_ACTION:
            if not beliefs.parsing_completed:
                return Check(False, "Input files must be parsed before validation")
            if beliefs.validation_completed:
                return Check(False, "Data validation is already complete")
            return Check(True)

        if step.action == BUILD_REPORT_ACTION:
            if not beliefs.validation_completed:
                return Check(False, "Validation must be completed before report generation")
            if beliefs.report_built:
                return Check(False, "Report is already built")
            return Check(True)

        if step.action == SAVE_REPORT_ACTION:
            if not beliefs.report_built:
                return Check(False, "Report must be built before saving")
            if beliefs.report_saved:
                return Check(False, "Report is already saved")
            return Check(True)

        return Check(False, f"Action is not part of this profile: {step.action}")

    async def integrate(self, request: ActionRequest, result: ActionResult) -> None:
        """Interpret one executor result and commit a new immutable snapshot."""
        task_id = request.context.task.id
        context_beliefs = _as_beliefs(request.context.beliefs)
        current = self._states.get(task_id)

        if current is None or _belief_task_config(current) != _belief_task_config(context_beliefs):
            current = context_beliefs
        elif current.revision != context_beliefs.revision:
            raise ValueError(
                "stale ActionRequest beliefs: current profile revision differs "
                "from the revision used to execute the action"
            )

        observations = result.observations
        action = request.step.action

        if action == SCAN_ACTION:
            if observations.get("inventory_completed") is not True:
                raise ValueError("scan_source_directory must return inventory_completed=True")
            files = _parse_inventory_files(observations.get("files"))
            findings = _parse_findings(observations.get("findings", ()))
            if not files:
                findings = _merge_findings(
                    findings,
                    (
                        ValidationFinding(
                            None,
                            "no_input_files",
                            "error",
                            "No supported input files were found in the source.",
                        ),
                    ),
                )
            updated = self._commit(
                current,
                inventory_completed=True,
                files=files,
                findings=_merge_findings(current.findings, findings),
            )

        elif action == PARSE_ACTION:
            if observations.get("parsing_completed") is not True:
                raise ValueError("parse_input_files must return parsing_completed=True")
            parsed_files = _parse_parsed_files(observations.get("files"), current.files)
            findings = _parse_findings(observations.get("findings", ()))
            updated = self._commit(
                current,
                files=parsed_files,
                parsing_completed=True,
                findings=_merge_findings(current.findings, findings),
            )

        elif action == VALIDATE_ACTION:
            if observations.get("validation_completed") is not True:
                raise ValueError("validate_input_data must return validation_completed=True")
            findings = _parse_findings(observations.get("findings", ()))
            updated = self._commit(
                current,
                validation_completed=True,
                findings=_merge_findings(current.findings, findings),
            )

        elif action == BUILD_REPORT_ACTION:
            if observations.get("report_built") is not True:
                raise ValueError("build_validation_report must return report_built=True")
            updated = self._commit(current, report_built=True)

        elif action == SAVE_REPORT_ACTION:
            if observations.get("report_saved") is not True:
                raise ValueError("save_validation_report must return report_saved=True")
            report_path = _non_empty(
                observations.get("report_path"), "observations.report_path"
            )
            updated = self._commit(
                current,
                report_saved=True,
                report_path=report_path,
            )

        else:
            raise ValueError(f"integrate received unsupported action: {action}")

        self._states[task_id] = updated

    async def validate_proposal(
        self, proposal: PlanProposal, context: Context
    ) -> Check:
        plan = proposal.plan
        if plan.goal_id != context.goal.id:
            return Check(False, "Proposed plan targets a different goal")

        allowed = _ALLOWED_ACTIONS.get(context.goal.id)
        if allowed is None:
            return Check(False, f"No actions are allowed for goal {context.goal.id}")
        actions = tuple(step.action for step in plan.steps)
        if any(action not in allowed for action in actions):
            return Check(False, "Proposal contains an action outside the profile allowlist")

        required_terminal_action = {
            INVENTORY_GOAL: SCAN_ACTION,
            VALIDATION_GOAL: VALIDATE_ACTION,
            REPORT_GOAL: SAVE_REPORT_ACTION,
        }[context.goal.id]
        if required_terminal_action not in actions:
            return Check(False, f"Proposal must contain {required_terminal_action}")

        # Enforce ordering where one action produces a prerequisite for the next.
        order_pairs = {
            VALIDATION_GOAL: ((PARSE_ACTION, VALIDATE_ACTION),),
            REPORT_GOAL: ((BUILD_REPORT_ACTION, SAVE_REPORT_ACTION),),
        }.get(context.goal.id, ())
        for before, after in order_pairs:
            if before in actions and actions.index(before) > actions.index(after):
                return Check(False, f"{before} must precede {after}")

        plan_check = await self.plan_applicable(plan, context)
        if not plan_check.allowed:
            return plan_check
        return await self.step_applicable(plan.steps[0], context)

    def _initial_beliefs(
        self,
        task_id: str,
        task_config: tuple[str | None, bool, str | None, str],
    ) -> DataValidationBeliefs:
        source_path, recursive, report_path, report_format = task_config
        initial = DataValidationBeliefs(
            revision="pending",
            task_id=task_id,
            source_path=source_path,
            recursive=recursive,
            requested_report_path=report_path,
            report_format=report_format,
        )
        return replace(initial, revision=self._revision(initial))

    def _commit(
        self, current: DataValidationBeliefs, **changes: object
    ) -> DataValidationBeliefs:
        candidate = replace(current, revision="pending", **changes)
        return replace(candidate, revision=self._revision(candidate))

    def _revision(self, beliefs: DataValidationBeliefs) -> str:
        payload = {
            "profile_version": PROFILE_VERSION,
            "policy": self.policy.as_revision_payload(),
            "task_id": beliefs.task_id,
            "source_path": beliefs.source_path,
            "recursive": beliefs.recursive,
            "requested_report_path": beliefs.requested_report_path,
            "report_format": beliefs.report_format,
            "inventory_completed": beliefs.inventory_completed,
            "files": [
                {
                    "path": item.path,
                    "format": item.format,
                    "parse_status": item.parse_status,
                    "fields": item.fields,
                    "row_count": item.row_count,
                    "parse_error": item.parse_error,
                }
                for item in beliefs.files
            ],
            "parsing_completed": beliefs.parsing_completed,
            "validation_completed": beliefs.validation_completed,
            "findings": [
                {
                    "file_path": item.file_path,
                    "check_id": item.check_id,
                    "severity": item.severity,
                    "message": item.message,
                }
                for item in beliefs.findings
            ],
            "report_built": beliefs.report_built,
            "report_saved": beliefs.report_saved,
            "report_path": beliefs.report_path,
        }
        serialized = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _task_config(
    task: Task,
) -> tuple[str | None, bool, str | None, str]:
    source = task.payload.get("source_path")
    source_path = source.strip() if isinstance(source, str) and source.strip() else None

    recursive_value = task.payload.get("recursive", True)
    recursive = recursive_value if type(recursive_value) is bool else True

    report_path_value = task.payload.get("report_path")
    report_path = (
        report_path_value.strip()
        if isinstance(report_path_value, str) and report_path_value.strip()
        else None
    )

    report_format_value = task.payload.get("report_format", "json")
    report_format = (
        report_format_value.strip()
        if isinstance(report_format_value, str) and report_format_value.strip()
        else "json"
    )
    return source_path, recursive, report_path, report_format


def _belief_task_config(
    beliefs: DataValidationBeliefs,
) -> tuple[str | None, bool, str | None, str]:
    return (
        beliefs.source_path,
        beliefs.recursive,
        beliefs.requested_report_path,
        beliefs.report_format,
    )


def _as_beliefs(value: BeliefSnapshot) -> DataValidationBeliefs:
    if not isinstance(value, DataValidationBeliefs):
        raise TypeError("DataValidationProfile requires DataValidationBeliefs snapshots")
    return value


def _non_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _parse_inventory_files(value: object) -> tuple[FileBelief, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("observations.files must be a list or tuple")
    files: list[FileBelief] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("each inventory file must be a mapping")
        path = _non_empty(item.get("path"), "file.path")
        fmt_value = item.get("format", "unknown")
        fmt = _non_empty(fmt_value, "file.format").lower().lstrip(".")
        if fmt not in _FORMATS:
            fmt = "unknown"
        files.append(FileBelief(path=path, format=fmt))
    if len({item.path for item in files}) != len(files):
        raise ValueError("inventory contains duplicate file paths")
    return tuple(files)


def _parse_parsed_files(
    value: object, inventory: tuple[FileBelief, ...]
) -> tuple[FileBelief, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("parse observations.files must be a list or tuple")
    parsed: dict[str, FileBelief] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("each parsed file must be a mapping")
        path = _non_empty(item.get("path"), "parsed_file.path")
        fmt = _non_empty(item.get("format", "unknown"), "parsed_file.format")
        fmt = fmt.lower().lstrip(".")
        if fmt not in _FORMATS:
            fmt = "unknown"
        status = _non_empty(item.get("parse_status"), "parsed_file.parse_status")
        fields_value = item.get("fields", ())
        if not isinstance(fields_value, (list, tuple)):
            raise ValueError("parsed_file.fields must be a list or tuple")
        fields = tuple(_non_empty(field_name, "parsed field") for field_name in fields_value)
        row_count = item.get("row_count")
        parse_error = item.get("parse_error")
        parsed_file = FileBelief(
            path=path,
            format=fmt,
            parse_status=status,
            fields=fields,
            row_count=row_count,
            parse_error=parse_error,
        )
        if path in parsed:
            raise ValueError(f"duplicate parse result for {path}")
        parsed[path] = parsed_file

    inventory_by_path = {item.path: item for item in inventory}
    if set(parsed) != set(inventory_by_path):
        missing = sorted(set(inventory_by_path) - set(parsed))
        extra = sorted(set(parsed) - set(inventory_by_path))
        raise ValueError(f"parse result must cover inventory exactly; missing={missing}, extra={extra}")

    for path, parsed_file in parsed.items():
        if parsed_file.format != inventory_by_path[path].format:
            raise ValueError(f"format changed between inventory and parsing for {path}")
    return tuple(parsed[path] for path in inventory_by_path)


def _parse_findings(value: object) -> tuple[ValidationFinding, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("observations.findings must be a list or tuple")
    findings: list[ValidationFinding] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("each finding must be a mapping")
        file_path = item.get("file_path")
        if file_path is not None:
            file_path = _non_empty(file_path, "finding.file_path")
        findings.append(
            ValidationFinding(
                file_path=file_path,
                check_id=_non_empty(item.get("check_id"), "finding.check_id"),
                severity=_non_empty(item.get("severity"), "finding.severity").lower(),
                message=_non_empty(item.get("message"), "finding.message"),
            )
        )
    return tuple(findings)


def _merge_findings(
    existing: tuple[ValidationFinding, ...],
    incoming: tuple[ValidationFinding, ...],
) -> tuple[ValidationFinding, ...]:
    return tuple(dict.fromkeys((*existing, *incoming)))
