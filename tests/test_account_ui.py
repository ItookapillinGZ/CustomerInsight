"""Offline account UI and tenant selection checks; no database/model traffic."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from backend.accounts import AccountError
from backend.client import JobApiClient, JobClientError
from tests.test_customer_app import APP_PATH


PRINCIPAL = {"id": "a" * 32, "username": "alice", "workspace_id": "b" * 32}
SESSION_TOKEN = "SESSION_TOKEN_PRIVATE_" + "s" * 44


class AccountUiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(dir=base))
        self.stack.enter_context(patch.dict("os.environ", {
            "APP_DATA_DIR": directory, "APP_ANALYSIS_MODE": "queued", "APP_UI_MODE": "api",
            "APP_API_URL": "http://api:8000", "APP_API_TOKEN": "DEPLOYMENT_TOKEN_MUST_NOT_BE_USED",
        }))
        self.stack.enter_context(patch("streamlit.delta_generator._maybe_print_use_warning"))
        self.stack.enter_context(patch("backend.database.database_enabled", return_value=True))
        self.accounts = Mock()
        self.accounts.authenticate.return_value = PRINCIPAL.copy()
        self.stack.enter_context(patch("backend.accounts.AccountService", return_value=self.accounts))
        self.history = Mock()
        self.history.list_batches.return_value = []
        self.history.get_mapping.return_value = None
        self.history.root_dir = Path(directory) / "workspaces" / PRINCIPAL["workspace_id"]
        self.history_factory = self.stack.enter_context(patch("backend.pg_history.PgHistoryStore", return_value=self.history))
        self.client = Mock(spec=JobApiClient)
        self.client.list_jobs.return_value = {"items": [], "total": 0}
        self.client.list_batches.return_value = {"items": [], "total": 0}
        self.client.me.return_value = PRINCIPAL.copy()
        self.client.status.return_value = {"configured": True, "remaining_today": 50, "daily_limit": 50,
                                           "request_batch_size": 10, "max_batch_characters": 12000}
        self.client.login.return_value = {"access_token": SESSION_TOKEN, "user": PRINCIPAL.copy()}
        self.client_factory = self.stack.enter_context(patch("backend.client.JobApiClient", return_value=self.client))
        self.upload = self.stack.enter_context(patch("streamlit.file_uploader", return_value=None))
        self.stack.enter_context(patch("core.product_service.get_service_status", return_value={
            "configured": True, "remaining_today": 50, "daily_limit": 50,
            "request_batch_size": 10, "max_batch_characters": 12000,
        }))
        self.analyzer = self.stack.enter_context(patch("core.product_service.analyze_customer_rows"))
        self.agent = self.stack.enter_context(patch("core.product_service.run_customer_report_agent"))

    def app(self, *, authenticated=False, stale=False):
        app = AppTest.from_file(str(APP_PATH), default_timeout=25)
        if authenticated:
            app.session_state["ci_account_token"] = SESSION_TOKEN
            app.session_state["ci_account_user"] = PRINCIPAL.copy()
        if stale:
            app.session_state["customer_report"] = {"PRIVATE_REPORT_MARKER": True}
            app.session_state["customer_agent_history"] = [{"question": "PRIVATE_CHAT_MARKER"}]
            app.session_state["customer_file_digest"] = "PRIVATE_UPLOAD_MARKER"
            app.session_state["customer_upload_bytes"] = b"PRIVATE_RAW_BYTES"
            app.session_state["customer_backup_blob"] = b"PRIVATE_BACKUP_BYTES"
        app.run()
        self.assertFalse(app.exception)
        return app

    def assert_old_data_gone(self, app):
        for key in ("customer_report", "customer_agent_history", "customer_file_digest",
                    "customer_upload_bytes", "customer_backup_blob"):
            self.assertNotIn(key, app.session_state)

    def login(self, app):
        app.text_input[0].set_value("alice")
        app.text_input[1].set_value("correct-password-123")
        next(button for button in app.button if button.label == "登录").click().run()
        self.assertFalse(app.exception)
        return app

    def test_logged_out_never_renders_upload_history_or_cached_private_report(self):
        app = self.app(stale=True)
        self.upload.assert_not_called()
        self.history_factory.assert_not_called()
        self.client.list_jobs.assert_not_called()
        self.assertFalse(any(element.label == "历史报告" for element in app.expander))
        self.assertFalse(any("PRIVATE_REPORT_MARKER" in element.value for element in app.markdown))
        self.assertTrue(any(button.label == "登录" for button in app.button))

    def test_successful_login_keeps_server_session_and_discards_previous_data(self):
        app = self.login(self.app(stale=True))
        self.client.login.assert_called_once_with("alice", "correct-password-123")
        self.assertEqual(app.session_state["ci_account_token"], SESSION_TOKEN)
        self.assertEqual(app.session_state["ci_account_user"], PRINCIPAL)
        self.assert_old_data_gone(app)
        self.assertFalse(any(SESSION_TOKEN in element.value for element in app.markdown))
        self.assertFalse(any(SESSION_TOKEN in element.value for element in app.caption))
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()

    def test_logout_revokes_server_session_then_discards_all_private_state(self):
        app = self.app(authenticated=True)
        app.session_state["customer_report"] = {"PRIVATE_REPORT_MARKER": True}
        app.session_state["customer_agent_history"] = [{"question": "PRIVATE_CHAT_MARKER"}]
        app.session_state["customer_upload_bytes"] = b"PRIVATE_RAW_BYTES"
        app.button(key="ci_account_logout").click().run()
        self.assertFalse(app.exception)
        self.client.logout.assert_called_once_with()
        self.assertNotIn("ci_account_token", app.session_state)
        self.assertNotIn("ci_account_user", app.session_state)
        self.assert_old_data_gone(app)
        self.assertTrue(any(button.label == "登录" for button in app.button))

    def test_expired_session_clears_old_report_before_render(self):
        self.client.me.side_effect = JobClientError("expired", status_code=401)
        app = self.app(authenticated=True, stale=True)
        self.assert_old_data_gone(app)
        self.assertNotIn("ci_account_token", app.session_state)
        self.upload.assert_not_called()
        self.history_factory.assert_not_called()
        self.client.list_jobs.assert_not_called()

    def test_workspace_comes_from_authenticated_principal_not_client_state(self):
        app = AppTest.from_file(str(APP_PATH), default_timeout=25)
        app.session_state["ci_account_token"] = SESSION_TOKEN
        app.session_state["ci_account_user"] = {**PRINCIPAL, "workspace_id": "f" * 32}
        app.run()
        self.assertFalse(app.exception)
        self.history_factory.assert_not_called()
        self.accounts.authenticate.assert_not_called()
        self.client.me.assert_called_with()
        self.client.list_batches.assert_called()

    def test_job_client_receives_account_session_not_shared_deployment_token(self):
        self.app(authenticated=True)
        self.assertTrue(self.client_factory.called)
        for call in self.client_factory.call_args_list:
            self.assertEqual(call.args, ("http://api:8000", SESSION_TOKEN))
        self.client.list_jobs.assert_called()

    def _password(self, app, current, first, second):
        next(item for item in app.text_input if item.label == "当前密码").set_value(current)
        next(item for item in app.text_input if item.label == "新密码（至少 12 位）").set_value(first)
        next(item for item in app.text_input if item.label == "再次输入新密码").set_value(second)
        next(button for button in app.button if button.label == "更新密码并退出").click().run()
        self.assertFalse(app.exception)

    def test_password_mismatch_does_not_send_change_request(self):
        app = self.app(authenticated=True)
        self._password(app, "current-password-123", "new-password-123", "different-password-123")
        self.client.change_password.assert_not_called()
        self.assertIn("ci_account_token", app.session_state)
        self.assertTrue(any("两次输入一致" in item.value for item in app.error))

    def test_successful_password_change_clears_session_and_old_private_data(self):
        app = self.app(authenticated=True)
        app.session_state["customer_upload_bytes"] = b"PRIVATE_RAW_BYTES"
        self._password(app, "current-password-123", "new-password-123", "new-password-123")
        self.client.change_password.assert_called_once_with("current-password-123", "new-password-123")
        self.assertNotIn("ci_account_token", app.session_state)
        self.assertNotIn("ci_account_user", app.session_state)
        self.assert_old_data_gone(app)
        self.assertTrue(any(button.label == "登录" for button in app.button))


if __name__ == "__main__":
    unittest.main()
