"""Run real Redis/Celery acceptance in a disposable, internal Docker network.

Only standard-library modules run on the host. No application configuration,
private workbook, history directory or model credential is read or mounted.
Evidence remains below .runtime/phase2/acceptance/<random run identifier>.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="customerinsight-backend:phase2")
    parser.add_argument("--keep-services", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9./:_-]+", args.image):
        parser.error("Invalid acceptance image name")
    repository = Path(__file__).resolve().parents[1]
    run_id = uuid4().hex
    run = repository / ".runtime" / "phase2" / "acceptance" / run_id
    data = run / "data"
    data.mkdir(parents=True)
    (data / ".isolated-queue-acceptance.json").write_text(json.dumps({
        "purpose": "isolated-queue-acceptance", "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    (run / "empty.env").write_text("# Deliberately empty; never load the deployment .env.\n", encoding="utf-8")
    project = "customerinsight-queue-acceptance-" + run_id[:12]
    environment = {
        "APP_DATA_DIR": "/acceptance/workspace", "APP_USAGE_DB_PATH": "/acceptance/usage.sqlite3",
        "APP_REDIS_URL": "redis://redis:6379/0", "APP_ANALYSIS_MODE": "queued",
        "APP_DAILY_REQUEST_LIMIT": "1000", "APP_GLOBAL_INFLIGHT": "2",
        "OPENAI_API_KEY": "queue-acceptance-unused", "OPENAI_MODEL": "acceptance-offline-model",
        "OPENAI_BASE_URL": "http://blocked.invalid/v1", "OPENAI_TIMEOUT_SECONDS": "2",
        "API_MODE": "chat_completions", "CHAT_OUTPUT_MODE": "json_object",
        "APP_JOB_LEASE_SECONDS": "90", "APP_LLM_BATCH_SIZE": "10",
        "APP_LLM_BATCH_MAX_CHARACTERS": "12000", "PYTHONUNBUFFERED": "1",
    }
    base = {"image": args.image, "working_dir": "/app", "init": True,
            "environment": environment, "networks": ["isolated"],
            "volumes": ["acceptance_state:/acceptance",
                        {"type": "bind", "source": str(repository / "tools"), "target": "/app/tools", "read_only": True}],
            "healthcheck": {"disable": True}, "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "depends_on": {"redis": {"condition": "service_healthy"},
                           "prepare": {"condition": "service_completed_successfully"}}}
    services = {"redis": {"image": "redis:7-alpine", "networks": ["isolated"],
                           "command": ["redis-server", "--appendonly", "yes"],
                           "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "1s", "timeout": "1s", "retries": 20},
                           "volumes": ["redis_state:/data"]}}
    # Keep SQLite WAL on a Linux filesystem, as in production. A Windows bind
    # mount can provide incompatible locking semantics and is not a database
    # volume. Only the synthetic marker is read from the host seed directory.
    services["prepare"] = {
        "image": args.image, "user": "0:0", "networks": ["isolated"],
        "entrypoint": ["python", "-c"],
        "command": ["import os,pathlib; p=pathlib.Path('/acceptance'); "
                    "(p/'.isolated-queue-acceptance.json').write_bytes("
                    "pathlib.Path('/seed/.isolated-queue-acceptance.json').read_bytes()); "
                    "os.chown(p,10001,10001)"],
        "volumes": ["acceptance_state:/acceptance", {"type": "bind", "source": str(data),
                     "target": "/seed", "read_only": True}],
        "healthcheck": {"disable": True}, "cap_drop": ["ALL"], "cap_add": ["CHOWN"],
        "security_opt": ["no-new-privileges:true"],
    }
    for name in ("worker_a", "worker_b"):
        services[name] = {**base, "environment": {**environment, "ACCEPTANCE_WORKER_NAME": name},
                          "command": ["python", "/app/tools/job_acceptance_worker.py", "worker"]}
    services["runner"] = {**base, "command": ["python", "/app/tools/job_acceptance_worker.py", "controller"]}
    configuration = {"services": services, "networks": {"isolated": {"internal": True}},
                     "volumes": {"redis_state": {}, "acceptance_state": {}}}
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
            raise RuntimeError("Isolated Docker acceptance command failed; see its dedicated log.")
        return result.stdout

    def controller(action):
        output = execute("run", "--rm", "--no-deps", "runner", "python", "/app/tools/job_acceptance_worker.py", action)
        lines = [line.split("ACCEPTANCE_JSON ", 1)[1] for line in output.splitlines() if "ACCEPTANCE_JSON " in line]
        if not lines:
            raise RuntimeError("Acceptance controller did not produce aggregate evidence.")
        return json.loads(lines[-1])

    try:
        execute("up", "-d", "redis", "worker_a", "worker_b", timeout=180)
        state = controller("controller")
        owner = state["kill_worker"]
        if owner not in {"worker_a", "worker_b"}:
            raise RuntimeError("Unexpected isolated worker identity")
        execute("kill", "--signal", "SIGKILL", owner)
        execute("up", "-d", owner)
        state = controller("recover_restart")
        owner = state["kill_worker"]
        if owner not in {"worker_a", "worker_b"}:
            raise RuntimeError("Unexpected isolated worker identity")
        execute("kill", "--signal", "SIGKILL", owner)
        execute("up", "-d", owner)
        state = controller("recover_unknown")
        (run / "summary.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(json.dumps({"verified": state["verified"], "bulk_rows": state["bulk_rows"],
                          "bulk_mock_requests": state["bulk_mock_requests"],
                          "duplicate_extra_requests": state["duplicate_extra_requests"],
                          "real_worker_restarts": state["real_worker_restarts"],
                          "unknown_status": state["unknown_status"], "live_llm_calls": 0,
                          "evidence": str(run / "summary.json")}, ensure_ascii=False), flush=True)
    finally:
        try:
            logs = execute("logs", "--no-color", timeout=30)
            (run / "workers.log").write_text(logs, encoding="utf-8")
        finally:
            if not args.keep_services:
                # The generated prefix and compose path identify only this
                # run. Never stop the user's API/UI or remove their volumes.
                if not project.startswith("customerinsight-queue-acceptance-") or compose.parent != run:
                    raise RuntimeError("Refusing cleanup outside this acceptance project")
                execute("down", "--volumes", "--remove-orphans", timeout=60)


if __name__ == "__main__":
    main()
