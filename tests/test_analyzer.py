from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import APIConnectionError, BadRequestError
from pydantic import ValidationError

from ai.analyzer import StructuredOutputError, analyze_feedback
from ai.client import get_openai_client
from core.config import Config
from models.models import CustomerFeedbackAnalysis, FeedbackAnalysis
from tests.helpers import fake_client, fake_response, valid_analysis


class ModelValidationTests(unittest.TestCase):
    def test_original_single_feedback_model_remains_usable(self):
        original = FeedbackAnalysis(
            sentiment="negative", actionable_items=valid_analysis()["actionable_items"]
        )
        self.assertEqual(set(original.model_dump()), {"sentiment", "actionable_items"})
        self.assertEqual(original.actionable_items[0].category, "bugs")

    def test_output_enums_reject_arbitrary_model_labels(self):
        for field, value in [("sentiment", "mixed"), ("category", "billing"), ("priority", "urgent")]:
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    CustomerFeedbackAnalysis.model_validate(valid_analysis(**{field: value}))

    def test_missing_summary_blank_text_and_too_many_actions_are_rejected(self):
        missing = valid_analysis()
        del missing["summary"]
        bad_action = valid_analysis()["actionable_items"][0].copy()
        bad_action["reasoning"] = "   "
        for payload in [
            missing,
            valid_analysis(summary="   "),
            valid_analysis(actionable_items=[bad_action]),
            valid_analysis(actionable_items=valid_analysis()["actionable_items"] * 6),
        ]:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    CustomerFeedbackAnalysis.model_validate(payload)


class SingleFeedbackTests(unittest.TestCase):
    def setUp(self):
        transport = patch.object(Config, "API_MODE", "responses")
        transport.start()
        self.addCleanup(transport.stop)

    def test_analysis_validates_response_and_keeps_original_output_fields(self):
        client = fake_client()
        result = analyze_feedback("Checkout fails", client=client)
        self.assertIsInstance(result, CustomerFeedbackAnalysis)
        self.assertEqual(result.sentiment, "negative")
        self.assertEqual(result.category, "bugs")
        self.assertEqual(result.priority, "high")
        self.assertEqual(result.actionable_items[0].action, "Fix the checkout failure.")
        self.assertEqual(client.responses.parse.call_count, 1)
        kwargs = client.responses.parse.call_args.kwargs
        self.assertIs(kwargs["text_format"], CustomerFeedbackAnalysis)
        self.assertIn("Checkout fails", str(kwargs["input"]))
        client.close.assert_not_called()

    def test_single_argument_api_creates_and_closes_one_client(self):
        client = fake_client()
        with patch("ai.analyzer.get_openai_client", return_value=client) as factory:
            result = analyze_feedback("Checkout fails")
        factory.assert_called_once_with()
        client.close.assert_called_once_with()
        self.assertEqual(result.sentiment, "negative")
        self.assertTrue(result.actionable_items)

    def test_no_parsed_output_and_incomplete_output_are_errors(self):
        client = fake_client()
        for response in [
            fake_response(output_parsed=None),
            fake_response(status="incomplete", incomplete_details=SimpleNamespace(reason="max_output_tokens")),
            fake_response(
                output_parsed=None,
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal", refusal="Declined")])],
            ),
        ]:
            with self.subTest(response=response):
                client.responses.parse.return_value = response
                with self.assertRaises(StructuredOutputError):
                    analyze_feedback("Checkout fails", client=client)

    def test_even_preconstructed_models_are_revalidated(self):
        client = fake_client()
        client.responses.parse.return_value = fake_response(
            CustomerFeedbackAnalysis.model_construct(**valid_analysis(sentiment="invented"))
        )
        with self.assertRaises(ValidationError):
            analyze_feedback("Checkout fails", client=client)

    def test_malformed_json_fields_are_errors_not_normal_categories(self):
        client = fake_client()
        for payload in [valid_analysis(sentiment="invented"), {"sentiment": "neutral"}, "not-json"]:
            with self.subTest(payload=payload):
                client.responses.parse.return_value = fake_response(payload)
                with self.assertRaises(ValidationError):
                    analyze_feedback("Checkout fails", client=client)

    def test_transient_errors_retry_with_exponential_backoff(self):
        client = fake_client()
        error = APIConnectionError(request=httpx.Request("POST", "https://example.invalid/v1/responses"))
        client.responses.parse.side_effect = [error, error, fake_response()]
        with patch.object(Config, "MAX_RETRIES", 2), patch.object(Config, "RETRY_BACKOFF_SECONDS", 1.0), patch("ai.analyzer.time.sleep") as sleep:
            result = analyze_feedback("Checkout fails", client=client)
        self.assertEqual(result.sentiment, "negative")
        self.assertEqual(client.responses.parse.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])

    def test_retry_limit_is_bounded_and_permanent_errors_are_not_retried(self):
        client = fake_client()
        connection_error = APIConnectionError(request=httpx.Request("POST", "https://example.invalid/v1/responses"))
        client.responses.parse.side_effect = connection_error
        with patch.object(Config, "MAX_RETRIES", 2), patch("ai.analyzer.time.sleep") as sleep:
            with self.assertRaises(APIConnectionError):
                analyze_feedback("Checkout fails", client=client)
        self.assertEqual(client.responses.parse.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

        client.responses.parse.reset_mock()
        response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid/v1/responses"))
        client.responses.parse.side_effect = BadRequestError("Invalid request", response=response, body={})
        with patch("ai.analyzer.time.sleep") as sleep:
            with self.assertRaises(BadRequestError):
                analyze_feedback("Checkout fails", client=client)
        self.assertEqual(client.responses.parse.call_count, 1)
        sleep.assert_not_called()

    def test_blank_or_nontext_input_does_not_make_an_sdk_call(self):
        client = fake_client()
        for text in ["", " \t ", None, 123]:
            with self.subTest(text=text):
                with self.assertRaises((ValueError, TypeError)):
                    analyze_feedback(text, client=client)
        client.responses.parse.assert_not_called()

    def test_single_feedback_length_limit_counts_original_text_before_trimming(self):
        client = fake_client()
        with patch.object(Config, "MAX_TEXT_LENGTH", 10):
            for text in ["x" * 11, "Short text   "]:
                with self.subTest(text=text):
                    with self.assertRaises(ValueError):
                        analyze_feedback(text, client=client)
        client.responses.parse.assert_not_called()


class ClientConfigurationTests(unittest.TestCase):
    def test_client_has_request_timeout_and_sdk_retries_do_not_multiply_app_retries(self):
        with patch.object(Config, "OPENAI_API_KEY", "offline-test-key"), patch("ai.client.OpenAI") as constructor:
            get_openai_client()
        kwargs = constructor.call_args.kwargs
        self.assertEqual(kwargs["api_key"], "offline-test-key")
        self.assertEqual(kwargs["timeout"], Config.REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["max_retries"], 0)

    def test_missing_key_is_clear_and_never_constructs_client(self):
        with patch.object(Config, "OPENAI_API_KEY", None), patch("ai.client.OpenAI") as constructor:
            with self.assertRaises(ValueError) as raised:
                get_openai_client()
        self.assertIn("OPENAI_API_KEY", str(raised.exception))
        constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
