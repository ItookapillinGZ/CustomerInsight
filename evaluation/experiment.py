"""Offline, development-only selection and a frozen plan for a single test pass.

This module never calls an LLM, edits gold labels, or registers a new prompt.
Its final workflow requires explicit review provenance in addition to
the CSV's review_status. Partial LIVE runs are deliberately rejected.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai.prompts import get_prompt, prompt_hash
from evaluation.data import DEFAULT_DATASET, DATASET_FIELDS, dataset_hash, load_dataset, select_split, validate_samples
from evaluation.metrics import LABELS
from evaluation.reports import validate_report
from evaluation.runner import compare_reports


# This rule is fixed before looking at any test predictions. Latency is
# inspected and reported; there is no invented maximum acceptable duration.
SELECTION_RULE = {
    "primary_metric": "category.macro_f1",
    "eligibility": "strictly_better_than_v1",
    "guards": ["structured_success_rate", "sentiment.macro_f1", "priority.macro_f1"],
    "guard_policy": "none_may_be_lower_than_v1",
    "tie_policy": "retain_v1_when_primary_ties_v1",
    "candidate_tiebreak": [
        "structured_success_rate_desc", "sentiment.macro_f1_desc",
        "priority.macro_f1_desc", "latency.average_ms_asc", "prompt_version_asc",
    ],
    "latency_policy": "report_average_and_p95; no_posthoc_threshold",
    "selection_split": "dev",
}
LIMITATION = "60 条合成示例，固定 dev 40 / test 20；单次结果不代表业务准确率、统计显著性或普遍性能提升。"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def _read_json(value: dict | str | Path) -> dict:
    if isinstance(value, dict):
        return json.loads(_canonical(value))
    try:
        result = json.loads(Path(value).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        raise ValueError("无法读取有效的实验清单 JSON。") from None
    if not isinstance(result, dict):
        raise ValueError("实验清单必须为 JSON 对象。")
    return result


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _complete_dataset(samples) -> list:
    checked = validate_samples(samples)
    expected = {f"dev-{index:03d}" for index in range(1, 41)} | {f"test-{index:03d}" for index in range(1, 21)}
    if len(checked) != 60 or {item.sample_id for item in checked} != expected:
        raise ValueError("最终实验必须使用原固定 60 条样本及 dev 40 / test 20 划分。")
    if any(item.split != item.sample_id.split("-", 1)[0] for item in checked):
        raise ValueError("固定开发集与测试集划分已变化，不能继续本次最终实验。")
    if any(item.review_status not in {"reviewed", "ai_reviewed"} for item in checked):
        raise ValueError("全部 60 条标签尚未完成复核；只能保存临时结果，不能进行最终选型或冻结。")
    return checked


def _review_provenance(samples) -> dict:
    counts = Counter(item.review_status for item in samples)
    if not counts["ai_reviewed"]:
        return {}
    return {
        "review_status_counts": {name: counts[name] for name in ("draft", "reviewed", "ai_reviewed")},
        "review_provenance": "mixed_human_ai" if counts["reviewed"] else "ai",
        "review_note": f"参考标签含 {counts['reviewed']} 条人工确认和 {counts['ai_reviewed']} 条 AI 复核；不是全人工金标，也不是业务数据。",
    }


def _complete_live_report(report: dict, samples, split: str) -> dict:
    checked = validate_report(report)
    meta = checked["metadata"]
    if checked["schema_version"] != 2 or meta.get("run_status") != "completed":
        raise ValueError("最终实验只接受请求控制生效且完整执行的 LIVE 报告。")
    if meta["mode"] != "live" or meta["split"] != split or not meta["all_labels_reviewed"]:
        raise ValueError("最终实验只接受相应 split 的已复核 LIVE 报告；DEMO 与临时结果不能用于选型。")
    selected = select_split(samples, split)
    by_id = {item.sample_id: item for item in selected}
    if (
        meta["dataset_hash"] != dataset_hash(samples)
        or meta["selected_samples_hash"] != dataset_hash(selected)
        or set(meta["sample_ids"]) != set(by_id)
        or meta["sample_count"] != len(selected)
        or len(checked["predictions"]) != len(selected)
        or any(row["status"] == "not_executed" for row in checked["predictions"])
    ):
        raise ValueError("报告为部分样本、未执行完整数据集，或不属于当前已复核数据 hash。")
    for row in checked["predictions"]:
        if {key: row[key] for key in DATASET_FIELDS} != by_id[row["sample_id"]].model_dump(mode="json"):
            raise ValueError("报告参考标签与当前已复核样本不一致。")
    if meta["max_workers"] != 2 or meta["application_max_retries"] != 0 or meta["sdk_max_retries"] != 0:
        raise ValueError("本阶段最终实验须保持并发 2、应用与 SDK 重试 0。")
    if meta["request_attempts_actual"] != len(selected) or any(row["request_attempts"] != 1 for row in checked["predictions"]):
        raise ValueError("最终实验每条样本须恰好执行一次实际请求，尝试计数必须完整可核验。")
    if meta["prompt_hash"] != prompt_hash(meta["prompt_version"]):
        raise ValueError("报告 Prompt hash 与当前注册版本不一致，不能据此冻结。")
    return checked


def _score(report: dict) -> dict:
    metrics = report["metrics"]
    return {
        "run_id": report["metadata"]["run_id"],
        "prompt_version": report["metadata"]["prompt_version"],
        "prompt_hash": report["metadata"]["prompt_hash"],
        "structured_success_rate": metrics["structured_success_rate"],
        "failed_count": metrics["failed_count"],
        "failures_by_status": metrics["failures_by_status"],
        "average_latency_ms": metrics["latency"]["average_ms"],
        "p95_latency_ms": metrics["latency"]["p95_ms"],
        **{f"{field}_{metric}": metrics[field][metric] for field in LABELS for metric in ("accuracy", "macro_f1")},
        "support": {field: {label: score["support"] for label, score in metrics[field]["per_class"].items()} for field in LABELS},
    }


def _errors(row: dict) -> list[dict]:
    if row["status"] == "validation_error":
        return [{"type": "structure_failure", "evidence": row["error_message"]}]
    if row["status"] == "failed":
        failure = row.get("failure_type") or "unknown"
        return [{"type": f"{failure}_failure", "evidence": row["error_message"]}]
    if row["status"] != "success":
        return []
    result = []
    ranks = {"low": 0, "medium": 1, "high": 2}
    for field in LABELS:
        gold, predicted = row[f"gold_{field}"], row[f"predicted_{field}"]
        if gold != predicted:
            kind = {"sentiment": "sentiment_error", "category": "category_confusion"}.get(field)
            if field == "priority":
                kind = "priority_too_high" if ranks[predicted] > ranks[gold] else "priority_too_low"
            result.append({"type": kind, "field": field, "gold": gold, "prediction": predicted})
    return result


def _prediction_evidence(row: dict) -> dict:
    return {
        "status": row["status"],
        **{field: row[f"predicted_{field}"] for field in LABELS},
        "latency_ms": row["latency_ms"], "errors": _errors(row),
        "failure_type": row.get("failure_type"),
        "error_message": row["error_message"],
    }


def _sample_differences(reports: list[dict]) -> tuple[list[dict], dict]:
    by_version = {report["metadata"]["prompt_version"]: {row["sample_id"]: row for row in report["predictions"]} for report in reports}
    baseline = by_version["v1"]
    differences = []
    count_keys = ["improved", "degraded", "mixed", "unchanged", *[f"{field}_{change}" for field in LABELS for change in ("improved", "degraded", "unchanged")]]
    counts = {version: Counter({key: 0 for key in count_keys}) for version in by_version if version != "v1"}
    for sample_id in sorted(baseline):
        base = baseline[sample_id]
        entry = {
            "sample_id": sample_id, "text": base["text"],
            "gold": {field: base[f"gold_{field}"] for field in LABELS},
            "versions": {version: _prediction_evidence(rows[sample_id]) for version, rows in by_version.items()},
            "changes_from_v1": {},
        }
        for version, rows in by_version.items():
            if version == "v1":
                continue
            candidate = rows[sample_id]
            changes = {}
            for field in LABELS:
                was_correct = base["status"] == "success" and base[f"predicted_{field}"] == base[f"gold_{field}"]
                is_correct = candidate["status"] == "success" and candidate[f"predicted_{field}"] == candidate[f"gold_{field}"]
                change = "improved" if is_correct and not was_correct else "degraded" if was_correct and not is_correct else "unchanged"
                changes[field] = change
                counts[version][f"{field}_{change}"] += 1
            observed = set(changes.values())
            outcome = "mixed" if {"improved", "degraded"} <= observed else "improved" if "improved" in observed else "degraded" if "degraded" in observed else "unchanged"
            changes["outcome"] = outcome
            changes["structured_success"] = (
                "improved" if base["status"] != "success" and candidate["status"] == "success" else
                "degraded" if base["status"] == "success" and candidate["status"] != "success" else "unchanged"
            )
            counts[version][outcome] += 1
            entry["changes_from_v1"][version] = changes
        differences.append(entry)
    return differences, {version: dict(counter) for version, counter in counts.items()}


def _error_analysis(differences: list[dict], *, development: bool = True) -> dict:
    observed = []
    patterns = Counter()
    type_counts = Counter()
    for sample in differences:
        sample_types = []
        for version, evidence in sample["versions"].items():
            for error in evidence["errors"]:
                sample_types.append(error["type"])
                type_counts[(version, error["type"])] += 1
                if error["type"] in {"category_confusion", "sentiment_error", "priority_too_high", "priority_too_low"}:
                    patterns[(version, error["type"], error.get("gold"), error.get("prediction"))] += 1
        if sample_types:
            observed_types = set(sample_types)
            if observed_types <= {"category_confusion", "sentiment_error", "priority_too_high", "priority_too_low"}:
                possible_reason = "推测：相关标注边界或影响判定可能未被正确应用；这里只观察到上述 gold 与预测差异，不能确认模型根因。"
            elif "structure_failure" in observed_types:
                possible_reason = "完整输出模型校验失败；现有安全失败记录不足以确认是漏字段、非法值还是其他输出问题。"
            else:
                possible_reason = "请求失败类型与安全错误信息见上述证据；不能凭一次失败确认网络、网关或上游服务的具体根因。"
            observed.append({
                **sample, "observed_error_types": sorted(set(sample_types)),
                "possible_reason": possible_reason,
                "reason_status": "hypothesis_only; no_verified_root_cause",
            })
    # Cover distinct observed types first, then use stable sample_id ordering.
    representatives, covered = [], set()
    for sample in observed:
        types = set(sample["observed_error_types"])
        if types - covered and len(representatives) < 10:
            representatives.append(sample)
            covered |= types
    selected_ids = {sample["sample_id"] for sample in representatives}
    for sample in observed:
        if len(representatives) == 10:
            break
        if sample["sample_id"] not in selected_ids:
            representatives.append(sample)
            selected_ids.add(sample["sample_id"])
    repeated = [{"prompt_version": version, "type": kind, "gold": gold, "prediction": predicted, "count": count} for (version, kind, gold, predicted), count in sorted(patterns.items()) if count >= 2]
    return {
        "error_counts": [{"prompt_version": version, "type": kind, "count": count} for (version, kind), count in sorted(type_counts.items())],
        "repeated_development_patterns": repeated if development else [],
        "representative_cases": representatives,
        "v3_decision": (
            "测试结果不能用于新增或修改 Prompt；未来优化须另建独立测试集。" if not development else
            "需依据重复开发集错误与标注规则进一步判断；本工具不自动修改或新增 Prompt。" if repeated else
            "未观察到重复的具体标签混淆，不强行新增 v3。"
        ),
    }


def compare_development_reports(reports: list[dict], samples) -> dict:
    """Select only from full reviewed LIVE dev; test data never affects choice."""
    all_samples = _complete_dataset(samples)
    checked = [_complete_live_report(report, all_samples, "dev") for report in reports]
    versions = [report["metadata"]["prompt_version"] for report in checked]
    if len(set(versions)) != len(versions) or not {"v1", "v2"} <= set(versions):
        raise ValueError("开发集选型至少需要完整且不重复的 v1 与 v2 报告。")
    compatibility = compare_reports(checked)
    scores = [_score(report) for report in checked]
    base = next(score for score in scores if score["prompt_version"] == "v1")
    checks, eligible = [], []
    for score in scores:
        better = score["category_macro_f1"] > base["category_macro_f1"]
        guards = {metric: score[metric] >= base[metric] for metric in ("structured_success_rate", "sentiment_macro_f1", "priority_macro_f1")}
        entry = {"prompt_version": score["prompt_version"], "category_strictly_better": better, "guards": guards, "eligible": better and all(guards.values())}
        checks.append(entry)
        if entry["eligible"]:
            eligible.append(score)
    if eligible:
        selected = sorted(eligible, key=lambda score: (
            -score["category_macro_f1"], -score["structured_success_rate"],
            -score["sentiment_macro_f1"], -score["priority_macro_f1"],
            score["average_latency_ms"], score["prompt_version"],
        ))[0]
        reason = "类别 Macro-F1 严格高于 v1，且结构成功率、情感与优先级 Macro-F1 未下降；耗时另列供核查。"
    else:
        selected, reason = base, "没有满足预先选型规则的严格改善；保留 v1，类别 Macro-F1 同分也保留 v1。"
    differences, change_counts = _sample_differences(checked)
    result = {
        "schema_version": 1, "kind": "development_comparison", "mode": "live", "split": "dev",
        "dataset_hash": compatibility["dataset_hash"], "sample_ids": sorted(checked[0]["metadata"]["sample_ids"]),
        "selection_rule": json.loads(_canonical(SELECTION_RULE)), "candidate_version": selected["prompt_version"],
        "selection_reason": reason, "selection_checks": checks, "scores": scores,
        "sample_differences": differences, "change_counts": change_counts,
        "error_analysis": _error_analysis(differences), "limitation": LIMITATION,
        **_review_provenance(all_samples),
    }
    result["comparison_sha256"] = _digest(result)
    return result


def comparison_markdown(comparison: dict) -> str:
    """Render exact observed metrics, support and at most ten actual cases."""
    def cell(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    lines = ["# LIVE 实验比较", "", comparison["limitation"], "", f"split：{comparison['split']}；dataset hash：`{comparison['dataset_hash']}`。", ""]
    if comparison.get("review_note"):
        lines.extend([comparison["review_note"], ""])
    if "candidate_version" in comparison:
        lines.extend([f"候选版本：**{comparison['candidate_version']}**。{comparison['selection_reason']}", "", "预先规则：类别 Macro-F1 为主；结构成功率、情感及优先级 Macro-F1 不低于 v1；主指标同分保留 v1。耗时展示，未设置事后阈值。", ""])
    if "frozen_candidate_version" in comparison:
        lines.extend([f"开发集已冻结候选：**{comparison['frozen_candidate_version']}**。", "", f"实验清单 hash：`{comparison['manifest_sha256']}`；本次测试不重新选型。", ""])
    lines.extend(["| Prompt | 类别 Macro-F1 | 情感 Macro-F1 | 优先级 Macro-F1 | 结构成功率 | 失败 | 平均 ms | P95 ms |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for score in comparison["scores"]:
        lines.append(f"| {score['prompt_version']} | {score['category_macro_f1']:.6f} | {score['sentiment_macro_f1']:.6f} | {score['priority_macro_f1']:.6f} | {score['structured_success_rate']:.6f} | {score['failed_count']} | {score['average_latency_ms']:.3f} | {score['p95_latency_ms']:.3f} |")
    for score in comparison["scores"]:
        lines.extend(["", f"## {score['prompt_version']} 固定标签 support", "", "| 字段 | 标签 | support |", "| --- | --- | ---: |"])
        for field, labels in score["support"].items():
            lines.extend(f"| {field} | {label} | {support} |" for label, support in labels.items())
    lines.extend(["", "## 逐样本改善与退化", "", "失败计错误；仅比较同一参考标签下是否正确。", "", "| ID | 版本 | 情感 | 类别 | 优先级 | 综合 |", "| --- | --- | --- | --- | --- | --- |"])
    for sample in comparison["sample_differences"]:
        for version, changes in sample["changes_from_v1"].items():
            lines.append("| " + " | ".join(cell(value) for value in (sample["sample_id"], version, changes["sentiment"], changes["category"], changes["priority"], changes["outcome"])) + " |")
    cases = comparison["error_analysis"]["representative_cases"]
    lines.extend(["", "## 最多 10 条代表案例", "", "按实际错误类型覆盖和固定 ID 顺序选择；可能原因均为未验证假设，不修改 gold。", ""])
    if not cases:
        lines.append("本次未观察到错误；不编造案例或原因。")
    for sample in cases:
        lines.extend([f"### {cell(sample['sample_id'])}", "", cell(sample["text"]), "", f"参考标签：{cell(sample['gold'])}", ""])
        for version, evidence in sample["versions"].items():
            lines.append(f"- {cell(version)}：{cell(evidence)}")
        lines.extend(["", sample["possible_reason"], ""])
    if comparison["split"] == "test":
        lines.extend(["", "测试集只用于这次冻结后的最终评估；不得依据结果继续调整 Prompt，后续优化须另建独立测试集。"])
    return "\n".join(lines) + "\n"


def save_development_comparison(comparison: dict, output_path: str | Path) -> dict[str, Path]:
    checked = {key: value for key, value in comparison.items() if key != "comparison_sha256"}
    if comparison.get("comparison_sha256") != _digest(checked):
        raise ValueError("开发集比较内容已变化，请重新从完整报告生成。")
    target = Path(output_path)
    markdown = target.with_suffix(".md")
    _write_json(target, comparison)
    markdown.write_text(comparison_markdown(comparison), encoding="utf-8")
    return {"comparison": target, "markdown": markdown}


def _validate_snapshot(snapshot: dict, samples) -> dict:
    required = {"dataset_hash", "snapshot_sha256", "dataset_path", "review_path", "review_sha256", "annotation_guide_sha256", "reviewed_count", "split_counts", "sample_ids", "frozen_at_utc"}
    if not isinstance(snapshot, dict) or not required <= set(snapshot):
        raise ValueError("缺少已确认人工复核的冻结快照追溯信息。")
    if snapshot["dataset_hash"] != dataset_hash(samples) or snapshot["reviewed_count"] != 60 or snapshot["split_counts"] != {"dev": 40, "test": 20}:
        raise ValueError("人工复核快照与当前完整数据集不一致。")
    if snapshot["sample_ids"] != [sample.sample_id for sample in load_dataset(snapshot["dataset_path"])]:
        raise ValueError("人工复核快照样本清单与冻结 CSV 不一致。")
    try:
        frozen = datetime.fromisoformat(snapshot["frozen_at_utc"])
        if frozen.tzinfo is None or frozen.utcoffset().total_seconds() != 0:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ValueError("人工复核快照缺少有效 UTC 冻结时间。") from None
    from evaluation.review import validate_review_evidence
    frozen_samples = load_dataset(snapshot["dataset_path"])
    if dataset_hash(frozen_samples) != dataset_hash(samples):
        raise ValueError("冻结 CSV 与当前数据集 hash 不一致。")
    counts = Counter(item.review_status for item in frozen_samples)
    if counts["ai_reviewed"]:
        expected_counts = {name: counts[name] for name in ("draft", "reviewed", "ai_reviewed")}
        if (snapshot.get("review_status_counts") != expected_counts
                or snapshot.get("human_reviewed_count") != counts["reviewed"]
                or snapshot.get("ai_reviewed_count") != counts["ai_reviewed"]
                or snapshot.get("review_provenance") != ("mixed_human_ai" if counts["reviewed"] else "ai")):
            raise ValueError("冻结快照必须准确记录人工与 AI 复核来源。")
    try:
        if sha256(Path(snapshot["dataset_path"]).read_bytes()).hexdigest() != snapshot["snapshot_sha256"] or sha256(Path(snapshot["review_path"]).read_bytes()).hexdigest() != snapshot["review_sha256"]:
            raise ValueError
        guide = Path(__file__).parent / "ANNOTATION_GUIDE.md"
        frozen_guide = Path(snapshot["dataset_path"]).parent / "ANNOTATION_GUIDE.md"
        if sha256(guide.read_bytes()).hexdigest() != snapshot["annotation_guide_sha256"] or sha256(frozen_guide.read_bytes()).hexdigest() != snapshot["annotation_guide_sha256"]:
            raise ValueError
    except (OSError, TypeError, ValueError):
        raise ValueError("冻结数据、复核证据或标注指南文件 hash 已变化。") from None
    validate_review_evidence(dataset_path=snapshot["dataset_path"], review_path=snapshot["review_path"], require_all_reviewed=True)
    return snapshot


def freeze_experiment_manifest(
    reports: list[dict], *, dataset_path: str | Path = DEFAULT_DATASET,
    review_path: str | Path | None = None, review_snapshot: dict | None = None,
    output_path: str | Path,
) -> dict:
    """Freeze from reviewed dev only; save this before any final test request."""
    samples = _complete_dataset(load_dataset(dataset_path))
    from evaluation.review import freeze_reviewed_dataset, validate_review_evidence
    validate_review_evidence(dataset_path=dataset_path, review_path=review_path, require_all_reviewed=True)
    comparison = compare_development_reports(reports, samples)
    checked = [_complete_live_report(report, samples, "dev") for report in reports]
    meta = checked[0]["metadata"]
    target = Path(output_path)
    if target.exists():
        raise ValueError("实验清单已存在；冻结后不可覆盖，请读取原清单或为独立实验使用新路径。")
    snapshot = review_snapshot or freeze_reviewed_dataset(dataset_path=dataset_path, review_path=review_path, output_dir=target.parent / "review_snapshot")
    _validate_snapshot(snapshot, samples)
    candidate = comparison["candidate_version"]
    versions = ["v1"] if candidate == "v1" else ["v1", candidate]
    manifest = {
        "schema_version": 1, "kind": "frozen_final_test_manifest", "experiment_id": uuid4().hex,
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": snapshot,
        "prompts": {
            "baseline": {"version": "v1", "hash": prompt_hash("v1")},
            "candidate": {"version": candidate, "hash": prompt_hash(candidate)},
        },
        "request_settings": meta["request_settings"],
        "runtime": {key: meta[key] for key in ("model", "provider", "api_mode", "configured_model", "configured_api_mode", "max_workers", "application_max_retries", "sdk_max_retries")},
        "selection_rule": json.loads(_canonical(SELECTION_RULE)),
        "selection_reason": comparison["selection_reason"],
        "development_comparison_sha256": comparison["comparison_sha256"],
        "development_runs": [{"run_id": report["metadata"]["run_id"], "prompt_version": report["metadata"]["prompt_version"], "prompt_hash": report["metadata"]["prompt_hash"], "completed_at": report["metadata"]["completed_at"], "report_sha256": _digest(report)} for report in checked],
        "test_plan": {"split": "test", "sample_ids": sorted(item.sample_id for item in select_split(samples, "test")), "versions": versions, "max_request_attempts": 20 * len(versions), "single_pass_per_version": True},
        "test_policy": "测试集仅作冻结后的最终评估；看到测试结果后不得据此修改 Prompt。未来优化需另建独立测试集。",
        "limitation": LIMITATION,
    }
    manifest["manifest_sha256"] = _digest(manifest)
    # Exclusive creation also prevents two processes from replacing a plan
    # between the earlier existence check and this write.
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False))
    except FileExistsError:
        raise ValueError("实验清单已存在，不能覆盖冻结记录。") from None
    return manifest


def validate_experiment_manifest(
    manifest: dict | str | Path, *, dataset_path: str | Path = DEFAULT_DATASET,
    review_path: str | Path | None = None, request_parameters: dict | None = None,
) -> dict:
    """Verify frozen files, human confirmations, prompt hashes and settings."""
    checked = _read_json(manifest)
    try:
        if checked["schema_version"] != 1 or checked["kind"] != "frozen_final_test_manifest" or checked["manifest_sha256"] != _digest({key: value for key, value in checked.items() if key != "manifest_sha256"}):
            raise ValueError
        frozen = datetime.fromisoformat(checked["frozen_at_utc"])
        if frozen.tzinfo is None or frozen.utcoffset().total_seconds() != 0:
            raise ValueError
        if checked["selection_rule"] != SELECTION_RULE:
            raise ValueError
        if checked["prompts"]["baseline"] != {"version": "v1", "hash": prompt_hash("v1")}:
            raise ValueError
        candidate = checked["prompts"]["candidate"]
        get_prompt(candidate["version"])
        if candidate["hash"] != prompt_hash(candidate["version"]):
            raise ValueError
        versions = ["v1"] if candidate["version"] == "v1" else ["v1", candidate["version"]]
        expected_plan = {"split": "test", "sample_ids": [f"test-{index:03d}" for index in range(1, 21)], "versions": versions, "max_request_attempts": 20 * len(versions), "single_pass_per_version": True}
        if checked["test_plan"] != expected_plan:
            raise ValueError
        runtime = checked["runtime"]
        if runtime["max_workers"] != 2 or runtime["application_max_retries"] != 0 or runtime["sdk_max_retries"] != 0:
            raise ValueError
        settings = checked["request_settings"]
        from evaluation.reports import _validate_request_settings
        _validate_request_settings(settings)
        if settings["max_retries"] != 0 or any(runtime[key] != settings[key] for key in ("model", "provider", "api_mode")):
            raise ValueError
        if request_parameters is not None:
            if request_parameters.get("request_settings") != settings or any(request_parameters.get(key) != value for key, value in runtime.items()):
                raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ValueError("实验清单的冻结 hash、Prompt、选型规则、测试计划或请求设置不一致。") from None
    samples = _complete_dataset(load_dataset(dataset_path))
    from evaluation.review import validate_review_evidence
    validate_review_evidence(dataset_path=dataset_path, review_path=review_path, require_all_reviewed=True)
    _validate_snapshot(checked["dataset"], samples)
    return checked


def validate_test_report(
    report: dict, manifest: dict | str | Path, *, dataset_path: str | Path = DEFAULT_DATASET,
    review_path: str | Path | None = None,
) -> dict:
    """Bind one complete test report to the pre-test frozen experiment."""
    frozen = validate_experiment_manifest(manifest, dataset_path=dataset_path, review_path=review_path)
    checked = _complete_live_report(report, load_dataset(dataset_path), "test")
    meta = checked["metadata"]
    versions = frozen["test_plan"]["versions"]
    if meta["prompt_version"] not in versions:
        raise ValueError("测试报告不属于已冻结的基线/候选版本。")
    if meta["request_settings"] != frozen["request_settings"] or any(meta[key] != value for key, value in frozen["runtime"].items()):
        raise ValueError("测试报告的模型、接口或运行参数与冻结清单不一致。")
    if datetime.fromisoformat(meta["started_at"]) < datetime.fromisoformat(frozen["frozen_at_utc"]):
        raise ValueError("测试运行早于实验清单冻结时间，不能作为最终测试评估。")
    expected = frozen["prompts"]["baseline"] if meta["prompt_version"] == "v1" else frozen["prompts"]["candidate"]
    if meta["prompt_hash"] != expected["hash"]:
        raise ValueError("测试报告 Prompt hash 与冻结清单不一致。")
    return checked


def summarize_test_reports(
    reports: list[dict], manifest: dict | str | Path, *, dataset_path: str | Path = DEFAULT_DATASET,
    review_path: str | Path | None = None,
) -> dict:
    """Describe a single frozen test pass; this cannot select a new candidate."""
    frozen = validate_experiment_manifest(manifest, dataset_path=dataset_path, review_path=review_path)
    checked = [validate_test_report(report, frozen, dataset_path=dataset_path, review_path=review_path) for report in reports]
    versions = [report["metadata"]["prompt_version"] for report in checked]
    if len(versions) != len(set(versions)) or set(versions) != set(frozen["test_plan"]["versions"]):
        raise ValueError("最终测试必须恰好包含清单中的版本各一次；候选为 v1 时只运行一次 v1。")
    if len(checked) > 1:
        compare_reports(checked)
    differences, counts = _sample_differences(checked)
    return {
        "schema_version": 1, "kind": "frozen_test_summary", "mode": "live", "split": "test",
        "manifest_sha256": frozen["manifest_sha256"], "dataset_hash": frozen["dataset"]["dataset_hash"],
        "frozen_candidate_version": frozen["prompts"]["candidate"]["version"],
        "scores": [_score(report) for report in checked], "sample_differences": differences,
        "change_counts": counts, "error_analysis": _error_analysis(differences, development=False),
        "selection_performed": False, "test_policy": frozen["test_policy"],
        "limitation": ("20 条含 AI 复核参考标签的合成示例上的单次结果；不是全人工金标，不声称统计显著、业务准确率或普遍性能提升。"
                       if any(item.review_status == "ai_reviewed" for item in load_dataset(dataset_path) if item.split == "test")
                       else "20 条已人工复核的合成示例上的单次最终结果；不声称统计显著、业务准确率或普遍性能提升。"),
        **_review_provenance([item for item in load_dataset(dataset_path) if item.split == "test"]),
    }


def save_test_summary(summary: dict, output_path: str | Path) -> dict[str, Path]:
    if summary.get("kind") != "frozen_test_summary" or summary.get("selection_performed") is not False:
        raise ValueError("只能导出冻结测试结果，不能使用测试集重新选型。")
    target = Path(output_path)
    markdown = target.with_suffix(".md")
    _write_json(target, summary)
    markdown.write_text(comparison_markdown(summary), encoding="utf-8")
    return {"summary": target, "markdown": markdown}
