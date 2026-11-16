"""Compose the existing runtime, domain profile and real file tools."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import uuid4

from agent_runtime import ActionRequest, ActionResult, AgentRuntime, RuntimeLimits, RuntimeStatus, Task
from data_validation_profile import DataValidationProfile, ValidationPolicy, VALIDATE_ACTION

from ._tools import ensure_tools_available
from .stubs import NoLLMProposalProvider, admission_not_assessed, pending_capabilities

ensure_tools_available()

from data_tools import DataValidationExecutor  # noqa: E402
from data_tools.report import build as build_report  # noqa: E402


class _CoverageExecutor(DataValidationExecutor):
    """Keep file-level evidence and make a zero-file validation explicit."""

    def __init__(self, *, default_report_dir: Path) -> None:
        super().__init__(default_report_dir=default_report_dir)
        self.validated_paths: tuple[str, ...] = ()

    async def execute(self, request: ActionRequest) -> ActionResult:
        result = await super().execute(request)
        if request.step.action == VALIDATE_ACTION and result.observations.get("validation_completed") is True:
            self.validated_paths = tuple(
                item.path for item in request.context.beliefs.files
                if item.parse_status == "parsed"
            )
            if not self.validated_paths:
                observations = dict(result.observations)
                observations["findings"] = [
                    *observations.get("findings", ()),
                    {
                        "file_path": None,
                        "check_id": "no_validated_files",
                        "severity": "error",
                        "message": "Ни один файл не был успешно прочитан и передан базовым проверкам.",
                    },
                ]
                return ActionResult(observations)
        return result


@dataclass(frozen=True)
class FirstAgentResult:
    task_id: str
    runtime_status: str
    reason: str
    technical_verdict: str
    coverage: dict[str, Any]
    admission: dict[str, str]
    stubs: list[dict[str, str]]
    reports: dict[str, str]
    exit_code: int
    runtime_metrics: dict[str, int]
    action_errors: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "runtime_status": self.runtime_status,
            "reason": self.reason,
            "technical_verdict": self.technical_verdict,
            "coverage": dict(self.coverage),
            "admission": dict(self.admission),
            "stubs": [dict(item) for item in self.stubs],
            "reports": dict(self.reports),
            "exit_code": self.exit_code,
            "runtime_metrics": dict(self.runtime_metrics),
            "action_errors": [dict(item) for item in self.action_errors],
        }


def _write_atomic(path: Path, text: str) -> None:
    """Write an application artifact using its own temporary filename."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _markdown_summary(result: FirstAgentResult) -> str:
    lines = [
        "", "## Выполнение сборки", "",
        f"- Runtime: `{result.runtime_status}`.",
        f"- Технический результат: `{result.technical_verdict}`.",
        f"- Окончательный допуск: `{result.admission['status']}` — {result.admission['reason']}",
        f"- Выполнение базовых проверок завершено: `{result.coverage['validation_completed']}`.",
        "", "## Охват файлов", "",
        "| Найдено | Прочитано | Ошибка чтения | Не поддерживается | Передано проверкам |",
        "|---|---|---|---|---|",
        "| {discovered} | {parsed} | {parse_failed} | {unsupported} | {validated} |".format(**result.coverage),
        "", "Успешное чтение и передача файла проверкам не подтверждают выполнение всех будущих предметных правил.",
    ]
    if result.action_errors:
        lines += ["", "## Ошибки выполнения", ""]
        for item in result.action_errors:
            error = item["error"].replace("\n", " ")
            lines.append(f"- `{item['step_id'] or item['kind']}`: {error}")
    lines += [
        "", "## Заглушки и ограничения", "",
        "| Компонент | Состояние | Ответственный | Что отсутствует |",
        "|---|---|---|---|",
    ]
    for item in result.stubs:
        cells = [item[key].replace("|", "\\|").replace("\n", " ") for key in ("id", "status", "owner", "reason")]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


async def run_validation(
    source_path: str | Path,
    output_dir: str | Path,
    task_id: str | None = None,
    policy: ValidationPolicy | None = None,
    recursive: bool = True,
    action_timeout_seconds: float = 30,
) -> FirstAgentResult:
    """Run a fresh task and write real reports plus explicit capability stubs.

    A task ID can be used once in an output directory. This entry point does not
    resume previous runtime/profile state or approve downstream use of the data.
    """
    if policy is not None and not isinstance(policy, ValidationPolicy):
        raise ValueError("policy must be a ValidationPolicy or None")
    if type(recursive) is not bool:
        raise ValueError("recursive must be bool")
    if isinstance(source_path, str) and not source_path.strip():
        raise ValueError("source_path must not be empty")
    source = Path(source_path).expanduser().resolve()
    output_root = Path(output_dir).expanduser().resolve()
    if source.is_relative_to(output_root):
        raise ValueError("source_path must not be inside or equal to output_dir")
    run_id = task_id if task_id is not None else f"validation-{uuid4().hex[:12]}"
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", run_id) is None:
        raise ValueError("task_id must contain 1..64 letters, digits, dots, underscores or hyphens, starting with a letter or digit")
    limits = RuntimeLimits(action_timeout_seconds=action_timeout_seconds)
    run_dir = output_root / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError(f"task_id already exists: {run_id}; use a new ID for a new run") from exc

    reports = {
        "json": str(run_dir / "validation.json"),
        "markdown": str(run_dir / "validation.md"),
        "result": str(run_dir / "result.json"),
    }
    # Keep the executor's artifact separate: a background save finishing after
    # a timeout must not overwrite the assembly's partial report and stubs.
    runtime_report_path = run_dir / "_runtime" / "validation.json"
    selected_policy = policy if policy is not None else ValidationPolicy()
    profile = DataValidationProfile(selected_policy)
    executor = _CoverageExecutor(default_report_dir=output_root)
    proposals = NoLLMProposalProvider()
    task = Task(
        id=run_id, description="Базовая обработка и проверка входных данных",
        payload={"source_path": str(source), "recursive": recursive,
                 "report_path": str(runtime_report_path), "report_format": "json"},
    )
    runtime = AgentRuntime(profile=profile, executor=executor, proposals=proposals, limits=limits)
    execution = await runtime.run(task)
    beliefs = await profile.observe(task)
    files = [
        {"path": item.path, "format": item.format, "parse_status": item.parse_status,
         "fields": list(item.fields), "row_count": item.row_count, "parse_error": item.parse_error}
        for item in beliefs.files
    ]
    findings = [
        {"file_path": item.file_path, "check_id": item.check_id,
         "severity": item.severity, "message": item.message}
        for item in beliefs.findings
    ]
    text, _ = build_report(run_id, str(source), files, findings, "json")
    document = json.loads(text)
    technical_verdict = document["verdict"] if beliefs.validation_completed else "unavailable"
    document["verdict"] = technical_verdict
    coverage = {
        "discovered": len(files),
        "parsed": sum(item["parse_status"] == "parsed" for item in files),
        "parse_failed": sum(item["parse_status"] == "failed" for item in files),
        "unsupported": sum(item["parse_status"] == "unsupported" for item in files),
        "validated": len(executor.validated_paths),
        "validation_completed": beliefs.validation_completed,
    }
    exit_code = 2 if execution.status != RuntimeStatus.SUCCEEDED else (1 if technical_verdict == "errors" else 0)
    action_errors = [
        {"kind": event.kind, "step_id": str(event.details.get("step_id", "")),
         "error": str(event.details.get("error", ""))}
        for event in execution.events
        if event.kind in {"action_failed", "adapter_error"}
    ]
    result = FirstAgentResult(
        task_id=run_id, runtime_status=execution.status.value, reason=execution.reason,
        technical_verdict=technical_verdict, coverage=coverage,
        admission=admission_not_assessed(), stubs=pending_capabilities(),
        reports=reports, exit_code=exit_code,
        runtime_metrics={"cycles": execution.cycles, "actions": execution.actions,
                         "replans": execution.replans, "llm_requests": proposals.requests},
        action_errors=action_errors,
    )
    document.update({
        "execution": {"status": execution.status.value, "reason": execution.reason,
                      "runtime_report_saved": beliefs.report_saved,
                      "runtime_report_path": str(runtime_report_path), "action_errors": action_errors},
        "technical_verdict": technical_verdict, "coverage": coverage,
        "admission": result.admission, "stubs": result.stubs,
        "policy": {
            "mode": "baseline_stub" if policy is None else "custom_unverified",
            "accepted_formats": list(selected_policy.accepted_formats),
            "required_fields_by_format": {key: list(value) for key, value in selected_policy.required_fields_by_format.items()},
            "numeric_ranges": {key: list(value) for key, value in selected_policy.numeric_ranges.items()},
        },
    })
    markdown, _ = build_report(run_id, str(source), files, findings, "md")
    headlines = {
        "ok": "**Итог базовых проверок:** ошибок не обнаружено; окончательный допуск не оценивался.  ",
        "warnings": "**Итог базовых проверок:** есть предупреждения; окончательный допуск не оценивался.  ",
        "errors": "**Итог базовых проверок:** обнаружены ошибки; окончательный допуск не оценивался.  ",
        "unavailable": "**Итог:** проверка не завершена; техническое качество входов не установлено.  ",
    }
    markdown = "\n".join(
        headlines[technical_verdict] if line.startswith("**Итог:**") else line
        for line in markdown.splitlines()
    ) + "\n"
    _write_atomic(Path(reports["json"]), json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    _write_atomic(Path(reports["markdown"]), markdown + _markdown_summary(result))
    _write_atomic(Path(reports["result"]), json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
    return result
