"""Workflow safety checks using temporary files and no real model requests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluation.data import dataset_hash, load_dataset
from evaluation.review import load_review_ledger
import evaluation.workflow_cli as workflow
from tests.helpers import offline_temp_root, write_draft_dataset


class EvaluationWorkflowCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.dataset = self.directory / "feedback.csv"
        self.review = self.directory / "feedback.review.json"
        self.output = self.directory / "output"
        self.output.mkdir()
        write_draft_dataset(self.dataset)
        self.manifest_path = self.directory / "experiment.json"
        self.manifest = {
            "manifest_sha256": "a" * 64,
            "dataset": {"dataset_hash": dataset_hash(load_dataset(self.dataset))},
            "test_plan": {"versions": ["v1"], "max_request_attempts": 20},
        }
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.marker_directory = self.directory / "project_test_executions"
        marker_patch = patch.object(workflow, "DEFAULT_TEST_EXECUTION_DIR", self.marker_directory)
        marker_patch.start()
        self.addCleanup(marker_patch.stop)

    def offline_execution(self, reports=None):
        """Mock only model/report boundaries; exercise actual durable reservations.

        Full manifest/review/report validation is covered independently. These
        workflow fixtures make no human confirmations and never touch real
        request budgets or production test-exposure records.
        """
        stack = ExitStack()
        self.addCleanup(stack.close)
        completed = {"metadata": {
            "run_id": "b" * 32, "run_status": "completed",
            "request_attempts_actual": 20, "cumulative_request_attempts": 20,
            "journal_path": str(self.directory / "offline_fixture.journal.jsonl"),
        }}
        mocks = SimpleNamespace()
        mocks.validate = stack.enter_context(patch.object(workflow, "validate_experiment_manifest", return_value=copy.deepcopy(self.manifest)))
        mocks.control = stack.enter_context(patch.object(workflow, "RunControl", return_value=SimpleNamespace(stopped=False)))
        mocks.run = stack.enter_context(patch.object(workflow, "run_evaluation", side_effect=reports if reports is not None else [completed]))
        mocks.save = stack.enter_context(patch.object(workflow, "save_report", return_value={"report": self.output / "offline_fixture.report.json"}))
        mocks.summarize = stack.enter_context(patch.object(workflow, "summarize_test_reports", return_value={"fixture": "no real evaluation"}))
        mocks.summary = stack.enter_context(patch.object(workflow, "save_test_summary", side_effect=lambda summary, path: {"summary": path}))
        return mocks

    def run_final(self, path=None):
        return workflow.run_final_test(path or self.manifest_path, dataset_path=self.dataset, review_path=self.review, output_dir=self.output)

    def exposure(self):
        paths = list(self.marker_directory.glob("*.execution.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def test_review_init_saves_draft_checklist_without_changing_or_confirming_labels(self):
        original = self.dataset.read_bytes()
        with patch("sys.stdout", new_callable=io.StringIO) as output, patch.object(workflow, "run_evaluation") as run:
            result = workflow.main(["review-init", "--dataset", str(self.dataset), "--review", str(self.review), "--output-dir", str(self.output)])
        self.assertEqual(result, 0)
        run.assert_not_called()
        self.assertEqual(self.dataset.read_bytes(), original)
        ledger = load_review_ledger(self.dataset, self.review)
        self.assertEqual(len(ledger["records"]), 60)
        self.assertTrue(all(record["review_status"] == "draft" and not record["history"] for record in ledger["records"]))
        self.assertIn('"reviewed": 0', output.getvalue())
        checklist = (self.output / "human_review_checklist.csv").read_text(encoding="utf-8-sig")
        self.assertNotIn("predicted_", checklist)
        self.assertTrue((self.output / "human_review_history.json").is_file())

    def test_unconfirmed_dataset_and_experiment_freeze_refused_without_model_calls(self):
        with patch("sys.stdout", new_callable=io.StringIO), patch.object(workflow, "run_evaluation") as run:
            self.assertEqual(workflow.main(["review-init", "--dataset", str(self.dataset), "--output-dir", str(self.output)]), 0)
            self.assertEqual(workflow.main(["freeze-dataset", "--dataset", str(self.dataset), "--output-dir", str(self.output)]), 2)
            self.assertEqual(workflow.main(["freeze-experiment", "--dataset", str(self.dataset), "--reports", "never-read.json", "--output-dir", str(self.output)]), 2)
        run.assert_not_called()
        self.assertFalse((self.output / "experiment_manifest.json").exists())
        self.assertEqual(list(self.output.rglob("dataset_manifest.json")), [])
        self.assertTrue(all(sample.review_status == "draft" for sample in load_dataset(self.dataset)))

    def test_test_final_requires_explicit_live_and_manifest_arguments(self):
        with patch("sys.stderr", new_callable=io.StringIO), patch.object(workflow, "run_final_test") as final:
            for args in (["test-final", "--manifest", str(self.manifest_path)], ["test-final", "--live"]):
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    workflow.main(args)
                self.assertEqual(error.exception.code, 2)
        final.assert_not_called()
        self.assertFalse(self.marker_directory.exists())

    def test_cli_passes_explicit_live_final_plan_and_returns_incomplete_exit_code(self):
        with patch("sys.stdout", new_callable=io.StringIO), patch.object(workflow, "run_final_test", return_value={"status": "incomplete"}) as final:
            result = workflow.main(["test-final", "--live", "--manifest", str(self.manifest_path), "--dataset", str(self.dataset), "--review", str(self.review), "--output-dir", str(self.output)])
        self.assertEqual(result, 2)
        final.assert_called_once_with(self.manifest_path, dataset_path=self.dataset, review_path=self.review, output_dir=self.output)

    def test_invalid_manifest_is_rejected_before_exposure_reservation_or_requests(self):
        with patch.object(workflow, "validate_experiment_manifest", side_effect=ValueError("invalid frozen settings")), patch.object(workflow, "run_evaluation") as run:
            with self.assertRaises(ValueError):
                self.run_final()
        run.assert_not_called()
        self.assertFalse(self.marker_directory.exists())
        self.assertFalse(self.manifest_path.with_suffix(".execution.json").exists())

    def test_complete_single_pass_saves_two_evidence_copies_and_binds_runtime(self):
        mocks = self.offline_execution()
        evidence = self.run_final()
        self.assertEqual(evidence["status"], "completed")
        self.assertEqual(self.exposure(), evidence)
        self.assertEqual(json.loads(self.manifest_path.with_suffix(".execution.json").read_text(encoding="utf-8")), evidence)
        self.assertEqual(evidence["runs"][0]["request_attempts_actual"], 20)
        self.assertTrue(evidence["runs"][0]["files"])
        self.assertEqual(mocks.run.call_count, 1)
        settings = mocks.run.call_args.kwargs
        self.assertEqual(settings["mode"], "live")
        self.assertEqual(settings["split"], "test")
        self.assertEqual(settings["max_workers"], 2)
        self.assertEqual(settings["max_retries"], 0)
        self.assertEqual(settings["experiment_manifest"]["manifest_sha256"], self.manifest["manifest_sha256"])
        self.assertEqual(mocks.validate.call_args.kwargs["request_parameters"]["request_settings"]["max_retries"], 0)
        mocks.summarize.assert_called_once()
        self.assertIn(self.manifest["manifest_sha256"][:16], mocks.summary.call_args.args[1].name)

    def test_same_plan_and_new_manifest_cannot_repeat_exposed_test_feedbacks(self):
        mocks = self.offline_execution()
        first = self.run_final()
        with self.assertRaises(ValueError):
            self.run_final()
        other = self.directory / "other_experiment.json"
        other.write_text(json.dumps(self.manifest), encoding="utf-8")
        # Even a changed dataset/manifest hash cannot make the same feedback
        # texts an independent test set. No production labels are edited here.
        changed = copy.deepcopy(self.manifest)
        changed["manifest_sha256"] = "c" * 64
        changed["dataset"]["dataset_hash"] = "d" * 64
        mocks.validate.return_value = changed
        with self.assertRaises(ValueError):
            self.run_final(other)
        self.assertEqual(mocks.run.call_count, 1)
        self.assertEqual(self.exposure(), first)
        self.assertFalse(other.with_suffix(".execution.json").exists())

    def test_concurrent_different_manifests_reserve_only_one_actual_pass(self):
        mocks = self.offline_execution()
        other = self.directory / "parallel_experiment.json"
        other.write_text(json.dumps(self.manifest), encoding="utf-8")

        def attempt(path):
            try:
                return self.run_final(path)["status"]
            except ValueError:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, [self.manifest_path, other]))
        self.assertEqual(sorted(results), ["blocked", "completed"])
        self.assertEqual(mocks.run.call_count, 1)
        self.assertEqual(self.exposure()["status"], "completed")

    def test_incomplete_first_version_retains_results_and_never_submits_candidate(self):
        self.manifest["test_plan"] = {"versions": ["v1", "v2"], "max_request_attempts": 40}
        report = {"metadata": {"run_id": "e" * 32, "run_status": "incomplete", "request_attempts_actual": 3, "cumulative_request_attempts": 3, "journal_path": "offline_fixture.journal.jsonl"}}
        mocks = self.offline_execution([report])
        evidence = self.run_final()
        self.assertEqual(evidence["status"], "incomplete")
        self.assertEqual(len(evidence["runs"]), 1)
        self.assertEqual(evidence["runs"][0]["request_attempts_actual"], 3)
        self.assertEqual(self.exposure()["status"], "incomplete")
        mocks.summarize.assert_not_called()
        self.assertEqual(mocks.run.call_count, 1)
        with self.assertRaises(ValueError):
            self.run_final()

    def test_keyboard_interrupt_retains_consumed_pass_without_raw_error_body(self):
        mocks = self.offline_execution([KeyboardInterrupt("PRIVATE_RAW_ERROR")])
        with self.assertRaises(KeyboardInterrupt):
            self.run_final()
        evidence = self.exposure()
        self.assertEqual(evidence["status"], "incomplete")
        self.assertIn("completed_at", evidence)
        self.assertIn("单次", evidence["interpretation"])
        self.assertNotIn("PRIVATE_RAW_ERROR", json.dumps(evidence))
        mocks.summarize.assert_not_called()
        with self.assertRaises(ValueError):
            self.run_final()
        self.assertEqual(mocks.run.call_count, 1)

    def test_budget_initialization_failure_is_saved_as_incomplete_before_requests(self):
        mocks = self.offline_execution()
        mocks.control.side_effect = ValueError("offline budget initialization failure")
        with self.assertRaises(ValueError):
            self.run_final()
        self.assertEqual(self.exposure()["status"], "incomplete")
        self.assertEqual(self.exposure()["runs"], [])
        mocks.run.assert_not_called()
        self.assertEqual(json.loads(self.manifest_path.with_suffix(".execution.json").read_text(encoding="utf-8"))["status"], "incomplete")

    def test_export_failure_preserves_completed_run_and_its_journal_reference(self):
        mocks = self.offline_execution()
        mocks.save.side_effect = OSError("PRIVATE_EXPORT_ERROR")
        with self.assertRaises(OSError):
            self.run_final()
        evidence = self.exposure()
        self.assertEqual(evidence["status"], "incomplete")
        self.assertEqual(len(evidence["runs"]), 1)
        self.assertEqual(evidence["runs"][0]["run_status"], "completed")
        self.assertTrue(evidence["runs"][0]["journal_path"])
        self.assertEqual(evidence["runs"][0]["files"], {})
        self.assertNotIn("PRIVATE_EXPORT_ERROR", json.dumps(evidence))
        mocks.summarize.assert_not_called()

    def test_legacy_manifest_execution_blocks_and_is_never_overwritten(self):
        mocks = self.offline_execution()
        legacy = {"status": "completed", "legacy": "preserve original evidence"}
        path = self.manifest_path.with_suffix(".execution.json")
        path.write_text(json.dumps(legacy), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_final()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), legacy)
        self.assertEqual(self.exposure()["status"], "incomplete")
        mocks.run.assert_not_called()

    def test_failure_writing_manifest_copy_still_updates_project_exposure_evidence(self):
        mocks = self.offline_execution()
        original_write = workflow._write
        manifest_execution = self.manifest_path.with_suffix(".execution.json")

        def fail_manifest_copy(path, value):
            if path == manifest_execution:
                raise OSError("offline fixture write failure")
            return original_write(path, value)

        with patch.object(workflow, "_write", side_effect=fail_manifest_copy):
            with self.assertRaises(OSError):
                self.run_final()
        self.assertEqual(self.exposure()["status"], "incomplete")
        mocks.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
