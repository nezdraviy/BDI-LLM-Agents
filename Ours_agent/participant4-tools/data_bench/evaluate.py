"""Автоматическая оценка первого агента: AgentRuntime + DataValidationProfile + наши инструменты.

Для каждого сценария и каждого seed: сгенерировать данные с известными дефектами,
прогнать агента, прочитать СОХРАНЁННЫЙ им отчёт и сравнить находки с «истиной».

Метрики прогона:
  status        итог runtime (succeeded / failed / blocked)
  recall        доля внесённых дефектов, найденных агентом
  precision     доля находок, которые действительно были внесены
  false_alarms  находки, которых не вносили
  actions, replans, retries — сколько работы и восстановлений понадобилось
Прогон пройден, если статус равен ожидаемому, а при успехе отчёт сохранён,
recall = 1 и ложных тревог нет.

Запуск из корня Ours_agent:
    python -m data_bench.evaluate                 # все сценарии, 20 seed
    python -m data_bench.evaluate --seeds 5
    python -m data_bench.evaluate --scenario gr_spike --seeds 1 -v
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from agent_runtime import AgentRuntime, RuntimeLimits, Task
from data_tools import DataValidationExecutor
from data_validation_profile import DataValidationProfile, ValidationPolicy

from . import synthetic as S
from .profiles import RetrySafeProfile

RESULTS = Path(__file__).resolve().parent / "results"


def make_agent(scn: S.Scenario, report_dir: Path):
    policy = ValidationPolicy(**S.POLICY_KW)
    profile = (RetrySafeProfile if scn.retry_safe_profile else DataValidationProfile)(policy)
    executor = DataValidationExecutor(default_report_dir=report_dir, faults=dict(scn.faults))
    runtime = AgentRuntime(profile=profile, executor=executor,
                           limits=RuntimeLimits(max_replans=5, action_timeout_seconds=30))
    return runtime, executor


def detected_pairs(findings: list[dict], source: Path) -> set[tuple[str, str]]:
    out = set()
    for f in findings:
        fp = f.get("file_path")
        if not fp or Path(fp) == source:
            rel = S.SOURCE
        else:
            try:
                rel = Path(fp).relative_to(source).as_posix()
            except ValueError:
                rel = fp
        out.add((rel, f["check_id"]))
    return out


async def run_one(scn: S.Scenario, seed: int, root: Path, report_format: str = "json") -> dict:
    source, expected = S.build(scn, root, seed)
    runtime, executor = make_agent(scn, root / "reports")
    report_path = root / f"report.{'md' if report_format == 'md' else 'json'}"
    task = Task(id=f"{scn.name}-s{seed}", description=scn.title,
                payload={"source_path": str(source), "report_format": report_format,
                         "report_path": str(report_path)})
    t0 = time.perf_counter()
    res = await runtime.run(task)
    elapsed = time.perf_counter() - t0

    report = None
    if report_path.exists() and report_format == "json":
        report = json.loads(report_path.read_text(encoding="utf-8"))
    detected = detected_pairs(report["findings"], source) if report else set()
    hit = expected & detected
    fa = detected - expected
    recall = len(hit) / len(expected) if expected else None
    precision = len(hit) / len(detected) if detected else None
    kinds = [e.kind for e in res.events]
    ok_status = res.status.value == scn.expect_status
    if scn.expect_status == "succeeded":
        passed = ok_status and report is not None and not (expected - detected) and not fa
    else:
        passed = ok_status
    return {
        "scenario": scn.name, "seed": seed, "status": res.status.value, "reason": res.reason,
        "expected": sorted(map(list, expected)), "detected": sorted(map(list, detected)),
        "missed": sorted(map(list, expected - detected)), "false_alarm_list": sorted(map(list, fa)),
        "recall": recall, "precision": precision, "false_alarms": len(fa),
        "verdict": report["verdict"] if report else None,
        "actions": res.actions, "replans": res.replans,
        "retries": kinds.count("retry_scheduled"), "failures": kinds.count("action_failed"),
        "seconds": round(elapsed, 3), "passed": passed, "result": res,
    }


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def aggregate(runs: list[dict]) -> dict:
    return {
        "runs": len(runs), "passed": sum(r["passed"] for r in runs),
        "status": "/".join(sorted({r["status"] for r in runs})),
        "recall": _mean(r["recall"] for r in runs),
        "precision": _mean(r["precision"] for r in runs),
        "false_alarms": sum(r["false_alarms"] for r in runs),
        "actions": _mean(r["actions"] for r in runs),
        "retries": _mean(r["retries"] for r in runs),
        "replans": _mean(r["replans"] for r in runs),
        "ms": 1000 * _mean(r["seconds"] for r in runs),
    }


async def evaluate(names: list[str], seeds: int, verbose: bool = False) -> dict:
    table = {}
    failures = []
    for name in names:
        scn = S.ALL[name]
        runs = []
        for seed in range(seeds):
            with tempfile.TemporaryDirectory() as tmp:
                r = await run_one(scn, seed, Path(tmp))
            r.pop("result")
            runs.append(r)
            if not r["passed"]:
                failures.append(r)
            if verbose:
                print(json.dumps(r, ensure_ascii=False, indent=1))
        table[name] = aggregate(runs)
    return {"seeds": seeds, "scenarios": table, "failures": failures}


def _f(x, nd=2):
    return "—" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def to_markdown(res: dict) -> str:
    def rows(names):
        out = ["| Сценарий | Что внесено | Прогонов | Пройдено | Статус | Recall | Precision | Ложные тревоги | Действий | Повторов | Пересмотров | мс |",
               "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for n in names:
            a = res["scenarios"][n]
            ok = "✅" if a["passed"] == a["runs"] else "❌"
            out.append(f"| `{n}` | {S.ALL[n].title} | {a['runs']} | {ok} {a['passed']} | {a['status']} | "
                       f"{_f(a['recall'])} | {_f(a['precision'])} | {a['false_alarms']} | {_f(a['actions'], 1)} | "
                       f"{_f(a['retries'], 1)} | {_f(a['replans'], 1)} | {_f(a['ms'], 0)} |")
        return out

    main = [s.name for s in S.SCENARIOS if s.name in res["scenarios"]]
    faults = [s.name for s in S.FAULT_SCENARIOS if s.name in res["scenarios"]]
    total = sum(a["runs"] for a in res["scenarios"].values())
    passed = sum(a["passed"] for a in res["scenarios"].values())
    lines = ["# Автооценка первого агента (проверка входных данных)", "",
             f"Связка: `AgentRuntime` (участник 1) + `DataValidationProfile` (участник 2) + "
             f"`DataValidationExecutor` (участник 4). Seed на сценарий: **{res['seeds']}**.", "",
             f"**Итого: {passed} из {total} прогонов пройдено.**", ""]
    if main:
        lines += ["## Качество обнаружения дефектов", ""] + rows(main) + [""]
    if faults:
        lines += ["## Устойчивость к сбоям инструментов", ""] + rows(faults) + [
            "", "`flaky_save` ожидаемо завершается `failed`: в профиле v1 шаги без `retry_safe`, "
            "поэтому один временный сбой исключает единственный план цели. С `retry_safe=True` "
            "(`RetrySafeProfile`) агент восстанавливается. Это предложение участнику 2.", ""]
    if res["failures"]:
        lines += ["## Непройденные прогоны", ""]
        for r in res["failures"][:20]:
            lines.append(f"- `{r['scenario']}` seed {r['seed']}: status={r['status']} ({r['reason']}); "
                         f"пропущено {r['missed']}; ложные {r['false_alarm_list']}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--scenario", action="append", choices=sorted(S.ALL))
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    names = a.scenario or list(S.ALL)
    res = asyncio.run(evaluate(names, a.seeds, a.verbose))
    RESULTS.mkdir(exist_ok=True)
    md = to_markdown(res)
    (RESULTS / "evaluation.md").write_text(md, encoding="utf-8")
    (RESULTS / "evaluation.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(md)
    print(f"Сохранено: {RESULTS / 'evaluation.md'}")


if __name__ == "__main__":
    main()
