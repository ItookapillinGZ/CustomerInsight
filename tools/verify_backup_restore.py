"""Real PG17 backup/recovery rehearsal using only generated synthetic data.

Creates two independent internal Docker networks, fresh named volumes, no
published ports and no worker. It never reads the deployment .env or contacts
an LLM. Only the aggregate summary is suitable for CI artifact upload.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import secrets
import sys
from textwrap import indent

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backup import BackupError
from tools.backup_workspace import backup_workspace
from tools.restore_workspace import restore_workspace
from tools.workspace_backup_common import Docker, compose_prefix, private_json


SEED = r'''
import json, os
from pathlib import Path
from urllib.parse import urlsplit
assert os.environ['OPENAI_BASE_URL'] == 'https://blocked.invalid'
assert not os.environ.get('OPENAI_API_KEY')
assert not Path('/app/.env').exists()
assert os.environ['APP_DATA_DIR'] == '/data/customerinsight'
assert urlsplit(os.environ['APP_DATABASE_URL']).hostname == 'postgres'
from alembic import command
from alembic.config import Config
command.upgrade(Config('/app/alembic.ini'), 'head')
from backend.database import get_engine
from backend.accounts import AccountService
from backend.pg_history import PgHistoryStore
from backend.pg_jobs import PgJobStore
from core.product_service import ProductRequestBudget
from core.customer_report import build_customer_report
import pandas as pd
engine = get_engine()
accounts = AccountService(engine)
user = accounts.create_user('backup_fixture', 'synthetic-only-password-2026')
accounts.login('backup_fixture', 'synthetic-only-password-2026', client_ip='synthetic')
source = pd.DataFrame({'review': ['Synthetic positive review', 'Synthetic delivery concern']})
results = pd.DataFrame([
    {'source_row': 2, 'processing_status': 'success', 'sentiment': 'positive',
     'category': 'product_quality', 'priority': 'low', 'summary': 'Synthetic quality'},
    {'source_row': 3, 'processing_status': 'success', 'sentiment': 'negative',
     'category': 'delivery', 'priority': 'medium', 'summary': 'Synthetic delivery'},
])
report = build_customer_report(source, results, 'review')
report['source_frame'] = source
report['analysis_meta'] = {'model': 'offline-fixture', 'requests': 0}
original = source.to_csv(index=False).encode()
history = PgHistoryStore(engine, workspace_id=user['workspace_id'])
mapping = {'text_column': 'review', 'sheet_name': None, 'encoding': 'utf-8-sig',
           'group_column': None, 'date_column': None}
identifier = history.save_batch(report, filename='synthetic.csv', file_bytes=original, mapping=mapping)
history.append_chat(identifier, {'question': 'Synthetic follow-up', 'answer': 'Synthetic offline answer'})
rows = [{'feedback_id': 'fixture-' + str(index), 'source_row': index + 2,
         'original_text': text, 'processing_status': 'pending'} for index, text in enumerate(source['review'])]
jobs = PgJobStore(engine, workspace_id=user['workspace_id'])
job, created = jobs.create_job(original=original, filename='synthetic.csv', request=mapping,
    config={'model': 'offline-fixture'}, groups=[[item] for item in rows], initial_results=rows,
    max_requests=2, idempotency_key='synthetic-backup-fixture', scope=user['workspace_id'])
first = jobs.claim_group(job['id'], 'synthetic-no-transport', lease_seconds=30)
jobs.mark_sent(job['id'], first['id'], first['lease_token'])
completed = dict(first['rows'][0], processing_status='success', sentiment='positive',
                 category='product_quality', priority='low', summary='Synthetic fixture')
jobs.checkpoint_group(job['id'], first['id'], first['lease_token'], results=[completed],
                      usage={'total_tokens': 0})
second = jobs.claim_group(job['id'], 'synthetic-no-transport', lease_seconds=30)
jobs.mark_sent(job['id'], second['id'], second['lease_token'])
jobs.record_failure(job['id'], second['id'], second['lease_token'], uncertain=True)
jobs.set_report(job['id'], identifier)
assert jobs.get_job(job['id'])['status'] == 'needs_review'
budget = ProductRequestBudget('/data/product_usage.sqlite3', daily_limit=10)
budget.reserve('synthetic-reservation-not-an-api-call')
assert budget.remaining() == 9
root = Path('/data/customerinsight')
(root / 'bootstrap').mkdir(exist_ok=True)
(root / 'bootstrap' / 'owner.json').write_text(json.dumps({'username': 'backup_fixture',
                                                       'password': 'synthetic-only-password-2026'}))
print(json.dumps({'reports': 1, 'chat_turns': 1, 'jobs': 1, 'unknown_groups': 1,
                  'result_rows': 2, 'sessions': 1, 'reserved_synthetic_quota': 1,
                  'model_requests': 0}))
'''


def _guarded_seed(source):
    """Return only a safe exception class when the isolated fixture fails.

    Docker.run already recognises a final stderr JSON object with an ``error``
    identifier. Ordinary Python tracebacks are intentionally suppressed there,
    so an unguarded fixture used to hide every useful failure classification.
    Never include the exception message: database errors may contain a URL.
    """
    return ("import json, sys\ntry:\n" + indent(source, "    ")
            + "\nexcept Exception as exc:\n"
            + "    print(json.dumps({'error': type(exc).__name__}), file=sys.stderr)\n"
            + "    raise SystemExit(1) from None\n")


def verify_backup_restore(*, image="customerinsight-backend:phase3"):
    if not isinstance(image, str) or not re.fullmatch(r"[a-z0-9./:_-]+", image):
        raise BackupError("验收镜像名称无效。")
    run_id = secrets.token_hex(8)
    project = "customerinsight-backup-check-" + run_id
    restore_project = "customerinsight-restore-check-" + run_id
    run = ROOT / ".runtime" / "phase4" / "backup-restore" / run_id
    run.mkdir(parents=True)
    run.chmod(0o700)
    empty = run / "empty.env"
    empty.write_text("# Isolated synthetic acceptance; deployment .env is never read.\n", encoding="utf-8")
    password = secrets.token_hex(24)
    configuration = {
        "services": {
            "postgres": {
                "image": "postgres:17-alpine",
                "environment": {"POSTGRES_USER": "fixture", "POSTGRES_DB": "fixture", "POSTGRES_PASSWORD": password},
                "volumes": ["postgres_data:/var/lib/postgresql/data"],
                "healthcheck": {"test": ["CMD", "pg_isready", "--host", "127.0.0.1", "-U", "fixture", "-d", "fixture"],
                                "interval": "1s", "timeout": "2s", "retries": 50},
            },
            "api": {
                "image": image, "init": True,
                "command": ["python", "-c", "import time; time.sleep(7200)"],
                "healthcheck": {"disable": True}, "volumes": ["usage_data:/data"],
                "environment": {"APP_DATABASE_URL": f"postgresql+psycopg://fixture:{password}@postgres:5432/fixture",
                                "APP_DATA_DIR": "/data/customerinsight", "APP_USAGE_DB_PATH": "/data/product_usage.sqlite3",
                                "OPENAI_API_KEY": "", "OPENAI_BASE_URL": "https://blocked.invalid"},
                "depends_on": {"postgres": {"condition": "service_healthy"}},
            },
        },
        "volumes": {"usage_data": {}, "postgres_data": {}},
        "networks": {"default": {"internal": True}},
    }
    compose = run / "compose.json"
    private_json(compose, configuration)
    docker = Docker()
    prefix = compose_prefix([compose], project, empty)
    summary = None
    phase = "source_start"
    primary_error = None
    try:
        docker.run(*prefix, "up", "--detach", "--wait", "--wait-timeout", "90", timeout=180)
        phase = "source_container_lookup"
        identifiers = docker.ids(*prefix, "ps", "--quiet", "api")
        if len(identifiers) != 1:
            raise BackupError("独立验收 API 占位容器不存在。")
        phase = "synthetic_seed"
        seeded = json.loads(docker.run("exec", "-i", identifiers[0], "python", "-",
                                      input=_guarded_seed(SEED).encode()).decode().splitlines()[-1])
        phase = "backup"
        backup = backup_workspace(output=run / "synthetic-workspace.zip", compose_files=[compose],
                                  project=project, env_file=empty, docker=docker)
        phase = "isolated_restore"
        restored = restore_workspace(run / "synthetic-workspace.zip", project=restore_project,
                                     app_image=image, cleanup=True, evidence=run / "restore-verification.json", docker=docker)
        phase = "verify_restored_evidence"
        proof = json.loads((run / "restore-verification.json").read_text("utf-8"))
        if (proof["table_counts"].get("history_batches") != 1
                or proof["table_counts"].get("analysis_jobs") != 1
                or proof["table_counts"].get("analysis_results") != 2
                or proof["revoked_sessions"] != 1
                or proof["job_statuses"].get("analysis_jobs") != {"needs_review": 1}
                or proof["job_statuses"].get("analysis_groups") != {"succeeded": 1, "uncertain": 1}):
            raise BackupError("隔离恢复验收未满足预设数据/断点要求。")
        summary = {
            "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(), "verified": True,
            "source_project": project, "restore_project": restore_project,
            "fixture": seeded, "tables": backup["postgres_tables"], "files": backup["files"],
            "hashes_and_counts_equal": True, "sessions_revoked": 1, "uncertain_state_preserved": True,
            "source_writers_restarted": backup["writers_restored"],
            "restored_resources_removed": restored["cleaned_up"],
            "published_ports": [], "model_requests": 0,
        }
    except BackupError as exc:
        primary_error = BackupError("合成备份验收阶段 " + phase + " 未完成：" + str(exc))
        raise primary_error from exc
    except Exception as exc:
        primary_error = BackupError("合成备份验收阶段 " + phase + " 未完成；错误类型：" + type(exc).__name__)
        raise primary_error from exc
    finally:
        # A UUID-only project created here, with an explicit generated Compose
        # file, is the only source deployment this fixture can ever remove.
        if not re.fullmatch(r"customerinsight-backup-check-[0-9a-f]{16}", project) or compose.parent != run:
            raise BackupError("拒绝清理非验收项目。")
        try:
            docker.run(*prefix, "down", "--volumes", "--remove-orphans", timeout=90)
        except BackupError as exc:
            cleanup_error = "合成备份验收阶段 source_cleanup 未完成：" + str(exc)
            if primary_error is not None:
                # Preserve the original failure if cleanup also fails. These
                # two messages contain only fixed stages and safe Docker errors.
                print(cleanup_error, file=sys.stderr)
            else:
                raise BackupError(cleanup_error) from exc
    summary["source_resources_removed"] = True
    private_json(run / "summary.json", summary)
    return {**summary, "evidence": str(run / "summary.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="customerinsight-backend:phase3")
    args = parser.parse_args()
    try:
        print(json.dumps(verify_backup_restore(**vars(args)), ensure_ascii=False))
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print("合成备份验收未完成，错误类型：" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
