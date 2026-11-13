"""Автоматическая оценка: прогон агента по всем сценариям и сравнение с «истиной».

Запуск из папки Ours_agent:   python -m geo_bench.evaluate
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path


from agent_runtime import AgentRuntime, RuntimeStatus, Task

from geo_bench.demo_profile import DemoGeoProfile
from geo_bench.synthetic import SCENARIOS, build
from geo_tools import GeoToolExecutor

ROOT = Path(__file__).resolve().parent
OUT, RESULTS = ROOT / "out", ROOT / "results"


async def run_scenario(name: str, seed: int = 0, verbose: bool = False) -> dict:
    folder = OUT / name
    shutil.rmtree(folder, ignore_errors=True)
    gt = build(name, folder, seed)
    faults = {"load_model": 2} if name == "flaky_storage" else None
    executor = GeoToolExecutor(faults=faults)
    task = Task(id=f"qc-{name}", description="Проверить увязку свойств 3D-модели",
                payload={"model_dir": str(folder), "output_dir": str(folder / "result")})
    runtime = AgentRuntime(profile=DemoGeoProfile(), executor=executor)
    res = await runtime.run(task)

    issues_file = folder / "result" / "issues.json"
    issues = json.loads(issues_file.read_text(encoding="utf-8")) if issues_file.exists() else []
    flagged = {i["cell"] for i in issues if i["cell"] >= 0}
    truth = {int(c) for c in gt["anomalies"]}
    tp = len(flagged & truth)
    alert = (folder / "result" / "alert.json").exists()

    first_hit = None
    for pos, h in enumerate(executor.session(task.id).history, 1):
        if truth & set(h["flagged"]):
            first_hit = f"{h['action']} (действие №{pos})"
            break
    if verbose:
        for e in res.events:
            print(f"  {e.sequence:>3} {e.kind:<20} {dict(e.details)}")
    return {
        "scenario": name,
        "status": res.status.value,
        "reason": res.reason,
        "injected": len(truth),
        "detected": tp,
        "recall": round(tp / len(truth), 3) if truth else None,
        "false_alarms": len(flagged - truth),
        "precision": round(tp / len(flagged), 3) if flagged else None,
        "alert_expected": gt["expect_alert"],
        "alert_sent": alert,
        "first_detection": first_hit,
        "actions": res.actions,
        "retries": sum(1 for e in res.events if e.kind == "retry_scheduled"),
        "replans": res.replans,
        "passed": res.status is RuntimeStatus.SUCCEEDED and alert == gt["expect_alert"]
                  and (not truth or tp / len(truth) >= 0.9),
    }


def _fmt(v):
    return "—" if v is None else ("✅" if v is True else "❌" if v is False else str(v))


def to_markdown(rows: list[dict]) -> str:
    cols = ["scenario", "status", "injected", "detected", "recall", "false_alarms",
            "alert_sent", "first_detection", "actions", "retries", "replans", "passed"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_fmt(r[c]) for c in cols) + " |" for r in rows]
    ok = sum(r["passed"] for r in rows)
    lines += ["", f"**Пройдено сценариев: {ok}/{len(rows)}**"]
    return "\n".join(lines)


async def summary(seeds: int) -> str:
    """Каждый сценарий на `seeds` разных моделях: устойчивость метрик, а не один удачный прогон."""
    lines = ["| scenario | runs | passed | mean recall | false alarms (sum) |", "|---|---|---|---|---|"]
    for name in SCENARIOS:
        rs = [await run_scenario(name, s) for s in range(seeds)]
        rec = [r["recall"] for r in rs if r["recall"] is not None]
        lines.append(f"| {name} | {len(rs)} | {sum(r['passed'] for r in rs)} | "
                     f"{sum(rec) / len(rec):.3f} | {sum(r['false_alarms'] for r in rs)} |" if rec else
                     f"| {name} | {len(rs)} | {sum(r['passed'] for r in rs)} | — | {sum(r['false_alarms'] for r in rs)} |")
    return "\n".join(lines)


async def main(names=None, seeds: int = 1) -> list[dict]:
    rows = [await run_scenario(n) for n in (names or SCENARIOS)]
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "evaluation.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
    md = to_markdown(rows)
    text = "# Результаты автоматической оценки (3D-модель)\n\n## Подробно, seed 0\n\n" + md + "\n"
    if seeds > 1:
        text += f"\n## Сводка по {seeds} моделям\n\n" + await summary(seeds) + "\n"
    (RESULTS / "evaluation.md").write_text(text, "utf-8")
    print(text)
    return rows


if __name__ == "__main__":
    args = sys.argv[1:]
    seeds = 1
    if "--seeds" in args:
        i = args.index("--seeds")
        seeds = int(args[i + 1])
        del args[i:i + 2]
    rows = asyncio.run(main(args or None, seeds))
    sys.exit(0 if all(r["passed"] for r in rows) else 1)
