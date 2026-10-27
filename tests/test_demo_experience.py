"""Offline end-to-end demo, evidence focus, replay and explicit live handoff."""
import unittest
from unittest.mock import patch

from core.history_store import HistoryStore
from core.report_interactions import focus_rows
from tests import test_customer_app as customer_test_support


class DemoExperienceTests(unittest.TestCase):
    setUp = customer_test_support.CustomerAppTests.setUp
    new_app = customer_test_support.CustomerAppTests.new_app

    def open_demo(self):
        app = self.new_app().run()
        app.button(key="customer_view_workspace").click().run()
        self.assertEqual(len(app.exception), 0)
        return app

    def test_complete_demo_opens_without_a_key_request_or_history_write(self):
        self.status.return_value["configured"] = False
        app = self.open_demo()
        workspace = app.session_state["customer_demo_workspace"]
        self.assertEqual(workspace["current_report"]["metrics"]["total_rows"], 30)
        self.assertEqual(workspace["previous_report"]["metrics"]["total_rows"], 30)
        self.assertEqual(app.session_state["customer_demo_replay_count"], 0)
        self.assertEqual(len(HistoryStore().list_batches()), 0)
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()

    def test_two_replay_turns_show_charts_evidence_and_downloadable_brief(self):
        app = self.open_demo()
        app.button(key="customer_demo_replay").click().run()
        app.button(key="customer_demo_replay").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["customer_demo_replay_count"], 2)
        second = app.session_state["customer_demo_workspace"]["replay_turns"][1]
        self.assertTrue(second["historical_evidence"])
        self.assertTrue(second["charts"])
        self.assertEqual(second["model_calls"], 0)
        self.assertEqual(second["answer_origin"], "fallback")
        app.button(key="customer_agent_brief_add_1").click().run()
        self.assertEqual(len(app.exception), 0)
        labels = {item.label for item in app.get("download_button")}
        self.assertIn("下载经营简报（含问答、图表与原文）", labels)
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()

    def test_adding_regenerated_answer_replaces_previous_version(self):
        app = self.open_demo()
        app.button(key="customer_demo_replay").click().run()
        answer = dict(app.session_state["customer_demo_workspace"]["replay_turns"][0])
        answer["conclusion"] = "较早的同题回答"
        app.session_state["customer_agent_brief"] = [answer]
        app.button(key="customer_agent_brief_add_0").click().run()
        saved = app.session_state["customer_agent_brief"]
        self.assertEqual(len(saved), 1)
        self.assertNotEqual(saved[0]["conclusion"], "较早的同题回答")

    def test_focus_and_prefill_are_local_and_do_not_change_batch_metrics(self):
        app = self.open_demo()
        total = app.session_state["customer_demo_report"]["metrics"]["total_rows"]
        app.button(key="customer_topic_focus_0").click().run()
        focus = app.session_state["customer_focus"]
        selected = focus_rows(app.session_state["customer_demo_report"], focus)
        self.assertGreater(len(selected), 0)
        self.assertLess(len(selected), total)
        app.button(key="customer_focus_ask").click().run()
        self.assertIn("原表第", app.session_state["customer_agent_question"])
        self.assertEqual(app.session_state["customer_demo_report"]["metrics"]["total_rows"], total)
        app.button(key="customer_focus_clear").click().run()
        self.assertNotIn("customer_focus", app.session_state)
        self.agent.assert_not_called()

    def test_explicit_save_hands_off_to_private_live_chat_without_model_call(self):
        app = self.open_demo()
        app.button(key="customer_demo_activate").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("customer_demo_workspace", app.session_state)
        self.assertEqual(app.session_state["customer_report"]["data_origin"], "synthetic")
        self.assertEqual(len(HistoryStore().list_batches()), 2)
        self.assertEqual(len(app.session_state["customer_active_batch_id"]), 32)
        self.assertEqual(len(app.session_state["customer_previous_batch_id"]), 32)
        self.assertFalse(app.checkbox(key="customer_agent_consent").value)
        self.assertEqual(len(app.chat_message), 0)
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()
        app.button(key="customer_agent_suggestion_0").click().run()
        self.assertIn("原文", app.text_area(key="customer_agent_question").value)
        self.agent.assert_not_called()

    def test_public_demo_is_available_without_opening_private_history(self):
        with patch("backend.client.api_ui_enabled", return_value=True), \
             patch("backend.client.JobApiClient.me", side_effect=AssertionError("private access")), \
             patch("backend.client.JobApiClient.list_batches", side_effect=AssertionError("private access")):
            app = self.new_app().run()
            app.button(key="customer_public_demo").click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertIn("customer_demo_workspace", app.session_state)
            self.assertTrue(app.button(key="customer_demo_activate").disabled)
            self.agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
