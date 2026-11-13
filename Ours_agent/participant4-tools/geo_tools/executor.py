"""GeoToolExecutor — реализация ActionExecutor (участник 4).

Обёртывает функции из grid.py / checks.py в действия агента, превращает технические
сбои в ActionError, а предметные находки — в observations.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from agent_runtime import ActionError, ActionRequest, ActionResult

from . import checks as C
from .grid import GridModel, WellSample, load_model, load_wells

MAX_EXAMPLES = 5


@dataclass
class Session:
    """Рабочее состояние одной задачи (ключ — task.id)."""
    model: GridModel | None = None
    wells: list[WellSample] | None = None
    issues: dict[str, list[C.Issue]] = field(default_factory=dict)
    volumes: dict | None = None
    history: list[dict] = field(default_factory=list)   # для стенда: что и когда найдено


class GeoToolExecutor:
    """Действия: load_model, load_wells, check_*, compute_volumes, write_report, send_alert."""

    def __init__(self, *, faults: dict[str, int] | None = None,
                 on_alert: Callable[[dict], None] | None = None):
        # faults: {"load_model": 2} — первые 2 вызова завершатся временным сбоем (для стенда)
        self._faults = dict(faults or {})
        self._on_alert = on_alert
        self._sessions: dict[str, Session] = {}
        self._done: dict[str, dict] = {}               # idempotency_key -> observations
        self._handlers: dict[str, Callable[[ActionRequest, Session], Awaitable[dict]]] = {
            "load_model": self._load_model,
            "load_wells": self._load_wells,
            **{name: self._make_check(name) for name in C.CHECKS},
            "check_well_tie": self._check_well_tie,
            "compute_volumes": self._compute_volumes,
            "write_report": self._write_report,
            "send_alert": self._send_alert,
        }

    # --- контракт ActionExecutor ---------------------------------------------------
    def supports(self, action: str) -> bool:
        return action in self._handlers

    async def execute(self, request: ActionRequest) -> ActionResult:
        action = request.step.action
        handler = self._handlers.get(action)
        if handler is None:
            raise ActionError(f"действие «{action}» не поддерживается", retryable=False)
        if request.idempotency_key in self._done:          # повтор уже выполненного
            return ActionResult(observations=self._done[request.idempotency_key])
        if self._faults.get(action, 0) > 0:
            self._faults[action] -= 1
            raise ActionError(f"{action}: имитация временного сбоя хранилища", retryable=True)
        session = self._sessions.setdefault(request.context.task.id, Session())
        try:
            obs = await handler(request, session)
        except ActionError:
            raise
        except FileNotFoundError as exc:
            raise ActionError(f"{action}: файл не найден: {exc.filename}", retryable=False) from exc
        except (TimeoutError, ConnectionError) as exc:
            raise ActionError(f"{action}: временная ошибка: {exc}", retryable=True) from exc
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ActionError(f"{action}: некорректные данные: {exc}", retryable=False) from exc
        obs = {"action": action, **obs}
        if action in ("write_report", "send_alert"):
            self._done[request.idempotency_key] = obs
        session.history.append({"action": action, "flagged": obs.get("flagged", ())})
        return ActionResult(observations=obs)

    # --- для стенда ---------------------------------------------------------------
    def session(self, task_id: str) -> Session:
        return self._sessions[task_id]

    # --- обработчики --------------------------------------------------------------
    @staticmethod
    def _model_dir(req: ActionRequest) -> str:
        return str(req.step.parameters.get("model_dir") or req.context.task.payload["model_dir"])

    @staticmethod
    def _need_model(s: Session) -> GridModel:
        if s.model is None:
            raise ActionError("модель не загружена: сначала выполните load_model", retryable=False)
        return s.model

    async def _load_model(self, req, s):
        s.model = await asyncio.to_thread(load_model, self._model_dir(req))
        m = s.model
        return {"cells": m.size, "dims": (m.nx, m.ny, m.nz), "owc": m.owc}

    async def _load_wells(self, req, s):
        s.wells = await asyncio.to_thread(load_wells, self._model_dir(req))
        return {"wells": tuple(sorted({w.well for w in s.wells})), "samples": len(s.wells)}

    @staticmethod
    def _summary(name: str, issues: list[C.Issue], extra: dict) -> dict:
        errors = sum(1 for i in issues if i.severity == C.ERROR)
        return {
            "check": name,
            "issues": len(issues),
            "errors": errors,
            "warnings": len(issues) - errors,
            "flagged": tuple(sorted({i.cell for i in issues if i.cell >= 0})),
            "examples": tuple(i.message for i in issues[:MAX_EXAMPLES]),
            **extra,
        }

    def _make_check(self, name):
        func = C.CHECKS[name]

        async def handler(req, s):
            m = self._need_model(s)
            issues, extra = await asyncio.to_thread(func, m, **dict(req.step.parameters))
            s.issues[name] = issues
            return self._summary(name, issues, extra)
        return handler

    async def _check_well_tie(self, req, s):
        m = self._need_model(s)
        if s.wells is None:
            raise ActionError("скважины не загружены: сначала выполните load_wells")
        issues, extra = await asyncio.to_thread(C.check_well_tie, m, s.wells, **dict(req.step.parameters))
        s.issues["check_well_tie"] = issues
        return self._summary("check_well_tie", issues, extra)

    async def _compute_volumes(self, req, s):
        s.volumes = C.compute_volumes(self._need_model(s))
        return dict(s.volumes)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    async def _write_report(self, req, s):
        out = Path(str(req.context.task.payload.get("output_dir", "out")))
        all_issues = [i for lst in s.issues.values() for i in lst]
        self._atomic_write(out / "issues.json", json.dumps(
            [i.as_dict() for i in all_issues], ensure_ascii=False, indent=1))
        lines = [f"# Отчёт о проверке модели — {req.context.task.id}", "",
                 "| Проверка | Ошибки | Предупреждения |", "|---|---|---|"]
        for name, lst in s.issues.items():
            e = sum(1 for i in lst if i.severity == C.ERROR)
            lines.append(f"| {name} | {e} | {len(lst) - e} |")
        lines += ["", "## Примеры замечаний", ""]
        lines += [f"- {i.message}" for i in all_issues[:30]] or ["- замечаний нет"]
        self._atomic_write(out / "report.md", "\n".join(lines) + "\n")
        errors = sum(1 for i in all_issues if i.severity == C.ERROR)
        return {"report_path": str(out / "report.md"), "issues_total": len(all_issues),
                "errors_total": errors}

    async def _send_alert(self, req, s):
        out = Path(str(req.context.task.payload.get("output_dir", "out")))
        by_check = {n: len(v) for n, v in s.issues.items() if v}
        alert = {"task": req.context.task.id,
                 "severity": req.step.parameters.get("severity", "error"),
                 "summary": by_check,
                 "message": "В модели найдены несогласованности свойств: " +
                            ", ".join(f"{k} — {v}" for k, v in by_check.items())}
        self._atomic_write(out / "alert.json", json.dumps(alert, ensure_ascii=False, indent=1))
        if self._on_alert:
            self._on_alert(alert)          # сюда можно подключить почту/Telegram
        return {"alert_path": str(out / "alert.json"), "sent": True}
