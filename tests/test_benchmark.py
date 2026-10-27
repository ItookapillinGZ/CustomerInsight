"""Check the public statistical convention against independently known values."""

import unittest

from tools.benchmark_metrics import latency_summary, percentile


class BenchmarkStatisticsTests(unittest.TestCase):
    def test_type_seven_quantiles_are_order_independent(self):
        self.assertEqual(percentile([10, 0, 30, 20], .5), 15)
        self.assertAlmostEqual(percentile([10, 0, 30, 20], .95), 28.5)
        self.assertEqual(percentile([9], .95), 9)
        self.assertEqual(percentile([1, 2], 0), 1)
        self.assertEqual(percentile([1, 2], 1), 2)

    def test_rejects_misleading_or_undefined_statistics(self):
        for values, quantile in (([], .5), ([float("nan")], .5), ([-1], .5),
                                 ([1], 1.1), ([1], float("inf"))):
            with self.subTest(values=values, quantile=quantile), self.assertRaises(ValueError):
                percentile(values, quantile)

    def test_units_and_missing_sample_convention(self):
        self.assertEqual(latency_summary([.1, .2, .3]), {
            "samples": 3, "p50_ms": 200.0, "p95_ms": 290.0, "max_ms": 300.0})
        self.assertIsNone(latency_summary([])["p95_ms"])


if __name__ == "__main__":
    unittest.main()
