"""Verify PostgreSQL tenant isolation with real Redis/Celery, fully offline.

Host execution uses only the standard library. The generated Compose project
has an internal network, fresh Linux volumes, no ports, and no deployment .env.
Only fixed synthetic feedback is sent to the tool-owned mock SDK adapter.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import subprocess
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="customerinsight-backend:phase3")
    parser.add_argument("--keep-services", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9./:_-]+", args.image):
        parser.error("Invalid acceptance image name")
    repository = Path(__file__).resolve().parents[1]
    run_id = uuid4().hex
    run = repository / ".runtime" / "phase3" / "acceptance" / run_id
    seed = run / "seed"
    seed.mkdir(parents=True)
    (seed / ".isolated-account-acceptance.json").write_text(json.dumps({
        "purpose": "isolated-account-acceptance", "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    (run / "empty.env").write_text("# Deliberately empty; never read deployment configuration.\n", encoding="utf-8")
    project = "customerinsight-account-acceptance-" + run_id[:12]
    password = secrets.token_urlsafe(30)
    environment = {
        "APP_DATABASE_URL": f"postgresql+psycopg://acceptance:{password}@postgres:5432/acceptance",
        "APP_DATA_DIR": "/acceptance/workspace", "APP_USAGE_DB_PATH": "/acceptance/usage.sqlite3",
        "APP_REDIS_URL": "redis://redis:6379/0", "APP_ANALYSIS_MODE": "queued",
        "APP_API_TOKEN": "", "APP_DAILY_REQUEST_LIMIT": "1000", "APP_GLOBAL_INFLIGHT": "2",
        "OPENAI_API_KEY": "account-acceptance-unused", "OPENAI_MODEL": "acceptance-offline-model",
        "OPENAI_BASE_URL": "http://blocked.invalid/v1", "OPENAI_TIMEOUT_SECONDS": "2",
        "API_MODE": "chat_completions", "CHAT_OUTPUT_MODE": "json_object",
        "APP_JOB_LEASE_SECONDS": "90", "APP_LLM_BATCH_SIZE": "10",
        "APP_LLM_BATCH_MAX_CHARACTERS": "12000", "PYTHONUNBUFFERED": "1",
    }
    volumes = ["acceptance_state:/acceptance", {"type": "bind", "source": str(repository / "tools"),
               "target": "/app/tools", "read_only": True}]
    base = {"image": args.image, "working_dir": "/app", "init": True,
            "environment": environment, "networks": ["isolated"], "volumes": volumes,
            "healthcheck": {"disable": True}, "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"]}
    services = {
        "postgres": {"image": "postgres:17-alpine", "networks": ["isolated"],
                     "environment": {"POSTGRES_USER": "acceptance", "POSTGRES_DB": "acceptance",
                                     "POSTGRES_PASSWORD": password},
                     "volumes": ["pg_state:/var/lib/postgresql/data"],
                     "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U acceptance -d acceptance"],
                                     "interval": "1s", "timeout": "2s", "retries": 40}},
        "redis": {"image": "redis:7-alpine", "networks": ["isolated"],
                  "command": ["redis-server", "--appendonly", "yes"],
                  "volumes": ["redis_state:/data"],
                  "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "1s",
                                  "timeout": "1s", "retries": 30}},
        "prepare": {"image": args.image, "user": "0:0", "networks": ["isolated"],
                    "entrypoint": ["python", "-c"],
                    "command": ["import os,pathlib; p=pathlib.Path('/acceptance'); "
                                "(p/'.isolated-account-acceptance.json').write_bytes("
                                "pathlib.Path('/seed/.isolated-account-acceptance.json').read_bytes()); "
                                "os.chown(p,10001,10001)"],
                    "volumes": ["acceptance_state:/acceptance", {"type": "bind", "source": str(seed),
                                "target": "/seed", "read_only": True}],
                    "healthcheck": {"disable": True}, "cap_drop": ["ALL"], "cap_add": ["CHOWN"],
                    "security_opt": ["no-new-privileges:true"]},
    }
    services["initialize"] = {**base,
        "command": ["python", "/app/tools/account_acceptance_worker.py", "initialize"],
        "depends_on": {"postgres": {"condition": "service_healthy"},
                       "prepare": {"condition": "service_completed_successfully"}}}
    dependencies = {"redis": {"condition": "service_healthy"},
                    "initialize": {"condition": "service_completed_successfully"}}
    services["worker"] = {**base, "command": ["python", "/app/tools/account_acceptance_worker.py", "worker"],
                          "depends_on": dependencies}
    services["runner"] = {**base, "command": ["python", "/app/tools/account_acceptance_worker.py", "controller"],
                          "depends_on": dependencies}
    configuration = {"services": services, "networks": {"isolated": {"internal": True}},
                     "volumes": {"pg_state": {}, "redis_state": {}, "acceptance_state": {}}}
    compose = run / "compose.json"
    compose.write_text(json.dumps(configuration, indent=2), encoding="utf-8")
    command = ["docker", "compose", "--env-file", str(run / "empty.env"), "-p", project, "-f", str(compose)]

    def execute(*parameters, timeout=240):
        result = subprocess.run([*command, *parameters], cwd=run, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace", timeout=timeout)
        with (run / "docker-acceptance.log").open("a", encoding="utf-8") as log:
            log.write(result.stdout + "\n")
        if result.returncode:
            raise RuntimeError("Isolated account acceptance command failed; inspect its dedicated log.")
        return result.stdout

    def controller(action):
        output = execute("run", "--rm", "--no-deps", "runner", "python", "/app/tools/account_acceptance_worker.py", action)
        lines = [line.split("ACCOUNT_ACCEPTANCE_JSON ", 1)[1] for line in output.splitlines()
                 if "ACCOUNT_ACCEPTANCE_JSON " in line]
        if not lines:
            raise RuntimeError("Acceptance did not produce aggregate evidence.")
        return json.loads(lines[-1])

    try:
        execute("up", "-d", "worker", timeout=240)
        controller("controller")
        execute("restart", "worker", timeout=60)
        state = controller("verify_replay")
        (run / "summary.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(json.dumps({**state, "evidence": str(run / "summary.json")}, ensure_ascii=False), flush=True)
    finally:
        try:
            logs = execute("logs", "--no-color", timeout=30)
            (run / "workers.log").write_text(logs, encoding="utf-8")
        finally:
            if not args.keep_services:
                if not project.startswith("customerinsight-account-acceptance-") or compose.parent != run:
                    raise RuntimeError("Refusing cleanup outside this acceptance project")
                execute("down", "--volumes", "--remove-orphans", timeout=60)


if __name__ == "__main__":
    main()
