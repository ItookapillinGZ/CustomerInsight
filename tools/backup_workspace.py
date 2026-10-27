"""Quiesced PostgreSQL + /data backup; does not make any model requests."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backup import BackupError, FORMAT_VERSION, digest_file, verify_archive
from tools.workspace_backup_common import Docker, private_json, reject_active_jobs, source_deployment


def backup_workspace(*, output=None, compose_files=None, project=None,
                     allow_active_jobs=False, env_file=None, docker=None):
    docker = docker or Docker()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(output) if output else ROOT / ".runtime" / "backups" / f"workspace-{stamp}-{secrets.token_hex(3)}.zip"
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise BackupError("备份目标已存在，未覆盖。")
    # Keep backups out of source/public artifacts; callers can choose another
    # private directory explicitly, but never the website's exported outputs.
    public = (ROOT / "outputs").resolve()
    if output.resolve() == public or public in output.resolve().parents:
        raise BackupError("完整备份含敏感数据，不能放入 outputs 下载目录。")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.parent.chmod(0o700)
    source = source_deployment(docker, compose_files, project, env_file)
    version = docker.run("exec", source["postgres"], "pg_dump", "--version").decode("ascii", errors="replace")
    if " 17." not in version:
        raise BackupError("备份当前只支持 PostgreSQL 17。")
    helper_kwargs = {"image": source["image"], "network": source["network"],
                     "database_url": source["database_url"]}
    initial = docker.helper(mode="postgres", **helper_kwargs)
    if not allow_active_jobs:
        reject_active_jobs(initial)
    stopped = []
    restart_error = None
    try:
        # Stop admission first and worker delivery before the worker. Every
        # service is recorded before stopping, so finally restores it on failure.
        rank = {"customerinsight": 0, "api": 1, "dispatcher": 2, "worker": 3}
        for name, identifier in sorted(source["writers"], key=lambda item: rank[item[0]]):
            stopped.append((name, identifier))
            docker.run("stop", "--time", "90", identifier, timeout=120)
        for _, identifier in stopped:
            if docker.json("inspect", identifier)[0].get("State", {}).get("Running"):
                raise BackupError("写服务未完全停止，未创建备份。")
        frozen = docker.helper(mode="postgres", **helper_kwargs)
        if not allow_active_jobs:
            reject_active_jobs(frozen)
        with tempfile.TemporaryDirectory(prefix=".workspace-backup-", dir=output.parent) as temporary:
            staging = Path(temporary)
            dump = staging / "postgres.dump"
            with dump.open("xb") as binary:
                docker.run("exec", source["postgres"], "pg_dump", "--format=custom",
                           "--no-owner", "--no-privileges", "--username", source["pg_user"],
                           "--dbname", source["pg_database"], output=binary)
            if not dump.stat().st_size:
                raise BackupError("PostgreSQL 导出为空，未创建备份。")
            data = docker.helper(source["image"], "snapshot", volume=source["volume"], directory=staging)
            after = docker.helper(mode="postgres", **helper_kwargs)
            if after != frozen:
                raise BackupError("停写窗口内数据库仍有变化，未创建备份。")
            manifest = {
                "format": FORMAT_VERSION, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "consistency": "application-writers-stopped",
                "sensitive": True, "model_credentials_included": False,
                "source": {"project": source["project"], "image_id": source["image"],
                           "running_writers": [name for name, _ in source["writers"]]},
                "postgres": frozen, "data": data,
                "artifacts": {name: {"size": (staging / name).stat().st_size,
                                     "sha256": digest_file(staging / name)}
                              for name in ("postgres.dump", "data.tar")},
                "restore_policy": {"isolated_only": True, "revoke_sessions": True,
                                   "start_workers": False, "redis_messages_restored": False},
            }
            private_json(staging / "manifest.json", manifest)
            package = staging / "package.zip"
            with zipfile.ZipFile(package, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for name in ("manifest.json", "postgres.dump", "data.tar"):
                    archive.write(staging / name, name)
            check = staging / "verified"
            check.mkdir()
            verify_archive(package, check)
            # Hard-link publication is atomic and refuses a pre-existing file,
            # including a file created after the initial existence check.
            package.chmod(0o600)
            os.link(package, output)
        return {"archive": str(output), "sha256": digest_file(output),
                "postgres_tables": len(frozen["tables"]), "files": len(data["files"]),
                "data_bytes": data["total_bytes"], "model_requests": 0,
                "writers_restored": [name for name, _ in source["writers"]]}
    finally:
        for _, identifier in reversed(stopped):
            try:
                docker.run("start", identifier)
            except BackupError as exc:
                restart_error = exc
        if restart_error is not None:
            raise BackupError("备份维护结束，但有原运行服务未能重新启动；请检查 Docker 服务状态。") from restart_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--compose-file", action="append", dest="compose_files",
                        help="Repeat for legacy overlays; defaults to compose.production.yml")
    parser.add_argument("--project")
    parser.add_argument("--env-file", help="Explicit empty file for isolated fixtures; default uses normal Compose configuration")
    parser.add_argument("--allow-active-jobs", action="store_true",
                        help="Explicit maintenance override; uncertain attempts remain uncertain")
    args = parser.parse_args()
    try:
        print(json.dumps(backup_workspace(**vars(args)), ensure_ascii=False))
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print("备份未完成，错误类型：" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
