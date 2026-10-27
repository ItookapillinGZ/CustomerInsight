"""Offline checks for configuration, explicit transport, and prompt selection."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import BadRequestError
from pydantic import ValidationError

from ai.analyzer import analyze_feedback, StructuredOutputError
from ai.batch import analyze_batch, describe_error
from ai.client import get_openai_client, request_parameters, request_settings
from ai.prompts import get_prompt, prompt_hash
from core.config import Config, ConfigurationError, load_project_env, load_settings
from models.models import FeedbackResult
from tests.helpers import fake_client, valid_analysis


class ConfigurationTests(unittest.TestCase):
    def test_project_dotenv_overrides_inherited_key_without_changing_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / ".env"
            content = "OPENAI_API_KEY='offline-project-key'\nOPENAI_MODEL=offline-model\n"
            path.write_text(content, encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "offline-host-key"}):
                self.assertTrue(load_project_env(path))
                self.assertEqual(os.environ["OPENAI_API_KEY"], "offline-project-key")
                self.assertEqual(os.environ["OPENAI_MODEL"], "offline-model")
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_no_dotenv_preserves_explicit_process_configuration(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.dict(os.environ, {"OPENAI_API_KEY": "offline-host-key"}):
            self.assertFalse(load_project_env(Path(directory) / "missing.env"))
            self.assertEqual(os.environ["OPENAI_API_KEY"], "offline-host-key")

    def test_environment_maps_every_migrated_setting(self):
        settings = load_settings({
            "LLM_PROVIDER": "openai_compatible", "OPENAI_API_KEY": "offline-secret",
            "OPENAI_MODEL": "custom-model", "OPENAI_BASE_URL": "https://gateway.invalid/v1",
            "OPENAI_TIMEOUT_SECONDS": "8.5", "OPENAI_REASONING_EFFORT": "high",
            "OPENAI_MAX_OUTPUT_TOKENS": "256", "API_MODE": "chat_completions",
            "CHAT_TOKEN_PARAMETER": "max_tokens", "CHAT_OUTPUT_MODE": "text",
        })
        self.assertEqual(settings["REQUEST_TIMEOUT_SECONDS"], 8.5)
        self.assertEqual(settings["MAX_OUTPUT_TOKENS"], 256)
        self.assertEqual(settings["OPENAI_REASONING_EFFORT"], "high")
        with patch.multiple(Config, **settings), patch("ai.client.OpenAI") as constructor:
            get_openai_client()
            self.assertEqual(constructor.call_args.kwargs["base_url"], "https://gateway.invalid/v1")
            self.assertEqual(constructor.call_args.kwargs["timeout"], 8.5)
            self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
            self.assertNotIn("offline-secret", json.dumps(request_settings()))

    def test_invalid_numbers_are_rejected_without_echoing_values(self):
        for name, values in {
            "OPENAI_TIMEOUT_SECONDS": ["0", "-1", "nan", "inf", "secret-invalid"],
            "OPENAI_MAX_OUTPUT_TOKENS": ["0", "-10", "1.5", "nan", "secret-invalid"],
            "LLM_MAX_RETRIES": ["-1", "1.5"],
            "LLM_RETRY_BACKOFF_SECONDS": ["-1", "nan"],
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ConfigurationError) as raised:
                    load_settings({name: value})
                self.assertNotIn("secret-invalid", str(raised.exception))

    def test_unsupported_modes_and_unsafe_endpoints_reject(self):
        for setting in [
            {"API_MODE": "auto"}, {"CHAT_OUTPUT_MODE": "strict-auto"},
            {"CHAT_TOKEN_PARAMETER": "max_output_tokens"},
            {"OPENAI_BASE_URL": "https://user:credential@gateway.invalid/v1"},
            {"OPENAI_BASE_URL": "https://gateway.invalid/v1?key=credential"},
            {"OPENAI_BASE_URL": "file:///private"},
        ]:
            with self.subTest(setting=setting), self.assertRaises(ConfigurationError):
                load_settings(setting)


class TransportTests(unittest.TestCase):
    def test_installed_sdk_serializes_both_modes_to_custom_gateway(self):
        from openai import OpenAI
        for mode in ["responses", "chat_completions"]:
            captured = []

            def transport(request):
                captured.append((request.url.path, json.loads(request.content)))
                if mode == "chat_completions":
                    payload = {"id": "chat-offline", "object": "chat.completion", "created": 1,
                               "model": "offline-model", "choices": [{"index": 0, "finish_reason": "stop",
                               "message": {"role": "assistant", "content": json.dumps(valid_analysis())}}]}
                else:
                    payload = {"id": "resp-offline", "object": "response", "created_at": 1,
                               "model": "offline-model", "status": "completed", "output": [{
                               "id": "msg-offline", "type": "message", "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": json.dumps(valid_analysis()), "annotations": []}]}]}
                return httpx.Response(200, json=payload)

            http_client = httpx.Client(transport=httpx.MockTransport(transport))
            with self.subTest(mode=mode), patch.multiple(Config, API_MODE=mode, CHAT_OUTPUT_MODE="json_schema", CHAT_TOKEN_PARAMETER="max_completion_tokens", OPENAI_API_KEY="offline-test", OPENAI_BASE_URL="https://gateway.invalid/custom/v1", OPENAI_MODEL="offline-model", OPENAI_REASONING_EFFORT="low", MAX_OUTPUT_TOKENS=444), patch("ai.client.OpenAI", side_effect=lambda **kwargs: OpenAI(**kwargs, http_client=http_client)):
                result = analyze_feedback("合成SDK验证输入")
            self.assertEqual(result.sentiment, "negative")
            path, payload = captured[0]
            self.assertEqual(path, "/custom/v1/" + ("responses" if mode == "responses" else "chat/completions"))
            self.assertEqual(payload["model"], "offline-model")
            self.assertFalse(payload["store"])
            token = "max_output_tokens" if mode == "responses" else "max_completion_tokens"
            self.assertEqual(payload[token], 444)
            if mode == "responses":
                self.assertEqual(payload["text"]["format"]["type"], "json_schema")
                self.assertEqual(payload["reasoning"], {"effort": "low"})
            else:
                self.assertTrue(payload["response_format"]["json_schema"]["strict"])
                self.assertEqual(payload["reasoning_effort"], "low")
            self.assertEqual(len(captured), 1)

    def chat_response(self, content=None, finish_reason="stop", refusal=None):
        message = SimpleNamespace(content=content if content is not None else json.dumps(valid_analysis()), refusal=refusal)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])

    def test_responses_receives_output_limit_and_reasoning_object(self):
        client = fake_client()
        with patch.multiple(Config, API_MODE="responses", MAX_OUTPUT_TOKENS=432, REQUEST_TIMEOUT_SECONDS=9.5, OPENAI_REASONING_EFFORT="low"):
            analyze_feedback("合成输入", client=client)
        params = client.responses.parse.call_args.kwargs
        self.assertEqual(params["max_output_tokens"], 432)
        self.assertEqual(params["reasoning"], {"effort": "low"})
        self.assertEqual(params["timeout"], 9.5)
        self.assertFalse(params["store"])
        self.assertNotIn("max_completion_tokens", params)

    def test_chat_uses_one_correct_token_parameter_and_revalidates(self):
        for token_parameter in ["max_completion_tokens", "max_tokens"]:
            for output_mode in ["json_schema", "json_object", "text"]:
                client = fake_client()
                client.chat.completions.create.return_value = self.chat_response()
                with self.subTest(token=token_parameter, mode=output_mode), patch.multiple(Config, API_MODE="chat_completions", CHAT_OUTPUT_MODE=output_mode, CHAT_TOKEN_PARAMETER=token_parameter, MAX_OUTPUT_TOKENS=987, OPENAI_REASONING_EFFORT="medium"):
                    result = analyze_feedback("合成输入", client=client, prompt_version="v2")
                self.assertEqual(result.category, "bugs")
                params = client.chat.completions.create.call_args.kwargs
                self.assertEqual(params[token_parameter], 987)
                self.assertEqual(params["reasoning_effort"], "medium")
                other = "max_tokens" if token_parameter == "max_completion_tokens" else "max_completion_tokens"
                self.assertNotIn(other, params)
                self.assertNotIn("max_output_tokens", params)
                self.assertNotIn("reasoning", params)
                if output_mode == "text":
                    self.assertNotIn("response_format", params)
                else:
                    self.assertEqual(params["response_format"]["type"], output_mode)
                client.responses.parse.assert_not_called()

    def test_chat_refusal_truncation_blank_and_invalid_json_never_succeed(self):
        cases = [
            self.chat_response(finish_reason="length"),
            self.chat_response(refusal="refused"), self.chat_response(content=""),
            SimpleNamespace(choices=[]),
            self.chat_response(content="{\"sentiment\":\"neutral\"}"),
            self.chat_response(content="```json\n{}\n```"),
            self.chat_response(content=json.dumps(valid_analysis(category="invented"))),
        ]
        with patch.multiple(Config, API_MODE="chat_completions", CHAT_OUTPUT_MODE="text"):
            for response in cases:
                client = fake_client()
                client.chat.completions.create.return_value = response
                with self.subTest(response=response), self.assertRaises((StructuredOutputError, ValidationError)):
                    analyze_feedback("合成输入", client=client)
                self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_unsupported_capability_is_not_retried_or_silently_fallback(self):
        client = fake_client()
        response = httpx.Response(400, request=httpx.Request("POST", "https://gateway.invalid/v1/chat/completions"))
        client.chat.completions.create.side_effect = BadRequestError("secret-provider-body", response=response, body={})
        with patch.object(Config, "API_MODE", "chat_completions"), self.assertRaises(BadRequestError) as raised:
            analyze_feedback("合成输入", client=client)
        self.assertNotIn("secret-provider-body", describe_error(raised.exception))
        self.assertIn("400", describe_error(raised.exception))
        self.assertEqual(client.chat.completions.create.call_count, 1)
        client.responses.parse.assert_not_called()

    def test_empty_reasoning_is_omitted_and_no_input_mutation(self):
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "text"}]
        with patch.multiple(Config, API_MODE="chat_completions", OPENAI_REASONING_EFFORT=None):
            params = request_parameters(messages)
        self.assertNotIn("reasoning_effort", params)
        self.assertEqual(messages[0]["content"], "system")


class PromptTests(unittest.TestCase):
    def test_versions_have_distinct_traceable_templates(self):
        self.assertEqual(prompt_hash("v1"), "ceb349897427181de8c3e9e8c622968258fd20f56c82a234b6250f38df0131c6")
        self.assertNotEqual(prompt_hash("v1"), prompt_hash("v2"))
        self.assertIn("Prioritization rubric", get_prompt("v1"))
        self.assertIn("相同影响", get_prompt("v2"))
        self.assertTrue(get_prompt("v3").startswith(get_prompt("v2")))
        self.assertEqual(prompt_hash("v3"), "19ae88284b1d4634314da8846fe9da4fc8e8c804327ce7124a2c5a068bcf0a64")

    def test_unknown_version_rejects_before_client_or_request(self):
        with patch("ai.analyzer.get_openai_client") as factory, self.assertRaises(ValueError):
            analyze_feedback("合成输入", prompt_version="unregistered-test-version")
        factory.assert_not_called()
        client = fake_client()
        with self.assertRaises(ValueError):
            analyze_batch([], client=client, prompt_version="unregistered-test-version")
        client.responses.parse.assert_not_called()

    def test_batch_forwards_v2_and_keeps_identity(self):
        client = fake_client()
        row = FeedbackResult(feedback_id="original-id", source_row=2, original_text="合成输入")
        with patch.object(Config, "API_MODE", "responses"):
            output = analyze_batch([row], client=client, prompt_version="v2")
        self.assertEqual(output[0].feedback_id, "original-id")
        self.assertIn("相同影响", client.responses.parse.call_args.kwargs["input"][-1]["content"])

    def test_probe_override_disables_application_retries(self):
        from openai import APIConnectionError
        client = fake_client()
        client.responses.parse.side_effect = APIConnectionError(request=httpx.Request("POST", "https://gateway.invalid/v1/responses"))
        with patch.object(Config, "API_MODE", "responses"), patch("ai.analyzer.time.sleep") as sleep, self.assertRaises(APIConnectionError):
            analyze_feedback("合成输入", client=client, max_retries=0)
        sleep.assert_not_called()
        self.assertEqual(client.responses.parse.call_count, 1)


if __name__ == "__main__":
    unittest.main()
