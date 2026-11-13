"""Сборка отчёта о проверке входных данных: JSON (для машин) и Markdown (для людей)."""

from __future__ import annotations

import json
from pathlib import Path

SEVERITY_ORDER = ("critical", "error", "warning", "info")
FORMATS = {"json": "json", "md": "md", "markdown": "md"}


def verdict(findings: list[dict]) -> str:
    sev = {f["severity"] for f in findings}
    if sev & {"critical", "error"}:
        return "errors"
    if "warning" in sev:
        return "warnings"
    return "ok"


def build(task_id: str, source: str | None, files: list[dict], findings: list[dict],
          fmt: str) -> tuple[str, str]:
    """Вернуть (текст, расширение). fmt: json | md | markdown."""
    ext = FORMATS[fmt.lower()]
    findings = sorted(findings, key=lambda f: (SEVERITY_ORDER.index(f["severity"]),
                                               f["file_path"] or "", f["check_id"], f["message"]))
    counts = {s: sum(f["severity"] == s for f in findings) for s in SEVERITY_ORDER}
    data = {
        "task_id": task_id,
        "source_path": source,
        "verdict": verdict(findings),
        "summary": {"files": len(files),
                    "parsed": sum(f.get("parse_status") == "parsed" for f in files),
                    "findings": counts},
        "files": files,
        "findings": findings,
    }
    if ext == "json":
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n", ext
    return _markdown(data), ext


def _short(path, source) -> str:
    if not path:
        return "—"
    if source:
        try:
            return str(Path(path).relative_to(source).as_posix())
        except ValueError:
            pass
    return str(path)


def _markdown(d: dict) -> str:
    src = d["source_path"]
    icon = {"ok": "✅ данные в порядке", "warnings": "⚠️ есть предупреждения",
            "errors": "❌ найдены ошибки"}[d["verdict"]]
    c = d["summary"]["findings"]
    lines = [f"# Проверка входных данных: {d['task_id']}", "",
             f"**Источник:** `{d['source_path']}`  ", f"**Итог:** {icon}  ",
             f"**Файлов:** {d['summary']['files']}, прочитано {d['summary']['parsed']}  ",
             f"**Находки:** critical {c['critical']}, error {c['error']}, "
             f"warning {c['warning']}, info {c['info']}", "",
             "## Файлы", "", "| Файл | Формат | Статус | Полей | Строк |", "|---|---|---|---|---|"]
    for f in d["files"]:
        lines.append(f"| `{_short(f['path'], src)}` | {f['format']} | {f.get('parse_status', '—')} | "
                     f"{len(f.get('fields') or ())} | {f.get('row_count') if f.get('row_count') is not None else '—'} |")
    lines += ["", "## Находки", ""]
    if not d["findings"]:
        lines.append("Нет.")
    else:
        lines += ["| Важность | Файл | Проверка | Описание |", "|---|---|---|---|"]
        for f in d["findings"]:
            lines.append(f"| {f['severity']} | `{_short(f['file_path'], src)}` | {f['check_id']} | {f['message']} |")
    return "\n".join(lines) + "\n"
