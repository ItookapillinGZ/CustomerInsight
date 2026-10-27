"""Backup security and failure recovery without Docker, credentials or LLMs."""
from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from backend.backup import (BackupError, canonical, data_inventory, digest_file,
                            extract_data_tar, rows_fingerprint, safe_name,
                            snapshot_data, sqlite_inventory, validate_data_tar,
                            validate_restore_project, verify_archive)
from tools.backup_workspace import backup_workspace
from tools.restore_workspace import _assert_absent, _cleanup, _verify_owned, restore_workspace
from tools.workspace_backup_common import Docker, reject_active_jobs, source_deployment


def inventory(files=(), directories=()):
    values = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
              for name, data in files]
    return {"files": values, "directories": list(directories), "sqlite": {},
            "total_bytes": sum(item["size"] for item in values)}


def make_tar(path, members):
    with tarfile.open(path, "w") as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            else:
                info.linkname = "../../outside"
                archive.addfile(info)


def make_package(root, *, override=None):
    tar = root / "data.tar"
    make_tar(tar, [("originals", b"", tarfile.DIRTYPE), ("originals/review.csv", b"review\nGood\n", tarfile.REGTYPE)])
    dump = root / "postgres.dump"
    dump.write_bytes(b"PGDMP\x00\x01synthetic-fixture")
    manifest = {
        "format": 1, "source": {"project": "customerinsight-production"},
        "data": inventory([("originals/review.csv", b"review\nGood\n")], ["originals"]),
        "postgres": {"tables": {}, "statuses": {}, "sequences": {}},
        "artifacts": {name: {"size": (root / name).stat().st_size, "sha256": digest_file(root / name)}
                      for name in ("data.tar", "postgres.dump")},
    }
    if override:
        override(manifest)
    package = root / "fixture.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", canonical(manifest))
        archive.write(tar, "data.tar")
        archive.write(dump, "postgres.dump")
    return package, manifest


class BackupArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_safe_paths_reject_platform_specific_escapes(self):
        for name in ("../x", "/tmp/x", "a/../b", "a//b", "./a", "a\\b", "C:/x", "a\x00b", ""):
            with self.subTest(name=name), self.assertRaises(BackupError):
                safe_name(name)
        self.assertEqual(str(safe_name("originals/你好.csv")), "originals/你好.csv")

    def test_restore_project_is_strictly_isolated(self):
        for project in ("production", "customerinsight", "customerinsight-restore-", "customerinsight-restore-../prod",
                        "customerinsight-restore-Xyz", "customerinsight-restore-ok;docker", "customerinsight-restore-x"):
            with self.subTest(project=project), self.assertRaises(BackupError):
                validate_restore_project(project)
        self.assertEqual(validate_restore_project("customerinsight-restore-phase4-001"), "customerinsight-restore-phase4-001")

    def test_valid_archive_fully_verified_before_use(self):
        archive, manifest = make_package(self.root)
        staging = self.root / "staging"
        staging.mkdir()
        self.assertEqual(verify_archive(archive, staging), manifest)
        self.assertEqual((staging / "postgres.dump").read_bytes(), b"PGDMP\x00\x01synthetic-fixture")

    def test_wrong_outer_checksum_rejected(self):
        archive, _ = make_package(self.root, override=lambda value: value["artifacts"]["data.tar"].update(sha256="0" * 64))
        staging = self.root / "staging"
        staging.mkdir()
        with self.assertRaises(BackupError):
            verify_archive(archive, staging)

    def test_extra_and_duplicate_zip_members_rejected(self):
        archive, _ = make_package(self.root)
        with zipfile.ZipFile(archive, "a") as output:
            output.writestr("../../outside", "no")
        staging = self.root / "staging"
        staging.mkdir()
        with self.assertRaises(BackupError):
            verify_archive(archive, staging)
        self.assertFalse((self.root.parent / "outside").exists())

    def test_unsupported_manifest_format_rejected(self):
        archive, _ = make_package(self.root, override=lambda value: value.update(format=999))
        with self.assertRaises(BackupError):
            verify_archive(archive, self.root)

    def test_corrupt_zip_rejected(self):
        archive = self.root / "corrupt.zip"
        archive.write_bytes(b"not a zip")
        with self.assertRaises(BackupError):
            verify_archive(archive, self.root)

    def test_tar_links_and_special_files_rejected(self):
        path = self.root / "data.tar"
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                make_tar(path, [("x", b"", kind)])
                with self.assertRaises(BackupError):
                    validate_data_tar(path, inventory())

    def test_tar_path_traversal_rejected(self):
        path = self.root / "data.tar"
        for name in ("../../outside", "/outside", "C:/outside", "a\\outside"):
            with self.subTest(name=name):
                make_tar(path, [(name, b"no", tarfile.REGTYPE)])
                with self.assertRaises(BackupError):
                    validate_data_tar(path, inventory())

    def test_duplicate_tar_member_rejected(self):
        path = self.root / "data.tar"
        make_tar(path, [("x", b"a", tarfile.REGTYPE), ("x", b"a", tarfile.REGTYPE)])
        with self.assertRaises(BackupError):
            validate_data_tar(path, inventory([("x", b"a")]))

    def test_inner_content_hash_size_and_missing_files_checked(self):
        path = self.root / "data.tar"
        make_tar(path, [("x", b"one", tarfile.REGTYPE)])
        for expected in (inventory([("x", b"two")]), inventory([("x", b"longer")]),
                         inventory([("x", b"one"), ("y", b"missing")])):
            with self.subTest(expected=expected), self.assertRaises(BackupError):
                validate_data_tar(path, expected)

    def test_empty_target_required_before_restore(self):
        archive, manifest = make_package(self.root)
        target = self.root / "existing"
        target.mkdir()
        (target / "retained").write_text("retained")
        with self.assertRaises(BackupError):
            extract_data_tar(self.root / "data.tar", target, manifest["data"])
        self.assertEqual((target / "retained").read_text(), "retained")

    def test_extract_only_validated_regular_files(self):
        _, manifest = make_package(self.root)
        target = self.root / "empty"
        target.mkdir()
        # Keep this portable and avoid changing owner on developer machines.
        with patch("backend.backup.os.chown", create=True):
            actual = extract_data_tar(self.root / "data.tar", target, manifest["data"])
        self.assertEqual(actual, manifest["data"])

    def test_sqlite_wal_backup_contains_committed_quota_without_sidecars(self):
        data = self.root / "data"
        data.mkdir()
        (data / "originals").mkdir()
        (data / "originals" / "review.csv").write_bytes(b"review\nGood\n")
        database = data / "usage.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE budget(day TEXT, used INTEGER)")
            connection.execute("INSERT INTO budget VALUES ('fixture',17)")
            connection.commit()
            expected = sqlite_inventory(database)
            result = snapshot_data(data, self.root / "snapshot.tar")
            self.assertEqual(result["sqlite"]["usage.sqlite3"], expected)
            self.assertEqual(expected["tables"]["budget"]["count"], 1)
            self.assertFalse(any(item["path"].endswith(("-wal", "-shm")) for item in result["files"]))
            validate_data_tar(self.root / "snapshot.tar", result)

    def test_snapshot_refuses_existing_output(self):
        data = self.root / "data"
        data.mkdir()
        output = self.root / "existing.tar"
        output.write_bytes(b"retained")
        with self.assertRaises(BackupError):
            snapshot_data(data, output)
        self.assertEqual(output.read_bytes(), b"retained")

    def test_row_fingerprint_preserves_duplicates_order_independent_and_binary(self):
        first = rows_fingerprint([(1, b"a"), (2, b"b"), (1, b"a")])
        self.assertEqual(first, rows_fingerprint([(2, b"b"), (1, b"a"), (1, b"a")]))
        self.assertNotEqual(first, rows_fingerprint([(2, b"b"), (1, b"a")]))


class _BackupDocker:
    def __init__(self, data, *, fail_snapshot=False, fail_stop=False):
        self.data = data
        self.fail_snapshot = fail_snapshot
        self.fail_stop = fail_stop
        self.calls = []
        self.environment = {}

    def run(self, *args, output=None, **kwargs):
        self.calls.append(args)
        if args[:3] == ("exec", "pg-id", "pg_dump"):
            if "--version" in args:
                return b"pg_dump (PostgreSQL) 17.6\n"
            output.write(b"PGDMP\x00fixture")
        if args[0] == "stop" and self.fail_stop:
            raise BackupError("synthetic stop failure")
        return b""

    def json(self, *args):
        return [{"State": {"Running": False}}]

    def helper(self, image=None, mode=None, directory=None, **kwargs):
        if mode == "snapshot":
            if self.fail_snapshot:
                raise BackupError("synthetic snapshot failure")
            return snapshot_data(self.data, directory / "data.tar")
        return {"tables": {}, "statuses": {}, "sequences": {}}


class BackupOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "original.csv").write_bytes(b"synthetic")
        self.source = {"project": "synthetic", "postgres": "pg-id", "image": "fixture-image",
                       "network": "fixture-network", "database_url": "synthetic-only",
                       "pg_user": "fixture", "pg_database": "fixture", "volume": "fixture-volume",
                       "writers": [("api", "api-id"), ("customerinsight", "ui-id")]}

    def test_success_restarts_only_originally_running_writers_and_preserves_package(self):
        docker = _BackupDocker(self.data)
        output = self.root / "backup.zip"
        with patch("tools.backup_workspace.source_deployment", return_value=self.source):
            result = backup_workspace(output=output, docker=docker)
        self.assertEqual(result["model_requests"], 0)
        self.assertTrue(output.is_file())
        restarted = {args[1] for args in docker.calls if args[0] == "start"}
        self.assertEqual(restarted, {"api-id", "ui-id"})
        self.assertFalse(any("worker" in str(args) for args in docker.calls))

    def test_failed_snapshot_restores_writers_and_does_not_publish(self):
        docker = _BackupDocker(self.data, fail_snapshot=True)
        output = self.root / "backup.zip"
        with patch("tools.backup_workspace.source_deployment", return_value=self.source), self.assertRaises(BackupError):
            backup_workspace(output=output, docker=docker)
        self.assertFalse(output.exists())
        self.assertEqual({args[1] for args in docker.calls if args[0] == "start"}, {"api-id", "ui-id"})

    def test_failed_stop_still_attempts_to_restore_that_writer(self):
        docker = _BackupDocker(self.data, fail_stop=True)
        with patch("tools.backup_workspace.source_deployment", return_value=self.source), self.assertRaises(BackupError):
            backup_workspace(output=self.root / "backup.zip", docker=docker)
        self.assertIn(("start", "ui-id"), docker.calls)

    def test_existing_package_not_overwritten_or_services_stopped(self):
        output = self.root / "backup.zip"
        output.write_bytes(b"retained")
        docker = _BackupDocker(self.data)
        with self.assertRaises(BackupError):
            backup_workspace(output=output, docker=docker)
        self.assertEqual(output.read_bytes(), b"retained")
        self.assertEqual(docker.calls, [])

    def test_active_jobs_refused_but_uncertain_jobs_retained(self):
        for section, status in (("analysis_jobs", "queued"), ("analysis_jobs", "running"),
                                ("analysis_groups", "leased"), ("analysis_groups", "sent")):
            with self.subTest(status=status), self.assertRaises(BackupError):
                reject_active_jobs({"statuses": {section: {status: 1}}})
        reject_active_jobs({"statuses": {"analysis_jobs": {"needs_review": 2}, "analysis_groups": {"uncertain": 2}}})

    def test_restore_validates_package_and_project_before_creating_any_resources(self):
        archive, _ = make_package(self.root)
        docker = _BackupDocker(self.data)
        with self.assertRaises(BackupError):
            restore_workspace(archive, project="production", evidence=self.root / "proof.json", docker=docker)
        self.assertEqual(docker.calls, [])
        archive.write_bytes(b"corrupt")
        with self.assertRaises(BackupError):
            restore_workspace(archive, project="customerinsight-restore-fixture", evidence=self.root / "proof.json", docker=docker)
        self.assertEqual(docker.calls, [])

    def test_cleanup_refuses_resources_with_wrong_nonce(self):
        docker = _BackupDocker(self.data)
        with self.assertRaises(BackupError):
            _cleanup(docker, "customerinsight-restore-fixture", [("volume", "fixture")], "expected-nonce")
        self.assertEqual(docker.calls, [])

    def test_docker_errors_do_not_disclose_credentials(self):
        from subprocess import CompletedProcess
        result = CompletedProcess(["docker"], 1, b"", b"password=SECRET and private review")
        with patch("tools.workspace_backup_common.subprocess.run", return_value=result):
            with self.assertRaises(BackupError) as failure:
                Docker().run("anything")
        self.assertNotIn("SECRET", str(failure.exception))
        self.assertNotIn("private review", str(failure.exception))


class _SourceDocker:
    def __init__(self):
        self.project = "fixture-production"
        labels = lambda service: {"com.docker.compose.project": self.project, "com.docker.compose.service": service}
        network = {"fixture-network": {"Aliases": ["postgres", "fixture-postgres"]}}
        self.containers = [
            {"Id": "api-id", "Image": "sha256:" + "a" * 64,
             "Config": {"Labels": labels("api"), "Env": [
                 "APP_DATABASE_URL=postgresql+psycopg://fixture:private@postgres:5432/fixture"]},
             "State": {"Running": True}, "NetworkSettings": {"Networks": network},
             "Mounts": [{"Destination": "/data", "Type": "volume", "Name": "fixture-data", "RW": True}]},
            {"Id": "pg-id", "Config": {"Labels": labels("postgres"), "Env": [
                "POSTGRES_USER=fixture", "POSTGRES_DB=fixture", "POSTGRES_PASSWORD=private"]},
             "State": {"Running": True}, "NetworkSettings": {"Networks": network},
             "Mounts": [{"Destination": "/var/lib/postgresql/data", "Type": "volume", "Name": "fixture-pg", "RW": True}]},
        ]
        self.volume_owner = self.project
        self.network_owner = self.project
        self.unknown_writer = False

    def ids(self, *args):
        if args[0] == "compose":
            return ["api-id", "pg-id"]
        if args[0] == "ps":
            return ["api-id", "external"] if self.unknown_writer else ["api-id"]
        return []

    def json(self, *args):
        if args[0] == "volume":
            return [{"Labels": {"com.docker.compose.project": self.volume_owner}}]
        if args[0] == "network":
            return [{"Labels": {"com.docker.compose.project": self.network_owner}}]
        result = [copy.deepcopy(item) for item in self.containers if item["Id"] in args]
        if "external" in args:
            result.append({"Id": "external", "Mounts": [{"Name": "fixture-data", "RW": True}]})
        return result


class BackupOwnershipTests(unittest.TestCase):
    def test_api_only_ui_without_data_mount_still_stopped_for_admission(self):
        docker = _SourceDocker()
        ui = {"Id": "ui-id", "Config": {"Labels": {
            "com.docker.compose.project": docker.project, "com.docker.compose.service": "customerinsight"},
            "Env": ["APP_UI_MODE=api"]}, "State": {"Running": True}, "Mounts": []}
        docker.containers.append(ui)
        original_ids = docker.ids
        docker.ids = lambda *args: ["api-id", "pg-id", "ui-id"] if args[0] == "compose" else original_ids(*args)
        self.assertIn(("customerinsight", "ui-id"), source_deployment(docker)["writers"])
        ui["Config"]["Env"] = []
        with self.assertRaises(BackupError):
            source_deployment(docker)

    def test_api_only_ui_cannot_mount_an_unknown_data_volume(self):
        docker = _SourceDocker()
        docker.containers.append({"Id": "ui-id", "Config": {"Labels": {
            "com.docker.compose.project": docker.project, "com.docker.compose.service": "customerinsight"},
            "Env": ["APP_UI_MODE=api"]}, "State": {"Running": True},
            "Mounts": [{"Destination": "/data", "Type": "volume", "Name": "someone-else"}]})
        original_ids = docker.ids
        docker.ids = lambda *args: ["api-id", "pg-id", "ui-id"] if args[0] == "compose" else original_ids(*args)
        with self.assertRaises(BackupError):
            source_deployment(docker)

    def test_source_project_and_volume_must_match(self):
        docker = _SourceDocker()
        result = source_deployment(docker)
        self.assertEqual(result["project"], "fixture-production")
        self.assertEqual(result["writers"], [("api", "api-id")])
        docker.volume_owner = "someone-else"
        with self.assertRaises(BackupError):
            source_deployment(docker)

    def test_source_database_must_belong_to_dumped_service(self):
        docker = _SourceDocker()
        docker.containers[0]["Config"]["Env"] = [
            "APP_DATABASE_URL=postgresql+psycopg://fixture:private@external-db.invalid:5432/fixture"]
        with self.assertRaises(BackupError):
            source_deployment(docker)

    def test_unknown_writer_to_same_data_volume_rejected(self):
        docker = _SourceDocker()
        docker.unknown_writer = True
        with self.assertRaises(BackupError):
            source_deployment(docker)

    def test_container_service_and_network_ownership_rejected(self):
        docker = _SourceDocker()
        docker.containers[1]["Config"]["Labels"]["com.docker.compose.project"] = "other-project"
        with self.assertRaises(BackupError):
            source_deployment(docker)
        docker = _SourceDocker()
        docker.network_owner = "other-project"
        with self.assertRaises(BackupError):
            source_deployment(docker)

    def test_existing_restore_project_refused(self):
        class Existing:
            def ids(self, *args):
                return ["occupied"]
        with self.assertRaises(BackupError):
            _assert_absent(Existing(), "customerinsight-restore-fixture", {"data": "new-data"})

    def test_restore_nonce_prevents_concurrent_volume_reuse(self):
        class Existing:
            def json(self, *args):
                return [{"Labels": {"com.docker.compose.project": "customerinsight-restore-fixture",
                                    "customerinsight.restore": "1",
                                    "customerinsight.restore.run": "other-run"}}]
        with self.assertRaises(BackupError):
            _verify_owned(Existing(), "volume", "data", "customerinsight-restore-fixture", "this-run")


if __name__ == "__main__":
    unittest.main()
