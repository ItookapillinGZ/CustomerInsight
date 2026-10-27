"""Public JSON projections; storage envelopes and source-frame attributes stay private."""

from datetime import date, datetime, time, timedelta
from enum import Enum
import math

import numpy as np
import pandas as pd


ANALYSIS_FIELDS = (
    "mode", "prompt_version", "planned_requests", "batch_size", "max_characters",
    "unique_pending_rows", "run_status", "actual_requests", "elapsed_seconds",
    "total_tokens", "usage_reported_requests", "model", "data_origin", "label_origin", "demo_version",
)
REPORT_FIELDS = (
    "metrics", "sentiment_counts", "category_counts", "priority_counts",
    "problem_ranking", "grouped", "detail_columns", "notes", "text_column",
    "group_column", "period", "topics", "topics_version", "taxonomy_version", "top_topics",
    "topic_coverage", "insights", "executive_summary", "data_origin", "provenance",
)
METADATA_FIELDS = (
    "id", "filename", "created_at", "updated_at", "total_rows", "success_rows",
    "text_column", "group_column", "mapping", "content_hash", "report_fingerprint",
)


def public_json(value, depth=0):
    """Normalize supported table values without serializing arbitrary objects."""
    if depth > 32:
        raise ValueError("JSON 数据嵌套过深。")
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, Enum):
        return public_json(value.value, depth + 1)
    if isinstance(value, pd.DataFrame):
        return public_json(value.to_dict(orient="records"), depth + 1)
    if isinstance(value, np.generic):
        return public_json(value.item(), depth + 1)
    if isinstance(value, bool) or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(key): public_json(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_json(item, depth + 1) for item in value]
    raise ValueError("该数据类型不能作为公开 JSON 返回。")


def analysis_metadata(value):
    value = value if isinstance(value, dict) else {}
    return public_json({key: value[key] for key in ANALYSIS_FIELDS if key in value})


def batch_metadata(record):
    result = {key: record[key] for key in METADATA_FIELDS if key in record}
    result["analysis_meta"] = analysis_metadata(record.get("analysis_meta"))
    return public_json(result)


def report_projection(report):
    result = {key: report[key] for key in REPORT_FIELDS if key in report}
    result["analysis_meta"] = analysis_metadata(report.get("analysis_meta"))
    return public_json(result)
