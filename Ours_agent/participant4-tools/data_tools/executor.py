"""DataValidationExecutor — ActionExecutor (участник 4) под профиль DataValidationProfile
(участник 2). Схемы observations — строго по DATA_VALIDATION_PROFILE_CONTRACT.md.

Действия:
    scan_source_directory   найти входные файлы и определить их формат
    parse_input_files       прочитать каждый файл (LAS/CSV), собрать поля и число строк
    validate_input_data     проверить обязательные поля, диапазоны, глубину, пустые кривые
    build_validation_report собрать отчёт (json | md) в памяти
    save_validation_report  атомарно записать отчёт на диск

Правила:
  * Проблема с ДАННЫМИ -> finding в observations (агент продолжает работу).
  * Сбой ИНСТРУМЕНТА  -> ActionError (retryable=True для временных сбоев).
  * Все действия идемпотентны: повтор с тем же входом даёт тот же результат,
    поэтому профиль может безопасно ставить retry_safe=True.
  * Сырые массивы кривых живут здесь (в сессии задачи), в beliefs уходят только метаданные.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from agent_runtime import ActionError, ActionRequest, ActionResult

from . import checks as C
from . import report as R
from .tables import ParseError, Table, UnsupportedFile, detect_format, parse_file

SCAN = "scan_source_directory"
PARSE = "parse_input_files"
VALIDATE = "validate_input_data"
BUILD = "build_validation_report"
SAVE = "save_validation_report"
ACTIONS = (SCAN, PARSE, VALIDATE, BUILD, SAVE)


@dataclass
class _Session:
    """Состояние инструмента для одной задачи (Task.id)."""
    source: str | None = None
    inventory: list[dict] = field(default_factory=list)
    tables: dict[str, Table] = field(default_factory=dict)
    report_text: str | None = None
    report_ext: str = "json"


class DataValidationExecutor:
    def __init__(self, *, default_report_dir: str | Path = "reports",
                 faults: Mapping[str, int] | None = None, fault_retryable: bool = True):
        """faults: {action: N} — первые N вызовов действия завершатся временной
        ошибкой (имитация сбоя диска/сети для стенда)."""
        self.default_report_dir = Path(default_report_dir)
        self._faults = dict(faults or {})
        self._fault_retryable = fault_retryable
        self._sessions: dict[str, _Session] = {}
        self.calls: list[tuple[str, int, str]] = []   # (action, attempt, idempotency_key)
        self._handlers = {SCAN: self._scan, PARSE: self._parse, VALIDATE: self._validate,
                          BUILD: self._build, SAVE: self._save}

    # ------------------------------------------------------------ контракт
    def supports(self, action: str) -> bool:
        return action in self._handlers

    async def execute(self, request: ActionRequest) -> ActionResult:
        action = request.step.action
        if action not in self._handlers:
            raise ActionError(f"unsupported action: {action}")
        self.calls.append((action, request.attempt, request.idempotency_key))
        if self._faults.get(action, 0) > 0:
            self._faults[action] -= 1
            raise ActionError(f"{action}: simulated transient failure",
                              retryable=self._fault_retryable)
        session = self._sessions.setdefault(request.context.task.id, _Session())
        params = dict(request.step.parameters)
        try:
            obs = await self._handlers[action](request, session, params)
        except ActionError:
            raise
        except OSError as exc:  # диск/сеть: обычно временная проблема
            raise ActionError(f"{action}: {exc}", retryable=True) from exc
        return ActionResult(observations=obs)

    def session(self, task_id: str) -> _Session | None:
        return self._sessions.get(task_id)

    # ------------------------------------------------------------ действия
    async def _scan(self, req, s: _Session, p: dict) -> dict:
        source = str(p.get("source_path") or "").strip()
        recursive = p.get("recursive", True) is not False
        s.source = source
        files, findings = await asyncio.to_thread(_inventory, source, recursive,
                                                  self.default_report_dir)
        s.inventory = files
        return {"inventory_completed": True, "files": files, "findings": findings}

    async def _parse(self, req, s: _Session, p: dict) -> dict:
        accepted = {str(f).lower() for f in p.get("accepted_formats", ("las", "csv"))}
        inventory = _belief_files(req) or s.inventory
        out_files, findings = [], []
        for item in inventory:
            path, fmt = item["path"], item["format"]
            rec = {"path": path, "format": fmt, "parse_status": "parsed",
                   "fields": [], "row_count": None, "parse_error": None}
            if fmt not in accepted:
                rec["parse_status"] = "unsupported"
                why = "unknown file type" if fmt == "unknown" else f"format {fmt} is not accepted by policy"
                findings.append(C.finding(path, "unsupported_format", f"Skipped: {why}"))
            else:
                try:
                    table = await asyncio.to_thread(parse_file, path, fmt)
                except UnsupportedFile as exc:
                    rec["parse_status"] = "unsupported"
                    findings.append(C.finding(path, "unsupported_format", str(exc)))
                except ParseError as exc:
                    rec.update(parse_status="failed", parse_error=str(exc))
                    findings.append(C.finding(path, "parse_error", str(exc)))
                else:
                    s.tables[path] = table
                    rec.update(fields=list(table.fields), row_count=table.row_count)
                    findings += [C.finding(path, cid, msg, sev) for cid, sev, msg in table.notes]
            out_files.append(rec)
        return {"parsing_completed": True, "files": out_files, "findings": findings}

    async def _validate(self, req, s: _Session, p: dict) -> dict:
        required = {fmt: tuple(fields) for fmt, fields in p.get("required_fields_by_format", ())}
        ranges = {name: (lo, hi) for name, lo, hi in p.get("numeric_ranges", ())}
        files = _belief_files(req, parsed_only=True)
        if files is None:  # профиль без файлов в beliefs: берём то, что прочитали сами
            files = [{"path": k, "format": t.format} for k, t in s.tables.items()]
        findings = []
        for item in files:
            table = s.tables.get(item["path"])
            if table is None:  # исполнитель перезапущен: перечитать файл
                table = await asyncio.to_thread(parse_file, item["path"], item["format"])
                s.tables[item["path"]] = table
            findings += C.validate_table(table, required.get(table.format, ()), ranges)
        return {"validation_completed": True, "findings": findings}

    async def _build(self, req, s: _Session, p: dict) -> dict:
        fmt = str(p.get("format", "json"))
        if fmt.lower() not in R.FORMATS:
            raise ActionError(f"unsupported report format: {fmt}")
        files, findings = _belief_report_data(req, s)
        b = req.context.beliefs
        s.report_text, s.report_ext = R.build(req.context.task.id,
                                              getattr(b, "source_path", None) or s.source,
                                              files, findings, fmt)
        return {"report_built": True, "verdict": R.verdict(findings),
                "findings_total": len(findings)}

    async def _save(self, req, s: _Session, p: dict) -> dict:
        if s.report_text is None:  # перезапуск между build и save
            await self._build(req, s, {"format": getattr(req.context.beliefs, "report_format", "json")})
        out = p.get("output_path")
        path = Path(out) if out else self.default_report_dir / f"{req.context.task.id}.{s.report_ext}"
        await asyncio.to_thread(_atomic_write, path, s.report_text)
        return {"report_saved": True, "report_path": str(path)}


# ---------------------------------------------------------------- помощники

def _inventory(source: str, recursive: bool, report_dir: Path) -> tuple[list[dict], list[dict]]:
    root = Path(source) if source else None
    if root is None or not root.exists():
        return [], [C.finding(source or None, "source_not_found",
                              f"Source path does not exist: {source!r}")]
    if root.is_file():
        paths = [root]
    else:
        it = root.rglob("*") if recursive else root.glob("*")
        skip = report_dir.resolve()
        paths = sorted(p for p in it if p.is_file()
                       and not any(part.startswith(".") for part in p.relative_to(root).parts)
                       and skip not in p.resolve().parents)
    return [{"path": str(p), "format": detect_format(p)} for p in paths], []


def _belief_files(req: ActionRequest, parsed_only: bool = False) -> list[dict] | None:
    files = getattr(req.context.beliefs, "files", None)
    if files is None:
        return None
    return [{"path": f.path, "format": f.format} for f in files
            if not parsed_only or getattr(f, "parse_status", "parsed") == "parsed"]


def _belief_report_data(req: ActionRequest, s: _Session) -> tuple[list[dict], list[dict]]:
    b = req.context.beliefs
    files = [{"path": f.path, "format": f.format, "parse_status": f.parse_status,
              "fields": list(f.fields), "row_count": f.row_count, "parse_error": f.parse_error}
             for f in getattr(b, "files", ())]
    findings = [{"file_path": f.file_path, "check_id": f.check_id, "severity": f.severity,
                 "message": f.message} for f in getattr(b, "findings", ())]
    return files, findings


def _atomic_write(path: Path, text: str) -> None:
    """Запись «всё или ничего»; повтор с тем же содержимым ничего не меняет."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
