"""Демонстрационный запуск первого агента с читаемым журналом событий.

    python -m data_bench.run_demo                 # сценарий combined
    python -m data_bench.run_demo flaky_all_retry
    python -m data_bench.run_demo --list

Входные данные и отчёт (Markdown) остаются в data_bench/out/<сценарий>/.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from . import synthetic as S
from .evaluate import run_one

OUT = Path(__file__).resolve().parent / "out"

STEP_RU = {
    "scan-source": "найти входные файлы",
    "parse-input-files": "прочитать файлы",
    "validate-input-data": "проверить данные",
    "build-report": "собрать отчёт",
    "save-report": "сохранить отчёт",
}


def describe(e) -> str | None:
    d = e.details
    k = e.kind
    if k == "task_started":
        return "▶ задача принята"
    if k == "intention_created":
        return f"🎯 цель «{d['goal_id']}»: выбран план {d['plan_id']}"
    if k == "action_started":
        tail = f" (попытка {d['attempt']})" if d["attempt"] > 1 else ""
        return f"   → {d['step_id']}: {STEP_RU.get(d['step_id'], '')}{tail}"
    if k == "action_completed":
        return "     ✓ выполнено"
    if k == "action_failed":
        return f"     ✗ сбой: {d['error']}{' (временный)' if d['retryable'] else ''}"
    if k == "retry_scheduled":
        return f"     ↻ повтор, попытка {d['next_attempt']}"
    if k == "reconsideration":
        return f"   ⟲ пересмотр решения по цели «{d['goal_id']}»: {d['reason']}"
    if k == "gap_requested":
        return "   ? нет применимого плана: запрос предложения у LLM-модуля"
    if k == "task_finished":
        return f"■ задача завершена: {d['status']} — {d['reason']}"
    return None


async def demo(name: str) -> None:
    scn = S.ALL[name]
    root = OUT / name
    shutil.rmtree(root, ignore_errors=True)
    print(f"Сценарий: {name} — {scn.title}\n")
    r = await run_one(scn, seed=0, root=root, report_format="md")
    seen = set()
    for e in r["result"].events:
        if e.kind == "goal_assessed":
            key = (e.details["goal_id"], e.details["status"])
            if e.details["status"] == "achieved" and key not in seen:
                seen.add(key)
                print(f"   ★ цель «{e.details['goal_id']}» достигнута")
            continue
        line = describe(e)
        if line:
            print(line)
    report = root / "report.md"
    print(f"\nДействий: {r['actions']}, повторов: {r['retries']}, пересмотров: {r['replans']}")
    if report.exists():
        print(f"Отчёт: {report}\n")
        print(report.read_text(encoding="utf-8"))
    print("Внесённые дефекты (истина для сравнения):")
    for rel, cid in r["expected"] or [("—", "нет")]:
        print(f"  - {rel}: {cid}")


def main():
    if "--list" in sys.argv:
        for s in S.ALL.values():
            print(f"{s.name:20} {s.title}")
        return
    name = sys.argv[1] if len(sys.argv) > 1 else "combined"
    if name not in S.ALL:
        sys.exit(f"Неизвестный сценарий {name!r}; см. --list")
    asyncio.run(demo(name))


if __name__ == "__main__":
    main()
