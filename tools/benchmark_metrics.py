"""Small, dependency-free statistics shared by the isolated load runner."""

from __future__ import annotations

import math


def percentile(samples, quantile):
    """Linear interpolation at (n - 1) * quantile (Hyndman/Fan type 7)."""
    values = sorted(float(value) for value in samples)
    if not values or not all(math.isfinite(value) and value >= 0 for value in values):
        raise ValueError("Expected finite, non-negative latency samples")
    if not math.isfinite(quantile) or not 0 <= quantile <= 1:
        raise ValueError("Quantile must be between zero and one")
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def latency_summary(samples):
    values = list(samples)
    if not values:
        return {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {"samples": len(values), "p50_ms": round(percentile(values, .5) * 1000, 3),
            "p95_ms": round(percentile(values, .95) * 1000, 3),
            "max_ms": round(max(values) * 1000, 3)}
