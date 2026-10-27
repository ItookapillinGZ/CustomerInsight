"""Exercise API-only report operations on a fresh isolated PostgreSQL server.

Runs only HTTP/unit test classes, never Streamlit AppTest, a worker or a model.
The tests directory is read-only and disposable files use a dedicated volume.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backup import BackupError
from tools.workspace_backup_common import Docker, compose_prefix, private_json


CONTROLLER = r'''
import io, json, os, pathlib, unittest
assert 'APP_DATABASE_URL' not in os.environ
assert not pathlib.Path('/app/.env').exists()
assert os.environ['TEST_PG_URL'].split('@', 1)[1] == 'postgres:5432/phase4_test'
assert os.environ['OPENAI_BASE_URL'] == 'https://blocked.invalid'
assert not os.environ['OPENAI_API_KEY']
names = ['tests.test_remote_ui.RemoteWorkspaceApiTests',
         'tests.test_operations.ScopedOperationsTests']
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in names)
stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
pathlib.Path('/app/.test-tmp/remote-pg-tests.log').write_text(stream.getvalue())
print('REMOTE_PG_SUMMARY ' + json.dumps({
    'verified': result.wasSuccessful(), 'tests_run': result.testsRun,
    'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
    'failed_test_ids': [case.id() for case, _ in result.failures + result.errors],
    'classes': names, 'remote_api_database': 'postgresql-17',
    'operations_database': 'isolated-sqlite', 'model_requests': 0,
    'app_test_run': False, 'deployment_configuration_read': False,
}))
'''


def verify_remote_api_postgres(*, image="customerinsight-backend:phase4"):
    if not isinstance(image, str) or not re.fullmatch(r"[a-z0-9./:_-]+", image):
        raise BackupError("验收镜像名称无效。")
    run_id = secrets.token_hex(8)
    project = "customerinsight-api-pg-check-" + run_id
    run = ROOT / ".runtime" / "phase4" / "remote-api-pg" / run_id
    run.mkdir(parents=True)
    run.chmod(0o700)
    empty = run / "empty.env"
    empty.write_text("# No production environment is loaded.\n", encoding="utf-8")
    password = secrets.token_hex(24)
    compose = run / "compose.json"
    private_json(compose, {
        "services": {
            "postgres": {
                "image": "postgres:17-alpine",
                "environment": {"POSTGRES_USER": "phase4_test", "POSTGRES_DB": "phase4_test",
                                "POSTGRES_PASSWORD": password},
                "volumes": ["pg_state:/var/lib/postgresql/data"],
                "healthcheck": {"test": ["CMD", "pg_isready", "--host", "127.0.0.1",
                                          "-U", "phase4_test", "-d", "phase4_test"],
                                "interval": "1s", "timeout": "2s", "retries": 50},
            },
            "runner": {
                "image": image, "user": "0:0", "init": True, "working_dir": "/app",
                "entrypoint": ["python", "-c"], "command": [CONTROLLER],
                "healthcheck": {"disable": True},
                "volumes": ["test_tmp:/app/.test-tmp", {"type": "bind", "source": str(ROOT / "tests"),
                                                       "target": "/app/tests", "read_only": True}],
                "environment": {
                    "TEST_PG_URL": f"postgresql+psycopg://phase4_test:{password}@postgres:5432/phase4_test",
                    "OPENAI_API_KEY": "", "OPENAI_BASE_URL": "https://blocked.invalid",
                    "APP_PUBLIC_MODE": "false", "PYTHONDONTWRITEBYTECODE": "1",
                },
                "depends_on": {"postgres": {"condition": "service_healthy"}},
                "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"],
            },
        },
        "volumes": {"pg_state": {}, "test_tmp": {}},
        "networks": {"default": {"internal": True}},
    })
    docker = Docker()
    prefix = compose_prefix([compose], project, empty)
    result = None
    try:
        docker.run(*prefix, "up", "--detach", "--wait", "--wait-timeout", "90", "postgres", timeout=180)
        # Controller returns test outcomes as aggregate JSON; assertion failures
        # remain a failed host acceptance rather than leaking synthetic bodies.
        output = docker.run(*prefix, "run", "--rm", "--no-deps", "-T", "runner", timeout=240).decode("utf-8")
        lines = [line.split("REMOTE_PG_SUMMARY ", 1)[1] for line in output.splitlines() if "REMOTE_PG_SUMMARY " in line]
        if not lines:
            raise BackupError("独立 PostgreSQL API 测试未返回摘要。")
        result = json.loads(lines[-1])
    finally:
        if not re.fullmatch(r"customerinsight-api-pg-check-[0-9a-f]{16}", project) or compose.parent != run:
            raise BackupError("拒绝清理非验收项目。")
        docker.run(*prefix, "down", "--volumes", "--remove-orphans", timeout=90)
    result.update({"project": project, "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "internal_network": True, "published_ports": [], "resources_removed": True})
    private_json(run / "summary.json", result)
    if not result["verified"]:
        print(json.dumps({**result, "evidence": str(run / "summary.json")}, ensure_ascii=False))
        raise BackupError("独立 PostgreSQL API 专项测试未全部通过。")
    return {**result, "evidence": str(run / "summary.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="customerinsight-backend:phase4")
    args = parser.parse_args()
    try:
        print(json.dumps(verify_remote_api_postgres(**vars(args)), ensure_ascii=False))
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print("独立 PostgreSQL API 验收未完成，错误类型：" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
