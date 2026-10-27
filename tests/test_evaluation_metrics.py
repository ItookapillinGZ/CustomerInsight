import unittest

from evaluation.metrics import FAILURE_LABEL, LABELS, compute_metrics


def prediction(gold, predicted, *, status="success", latency=10.0):
    return {
        "gold_sentiment": gold, "predicted_sentiment": predicted,
        "gold_category": "bugs", "predicted_category": "bugs" if status == "success" else None,
        "gold_priority": "high", "predicted_priority": "high" if status == "success" else None,
        "status": status, "latency_ms": latency,
    }


class EvaluationMetricTests(unittest.TestCase):
    def test_hand_calculated_metrics_include_failure_in_denominator(self):
        rows = [
            prediction("positive", "positive", latency=10),
            prediction("positive", "negative", latency=20),
            prediction("negative", None, status="failed", latency=30),
            prediction("neutral", "neutral", latency=40),
        ]
        metrics = compute_metrics(rows, mode="live")
        sentiment = metrics["sentiment"]
        self.assertEqual(sentiment["accuracy"], 0.5)
        self.assertAlmostEqual(sentiment["macro_f1"], 5 / 9)
        self.assertEqual(sentiment["per_class"]["positive"]["precision"], 1)
        self.assertEqual(sentiment["per_class"]["positive"]["recall"], 0.5)
        self.assertAlmostEqual(sentiment["per_class"]["positive"]["f1"], 2 / 3)
        self.assertEqual([item["support"] for item in sentiment["per_class"].values()], [2, 1, 1])
        matrix = sentiment["confusion_matrix"]
        self.assertEqual(matrix["prediction_labels"][-1], FAILURE_LABEL)
        self.assertEqual(matrix["values"], [[1, 1, 0, 0], [0, 0, 0, 1], [0, 0, 1, 0]])
        self.assertEqual(metrics["structured_success_rate"], 0.75)
        self.assertEqual(metrics["failed_count"], 1)
        self.assertEqual(metrics["latency"]["average_ms"], 25)
        self.assertAlmostEqual(metrics["latency"]["p95_ms"], 38.5)
        self.assertTrue(metrics["latency"]["includes_application_retries_and_backoff"])
        self.assertFalse(metrics["latency"]["includes_thread_queue_wait"])

    def test_missing_classes_still_contribute_zero_to_macro_f1(self):
        metrics = compute_metrics([prediction("positive", "positive")], mode="demo")
        self.assertAlmostEqual(metrics["sentiment"]["macro_f1"], 1 / 3)
        self.assertAlmostEqual(metrics["category"]["macro_f1"], 1 / 9)
        self.assertAlmostEqual(metrics["priority"]["macro_f1"], 1 / 3)
        for field, labels in LABELS.items():
            self.assertEqual(list(metrics[field]["per_class"]), labels)
            self.assertEqual(sum(item["support"] for item in metrics[field]["per_class"].values()), 1)
        self.assertEqual(metrics["category"]["per_class"]["other"]["support"], 0)
        self.assertEqual(metrics["category"]["per_class"]["other"]["f1"], 0)
        self.assertTrue(metrics["latency"]["is_mock"])

    def test_all_failures_have_no_class_prediction_and_zero_scores(self):
        rows = [prediction("neutral", None, status="failed"), prediction("negative", None, status="validation_error")]
        metrics = compute_metrics(rows, mode="live")
        self.assertEqual(metrics["structured_success_rate"], 0)
        self.assertEqual(metrics["failures_by_status"], {"failed": 1, "validation_error": 1})
        for field in LABELS:
            self.assertEqual(metrics[field]["accuracy"], 0)
            self.assertEqual(metrics[field]["macro_f1"], 0)
            matrix = metrics[field]["confusion_matrix"]["values"]
            self.assertEqual(sum(row[-1] for row in matrix), 2)
            self.assertEqual(sum(sum(row[:-1]) for row in matrix), 0)

    def test_failed_predictions_cannot_masquerade_as_neutral(self):
        with self.assertRaises(ValueError):
            compute_metrics([prediction("neutral", "neutral", status="failed")], mode="demo")
        for latency in (float("nan"), float("inf"), -1, True):
            with self.subTest(latency=latency), self.assertRaises(ValueError):
                compute_metrics([prediction("neutral", "neutral", latency=latency)], mode="demo")


if __name__ == "__main__":
    unittest.main()
