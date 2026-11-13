"""Демонстрационный запуск одного сценария с журналом событий.

python -m geo_bench.run_demo combined
"""
import asyncio
import json
import sys

from geo_bench.evaluate import OUT, run_scenario

name = sys.argv[1] if len(sys.argv) > 1 else "combined"
print(f"=== Сценарий: {name} ===")
row = asyncio.run(run_scenario(name, verbose=True))
print(json.dumps(row, ensure_ascii=False, indent=1))
print(f"\nОтчёт: {OUT / name / 'result' / 'report.md'}")
