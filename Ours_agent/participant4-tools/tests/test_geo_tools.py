import asyncio
import tempfile
import unittest
from pathlib import Path

import geo_tools  # noqa: F401
from agent_runtime import (ActionError, ActionRequest, Context, Goal, Step, Task)

from geo_bench.evaluate import run_scenario
from geo_bench.synthetic import build, clean_model
from geo_tools import GeoToolExecutor
from geo_tools import checks as C


def request(task, action, key="i1:s", attempt=1, **params):
    ctx = Context(task, None, Goal("g"))
    return ActionRequest(ctx, "i1", "p1", Step("s", action, params), attempt, key)


class ChecksTest(unittest.TestCase):
    def test_clean_model_has_no_errors(self):
        m, wells = clean_model(seed=3)
        for name, func in C.CHECKS.items():
            issues, _ = func(m)
            self.assertEqual([i for i in issues if i.severity == C.ERROR], [], name)
        self.assertEqual(C.check_well_tie(m, wells)[0], [])

    def test_percent_units_detected(self):
        m, _ = clean_model()
        m.poro[:] = [p * 100 for p in m.poro]
        _, extra = C.check_ranges(m)
        self.assertTrue(extra["unit_error_suspected"])

    def test_volumes_positive(self):
        m, _ = clean_model()
        self.assertGreater(C.compute_volumes(m)["stoiip_mln_m3"], 0)


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        build("combined", self.tmp)
        self.task = Task("t", "qc", {"model_dir": str(self.tmp), "output_dir": str(self.tmp / "r")})

    def run_(self, ex, action, **kw):
        return asyncio.run(ex.execute(request(self.task, action, **kw)))

    def test_supports(self):
        ex = GeoToolExecutor()
        self.assertTrue(ex.supports("check_contacts"))
        self.assertFalse(ex.supports("drill_well"))

    def test_unknown_action_is_not_retryable(self):
        with self.assertRaises(ActionError) as cm:
            self.run_(GeoToolExecutor(), "drill_well")
        self.assertFalse(cm.exception.retryable)

    def test_check_without_model_fails(self):
        with self.assertRaises(ActionError):
            self.run_(GeoToolExecutor(), "check_ranges")

    def test_missing_file_is_not_retryable(self):
        task = Task("t", "qc", {"model_dir": str(self.tmp / "nope")})
        with self.assertRaises(ActionError) as cm:
            asyncio.run(GeoToolExecutor().execute(request(task, "load_model")))
        self.assertFalse(cm.exception.retryable)

    def test_fault_injection_is_retryable(self):
        ex = GeoToolExecutor(faults={"load_model": 1})
        with self.assertRaises(ActionError) as cm:
            self.run_(ex, "load_model")
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(self.run_(ex, "load_model").observations["cells"], 4000)

    def test_observations_are_python_objects(self):
        ex = GeoToolExecutor()
        self.run_(ex, "load_model")
        obs = self.run_(ex, "check_contacts", key="i1:c").observations
        self.assertEqual(obs["check"], "check_contacts")
        self.assertIsInstance(obs["flagged"], tuple)
        self.assertGreater(obs["errors"], 0)

    def test_report_is_idempotent(self):
        ex = GeoToolExecutor()
        self.run_(ex, "load_model")
        first = self.run_(ex, "write_report", key="i1:report")
        (self.tmp / "r" / "report.md").write_text("changed")
        second = self.run_(ex, "write_report", key="i1:report", attempt=2)
        self.assertEqual(dict(first.observations), dict(second.observations))
        self.assertEqual((self.tmp / "r" / "report.md").read_text(), "changed")


class BenchTest(unittest.TestCase):
    def test_key_scenarios(self):
        for name in ("clean", "combined", "percent_units", "flaky_storage"):
            with self.subTest(name=name):
                self.assertTrue(asyncio.run(run_scenario(name))["passed"])


if __name__ == "__main__":
    unittest.main()
