"""Run a real HTTP/PostgreSQL/Redis/Celery benchmark with an offline SDK.

The host needs only Python's standard library and Docker Compose. Each run owns
an internal network and UUID-prefixed fresh volumes, never the deployment .env.
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="customerinsight-backend:phase4")
    parser.add_argument("--accounts", type=int, default=5)
    parser.add_argument("--rows-per-account", type=int, default=100)
    parser.add_argument("--mock-delay-ms", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--keep-services", action="store_true")
    args = parser.parse_args()
    if (not re.fullmatch(r"[a-z0-9./:_-]+", args.image) or not 1 <= args.accounts <= 10
            or not 1 <= args.rows_per_account <= 1000 or not 0 <= args.mock_delay_ms <= 5000
            or not 30 <= args.timeout <= 3600):
        parser.error("Use a valid image, 1..10 accounts, 1..1000 rows, 0..5000 ms and 30..3600 s")
    repository = Path(__file__).resolve().parents[1]
    run_id = uuid4().hex
    run = repository / ".runtime" / "phase4" / "benchmark" / run_id
    seed = run / "seed"
    seed.mkdir(parents=True)
    metadata = {"purpose": "isolated-backend-benchmark", "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "accounts": args.accounts, "rows_per_account": args.rows_per_account,
                "mock_delay_ms": args.mock_delay_ms, "timeout": args.timeout}
    (seed / ".isolated-backend-benchmark.json").write_text(json.dumps(metadata), encoding="utf-8")
    (run / "empty.env").write_text("# No deployment environment is loaded.\n", encoding="utf-8")
    project = "customerinsight-benchmark-" + run_id[:12]
    password = secrets.token_urlsafe(30)
    environment = {
        "APP_DATABASE_URL": f"postgresql+psycopg://benchmark:{password}@postgres:5432/benchmark",
        "APP_DATA_DIR": "/benchmark/workspace", "APP_USAGE_DB_PATH": "/benchmark/usage.sqlite3",
        "APP_REDIS_URL": "redis://redis:6379/0", "APP_ANALYSIS_MODE": "queued",
        "APP_API_TOKEN": "", "APP_DAILY_REQUEST_LIMIT": "2000", "APP_GLOBAL_INFLIGHT": "1",
        "OPENAI_API_KEY": "benchmark-unused", "OPENAI_MODEL": "benchmark-offline-model",
        "OPENAI_BASE_URL": "http://blocked.invalid/v1", "OPENAI_TIMEOUT_SECONDS": "10",
        "API_MODE": "chat_completions", "CHAT_OUTPUT_MODE": "json_object",
        "APP_JOB_LEASE_SECONDS": "90", "APP_LLM_BATCH_SIZE": "10",
        "APP_LLM_BATCH_MAX_CHARACTERS": "12000", "APP_DISPATCH_INTERVAL_SECONDS": "5",
        "PYTHONUNBUFFERED": "1",
    }
    mounts = ["benchmark_state:/benchmark", {"type": "bind", "source": str(repository / "tools"),
              "target": "/app/tools", "read_only": True}]
    base = {"image": args.image, "working_dir": "/app", "init": True,
            "environment": environment, "networks": ["isolated"], "volumes": mounts,
            "healthcheck": {"disable": True}, "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"], "cpus": 2, "mem_limit": "768m"}
    service_limits = {"cpus": 1, "mem_limit": "384m"}
    services = {
        "postgres": {"image": "postgres:17-alpine", "networks": ["isolated"], **service_limits,
                     "environment": {"POSTGRES_USER": "benchmark", "POSTGRES_DB": "benchmark",
                                     "POSTGRES_PASSWORD": password},
                     "volumes": ["pg_state:/var/lib/postgresql/data"],
                     "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U benchmark -d benchmark"],
                                     "interval": "1s", "timeout": "2s", "retries": 40}},
        "redis": {"image": "redis:7-alpine", "networks": ["isolated"], **service_limits,
                  "command": ["redis-server", "--appendonly", "yes"], "volumes": ["redis_state:/data"],
                  "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "1s",
                                  "timeout": "1s", "retries": 30}},
        "prepare": {"image": args.image, "user": "0:0", "networks": ["isolated"],
                    "entrypoint": ["python", "-c"], "command": [
                        "import os,pathlib; p=pathlib.Path('/benchmark'); "
                        "(p/'.isolated-backend-benchmark.json').write_bytes("
                        "pathlib.Path('/seed/.isolated-backend-benchmark.json').read_bytes()); "
                        "os.chown(p,10001,10001)"],
                    "volumes": ["benchmark_state:/benchmark", {"type": "bind", "source": str(seed),
                                "target": "/seed", "read_only": True}],
                    "healthcheck": {"disable": True}, "cap_drop": ["ALL"], "cap_add": ["CHOWN"],
                    "security_opt": ["no-new-privileges:true"]},
    }
    services["initialize"] = {**base, "command": ["python", "/app/tools/benchmark_worker.py", "initialize"],
        "depends_on": {"postgres": {"condition": "service_healthy"},
                       "prepare": {"condition": "service_completed_successfully"}}}
    dependencies = {"redis": {"condition": "service_healthy"},
                    "initialize": {"condition": "service_completed_successfully"}}
    for name, action in (("api", "api"), ("worker", "worker"), ("runner", "controller")):
        services[name] = {**base, "command": ["python", "/app/tools/benchmark_worker.py", action],
                          "depends_on": dependencies}
    services["dispatcher"] = {**base, "command": ["python", "-m", "backend.dispatch"],
                              "depends_on": dependencies}
    configuration = {"services": services, "networks": {"isolated": {"internal": True}},
                     "volumes": {"pg_state": {}, "redis_state": {}, "benchmark_state": {}}}
    compose = run / "compose.json"
    compose.write_text(json.dumps(configuration, indent=2), encoding="utf-8")
    command = ["docker", "compose", "--env-file", str(run / "empty.env"), "-p", project, "-f", str(compose)]

    def execute(*parameters, timeout=240):
        result = subprocess.run([*command, *parameters], cwd=run, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace", timeout=timeout)
        with (run / "docker-benchmark.log").open("a", encoding="utf-8") as log:
            log.write(result.stdout + "\n")
        if result.returncode:
            raise RuntimeError("Isolated benchmark failed; inspect its dedicated log.")
        return result.stdout

    def readiness(stage):
        output = execute("run", "--rm", "--no-deps", "runner", "python",
                         "/app/tools/benchmark_worker.py", stage, timeout=75)
        lines = [line.split("READINESS_JSON ", 1)[1] for line in output.splitlines() if "READINESS_JSON " in line]
        if not lines:
            raise RuntimeError("Isolated readiness check produced no aggregate proof")
        return json.loads(lines[-1])

    try:
        execute("up", "-d", "api", timeout=240)
        readiness_checks = [readiness("missing_services")]
        execute("up", "-d", "worker", "dispatcher", timeout=120)
        readiness_checks.append(readiness("all_ready"))
        output = execute("run", "--rm", "--no-deps", "runner", timeout=args.timeout + 90)
        lines = [line.split("BENCHMARK_JSON ", 1)[1] for line in output.splitlines() if "BENCHMARK_JSON " in line]
        if not lines:
            raise RuntimeError("Benchmark did not produce aggregate evidence")
        summary = json.loads(lines[-1])
        # These fault checks run only after controller() froze its timing.
        # SIGKILL preserves a stale heartbeat, exercising real TTL expiry.
        execute("kill", "--signal", "SIGKILL", "dispatcher", timeout=30)
        readiness_checks.append(readiness("dispatcher_expired"))
        execute("start", "dispatcher", timeout=30)
        readiness_checks.append(readiness("recovered_ready"))
        summary["readiness_checks"] = readiness_checks
        inspect = execute("ps", "-q", "api", "worker", "dispatcher", "postgres", "redis").splitlines()
        container_resources = []
        for container_id in inspect:
            result = subprocess.run(["docker", "inspect", "--format",
                '{"name":{{json .Name}},"image_id":{{json .Image}},"resources":{{json .HostConfig}}}', container_id],
                check=True, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30)
            inspected = json.loads(result.stdout)
            config = inspected["resources"]
            container_resources.append({"container_id": container_id[:12], "name": inspected["name"],
                "image_id": inspected["image_id"],
                "memory_limit_bytes": config["Memory"], "nano_cpus": config["NanoCpus"]})
        summary["container_resources"] = container_resources
        summary["image"] = args.image
        summary["created_at"] = metadata["created_at"]
        evidence = run / "summary.json"
        evidence.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        # ASCII JSON is portable even when the Windows host console uses GBK.
        print(json.dumps({**summary, "evidence": str(evidence)}), flush=True)
    finally:
        try:
            logs = execute("logs", "--no-color", timeout=30)
            (run / "workers.log").write_text(logs, encoding="utf-8")
        finally:
            if not args.keep_services:
                if not project.startswith("customerinsight-benchmark-") or compose.parent != run:
                    raise RuntimeError("Refusing cleanup outside this benchmark project")
                execute("down", "--volumes", "--remove-orphans", timeout=90)


if __name__ == "__main__":
    main()
