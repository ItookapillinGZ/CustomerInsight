"""Restore a verified backup into a new, network-isolated rehearsal project.

No web application, worker, dispatcher or model client is started. Existing
Docker resources are always refused, even if their names look like test names.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backup import BackupError, canonical, validate_restore_project, verify_archive
from tools.workspace_backup_common import Docker, private_json


def _assert_absent(docker, project, names):
    for kind in ("container", "volume", "network"):
        options = ["ps", "--all", "--quiet"] if kind == "container" else [kind, "ls", "--quiet"]
        if docker.ids(*options, "--filter", f"label=com.docker.compose.project={project}"):
            raise BackupError("恢复项目已经存在，绝不复用或覆盖现有环境。")
    # Names may be occupied by resources without our labels; inspect via a list
    # rather than treating all inspect errors as 'not found'.
    occupied = set(docker.run("ps", "--all", "--format", "{{.Names}}").decode().split())
    occupied |= set(docker.ids("volume", "ls", "--quiet"))
    occupied |= set(docker.run("network", "ls", "--format", "{{.Name}}").decode().split())
    if occupied & set(names.values()):
        raise BackupError("恢复资源名称已被占用，未修改现有资源。")


def _verify_owned(docker, kind, name, project, run_id):
    inspected = docker.json("inspect" if kind == "container" else kind,
                            *([] if kind == "container" else ["inspect"]), name)
    item = inspected[0]
    labels = ((item.get("Config") or {}).get("Labels") if kind == "container" else item.get("Labels")) or {}
    if (labels.get("com.docker.compose.project") != project or labels.get("customerinsight.restore") != "1"
            or labels.get("customerinsight.restore.run") != run_id):
        raise BackupError("隔离资源的归属不匹配，未写入或删除该资源。")
    return item


def _cleanup(docker, project, created, run_id):
    """Remove only resources we created and can still prove we own."""
    for kind, name in reversed(created):
        _verify_owned(docker, kind, name, project, run_id)
        docker.run("rm" if kind == "container" else kind, *(["--force"] if kind == "container" else ["rm"]), name)


def restore_workspace(archive, *, project, app_image="customerinsight-backend:phase3",
                      cleanup=False, evidence=None, docker=None):
    project = validate_restore_project(project)
    docker = docker or Docker()
    names = {"network": project + "-private", "data": project + "-data",
             "pg_data": project + "-postgres-data", "postgres": project + "-postgres"}
    evidence = Path(evidence) if evidence else ROOT / ".runtime" / "backups" / (project + "-verification.json")
    if evidence.exists():
        raise BackupError("恢复验收文件已存在，未覆盖。")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.parent.chmod(0o700)
    created = []
    with tempfile.TemporaryDirectory(prefix=".workspace-restore-", dir=evidence.parent) as temporary:
        staging = Path(temporary)
        manifest = verify_archive(archive, staging)
        private_json(staging / "manifest.json", manifest)
        if manifest.get("source", {}).get("project") == project:
            raise BackupError("恢复项目不得等于源项目。")
        _assert_absent(docker, project, names)
        # App image is operator selected, never a command or image taken from
        # an untrusted archive. The source image identifier is audit data only.
        inspected_image = docker.json("image", "inspect", app_image)
        if (len(inspected_image) != 1 or not isinstance(inspected_image[0].get("Id"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", inspected_image[0]["Id"])):
            raise BackupError("应用镜像标识无效。")
        # Pin one local image for the entire rehearsal, even if another build
        # changes a human-readable tag while the restore is in progress.
        app_image = inspected_image[0]["Id"]
        docker.json("image", "inspect", "postgres:17-alpine")
        run_id = secrets.token_hex(16)
        labels = ["--label", f"com.docker.compose.project={project}",
                  "--label", "customerinsight.restore=1",
                  "--label", f"customerinsight.restore.run={run_id}"]
        docker.run("network", "create", "--internal", *labels, names["network"])
        network = _verify_owned(docker, "network", names["network"], project, run_id)
        created.append(("network", names["network"]))
        if not network.get("Internal"):
            raise BackupError("恢复网络未隔离，已停止。")
        for key in ("data", "pg_data"):
            docker.run("volume", "create", *labels, names[key])
            _verify_owned(docker, "volume", names[key], project, run_id)
            created.append(("volume", names[key]))
        docker.helper(app_image, "restore-data", volume=names["data"], directory=staging, writable=True)
        password = secrets.token_hex(24)
        environment = dict(docker.environment)
        environment["POSTGRES_PASSWORD"] = password
        private_docker = Docker(environment=environment)
        private_docker.run("run", "--detach", "--name", names["postgres"], *labels,
                           "--network", names["network"], "--mount",
                           f"type=volume,source={names['pg_data']},target=/var/lib/postgresql/data",
                           "--env", "POSTGRES_USER=customerinsight", "--env", "POSTGRES_DB=customerinsight",
                           "--env", "POSTGRES_PASSWORD", "--security-opt", "no-new-privileges:true",
                           "postgres:17-alpine")
        created.append(("container", names["postgres"]))
        _verify_owned(docker, "container", names["postgres"], project, run_id)
        deadline = time.monotonic() + 90
        while True:
            try:
                # The image's initialization server listens only on a Unix
                # socket; TCP readiness avoids racing its final restart.
                docker.run("exec", names["postgres"], "pg_isready", "--host", "127.0.0.1",
                           "-U", "customerinsight", "-d", "customerinsight", timeout=10)
                break
            except BackupError:
                if time.monotonic() >= deadline:
                    raise BackupError("隔离 PostgreSQL 未能就绪。") from None
                time.sleep(1)
        # Binary input is never routed through PowerShell or decoded as text.
        # The generic Docker helper takes byte input; the size cap was already
        # checked before resources were created.
        with (staging / "postgres.dump").open("rb") as binary:
            docker.run("exec", "-i", names["postgres"], "pg_restore", "--exit-on-error",
                       "--no-owner", "--no-privileges", "--username", "customerinsight", "--dbname", "customerinsight",
                       stdin=binary)
        url = f"postgresql+psycopg://customerinsight:{password}@{names['postgres']}:5432/customerinsight"
        kwargs = {"image": app_image, "network": names["network"], "database_url": url}
        restored_pg = docker.helper(mode="postgres", **kwargs)
        restored_files = docker.helper(app_image, "files", volume=names["data"])
        if restored_pg != manifest["postgres"] or restored_files != manifest["data"]:
            raise BackupError("恢复内容与备份指纹不一致，隔离环境已保留供排查。")
        revocation = docker.helper(mode="revoke-sessions", **kwargs)
        final = docker.helper(mode="postgres", **kwargs)
        final_comparable = {key: value for key, value in final["tables"].items() if key != "app_sessions"}
        expected_comparable = {key: value for key, value in restored_pg["tables"].items() if key != "app_sessions"}
        if (final_comparable != expected_comparable or final["statuses"] != restored_pg["statuses"]
                or final.get("sequences") != restored_pg.get("sequences")):
            raise BackupError("撤销登录会话时发生意外数据变化，隔离环境已保留。")
        receipt = {
            "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(), "project": project,
            "archive": str(Path(archive).resolve()), "verified": True,
            "postgres_tables": len(restored_pg["tables"]),
            "table_counts": {name: value["count"] for name, value in restored_pg["tables"].items()},
            "files": len(restored_files["files"]), "sqlite_databases": len(restored_files["sqlite"]),
            "files_and_database_fingerprints_equal": True,
            "job_statuses": restored_pg["statuses"],
            "expected_change": "Restored login sessions revoked; users must log in again.",
            "revoked_sessions": revocation["revoked_sessions"],
            "model_credentials_copied": False, "model_requests": 0,
            "workers_started": False, "published_ports": [], "internal_network": True,
            "app_image_id": app_image,
            "cleaned_up": bool(cleanup), "resources": names,
        }
        if cleanup:
            _cleanup(docker, project, created, run_id)
        private_json(evidence, receipt)
        return {"verified": True, "evidence": str(evidence), "project": project,
                "tables": len(restored_pg["tables"]), "files": len(restored_files["files"]),
                "revoked_sessions": revocation["revoked_sessions"], "model_requests": 0,
                "cleaned_up": bool(cleanup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--project", required=True)
    parser.add_argument("--app-image", default="customerinsight-backend:phase3")
    parser.add_argument("--cleanup", action="store_true", help="Remove this new isolated environment after successful verification")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    try:
        print(json.dumps(restore_workspace(**vars(args)), ensure_ascii=False))
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print("隔离恢复未完成，错误类型：" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
