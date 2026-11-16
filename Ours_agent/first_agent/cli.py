"""Command-line entry point for the assembled first agent."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from data_validation_profile import ValidationPolicy

from .app import run_validation


def _load_policy(path: str | None) -> ValidationPolicy | None:
    if path is None:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("policy JSON must contain an object")
    allowed = {"accepted_formats", "required_fields_by_format", "numeric_ranges"}
    if set(data) - allowed:
        raise ValueError("unknown policy fields: " + ", ".join(sorted(set(data) - allowed)))
    if "accepted_formats" in data and not isinstance(data["accepted_formats"], list):
        raise ValueError("accepted_formats must be an array")
    required = data.get("required_fields_by_format", {})
    if not isinstance(required, dict) or any(not isinstance(value, list) for value in required.values()):
        raise ValueError("required_fields_by_format must map format names to arrays")
    ranges = data.get("numeric_ranges", {})
    if not isinstance(ranges, dict) or any(not isinstance(value, list) for value in ranges.values()):
        raise ValueError("numeric_ranges must map field names to two-element arrays")
    return ValidationPolicy(**data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Первый агент: чтение LAS/CSV/TSV, базовые проверки и отчёт с явными заглушками.")
    parser.add_argument("--source", required=True, help="Путь к файлу или папке входных данных")
    parser.add_argument("--output", default="reports/first-agent", help="Корневая папка результатов")
    parser.add_argument("--task-id", help="Уникальный ID нового запуска; по умолчанию генерируется")
    parser.add_argument("--policy", help="JSON с конфигурацией проверок; предметные правила по умолчанию не заданы")
    parser.add_argument("--no-recursive", action="store_true", help="Не сканировать вложенные папки")
    parser.add_argument("--action-timeout", type=float, default=30, help="Таймаут одного действия, секунд")
    args = parser.parse_args(argv)
    try:
        policy = _load_policy(args.policy)
        result = asyncio.run(run_validation(
            source_path=args.source, output_dir=args.output, task_id=args.task_id,
            policy=policy, recursive=not args.no_recursive,
            action_timeout_seconds=args.action_timeout,
        ))
    except (OSError, ValueError, TypeError) as exc:
        print(f"first-data-agent: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("first-data-agent: interrupted; start a new run with a new task ID", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=True, indent=2))
    return result.exit_code
