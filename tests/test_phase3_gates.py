import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.cli import main
from evaluation.data import load_dataset
from evaluation.runner import run_evaluation
from evaluation.ui import load_saved_report, saved_report_files
from evaluation.reports import save_report


class PhaseThreeGateTests(unittest.TestCase):
    def test_full_draft_dev_and_unfrozen_test_reject_before_client_creation(self):
        with patch("evaluation.runner.get_openai_client") as factory:
            with self.assertRaisesRegex(ValueError, "60"):
                run_evaluation(load_dataset(), mode="live")
            with self.assertRaisesRegex(ValueError, "冻结"):
                run_evaluation(load_dataset(), split="test", mode="live")
        factory.assert_not_called()

    def test_cli_cannot_use_test_for_a_small_probe(self):
        with patch("evaluation.cli.run_evaluation") as evaluate, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["run", "--split", "test", "--limit", "1", "--live"]), 2)
        evaluate.assert_not_called()

    def test_nested_smoke_reports_load_without_path_escape(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            report = run_evaluation(load_dataset(), limit=5)
            paths = save_report(report, root / "phase3" / "smoke")
            self.assertEqual(len(saved_report_files(root)), 1)
            name = paths["report"].relative_to(root).as_posix()
            self.assertEqual(load_saved_report(name, root), report)
            with self.assertRaises(ValueError):
                load_saved_report("../" + name, root)
            with self.assertRaises(ValueError):
                load_saved_report(str(paths["report"].resolve()), root)


if __name__ == "__main__":
    unittest.main()
