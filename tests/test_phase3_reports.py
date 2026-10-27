import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import Config
from evaluation.control import PersistentRequestBudget, RunControl
from evaluation.reports import load_report, save_report, validate_report
from evaluation.runner import compare_reports, run_evaluation
from tests.helpers import fake_client
from tests.test_evaluation_runner import samples


class ControlledReportTests(unittest.TestCase):
    def test_aborted_evidence_roundtrip_excludes_unexecuted_from_failure_metrics(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            control = RunControl(1, budget=PersistentRequestBudget(Path(folder) / "budget.json"))
            with patch.object(Config, "API_MODE", "responses"):
                report = run_evaluation(samples(), mode="live", client=fake_client(),
                                        max_workers=1, control=control, journal_dir=folder)
            self.assertEqual(report["metadata"]["run_status"], "incomplete")
            self.assertEqual(report["metadata"]["request_attempts_actual"], 1)
            self.assertEqual(report["metrics"]["sample_count"], 1)
            self.assertEqual(report["metrics"]["planned_sample_count"], 3)
            self.assertEqual(report["metrics"]["not_executed_count"], 2)
            self.assertEqual(report["metrics"]["failed_count"], 0)
            self.assertFalse(report["metrics"]["assessment_complete"])
            paths = save_report(report, folder)
            self.assertEqual(load_report(paths["report"]), report)
            self.assertIn("尚未执行", paths["markdown"].read_text(encoding="utf-8"))
            journal = [json.loads(line) for line in Path(report["metadata"]["journal_path"]).read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row["event"] == "prediction" for row in journal))
            with self.assertRaises(ValueError):
                compare_reports([report, report])
            for mutate in (
                lambda value: value["metadata"].update(run_status="completed"),
                lambda value: value["metadata"].update(request_attempts_actual=0),
                lambda value: value["predictions"][1].update(status="failed"),
                lambda value: value["predictions"][0].update(token_usage=[{"secret": "no"}]),
            ):
                changed = copy.deepcopy(report)
                mutate(changed)
                with self.assertRaises(ValueError):
                    validate_report(changed)
            completed = run_evaluation(samples())
            completed["metadata"]["stop_reason"] = "authentication_error"
            with self.assertRaises(ValueError):
                validate_report(completed)

    def test_original_phase2_reports_still_load(self):
        fixture = Path(__file__).parent / "fixtures" / "phase2_schema1.report.json"
        self.assertEqual(load_report(fixture)["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
