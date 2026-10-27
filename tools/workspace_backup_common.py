"""Stdlib Docker orchestration for private backups and isolated rehearsals."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

from backend.backup import BackupError, canonical


ROOT = Path(__file__).resolve().parents[1]
# New installations use the standalone production configuration. An older
# deployment can supply its original four --compose-file arguments explicitly.
DEFAULT_COMPOSE = ("compose.production.yml",)
WRITERS = {"customerinsight", "api", "worker", "dispatcher"}


class Docker:
    def __init__(self, *, environment=None):
        self.environment = dict(os.environ if environment is None else environment)

    def run(self, *args, input=None, stdin=None, output=None, timeout=300):
        """Never echo commands, raw stderr, environment, or data-bearing output."""
        try:
            result = subprocess.run(["docker", *map(str, args)], input=input,
                                    stdin=stdin,
                                    stdout=output if output is not None else subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=self.environment,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupError("Docker 操作失败或超时，请检查本机 Docker。") from exc
        if result.returncode:
            detail = ""
            try:
                error = json.loads(result.stderr.splitlines()[-1]).get("error")
                if isinstance(error, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error):
                    detail = "（" + error + "）"
            except (ValueError, AttributeError, IndexError, TypeError):
                pass
            raise BackupError("Docker 操作失败" + detail + "；未输出可能包含凭据的原始日志。")
        return result.stdout if output is None else b""

    def json(self, *args):
        try:
            return json.loads(self.run(*args))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BackupError("Docker 返回格式无效。") from exc

    def ids(self, *args):
        return self.run(*args).decode("utf-8").strip().split()

    def helper(self, image, mode, *, volume=None, network="none", directory=None,
               database_url=None, writable=False):
        command = ["run", "--rm", "-i", "--user", "0:0", "--network", network,
                   "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL"]
        if volume:
            command += ["--cap-add", "DAC_OVERRIDE"]
            if mode == "restore-data":
                command += ["--cap-add", "CHOWN", "--cap-add", "FOWNER"]
            command += ["--mount", f"type=volume,source={volume},target=/data" + ("" if writable else ",readonly")]
        if directory:
            command += ["--mount", f"type=bind,source={Path(directory).resolve()},target=/snapshot" +
                        ("" if mode == "snapshot" else ",readonly")]
        environment = dict(self.environment)
        if database_url:
            environment["APP_DATABASE_URL"] = database_url
            command += ["--env", "APP_DATABASE_URL"]
        # No host .env or model credentials are copied into this container.
        command += ["--env", "OPENAI_API_KEY=", "--env", "OPENAI_BASE_URL=https://blocked.invalid",
                    "--entrypoint", "python", image, "-", mode]
        source = (ROOT / "backend" / "backup.py").read_bytes()
        try:
            result = Docker(environment=environment).run(*command, input=source)
            return json.loads(result)
        except BackupError as exc:
            raise BackupError("快照阶段 " + mode + " 未完成：" + str(exc)) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise BackupError("快照辅助工具未返回有效结果。") from exc


def compose_prefix(files, project=None, env_file=None):
    prefix = ["compose", "--project-directory", str(ROOT)]
    if env_file is not None:
        path = Path(env_file).resolve()
        if not path.is_file():
            raise BackupError("指定的 Compose 环境文件不存在。")
        prefix += ["--env-file", str(path)]
    if project:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,80}", project):
            raise BackupError("源项目名称无效。")
        prefix += ["--project-name", project]
    for filename in files or DEFAULT_COMPOSE:
        path = Path(filename)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise BackupError("指定的 Compose 文件不存在。")
        prefix += ["--file", str(path)]
    return prefix


def source_deployment(docker, files=None, project=None, env_file=None):
    prefix = compose_prefix(files, project, env_file)
    identifiers = docker.ids(*prefix, "ps", "--all", "--quiet")
    if not identifiers:
        raise BackupError("未找到指定部署。")
    inspected = docker.json("inspect", *identifiers)
    services, actual_project = {}, None
    for item in inspected:
        labels = item.get("Config", {}).get("Labels") or {}
        owner = labels.get("com.docker.compose.project")
        service = labels.get("com.docker.compose.service")
        if not owner or not service or (actual_project is not None and owner != actual_project):
            raise BackupError("容器的 Compose 归属不一致。")
        if service in services:
            raise BackupError("同一服务存在多个容器，请先确认部署状态。")
        actual_project = owner
        services[service] = item
    if project and actual_project != project:
        raise BackupError("源项目归属不一致。")
    if not {"postgres", "api"} <= services.keys():
        raise BackupError("完整备份要求 PostgreSQL 和 API 部署。")
    pg, api = services["postgres"], services["api"]
    if not pg.get("State", {}).get("Running"):
        raise BackupError("PostgreSQL 未运行。")
    for service in ("db-init",):
        if services.get(service, {}).get("State", {}).get("Running"):
            raise BackupError("数据库迁移正在运行，请稍后备份。")
    mounts = [item for item in api.get("Mounts", []) if item.get("Destination") == "/data"]
    pg_mounts = [item for item in pg.get("Mounts", []) if item.get("Destination") == "/var/lib/postgresql/data"]
    if len(mounts) != 1 or mounts[0].get("Type") != "volume" or len(pg_mounts) != 1 or pg_mounts[0].get("Type") != "volume":
        raise BackupError("数据必须使用独立的 Docker 命名卷。")
    volume, pg_volume = mounts[0]["Name"], pg_mounts[0]["Name"]
    for name in (volume, pg_volume):
        labels = docker.json("volume", "inspect", name)[0].get("Labels") or {}
        if labels.get("com.docker.compose.project") != actual_project:
            raise BackupError("数据卷不属于指定 Compose 项目。")
    for service, item in services.items():
        if service in WRITERS:
            matching = [mount for mount in item.get("Mounts", []) if mount.get("Destination") == "/data"]
            writer_env = dict(value.split("=", 1) for value in item.get("Config", {}).get("Env", []) if "=" in value)
            if service == "customerinsight" and not matching and writer_env.get("APP_UI_MODE") == "api":
                # The API-only UI has no local customer data, but is still
                # stopped so it cannot admit HTTP writes during maintenance.
                continue
            if len(matching) != 1 or matching[0].get("Name") != volume:
                raise BackupError("应用写服务未共享同一个数据卷。")
    # Refuse a second, unidentified writer mounted to this same private volume.
    mounted_ids = docker.ids("ps", "--quiet", "--filter", f"volume={volume}")
    known = {item["Id"] for item in services.values()}
    if mounted_ids:
        for item in docker.json("inspect", *mounted_ids):
            if item["Id"] not in known and any(mount.get("Name") == volume and mount.get("RW")
                                                for mount in item.get("Mounts", [])):
                raise BackupError("共享数据卷还有未知写容器，请先停止该容器。")
    env = dict(value.split("=", 1) for value in api.get("Config", {}).get("Env", []) if "=" in value)
    database_url = env.get("APP_DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        raise BackupError("API 未配置 PostgreSQL。")
    network_names = set(api.get("NetworkSettings", {}).get("Networks", {})) & set(
        pg.get("NetworkSettings", {}).get("Networks", {}))
    if not network_names:
        raise BackupError("API 与 PostgreSQL 没有共同网络。")
    network = sorted(network_names)[0]
    network_labels = docker.json("network", "inspect", network)[0].get("Labels") or {}
    if network_labels.get("com.docker.compose.project") != actual_project:
        raise BackupError("网络不属于源 Compose 项目。")
    pg_env = dict(value.split("=", 1) for value in pg.get("Config", {}).get("Env", []) if "=" in value)
    parsed = urlsplit(database_url)
    pg_user = pg_env.get("POSTGRES_USER", "postgres")
    pg_database = pg_env.get("POSTGRES_DB", pg_user)
    aliases = set(pg.get("NetworkSettings", {}).get("Networks", {}).get(network, {}).get("Aliases") or [])
    aliases.add("postgres")
    try:
        target_matches = (parsed.hostname in aliases and parsed.port in {None, 5432}
                          and unquote(parsed.username or "") == pg_user
                          and unquote(parsed.path.lstrip("/")) == pg_database)
    except ValueError:
        target_matches = False
    if not target_matches:
        raise BackupError("API 的数据库地址不属于本次备份的 PostgreSQL 服务。")
    writers = [(name, item["Id"]) for name, item in services.items()
               if name in WRITERS and item.get("State", {}).get("Running")]
    return {"project": actual_project, "services": services, "writers": writers,
            "volume": volume, "pg_volume": pg_volume, "postgres": pg["Id"],
            "image": api["Image"], "database_url": database_url, "network": network,
            "pg_user": pg_user, "pg_database": pg_database}


def reject_active_jobs(inventory):
    statuses = inventory.get("statuses", {})
    active = sum(statuses.get("analysis_jobs", {}).get(name, 0) for name in ("queued", "running"))
    active += sum(statuses.get("analysis_groups", {}).get(name, 0) for name in ("leased", "sent"))
    if active:
        raise BackupError("有待运行或正在运行的任务；请先完成或明确取消，再执行维护备份。")


def private_json(path, value):
    path = Path(path)
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(canonical(value) + "\n")
    path.chmod(0o600)
