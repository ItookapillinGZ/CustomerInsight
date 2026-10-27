"""Private, offline workspace snapshots and strict archive validation.

The administration CLIs send this module to a short-lived container through
stdin. It deliberately does not import application configuration or API clients.
Backups contain customer data and password hashes and must remain private.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import datetime as dt
import decimal
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import zipfile


FORMAT_VERSION = 1
MAX_BYTES = 4 * 1024 ** 3
MAX_FILES = 100_000
MAX_MANIFEST_BYTES = 16 * 1024 ** 2
RESTORE_PROJECT = re.compile(r"customerinsight-restore-[a-z0-9][a-z0-9-]{2,40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BackupError(ValueError):
    """Safe administration error without data or credentials."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest_file(path):
    checksum = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def safe_name(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise BackupError("归档包含无效路径。")
    item = PurePosixPath(name)
    if item.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        raise BackupError("归档路径越界。")
    return item


def validate_restore_project(project):
    if not isinstance(project, str) or not RESTORE_PROJECT.fullmatch(project):
        raise BackupError("恢复项目必须是新的 customerinsight-restore-xxx 隔离项目。")
    return project


def _value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return {"datetime": value.isoformat()}
    if isinstance(value, decimal.Decimal):
        return {"decimal": str(value)}
    if isinstance(value, dict):
        return {key: _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value


def rows_fingerprint(rows):
    values = sorted(canonical(_value(list(row))) for row in rows)
    checksum = hashlib.sha256()
    for value in values:
        checksum.update(value.encode("utf-8"))
        checksum.update(b"\n")
    return {"count": len(values), "sha256": checksum.hexdigest()}


def sqlite_inventory(path, *, immutable=False):
    uri = Path(path).resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BackupError("SQLite 完整性检查失败。")
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = rows_fingerprint(connection.execute("SELECT * FROM " + quoted))
        return {"user_version": connection.execute("PRAGMA user_version").fetchone()[0],
                "tables": tables}


def postgres_inventory(*, revoke_sessions=False):
    # Imported here so archive validation remains a stdlib-only host operation.
    from sqlalchemy import create_engine, inspect, text
    url = os.environ.get("APP_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        raise BackupError("快照工具需要 PostgreSQL 连接。")
    engine = create_engine(url, hide_parameters=True, echo=False,
                           connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as connection:
            if revoke_sessions:
                from time import time
                count = connection.execute(text(
                    "UPDATE app_sessions SET revoked_at=:now WHERE revoked_at IS NULL"),
                    {"now": time()}).rowcount
                return {"revoked_sessions": count}
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            inspector = inspect(connection)
            tables = {}
            for name in sorted(inspector.get_table_names(schema="public")):
                quoted = engine.dialect.identifier_preparer.quote(name)
                tables[name] = rows_fingerprint(connection.exec_driver_sql("SELECT * FROM public." + quoted))
            statuses = {}
            for name in ("analysis_jobs", "analysis_groups"):
                if name in tables:
                    statuses[name] = {row[0]: row[1] for row in connection.exec_driver_sql(
                        "SELECT status, count(*) FROM " + name + " GROUP BY status ORDER BY status")}
            sequences = {}
            for name in sorted(inspector.get_sequence_names(schema="public")):
                quoted = engine.dialect.identifier_preparer.quote(name)
                sequences[name] = list(connection.exec_driver_sql(
                    "SELECT last_value, is_called FROM public." + quoted).one())
            return {"tables": tables, "statuses": statuses, "sequences": sequences}
    finally:
        engine.dispose()


def _files(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise BackupError("数据目录必须是真实目录。")
    files, directories = [], []
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or (hasattr(path, "is_junction") and path.is_junction()):
            raise BackupError("数据目录包含链接，已停止备份。")
        if stat.S_ISDIR(mode):
            directories.append(path)
        elif stat.S_ISREG(mode):
            files.append(path)
        else:
            raise BackupError("数据目录包含不支持的设备或特殊文件。")
    if len(files) + len(directories) > MAX_FILES:
        raise BackupError("备份文件数量超出限制。")
    return files, directories


def _is_sqlite(path):
    with path.open("rb") as source:
        return source.read(16) == b"SQLite format 3\x00"


def data_inventory(root):
    files, directories = _files(root)
    result, databases = [], {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        result.append({"path": relative, "size": path.stat().st_size, "sha256": digest_file(path)})
        if _is_sqlite(path):
            # This function inventories independent, closed snapshot files.
            # immutable=1 prevents SQLite from creating fresh WAL/SHM sidecars
            # while hashing a WAL-mode database restored from its backup API.
            databases[relative] = sqlite_inventory(path, immutable=True)
    return {"files": result, "directories": [path.relative_to(root).as_posix() for path in directories],
            "sqlite": databases, "total_bytes": sum(item["size"] for item in result)}


def snapshot_data(root, target):
    """Copy all durable files; SQLite databases use the online backup API.

    The host has already stopped every identified application writer. WAL/SHM
    sidecars belonging to a recognized SQLite database are excluded because the
    backup API folds committed WAL pages into the independent database snapshot.
    """
    root, target = Path(root), Path(target)
    if target.exists():
        raise BackupError("快照目标已存在，未覆盖。")
    files, directories = _files(root)
    database_names = {str(path) for path in files if _is_sqlite(path)}
    with tempfile.TemporaryDirectory(prefix="customerinsight-snapshot-") as temporary:
        copied = Path(temporary)
        for directory in directories:
            (copied / directory.relative_to(root)).mkdir(parents=True)
        total = 0
        for path in files:
            if any(str(path) == name + suffix for name in database_names for suffix in ("-wal", "-shm", "-journal")):
                continue
            destination = copied / path.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if str(path) in database_names:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(destination)) as output:
                        source.backup(output)
            else:
                shutil.copyfile(path, destination)
            total += destination.stat().st_size
            if total > MAX_BYTES:
                raise BackupError("数据快照大小超出限制。")
        inventory = data_inventory(copied)
        with tarfile.open(target, "w") as archive:
            for path in sorted(copied.rglob("*")):
                info = archive.gettarinfo(str(path), path.relative_to(copied).as_posix())
                info.uid = info.gid = 10001
                info.uname = info.gname = "app"
                info.mode = 0o700 if info.isdir() else 0o600
                if info.isfile():
                    with path.open("rb") as source:
                        archive.addfile(info, source)
                else:
                    archive.addfile(info)
        return inventory


def validate_data_tar(path, inventory):
    if not isinstance(inventory, dict) or not isinstance(inventory.get("files"), list):
        raise BackupError("数据清单无效。")
    expected = {}
    for item in inventory["files"]:
        name = item.get("path")
        safe_name(name)
        if name in expected or not isinstance(item.get("size"), int) or isinstance(item["size"], bool) or item["size"] < 0:
            raise BackupError("数据清单无效。")
        if not isinstance(item.get("sha256"), str) or not SHA256.fullmatch(item["sha256"]):
            raise BackupError("数据校验码无效。")
        expected[name] = item
    directories = inventory.get("directories", [])
    if (not isinstance(directories, list) or not all(isinstance(name, str) for name in directories)
            or len(set(directories)) != len(directories)):
        raise BackupError("目录清单无效。")
    for name in directories:
        safe_name(name)
    if len(expected) + len(directories) > MAX_FILES:
        raise BackupError("归档文件数量超出限制。")
    total = sum(item["size"] for item in expected.values())
    if total > MAX_BYTES or inventory.get("total_bytes") != total:
        raise BackupError("数据大小清单不一致。")
    seen = set()
    try:
        with tarfile.open(path, "r:") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                safe_name(name)
                if name in seen or not (member.isfile() or member.isdir()):
                    raise BackupError("归档含有重复路径、链接或特殊文件。")
                seen.add(name)
                if member.isdir():
                    if name not in directories:
                        raise BackupError("目录不在清单内。")
                    continue
                item = expected.get(name)
                if item is None or member.size != item["size"]:
                    raise BackupError("归档文件大小与清单不一致。")
                checksum = hashlib.sha256()
                source = archive.extractfile(member)
                actual_size = 0
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    actual_size += len(block)
                    checksum.update(block)
                if actual_size != item["size"] or checksum.hexdigest() != item["sha256"]:
                    raise BackupError("归档文件校验失败。")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError("数据归档损坏。") from exc
    if seen != set(expected) | set(directories):
        raise BackupError("归档缺少清单文件。")
    return inventory


def extract_data_tar(path, target, inventory):
    validate_data_tar(path, inventory)
    target = Path(target)
    if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
        raise BackupError("恢复仅允许空的专用数据卷。")
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            destination = target.joinpath(*safe_name(member.name.rstrip("/")).parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o600)
    # Restored browser/worker images run as uid 10001. No executable file is
    # needed in /data; restore ownership without preserving archive permissions.
    if hasattr(os, "chown"):
        for directory in [target, *target.rglob("*")]:
            os.chown(directory, 10001, 10001)
            if directory.is_dir():
                directory.chmod(0o700)
    return data_inventory(target)


def verify_archive(archive_path, staging):
    """Verify the entire private package before creating Docker resources."""
    archive_path, staging = Path(archive_path), Path(staging)
    if archive_path.is_symlink() or not archive_path.is_file() or archive_path.stat().st_size > MAX_BYTES * 2:
        raise BackupError("备份包路径或大小无效。")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) != 3 or {item.filename for item in members} != {"manifest.json", "postgres.dump", "data.tar"}:
                raise BackupError("备份包成员无效。")
            for item in members:
                if stat.S_ISLNK(item.external_attr >> 16) or item.file_size > MAX_BYTES + 128 * 1024 ** 2:
                    raise BackupError("备份包包含链接或超大文件。")
            if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                raise BackupError("备份清单过大。")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_VERSION:
                raise BackupError("备份格式不受支持。")
            expected = manifest.get("artifacts")
            if not isinstance(expected, dict) or set(expected) != {"postgres.dump", "data.tar"}:
                raise BackupError("备份产物清单无效。")
            for name, item in expected.items():
                if not isinstance(item, dict) or type(item.get("size")) is not int or item["size"] <= 0:
                    raise BackupError("备份产物大小无效。")
                if not isinstance(item.get("sha256"), str) or not SHA256.fullmatch(item["sha256"]):
                    raise BackupError("备份产物校验码无效。")
                if archive.getinfo(name).file_size != item["size"]:
                    raise BackupError("备份产物大小不一致。")
                destination = staging / name
                with archive.open(name) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                if digest_file(destination) != item["sha256"]:
                    raise BackupError("备份包校验失败。")
            validate_data_tar(staging / "data.tar", manifest.get("data"))
            if not isinstance(manifest.get("postgres", {}).get("tables"), dict):
                raise BackupError("PostgreSQL 指纹清单无效。")
            return manifest
    except BackupError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise BackupError("备份包损坏或格式无效。") from exc


def _main():
    parser = argparse.ArgumentParser(description="Private container-side backup helper")
    parser.add_argument("mode", choices=["postgres", "revoke-sessions", "snapshot", "files", "restore-data"])
    parser.add_argument("--root", default="/data")
    parser.add_argument("--target", default="/snapshot/data.tar")
    parser.add_argument("--manifest", default="/snapshot/manifest.json")
    args = parser.parse_args()
    try:
        if args.mode in {"postgres", "revoke-sessions"}:
            result = postgres_inventory(revoke_sessions=args.mode == "revoke-sessions")
        elif args.mode == "snapshot":
            result = snapshot_data(args.root, args.target)
        elif args.mode == "restore-data":
            inventory = json.loads(Path(args.manifest).read_text("utf-8"))["data"]
            result = extract_data_tar(args.target, args.root, inventory)
        else:
            result = data_inventory(args.root)
        print(canonical(result))
    except Exception as exc:
        print(canonical({"error": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    _main()
