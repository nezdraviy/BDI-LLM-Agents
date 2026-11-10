from __future__ import annotations

import unittest

from agent_runtime import (
    ActionResult,
    AgentRuntime,
    Check,
    Context,
    Goal,
    GoalStatus,
    Plan,
    PlanProposal,
    RuntimeStatus,
    Step,
    Task,
)
from data_validation_profile import (
    BUILD_REPORT_ACTION,
    INVENTORY_GOAL,
    PARSE_ACTION,
    REPORT_GOAL,
    SAVE_REPORT_ACTION,
    SCAN_ACTION,
    VALIDATE_ACTION,
    DataValidationBeliefs,
    DataValidationProfile,
    ValidationPolicy,
)


class DemoValidationExecutor:
    """Contract mock for the proposed v1 observations schema."""

    def __init__(self, files=None):
        self.files = files if files is not None else [
            {"path": "well_01.las", "format": "las"},
            {"path": "metadata.csv", "format": "csv"},
        ]
        self.calls = []

    def supports(self, action: str) -> bool:
        return action in {
            SCAN_ACTION,
            PARSE_ACTION,
            VALIDATE_ACTION,
            BUILD_REPORT_ACTION,
            SAVE_REPORT_ACTION,
        }

    async def execute(self, request):
        action = request.step.action
        self.calls.append(action)

        if action == SCAN_ACTION:
            return ActionResult({
                "inventory_completed": True,
                "files": self.files,
            })

        if action == PARSE_ACTION:
            parsed_files = [
                {
                    "path": item["path"],
                    "format": item["format"],
                    "parse_status": "parsed" if item["format"] in {"las", "csv"} else "unsupported",
                    "fields": ["DEPT", "GR", "RHOB"] if item["format"] == "las" else ["WELL", "DATE"],
                    "row_count": 100,
                }
                for item in self.files
            ]
            return ActionResult({
                "parsing_completed": True,
                "files": parsed_files,
            })

        if action == VALIDATE_ACTION:
            return ActionResult({
                "validation_completed": True,
                "findings": [
                    {
                        "file_path": "well_01.las",
                        "check_id": "required_curve_present",
                        "severity": "warning",
                        "message": "Example warning: project-specific check reported a missing curve.",
                    }
                ],
            })

        if action == BUILD_REPORT_ACTION:
            return ActionResult({"report_built": True})

        if action == SAVE_REPORT_ACTION:
            return ActionResult({
                "report_saved": True,
                "report_path": request.step.parameters.get(
                    "output_path", "reports/validation.json"
                ),
            })

        raise AssertionError(f"Unexpected action: {action}")


class DataValidationProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_validation_succeeds_even_with_findings(self):
        profile = DataValidationProfile()
        executor = DemoValidationExecutor()
        task = Task(
            "validation-001",
            "Check geological input files",
            {
                "source_path": "data/input",
                "report_path": "reports/validation-001.json",
            },
        )

        result = await AgentRuntime(profile, executor).run(task)
        beliefs = await profile.observe(task)

        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertEqual(
            executor.calls,
            [SCAN_ACTION, PARSE_ACTION, VALIDATE_ACTION, BUILD_REPORT_ACTION, SAVE_REPORT_ACTION],
        )
        self.assertTrue(beliefs.inventory_completed)
        self.assertTrue(beliefs.parsing_completed)
        self.assertTrue(beliefs.validation_completed)
        self.assertTrue(beliefs.report_built)
        self.assertTrue(beliefs.report_saved)
        self.assertEqual(beliefs.report_path, "reports/validation-001.json")
        self.assertEqual(len(beliefs.files), 2)
        self.assertEqual(len(beliefs.findings), 1)
        self.assertEqual(result.belief_revision, beliefs.revision)

    async def test_empty_source_is_reported_but_workflow_can_complete(self):
        profile = DataValidationProfile()
        executor = DemoValidationExecutor(files=[])
        task = Task("empty-input", "Check empty input", {"source_path": "data/empty"})

        result = await AgentRuntime(profile, executor).run(task)
        beliefs = await profile.observe(task)

        self.assertEqual(result.status, RuntimeStatus.SUCCEEDED)
        self.assertTrue(beliefs.validation_completed)
        self.assertTrue(beliefs.report_saved)
        self.assertIn("no_input_files", {finding.check_id for finding in beliefs.findings})

    async def test_missing_source_path_makes_inventory_goal_impossible(self):
        profile = DataValidationProfile()
        task = Task("missing-source", "Check data without a source path")
        executor = DemoValidationExecutor()

        result = await AgentRuntime(profile, executor).run(task)

        self.assertEqual(result.status, RuntimeStatus.FAILED)
        self.assertEqual(executor.calls, [])

    async def test_revision_changes_after_each_integrated_observation(self):
        profile = DataValidationProfile()
        executor = DemoValidationExecutor()
        task = Task("revision-test", "Check input", {"source_path": "data/input"})
        runtime = AgentRuntime(profile, executor)
        runtime.start(task)

        revisions = []
        for _ in range(10):
            if runtime.state != RuntimeStatus.RUNNING:
                break
            await runtime.tick()
            snapshot = await profile.observe(task)
            revisions.append(snapshot.revision)

        self.assertEqual(runtime.state, RuntimeStatus.SUCCEEDED)
        self.assertGreaterEqual(len(set(revisions)), 5)

    async def test_belief_snapshot_is_immutable(self):
        profile = DataValidationProfile()
        task = Task("immutable", "Check input", {"source_path": "data/input"})
        beliefs = await profile.observe(task)

        with self.assertRaises((AttributeError, TypeError)):
            beliefs.inventory_completed = True

        self.assertIsInstance(beliefs, DataValidationBeliefs)
        self.assertIsInstance(beliefs.files, tuple)
        self.assertIsInstance(beliefs.findings, tuple)

    async def test_goals_are_stable_and_all_three_are_returned(self):
        profile = DataValidationProfile()
        task = Task("goals", "Check input", {"source_path": "data/input"})
        beliefs = await profile.observe(task)

        goals = await profile.goals(task, beliefs)

        self.assertEqual(
            [(goal.id, goal.priority) for goal in goals],
            [
                (INVENTORY_GOAL, 30),
                ("validation_complete", 20),
                (REPORT_GOAL, 10),
            ],
        )

    async def test_proposal_with_action_outside_allowlist_is_rejected(self):
        profile = DataValidationProfile()
        task = Task("proposal", "Check input", {"source_path": "data/input"})
        beliefs = await profile.observe(task)
        goal = Goal(INVENTORY_GOAL, 30)
        context = Context(task, beliefs, goal)
        proposal = PlanProposal(
            Plan(
                id="llm-dangerous-plan",
                goal_id=INVENTORY_GOAL,
                steps=(Step("delete", "delete_source_directory"),),
            ),
            "Attempt to delete the source",
        )

        check = await profile.validate_proposal(proposal, context)

        self.assertFalse(check.allowed)
        self.assertIn("allowlist", check.reason)

    async def test_policy_is_immutable_and_affects_revision(self):
        task = Task("policy", "Check input", {"source_path": "data/input"})
        default_profile = DataValidationProfile()
        strict_profile = DataValidationProfile(
            ValidationPolicy(
                required_fields_by_format={"las": ("DEPT", "GR")},
                numeric_ranges={"GR": (0, 250)},
            )
        )

        default_beliefs = await default_profile.observe(task)
        strict_beliefs = await strict_profile.observe(task)

        self.assertNotEqual(default_beliefs.revision, strict_beliefs.revision)
        with self.assertRaises(TypeError):
            strict_profile.policy.required_fields_by_format["las"] = ("RHOB",)


    async def test_task_options_are_part_of_belief_revision(self):
        profile = DataValidationProfile()
        task_json = Task(
            "same-task-id",
            "Check input",
            {"source_path": "data/input", "report_format": "json"},
        )
        task_csv = Task(
            "same-task-id",
            "Check input",
            {"source_path": "data/input", "report_format": "csv"},
        )

        json_beliefs = await profile.observe(task_json)
        csv_beliefs = await profile.observe(task_csv)

        self.assertNotEqual(json_beliefs.revision, csv_beliefs.revision)
        self.assertEqual(json_beliefs.report_format, "json")
        self.assertEqual(csv_beliefs.report_format, "csv")

    async def test_parse_result_must_cover_all_inventoried_files(self):
        class BadParseExecutor(DemoValidationExecutor):
            async def execute(self, request):
                if request.step.action == PARSE_ACTION:
                    self.calls.append(PARSE_ACTION)
                    return ActionResult({
                        "parsing_completed": True,
                        "files": [],
                    })
                return await super().execute(request)

        profile = DataValidationProfile()
        executor = BadParseExecutor()
        task = Task("bad-parse", "Check input", {"source_path": "data/input"})

        result = await AgentRuntime(profile, executor).run(task)

        self.assertEqual(result.status, RuntimeStatus.FAILED)
        self.assertIn("adapter error", result.reason)
        self.assertEqual(executor.calls, [SCAN_ACTION, PARSE_ACTION])


if __name__ == "__main__":
    unittest.main()
