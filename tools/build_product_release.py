"""Build the self-hosted app from an explicit allowlist, without local secrets."""
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist/CustomerInsight_self_hosted_demo_20261025.zip"


def main():
    entries = {}

    def add(path, name=None):
        source = ROOT / path
        entries[name or path] = source.read_bytes()

    for path in (
        "app.py", "LICENSE", "SELF_HOSTING.md", "PRODUCT_DELIVERY.md", "BATCH_UI_DELIVERY.md", ".env.example",
        "SUMMARY_AGENT_DELIVERY.md", "AGENT_WORKFLOW_PLAN.md", "AGENT_V1_DELIVERY.md", "AGENT_LIVE_ACCEPTANCE.md",
        "MULTITURN_DELIVERY.md", "REASONING_AGENT_DELIVERY.md",
        "WORKSPACE_DELIVERY.md",
        "compose.production.yml", "compose.api.yml", "compose.jobs.yml", "compose.accounts.yml",
        "alembic.ini", "migrations/script.py.mako",
        "docs/BACKEND_STACK.md", "docs/DEMO_UPGRADE.md", "docs/DEMO_BENCHMARK_RESEARCH.md",
        "docs/API_PHASE1.md", "docs/API_PHASE1_DELIVERY.md", "docs/API_PHASE2.md", "docs/API_PHASE2_DELIVERY.md",
        "docs/API_PHASE3.md", "docs/API_PHASE3_DELIVERY.md", "docs/API_PHASE4.md", "docs/API_PHASE4_DELIVERY.md",
        "docs/BENCHMARK_METHOD.md", "docs/BACKEND_UPGRADE_PLAN.md", "docs/BATCH_COMPARISON_FIX.md",
        "validation/agent_v1_verification.json",
        "validation/multiturn_all_tests.log", "validation/multiturn_chat_preview.jpg",
        "validation/multiturn_package_tests.log", "validation/multiturn_verification.json",
        "validation/reasoning_agent_all_tests.log", "validation/reasoning_agent_package_tests.log",
        "validation/reasoning_agent_verification.json", "validation/reasoning_agent_preview.jpg",
        "validation/reasoning_agent_live_preview.jpg",
        "validation/summary_report_tests.log",
        "pyproject.toml", "uv.lock", "requirements.txt", "Dockerfile", ".dockerignore",
        "compose.yml", "compose.public.yml", "deploy/Caddyfile", "start.ps1", "start-local.cmd",
        ".streamlit/config.toml", "tools/prepare_public_reviews.py", "tools/test_public_reviews_live.py", "tools/test_grouped_reviews.py",
        "tools/test_agent_live_acceptance.py", "tools/test_reasoning_agent.py",
        "tools/prepare_account_deployment.py", "tools/backup_workspace.py", "tools/restore_workspace.py",
        "tools/workspace_backup_common.py", "tools/benchmark_backend.py", "tools/benchmark_worker.py",
        "tools/benchmark_metrics.py", "tools/verify_backup_restore.py", "tools/verify_account_queue.py",
        "tools/account_acceptance_worker.py",
        "tools/build_product_release.py", "README_SELF_HOSTED.md",
        "tests/__init__.py", "tests/test_customer_app.py", "tests/test_customer_report.py",
        "tests/test_excel_io.py", "tests/test_product_service.py", "tests/test_public_reviews_data.py", "tests/test_grouped_batch.py",
        "tests/test_analysis_agent.py", "tests/test_agent_table_tools.py",
        "tests/helpers.py", "tests/test_backend_api.py", "tests/test_demo_workspace.py", "tests/test_demo_api.py",
        "tests/test_demo_remote_ui.py", "tests/test_demo_experience.py", "tests/test_report_interactions.py",
        "tests/test_topic_normalization.py", "tests/test_agent_charts.py", "tests/test_agent_brief.py",
        "tests/test_history_store.py", "tests/test_history_workflow.py", "tests/test_history_app.py",
        "tests/test_history_agent.py", "tests/test_review_topics.py", "tools/test_workspace_acceptance.py",
        "tools/test_workspace_correction.py",
        "validation/docker_v2_verification.json", "validation/docker_v2_product_tests.log",
        "validation/product_v2_all_tests.log", "validation/product_v2_package_tests.log", "validation/product_v2_verification.json",
        "validation/ui_v2_report_desktop.jpg", "validation/ui_v2_export_charts.jpg",
    ):
        add(path)
    add("README_SELF_HOSTED.md", "README.md")
    for folder in ("ai", "core", "models", "backend", "migrations"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            add(path.relative_to(ROOT).as_posix())
    for folder, extensions in (
        ("examples", {".csv", ".xlsx"}),
        ("datasets/public_reviews", {".csv", ".xlsx", ".json", ".md"}),
        ("core/demo_data", {".json"}),
        ("datasets/demo_workspace", {".md"}),
        ("datasets/public_reviews/raw", {".zip", ".txt"}),
        ("validation/public_reviews_pilot", {".json", ".jsonl", ".csv", ".html"}),
        ("validation/grouped_public_reviews_pilot", {".json", ".jsonl", ".csv", ".html"}),
        ("validation/summary_update", {".json", ".html"}),
        ("validation/agent_live_acceptance", {".json"}),
        ("validation/reasoning_agent_acceptance", {".json", ".md"}),
        ("validation/workspace_acceptance", {".json", ".md"}),
    ):
        if not (ROOT / folder).exists():
            continue
        for path in sorted((ROOT / folder).iterdir()):
            if path.is_file() and path.suffix in extensions:
                add(path.relative_to(ROOT).as_posix())
    assert "validation/public_reviews_pilot/summary.json" in entries
    assert "validation/agent_live_acceptance/summary.json" in entries
    for path in ("validation/workspace_all_tests.log", "validation/workspace_package_tests.log",
                 "validation/workspace_final_core_tests.log", "validation/workspace_docker_verification.json",
                 "validation/workspace_verification.json", "validation/workspace_preview.jpg",
                 "validation/workspace_live_preview.jpg",
                 "validation/demo_upgrade_verification.json", "validation/demo_browser_check.json",
                 "validation/demo_deployed_check.json", "validation/demo_remote_ui_discover_final.log",
                 "validation/demo_remote_ui_sequence_final.log", "validation/demo_interactions_final.log",
                 "validation/demo_upgrade_all_tests_final.log"):
        if (ROOT / path).is_file():
            add(path)
    forbidden = {".env", "secrets.toml", "product_usage.sqlite3"}
    assert not any(Path(name).name in forbidden or Path(name).suffix in {".sqlite3", ".db"}
                   or "reviews/backups" in name or "originals/" in name for name in entries)
    local_values = dotenv_values(ROOT / ".env")
    secret_keys = {"OPENAI_API_KEY", "APP_ACCESS_PASSWORD", "APP_API_TOKEN",
                   "APP_POSTGRES_PASSWORD", "APP_DATABASE_URL"}
    secrets = [str(value).encode("utf-8") for key, value in local_values.items()
               if value and key in secret_keys]
    if any(secret in content for secret in secrets for content in entries.values()):
        raise RuntimeError("Release contains a local credential; stopped without printing it.")
    manifest = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(entries.items())}
    entries["RELEASE_MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    OUTPUT.parent.mkdir(exist_ok=True)
    with ZipFile(OUTPUT, "w", ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            archive.writestr(name, content)
    with ZipFile(OUTPUT) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(entries)
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    OUTPUT.with_suffix(".zip.sha256").write_text(f"{digest}  {OUTPUT.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(OUTPUT), "files": len(entries), "bytes": OUTPUT.stat().st_size,
                      "sha256": digest, "local_credentials_included": False}))


if __name__ == "__main__":
    main()
