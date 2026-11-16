"""Public first-agent API and CLI against real parsers and temporary files."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from data_validation_profile import ValidationPolicy
from first_agent import run_validation


LAS = """~Version
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
100.0 50
100.1 70
"""

APP_ROOT = Path(__file__).resolve().parents[1]


class TemporaryInputs:
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "input"
        self.source.mkdir()
        self.output = self.root / "output"

    def put(self, name: str, content: str) -> Path:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def report(self, result: dict) -> dict:
        return json.loads(Path(result["reports"]["json"]).read_text(encoding="utf-8"))


class FirstAgentAPITests(TemporaryInputs, unittest.IsolatedAsyncioTestCase):
    async def test_actual_las_and_csv_complete_pipeline_and_persist_result(self):
        self.put("well.las", LAS)
        self.put("wells.csv", "WELL,DATE\nA,2026-01-01\n")
        policy = ValidationPolicy(
            required_fields_by_format={"las": ("DEPT", "GR"), "csv": ("WELL", "DATE")},
            numeric_ranges={"GR": (0, 300)},
        )

        result = (await run_validation(
            self.source, self.output, task_id="complete", policy=policy,
        )).to_dict()

        self.assertEqual(result["task_id"], "complete")
        self.assertEqual(result["runtime_status"], "succeeded", result["reason"])
        self.assertEqual(result["technical_verdict"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["coverage"], {
            "discovered": 2, "parsed": 2, "parse_failed": 0,
            "unsupported": 0, "validated": 2, "validation_completed": True,
        })
        self.assertEqual(result["admission"]["status"], "not_assessed")
        self.assertTrue(result["admission"]["reason"])
        self.assertTrue(result["stubs"], "Unimplemented components must remain visible")
        for stub in result["stubs"]:
            self.assertTrue({"id", "status", "owner", "reason"} <= stub.keys())
            for field in ("id", "status", "owner", "reason"):
                self.assertTrue(stub[field], stub)

        run_dir = self.output / "complete"
        for key, filename in (
            ("json", "validation.json"),
            ("markdown", "validation.md"),
            ("result", "result.json"),
        ):
            path = Path(result["reports"][key])
            self.assertEqual(path.resolve(), (run_dir / filename).resolve())
            self.assertTrue(path.is_file(), path)
        persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted, result)
        report = self.report(result)
        self.assertEqual(report["task_id"], "complete")
        self.assertEqual(report["findings"], [])
        self.assertEqual({Path(f["path"]).name for f in report["files"]}, {"well.las", "wells.csv"})
        self.assertIn("complete", (run_dir / "validation.md").read_text(encoding="utf-8"))

    async def test_corrupt_file_is_reported_and_other_file_is_validated(self):
        self.put("good.csv", "WELL,VALUE\nA,1\n")
        self.put("broken.csv", "A,B\n1,2\n3,4,5\n")

        result = (await run_validation(self.source, self.output, task_id="corrupt")).to_dict()

        self.assertEqual(result["runtime_status"], "succeeded", result["reason"])
        self.assertEqual(result["technical_verdict"], "errors")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["coverage"]["discovered"], 2)
        self.assertEqual(result["coverage"]["parsed"], 1)
        self.assertEqual(result["coverage"]["parse_failed"], 1)
        self.assertEqual(result["coverage"]["validated"], 1)
        self.assertTrue(result["coverage"]["validation_completed"])
        findings = self.report(result)["findings"]
        self.assertTrue(any(
            f["check_id"] == "parse_error" and Path(f["file_path"]).name == "broken.csv"
            for f in findings
        ), findings)
        self.assertTrue(Path(result["reports"]["markdown"]).is_file())

    async def test_incomplete_run_does_not_publish_a_positive_report_verdict(self):
        # csv.Error currently escapes the parser as a technical action failure.
        # Exercise the public assembly without replacing its real tools.
        self.put("oversized.csv", "VALUE\n" + "x" * (csv.field_size_limit() + 1) + "\n")

        result = (await run_validation(self.source, self.output, task_id="incomplete")).to_dict()

        self.assertEqual(result["runtime_status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["technical_verdict"], "unavailable")
        self.assertFalse(result["coverage"]["validation_completed"])
        report = self.report(result)
        self.assertEqual(report["verdict"], "unavailable")
        self.assertEqual(report["technical_verdict"], "unavailable")
        self.assertTrue(result["action_errors"])
        self.assertTrue(any("field larger" in item["error"] for item in result["action_errors"]))
        self.assertEqual(report["execution"]["action_errors"], result["action_errors"])
        markdown = Path(result["reports"]["markdown"]).read_text(encoding="utf-8")
        self.assertNotIn("данные в порядке", markdown)
        self.assertIn("не завершена", markdown.lower())
        self.assertIn("field larger", markdown)

    async def test_empty_input_never_looks_like_technical_pass(self):
        result = (await run_validation(self.source, self.output, task_id="empty")).to_dict()

        self.assertNotEqual(result["technical_verdict"], "ok")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["coverage"]["discovered"], 0)
        self.assertEqual(result["coverage"]["validated"], 0)
        self.assertEqual(result["admission"]["status"], "not_assessed")
        self.assertIn("no_input_files", {f["check_id"] for f in self.report(result)["findings"]})

    async def test_only_unsupported_input_never_looks_like_technical_pass(self):
        self.put("readme.txt", "Unsupported input, not a LAS/CSV table.\n")

        result = (await run_validation(self.source, self.output, task_id="unsupported")).to_dict()

        self.assertNotEqual(result["technical_verdict"], "ok")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["coverage"]["discovered"], 1)
        self.assertEqual(result["coverage"]["parsed"], 0)
        self.assertEqual(result["coverage"]["unsupported"], 1)
        self.assertEqual(result["coverage"]["validated"], 0)
        self.assertIn("unsupported_format", {f["check_id"] for f in self.report(result)["findings"]})

    async def test_warning_with_a_validated_file_has_success_exit_code(self):
        self.put("good.csv", "A,B\n1,2\n")
        self.put("readme.txt", "This is not a supported input table.\n")

        result = (await run_validation(self.source, self.output, task_id="warning")).to_dict()

        self.assertEqual(result["runtime_status"], "succeeded")
        self.assertEqual(result["technical_verdict"], "warnings")
        self.assertEqual(result["coverage"]["validated"], 1)
        self.assertEqual(result["coverage"]["unsupported"], 1)
        self.assertEqual(result["exit_code"], 0)

    async def test_existing_task_id_is_rejected_without_overwriting_artifacts(self):
        self.put("good.csv", "A,B\n1,2\n")
        first = (await run_validation(self.source, self.output, task_id="same-id")).to_dict()
        original = {key: Path(path).read_bytes() for key, path in first["reports"].items()}
        self.put("good.csv", "A,B\n1,2,3\n")

        with self.assertRaises(ValueError):
            await run_validation(self.source, self.output, task_id="same-id")

        self.assertEqual(
            {key: Path(path).read_bytes() for key, path in first["reports"].items()}, original,
        )

    async def test_source_inside_output_root_is_rejected(self):
        nested_source = self.output / "input"
        nested_source.mkdir(parents=True)
        (nested_source / "good.csv").write_text("A,B\n1,2\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            await run_validation(nested_source, self.output, task_id="nested")

        self.assertFalse((self.output / "nested").exists())

    async def test_recursive_option_controls_nested_files(self):
        self.put("good.csv", "A,B\n1,2\n")
        self.put("nested/broken.csv", "A,B\n1,2,3\n")

        flat = (await run_validation(
            self.source, self.output, task_id="flat", recursive=False,
        )).to_dict()
        recursive = (await run_validation(
            self.source, self.output, task_id="recursive", recursive=True,
        )).to_dict()

        self.assertEqual(flat["coverage"]["discovered"], 1)
        self.assertEqual(flat["technical_verdict"], "ok")
        self.assertEqual(recursive["coverage"]["discovered"], 2)
        self.assertEqual(recursive["coverage"]["parse_failed"], 1)
        self.assertEqual(recursive["technical_verdict"], "errors")


class FirstAgentCLITests(TemporaryInputs, unittest.TestCase):
    def cli(self, *arguments: str) -> subprocess.CompletedProcess:
        unrelated_cwd = self.root / "elsewhere"
        unrelated_cwd.mkdir(exist_ok=True)
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", str(APP_ROOT / "run_first_agent.py"),
             "--source", str(self.source), "--output", str(self.output), *arguments],
            cwd=unrelated_cwd, env=environment, capture_output=True,
            text=True, encoding="utf-8", timeout=30, check=False,
        )

    def test_script_works_outside_project_without_pythonpath(self):
        self.put("good.csv", "A,B\n1,2\n")
        self.put("nested/broken.csv", "A,B\n1,2,3\n")

        process = self.cli("--task-id", "cli-clean", "--no-recursive", "--action-timeout", "10")

        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        result = json.loads((self.output / "cli-clean" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["coverage"]["discovered"], 1)
        self.assertEqual(result["runtime_status"], "succeeded")
        self.assertEqual(result["technical_verdict"], "ok")
        self.assertTrue(Path(result["reports"]["json"]).is_file())
        self.assertTrue(Path(result["reports"]["markdown"]).is_file())

    def test_data_errors_produce_exit_code_one_and_a_report(self):
        self.put("broken.csv", "A,B\n1,2,3\n")

        process = self.cli("--task-id", "cli-errors")

        self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
        result = json.loads((self.output / "cli-errors" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["technical_verdict"], "errors")
        self.assertTrue(any(f["check_id"] == "parse_error" for f in self.report(result)["findings"]))

    def test_invalid_policy_produces_exit_code_two(self):
        self.put("good.csv", "A,B\n1,2\n")
        invalid_policy = self.root / "invalid-policy.json"
        invalid_policy.write_text("{", encoding="utf-8")

        process = self.cli("--task-id", "cli-config", "--policy", str(invalid_policy))

        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
        self.assertFalse((self.output / "cli-config" / "validation.json").exists())


if __name__ == "__main__":
    unittest.main()
