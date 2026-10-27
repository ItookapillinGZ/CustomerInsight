"""Streamlit to authenticated API demo journey, with isolated real stores."""

from contextlib import ExitStack
import json
import os
import unittest
from unittest.mock import patch

import httpx
from streamlit.testing.v1 import AppTest

from backend.client import JobApiClient
from core.config import Config
from tests import test_demo_api as api_fixtures
from tests.test_customer_app import APP_PATH


class DemoRemoteUiTests(unittest.TestCase):
    def setUp(self):
        # Reuse the fixture method without importing TestCase classes into this
        # module, which would cause unittest to discover the API suite twice.
        api_fixtures.DemoWorkspaceApiTests.setUp(self)
        self.server = self.app
        self.budget.daily_limit = 100
        self.traffic = []
        self.import_responses = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "APP_DATA_DIR": str(self.root / "ui-only"), "APP_UI_MODE": "api",
            "APP_ANALYSIS_MODE": "queued", "APP_DATABASE_URL": "",
            "APP_API_URL": "http://demo-test:8000", "APP_API_TOKEN": "unused-deployment-token",
        }))
        self.stack.enter_context(patch.object(Config, "OPENAI_API_KEY", "offline-fixture-key"))
        self.stack.enter_context(patch("streamlit.delta_generator._maybe_print_use_warning"))
        self.stack.enter_context(patch("streamlit.file_uploader", return_value=None))
        self.local_history = self.stack.enter_context(patch("core.history_store.HistoryStore",
            side_effect=AssertionError("API UI must not open local history")))
        self.local_analysis = self.stack.enter_context(patch("core.product_service.analyze_customer_rows",
            side_effect=AssertionError("API UI must not call the local model service")))
        self.local_agent = self.stack.enter_context(patch("core.product_service.run_customer_report_agent",
            side_effect=AssertionError("API UI must not call the local agent service")))

        def bridge(request):
            self.traffic.append((request.method, request.url.path, request.content))
            response = self.client.request(request.method, request.url.raw_path.decode("ascii"),
                                           headers=dict(request.headers), content=request.content)
            if request.method == "POST" and request.url.path == "/api/v1/demo-workspace" and response.status_code == 200:
                self.import_responses.append(response.json())
            return httpx.Response(response.status_code, headers=dict(response.headers), content=response.content)

        self.transport = httpx.MockTransport(bridge)
        client_type = JobApiClient
        self.stack.enter_context(patch("backend.client.JobApiClient",
            side_effect=lambda base, token: client_type(base, token, transport=self.transport)))

    def new_ui(self):
        ui = AppTest.from_file(str(APP_PATH), default_timeout=45).run()
        self.assertFalse(ui.exception)
        return ui

    def public_example(self):
        ui = self.new_ui()
        ui.button(key="customer_public_demo").click().run()
        self.assertFalse(ui.exception)
        self.assertEqual(ui.session_state["customer_demo_report"]["data_origin"], "synthetic")
        return ui

    def login_ui(self, ui):
        next(item for item in ui.text_input if item.label == "用户名").set_value(self.alice["username"])
        next(item for item in ui.text_input if item.label == "密码").set_value(api_fixtures.PASSWORD)
        next(button for button in ui.button if button.label == "登录").click().run()
        self.assertFalse(ui.exception)
        self.assertEqual(ui.session_state["ci_account_user"]["workspace_id"], self.alice["workspace_id"])
        # Login clears all widget state. AppTest may retain the obsolete public
        # login widgets in its element tree; reload with only the actual server
        # session, as a fresh authenticated browser view would do.
        authenticated = AppTest.from_file(str(APP_PATH), default_timeout=45)
        for key in ("ci_account_token", "ci_account_user"):
            authenticated.session_state[key] = ui.session_state[key]
        authenticated.run()
        self.assertFalse(authenticated.exception)
        return authenticated

    def assert_no_local_model_or_history(self):
        self.analysis.assert_not_called()
        self.local_history.assert_not_called()
        self.local_analysis.assert_not_called()
        self.local_agent.assert_not_called()

    def test_public_example_and_two_turn_replay_need_neither_login_nor_api_calls(self):
        ui = self.public_example()
        self.assertTrue(ui.button(key="customer_demo_activate").disabled)
        for count in (1, 2):
            ui.button(key="customer_demo_replay").click().run()
            self.assertFalse(ui.exception)
            self.assertEqual(ui.session_state["customer_demo_replay_count"], count)
        self.assertEqual(self.traffic, [])
        self.assertEqual(self.import_responses, [])
        self.agent.assert_not_called()
        self.assert_no_local_model_or_history()
        self.assertFalse((self.root / "ui-only").exists())

    def test_login_then_explicit_import_uses_real_ids_and_separate_chat_consent(self):
        ui = self.login_ui(self.public_example())
        # Login discards prior view state. Reopening the public fixture is still
        # free, and saving it remains a separate explicit action.
        ui.button(key="customer_view_workspace").click().run()
        self.assertFalse(ui.exception)
        self.assertFalse(ui.button(key="customer_demo_activate").disabled)
        self.assertEqual(self.import_responses, [])
        self.agent.assert_not_called()
        ui.button(key="customer_demo_activate").click().run()
        self.assertFalse(ui.exception)
        self.assertEqual(len(self.import_responses), 1)
        imported = self.import_responses[0]
        current, previous = imported["current_batch_id"], imported["previous_batch_id"]
        self.assertRegex(current, r"^[0-9a-f]{32}$")
        self.assertRegex(previous, r"^[0-9a-f]{32}$")
        self.assertEqual(ui.session_state["customer_active_batch_id"], current)
        self.assertEqual(ui.session_state["customer_previous_batch_id"], previous)
        report = ui.session_state["customer_report"]
        self.assertEqual(report["historical_context"]["batch"]["id"], previous)
        self.assertTrue(report["historical_context"]["comparison"]["topics_comparable"])
        self.assertEqual(imported["model_calls"], 0)
        self.assertFalse(ui.checkbox(key="customer_agent_consent").value)
        self.assertTrue(ui.button(key="customer_agent_generate").disabled)
        ui.button(key="customer_agent_suggestion_0").click().run()
        self.assertFalse(ui.exception)
        self.assertTrue(ui.text_area(key="customer_agent_question").value)
        self.assertFalse(ui.checkbox(key="customer_agent_consent").value)
        self.assertTrue(ui.button(key="customer_agent_generate").disabled)
        self.agent.assert_not_called()
        self.assert_no_local_model_or_history()

        # Checking consent alone and changing the question do not submit it.
        question = "这些配送问题集中在哪个商品，和上批有什么变化？"
        ui.text_area(key="customer_agent_question").set_value(question).run()
        ui.checkbox(key="customer_agent_consent").check().run()
        self.assertFalse(ui.exception)
        self.agent.assert_not_called()
        self.assertFalse(ui.button(key="customer_agent_generate").disabled)
        ui.button(key="customer_agent_generate").click().run()
        self.assertFalse(ui.exception)
        self.agent.assert_called_once()
        self.assertEqual(self.agent.call_args.args[0]["historical_context"]["batch"]["id"], previous)
        self.assertEqual(self.agent.call_args.args[1], question)
        calls = [(path, json.loads(body)) for method, path, body in self.traffic
                 if method == "POST" and path.endswith("/chat")]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], f"/api/v1/batches/{current}/chat")
        self.assertEqual(calls[0][1]["comparison_id"], previous)
        self.assertTrue(calls[0][1]["allow_model_call"])
        service, _jobs = self.server.state.tenants.for_workspace(self.alice["workspace_id"])
        self.assertEqual(len(service.history.load_chat(current)), 1)
        self.assert_no_local_model_or_history()


if __name__ == "__main__":
    unittest.main()
