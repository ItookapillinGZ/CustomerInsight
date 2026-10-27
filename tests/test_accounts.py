"""Password, durable sessions, shared login budgets and tenant-path tests."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select, update

from backend.accounts import (
    AccountError, AccountService, AccountSession, LOGIN_IP_LIMIT,
    LOGIN_USERNAME_LIMIT, LOGIN_WINDOW_SECONDS, LoginThrottle, SESSION_SECONDS, User,
)
from backend.database import Base, DatabaseConfigurationError, database_enabled, get_engine, workspace_root
from tests.helpers import offline_temp_root


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.addCleanup(self.directory.cleanup)
        self.engine = get_engine("sqlite:///" + str(Path(self.directory.name) / "accounts.sqlite3"))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.now = 10_000.0
        self.service = AccountService(self.engine, clock=lambda: self.now)
        self.password = "correct horse battery staple"

    def make_user(self, name="alice"):
        return self.service.create_user(name, self.password)

    def assert_account_error(self, status, code, callback):
        with self.assertRaises(AccountError) as raised:
            callback()
        self.assertEqual((raised.exception.status_code, raised.exception.code), (status, code))
        return raised.exception.message

    def test_password_hash_and_session_digest_only_are_persisted(self):
        user = self.make_user("Alice")
        self.assertEqual(user["username"], "alice")
        self.assertEqual(len(user["workspace_id"]), 32)
        result = self.service.login(" ALICE ", self.password, client_ip="192.0.2.1")
        self.assertEqual(self.service.authenticate(result["access_token"]), user)
        self.assertEqual(result["expires_at"], self.now + SESSION_SECONDS)
        with self.engine.connect() as connection:
            stored = connection.execute(select(User.__table__)).mappings().one()
            session = connection.execute(select(AccountSession.__table__)).mappings().one()
        self.assertTrue(stored["password_hash"].startswith("$argon2id$"))
        self.assertNotIn(self.password, stored["password_hash"])
        self.assertEqual(session["token_hash"], hashlib.sha256(result["access_token"].encode()).hexdigest())
        self.assertNotIn(result["access_token"], repr(session))

    def test_unique_user_creation_is_atomic_and_workspace_is_private(self):
        first = self.make_user()
        self.assert_account_error(409, "username_unavailable", lambda: self.make_user("Alice"))
        second = self.make_user("bob")
        self.assertNotEqual(first["workspace_id"], second["workspace_id"])
        self.assertEqual(self.service.get_user("ALICE"), first)
        self.assertIsNone(self.service.get_user("missing"))
        from backend.accounts import Workspace
        with self.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(Workspace.__table__)).all()), 2)

    def test_input_validation_does_not_silently_weaken_password_rules(self):
        for name in ("aa", "../alice", "a@b.com", "a" * 65, None):
            self.assert_account_error(422, "invalid_username", lambda: self.make_user(name))
        for password in ("short", "a" * 257, None):
            self.assert_account_error(422, "invalid_password", lambda: self.service.create_user("alice", password))

    def test_unknown_wrong_and_disabled_accounts_share_error(self):
        self.make_user()
        messages = [self.assert_account_error(401, "invalid_credentials", callback) for callback in (
            lambda: self.service.login("missing", self.password),
            lambda: self.service.login("alice", "incorrect password"),
            lambda: self.service.login("../invalid", self.password),
        )]
        with self.engine.begin() as connection:
            connection.execute(update(User).values(active=False))
        messages.append(self.assert_account_error(401, "invalid_credentials", lambda: self.service.login("alice", self.password)))
        self.assertEqual(len(set(messages)), 1)

    def test_sessions_are_rechecked_for_expiry_revocation_and_deactivation(self):
        self.make_user()
        first = self.service.login("alice", self.password)["access_token"]
        second = self.service.login("alice", self.password)["access_token"]
        self.service.logout(first)
        self.service.logout(first)
        self.assert_account_error(401, "invalid_credentials", lambda: self.service.authenticate(first))
        self.assertEqual(self.service.authenticate(second)["username"], "alice")
        with self.engine.begin() as connection:
            connection.execute(update(User).values(active=False))
        self.assert_account_error(401, "invalid_credentials", lambda: self.service.authenticate(second))
        with self.engine.begin() as connection:
            connection.execute(update(User).values(active=True))
        self.now += SESSION_SECONDS
        self.assert_account_error(401, "invalid_credentials", lambda: self.service.authenticate(second))

    def test_opaque_tokens_are_unique_and_invalid_tokens_are_rejected(self):
        self.make_user()
        tokens = {self.service.login("alice", self.password)["access_token"] for _ in range(4)}
        self.assertEqual(len(tokens), 4)
        for token in (None, "", "x" * 63, "x" * 65, "x" * 63 + "!", "x" * 64):
            self.assert_account_error(401, "invalid_credentials", lambda: self.service.authenticate(token))
        self.service.logout("invalid")

    def test_username_budget_is_shared_across_service_instances_and_ips(self):
        other = AccountService(self.engine, clock=lambda: self.now)
        with patch("backend.accounts.PasswordHasher.verify", return_value=False) as verify:
            for number in range(LOGIN_USERNAME_LIMIT):
                chosen = self.service if number % 2 else other
                self.assert_account_error(401, "invalid_credentials", lambda: chosen.login("missing", self.password, client_ip=str(number)))
            self.assert_account_error(429, "login_rate_limited", lambda: other.login("missing", self.password, client_ip="new-ip"))
            self.assertEqual(verify.call_count, LOGIN_USERNAME_LIMIT)
            self.now += LOGIN_WINDOW_SECONDS
            self.assert_account_error(401, "invalid_credentials", lambda: other.login("missing", self.password, client_ip="new-ip"))

    def test_ip_budget_stops_random_username_hashing_and_record_growth(self):
        with patch("backend.accounts.PasswordHasher.verify", return_value=False) as verify:
            for number in range(LOGIN_IP_LIMIT):
                self.assert_account_error(401, "invalid_credentials", lambda: self.service.login(f"unknown{number}", self.password, client_ip="shared-ip"))
            for number in range(10):
                self.assert_account_error(429, "login_rate_limited", lambda: self.service.login(f"blocked{number}", self.password, client_ip="shared-ip"))
            self.assertEqual(verify.call_count, LOGIN_IP_LIMIT)
        with self.engine.connect() as connection:
            throttles = connection.execute(select(LoginThrottle.__table__)).mappings().all()
        self.assertEqual(len(throttles), LOGIN_IP_LIMIT + 1)
        self.assertNotIn("shared-ip", repr(throttles))
        self.assertNotIn("unknown", repr(throttles))

    def test_concurrent_login_budget_cannot_oversubscribe(self):
        def attempt(number):
            try:
                self.service.login("missing", self.password, client_ip=f"ip-{number}")
            except AccountError as error:
                return error.status_code
        with patch("backend.accounts.PasswordHasher.verify", return_value=False):
            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(attempt, range(20)))
        self.assertEqual(results.count(401), LOGIN_USERNAME_LIMIT)
        self.assertEqual(results.count(429), 20 - LOGIN_USERNAME_LIMIT)

    def test_inactive_user_existing_session_cannot_authenticate(self):
        user = self.make_user()
        token = self.service.login("alice", self.password)["access_token"]
        with self.engine.begin() as connection:
            connection.execute(update(User).where(User.id == user["id"]).values(active=False))
        self.assert_account_error(401, "invalid_credentials", lambda: self.service.authenticate(token))

    def test_password_change_revokes_all_existing_sessions(self):
        self.make_user()
        tokens = [self.service.login("alice", self.password)["access_token"] for _ in range(2)]
        new_password = "new correct horse battery staple"
        self.assert_account_error(401, "invalid_credentials", lambda: self.service.change_password(
            tokens[0], new_password, current_password="incorrect old password"))
        self.assertEqual(self.service.authenticate(tokens[0])["username"], "alice")
        self.service.change_password(tokens[0], new_password, current_password=self.password)
        for token in tokens:
            self.assert_account_error(401, "invalid_credentials", lambda: self.service.authenticate(token))
        self.assert_account_error(401, "invalid_credentials", lambda: self.service.login("alice", self.password))
        self.assertEqual(self.service.login("alice", new_password)["user"]["username"], "alice")


class DatabasePathTests(unittest.TestCase):
    def test_environment_database_is_postgres_only_and_errors_hide_connection(self):
        with patch.dict(os.environ, {"APP_DATABASE_URL": ""}):
            self.assertFalse(database_enabled())
            with self.assertRaises(DatabaseConfigurationError):
                get_engine()
        for value in ("sqlite:///:memory:", "not-a-url-secret", "mysql://user:secret@host/db"):
            with patch.dict(os.environ, {"APP_DATABASE_URL": value}):
                self.assertTrue(database_enabled())
                with self.assertRaises(DatabaseConfigurationError) as error:
                    get_engine()
                self.assertNotIn("secret", str(error.exception))

    def test_workspace_path_only_accepts_uuid_hex_and_does_not_share_dirs(self):
        with tempfile.TemporaryDirectory(dir=offline_temp_root()) as directory:
            first = workspace_root("a" * 32, directory)
            second = workspace_root("b" * 32, directory)
            self.assertEqual(first.parent, Path(directory).resolve() / "workspaces")
            self.assertNotEqual(first, second)
            for value in ("../outside", "A" * 32, "a" * 31, "a" * 33, None):
                with self.assertRaises(DatabaseConfigurationError):
                    workspace_root(value, directory)

    def test_workspace_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=offline_temp_root()) as directory:
            outside = Path(directory) / "outside"
            outside.mkdir()
            base = Path(directory) / "root"
            base.mkdir()
            try:
                (base / "workspaces").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation is not supported for this account")
            with self.assertRaises(DatabaseConfigurationError):
                workspace_root("a" * 32, base)


@unittest.skipUnless(os.getenv("TEST_PG_URL"), "Set TEST_PG_URL for isolated PostgreSQL acceptance")
class PostgresAccountTests(AccountTests):
    """Run the same contract against a fresh, explicitly migrated PG schema."""

    def setUp(self):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy.engine import make_url
        self.directory = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.addCleanup(self.directory.cleanup)
        self.admin_engine = get_engine(os.environ["TEST_PG_URL"])
        self.schema = "account_test_" + uuid4().hex
        with self.admin_engine.begin() as connection:
            connection.exec_driver_sql('CREATE SCHEMA "' + self.schema + '"')
        self.addCleanup(self.drop_schema)
        url = make_url(os.environ["TEST_PG_URL"]).update_query_dict({"options": "-csearch_path=" + self.schema})
        self.engine = get_engine(url.render_as_string(hide_password=False))
        self.addCleanup(self.engine.dispose)
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        self.now = 10_000.0
        self.service = AccountService(self.engine, clock=lambda: self.now)
        self.password = "correct horse battery staple"

    def drop_schema(self):
        with self.admin_engine.begin() as connection:
            connection.exec_driver_sql('DROP SCHEMA "' + self.schema + '" CASCADE')

    def test_frozen_alembic_baseline_matches_registered_metadata(self):
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        with self.engine.connect() as connection:
            difference = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        self.assertEqual(difference, [])

    def test_alembic_baseline_can_downgrade_and_reapply_in_disposable_schema(self):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import inspect
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "base")
            self.assertEqual(inspect(connection).get_table_names(), ["alembic_version"])
            command.upgrade(config, "head")
            self.assertEqual(len(inspect(connection).get_table_names()), len(Base.metadata.tables) + 1)


if __name__ == "__main__":
    unittest.main()
