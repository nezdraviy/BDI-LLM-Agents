"""Тесты набора инструментов первого агента (участник 4).

Уровни:
  1. Парсеры LAS/CSV.
  2. Проверки качества данных.
  3. Исполнитель: схема observations, идемпотентность, ошибки.
  4. Интеграция: AgentRuntime + DataValidationProfile + DataValidationExecutor
     на каждом сценарии стенда.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime import (ActionError, ActionRequest, AgentRuntime, Context, Goal,
                           RuntimeStatus, Step, Task)
from data_bench import synthetic as S
from data_bench.evaluate import run_one
from data_tools import ACTIONS, DataValidationExecutor
from data_tools import checks as C
from data_tools.tables import ParseError, UnsupportedFile, parse_csv, parse_las
from data_validation_profile import DataValidationProfile, ValidationPolicy

LAS = """~Version
 VERS. 2.0 : CWLS
 WRAP. NO  :
~Well
 STRT.M 100.0 :
 STOP.M 100.2 :
 STEP.M 0.1 :
 NULL.  -999.25 :
~Curve
 DEPT.M  : depth
 GR.API  : gamma
~A
100.0 50
100.1 -999.25
100.2 70
"""


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, name, text, enc="utf-8"):
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode(enc))
        return p


# ------------------------------------------------------------- 1. парсеры
class ParsersTest(TmpCase):
    def test_las_basic_and_null(self):
        t = parse_las(self.put("a.las", LAS))
        self.assertEqual(t.fields, ("DEPT", "GR"))
        self.assertEqual(t.row_count, 3)
        self.assertEqual(t.column("gr"), [50.0, None, 70.0])
        self.assertEqual((t.meta["strt"], t.meta["stop"]), (100.0, 100.2))

    def test_las_wrapped(self):
        text = LAS.replace("WRAP. NO", "WRAP. YES").replace("100.0 50\n", "100.0\n50\n")
        self.assertEqual(parse_las(self.put("w.las", text)).row_count, 3)

    def test_las_bad_row_is_parse_error(self):
        with self.assertRaisesRegex(ParseError, "expected 2 values"):
            parse_las(self.put("b.las", LAS.replace("100.2 70", "100.2")))

    def test_las_without_data_section(self):
        with self.assertRaisesRegex(ParseError, "~A"):
            parse_las(self.put("c.las", LAS.split("~A")[0]))

    def test_las_v3_unsupported(self):
        with self.assertRaises(UnsupportedFile):
            parse_las(self.put("d.las", LAS.replace("VERS. 2.0", "VERS. 3.0")))

    def test_csv_semicolon_decimal_comma_cp1251(self):
        t = parse_csv(self.put("k.csv", "WELL;PORO;COMMENT\nW-1;0,21;песчаник\n", "cp1251"))
        self.assertEqual(t.meta["delimiter"], ";")
        self.assertEqual(t.column("COMMENT"), ["песчаник"])

    def test_csv_ragged_row(self):
        with self.assertRaisesRegex(ParseError, "line 3"):
            parse_csv(self.put("r.csv", "A,B\n1,2\n3,4,5\n"))

    def test_csv_empty(self):
        with self.assertRaises(ParseError):
            parse_csv(self.put("e.csv", "\n\n"))


# ------------------------------------------------------------- 2. проверки
class ChecksTest(TmpCase):
    def table(self, text=LAS):
        return parse_las(self.put("x.las", text))

    def ids(self, findings):
        return sorted(f["check_id"] for f in findings)

    def test_clean_table_has_no_findings(self):
        self.assertEqual(C.validate_table(self.table(), ("DEPT", "GR"), {"GR": (0, 300)}), [])

    def test_required_and_range(self):
        out = C.validate_table(self.table(LAS.replace("100.2 70", "100.2 900")),
                               ("DEPT", "GR", "RHOB"), {"GR": (0, 300)})
        self.assertEqual(self.ids(out), ["required_field_missing", "value_out_of_range"])

    def test_depth_not_monotonic_and_header_mismatch(self):
        bad = LAS.replace("100.1 -999.25", "100.3 60").replace("STOP.M 100.2", "STOP.M 150")
        self.assertEqual(self.ids(C.check_depth(self.table(bad))),
                         ["depth_not_monotonic", "depth_range_mismatch"])

    def test_empty_curve(self):
        bad = LAS.replace("100.0 50", "100.0 -999.25").replace("100.2 70", "100.2 -999.25")
        self.assertEqual(self.ids(C.check_empty(self.table(bad))), ["empty_curve"])

    def test_non_numeric_in_csv(self):
        t = parse_csv(self.put("c.csv", "WELL,PERM\nA,12\nB,n/a\n"))
        self.assertEqual(self.ids(C.check_ranges(t, {"PERM": (0, None)})), ["non_numeric_value"])

    def test_every_check_id_has_valid_severity(self):
        self.assertTrue(set(C.SEVERITY.values()) <= {"info", "warning", "error", "critical"})


# ------------------------------------------------------------- 3. исполнитель
def request(task, action, params=None, beliefs=None, attempt=1):
    ctx = Context(task, beliefs, Goal("g"))
    return ActionRequest(ctx, "i1", "p1", Step("s", action, params or {}), attempt, "i1:s")


class ExecutorTest(TmpCase):
    def run_(self, coro):
        return asyncio.run(coro)

    def test_supports_exactly_contract_actions(self):
        ex = DataValidationExecutor()
        for a in ACTIONS:
            self.assertTrue(ex.supports(a))
        self.assertFalse(ex.supports("rm_rf"))
        self.assertEqual(set(ACTIONS), {"scan_source_directory", "parse_input_files",
                                        "validate_input_data", "build_validation_report",
                                        "save_validation_report"})

    def test_scan_missing_source_is_finding_not_exception(self):
        task = Task("t", "d", {})
        r = self.run_(DataValidationExecutor().execute(
            request(task, "scan_source_directory", {"source_path": str(self.dir / "nope")})))
        self.assertTrue(r.observations["inventory_completed"])
        self.assertEqual(r.observations["files"], [])
        self.assertEqual(r.observations["findings"][0]["check_id"], "source_not_found")

    def test_scan_skips_hidden_and_report_dir(self):
        self.put("in/a.las", LAS)
        self.put("in/.git/x.las", LAS)
        self.put("in/reports/old.json", "{}")
        ex = DataValidationExecutor(default_report_dir=self.dir / "in" / "reports")
        r = self.run_(ex.execute(request(Task("t", "d"), "scan_source_directory",
                                         {"source_path": str(self.dir / "in"), "recursive": True})))
        self.assertEqual([Path(f["path"]).name for f in r.observations["files"]], ["a.las"])

    def test_fault_injection_is_retryable(self):
        ex = DataValidationExecutor(faults={"scan_source_directory": 1})
        req = request(Task("t", "d"), "scan_source_directory", {"source_path": str(self.dir)})
        with self.assertRaises(ActionError) as cm:
            self.run_(ex.execute(req))
        self.assertTrue(cm.exception.retryable)
        self.run_(ex.execute(req))  # второй раз проходит

    def test_unknown_action_rejected(self):
        with self.assertRaises(ActionError):
            self.run_(DataValidationExecutor().execute(request(Task("t", "d"), "format_disk")))

    def test_save_is_idempotent_and_atomic(self):
        ex = DataValidationExecutor(default_report_dir=self.dir / "rep")
        task = Task("t", "d")
        self.run_(ex.execute(request(task, "build_validation_report", {"format": "json"})))
        out = self.dir / "rep" / "r.json"
        for _ in range(2):
            r = self.run_(ex.execute(request(task, "save_validation_report", {"output_path": str(out)})))
        self.assertEqual(r.observations, {"report_saved": True, "report_path": str(out)})
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["verdict"], "ok")
        self.assertFalse(any(p.suffix == ".tmp" for p in out.parent.iterdir()))

    def test_disk_error_becomes_retryable_action_error(self):
        ex = DataValidationExecutor()
        task = Task("t", "d")
        self.run_(ex.execute(request(task, "build_validation_report")))
        (self.dir / "occupied").mkdir()
        with self.assertRaises(ActionError) as cm:
            self.run_(ex.execute(request(task, "save_validation_report",
                                         {"output_path": str(self.dir / "occupied")})))
        self.assertTrue(cm.exception.retryable)


# ------------------------------------------------------------- 4. интеграция
class IntegrationTest(unittest.TestCase):
    def test_every_scenario_passes(self):
        """Полный цикл BDI-агента на каждом сценарии стенда (seed 0 и 1)."""
        for scn in S.ALL.values():
            for seed in (0, 1):
                with self.subTest(scenario=scn.name, seed=seed), tempfile.TemporaryDirectory() as tmp:
                    r = asyncio.run(run_one(scn, seed, Path(tmp)))
                    self.assertTrue(r["passed"], {k: r[k] for k in
                                                  ("status", "reason", "missed", "false_alarm_list")})

    def test_profile_accepts_observations_and_goals_achieved(self):
        """Контракт: profile.integrate принимает все наблюдения, цели достигнуты."""
        with tempfile.TemporaryDirectory() as tmp:
            source, _ = S.build(S.ALL["combined"], Path(tmp), seed=3)
            profile = DataValidationProfile(ValidationPolicy(**S.POLICY_KW))
            ex = DataValidationExecutor(default_report_dir=Path(tmp) / "rep")
            task = Task("contract-1", "d", {"source_path": str(source)})
            res = asyncio.run(AgentRuntime(profile=profile, executor=ex).run(task))
            self.assertEqual(res.status, RuntimeStatus.SUCCEEDED, res.reason)
            self.assertEqual([c[0] for c in ex.calls], list(ACTIONS))
            beliefs = profile._states["contract-1"]
            self.assertTrue(beliefs.report_saved)
            self.assertTrue(Path(beliefs.report_path).exists())
            self.assertEqual(len(beliefs.files), 6)


if __name__ == "__main__":
    unittest.main()
