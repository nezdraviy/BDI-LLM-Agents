"""Regression checks for invalid numeric LAS values and missing depth data."""

import json
import tempfile
import unittest
from pathlib import Path

from data_tools import checks as C
from data_tools import report
from data_tools.tables import Table, parse_las


LAS_HEADER = """~Version
 VERS. 2.0 : CWLS
 WRAP. NO :
~Well
 STRT.M 100.0 :
 STOP.M 100.1 :
 STEP.M 0.1 :
 NULL. -999.25 :
~Curve
 DEPT.M : depth
 GR.API : gamma
~A
"""


class NumericEdgeCasesTest(unittest.TestCase):
    def parse(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.las"
            path.write_text(LAS_HEADER + rows, encoding="utf-8")
            return parse_las(path)

    def test_las_non_finite_curve_is_an_error_without_numeric_ranges(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                table = self.parse(f"100.0 {token}\n100.1 20\n")
                findings = C.validate_table(table, (), {})
                non_finite = [f for f in findings if f["check_id"] == "non_finite_value"]
                self.assertEqual(len(non_finite), 1)
                self.assertEqual(non_finite[0]["severity"], "error")
                self.assertIn("GR", non_finite[0]["message"])
                text, _ = report.build("numeric-edge", "input", [], findings, "json")
                self.assertEqual(json.loads(text)["verdict"], "errors")

    def test_non_finite_depth_is_detected_even_when_monotonicity_cannot_detect_it(self):
        table = self.parse("NaN 10\n100.1 20\n")
        findings = C.validate_table(table, (), {})
        self.assertTrue(any(f["check_id"] == "non_finite_value"
                            and "DEPT" in f["message"] for f in findings))

    def test_range_checks_reject_already_numeric_non_finite_floats(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                table = Table("numeric.las", "las", ("GR",), {"GR": [value]}, 1)
                findings = C.check_ranges(table, {"GR": (None, None)})
                self.assertEqual([f["check_id"] for f in findings], ["non_numeric_value"])
                self.assertEqual(findings[0]["severity"], "error")

    def test_all_null_depth_is_reported_for_one_or_multiple_rows(self):
        for rows in ("-999.25 10\n", "-999.25 10\n-999.25 20\n"):
            with self.subTest(rows=rows):
                table = self.parse(rows)
                findings = C.validate_table(table, (), {})
                self.assertEqual([f["check_id"] for f in findings], ["empty_depth"])
                self.assertEqual(findings[0]["severity"], "error")
                self.assertEqual(report.verdict(findings), "errors")


if __name__ == "__main__":
    unittest.main()
