import ast
import asyncio
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


SOURCES = globals().get("_TRACING_SOURCES")
if SOURCES is None:
    root = Path(__file__).resolve().parents[1]
    SOURCES = {
        "filter": (root / "config/functions/langfuse_filter.py").read_text(encoding="utf-8"),
        "headers": (root / "backend/open_webui/utils/headers.py").read_text(encoding="utf-8"),
        "callbacks": (root / "config/litellm-callbacks.py").read_text(encoding="utf-8"),
    }


def load_module(name, source):
    module = ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module


class LangfuseValveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.filter_module = load_module("test_valve_filter", SOURCES["filter"])
        custom_logger = ModuleType("litellm.integrations.custom_logger")
        custom_logger.CustomLogger = object
        with patch.dict(sys.modules, {custom_logger.__name__: custom_logger}):
            cls.callback_module = load_module("test_valve_callbacks", SOURCES["callbacks"])
        tree = ast.parse(SOURCES["headers"])
        header_function = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_headers_and_cookies"
        )
        cls.header_scope = {
            "json": json,
            "Request": object,
            "ENABLE_FORWARD_USER_INFO_HEADERS": False,
            "get_custom_headers": AsyncMock(side_effect=lambda custom, *args, **kwargs: custom),
        }
        exec(compile(ast.Module(body=[header_function], type_ignores=[]), "headers.py", "exec"), cls.header_scope)

    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "OCCUMED_TRACING_ENABLED": "false",
            "OCCUMED_TRACING_PILOT_USER_IDS": "",
            "OCCUMED_TRACING_CAPTURE_CONTENT": "false",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.filter = self.filter_module.Filter()
        self.filter.valves.enabled = True
        self.filter.langfuse = Mock()
        self.request = SimpleNamespace(state=SimpleNamespace())
        self.metadata = {"chat_id": "chat", "message_id": "message"}
        self.body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "input-marker"}],
            "metadata": self.metadata,
        }
        self.logger = self.callback_module.OWUITraceCorrelator()
        self.logger._lf = Mock()

    def headers(self, config=None, metadata=None):
        asyncio.run(self.filter.inlet(
            self.body, __metadata__=self.metadata,
            __user__={"id": "not-in-any-pilot"}, __request__=self.request,
        ))
        headers, _ = asyncio.run(self.header_scope["get_headers_and_cookies"](
            self.request, "http://litellm:4000", config=config,
            metadata=self.metadata if metadata is None else metadata,
        ))
        return headers

    def emit(self, headers):
        kwargs = {
            "model": "test-model",
            "messages": [
                {"role": "user", "content": "input-marker"},
                {"role": "assistant", "content": "prior-output", "tool_calls": [{"arguments": "secret-args"}], "reasoning_content": "secret-reasoning"},
                {"role": "tool", "content": "secret-tool-output"},
            ],
            "response_cost": 0.01,
            "litellm_params": {"proxy_server_request": {"headers": headers}},
        }
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="output-marker", tool_calls=None, reasoning_content="secret-reasoning",
            ))],
        )
        self.logger._emit_generation(kwargs=kwargs, response_obj=response, start_time=None, end_time=None)

    def test_legacy_environment_cannot_disable_enabled_valve(self):
        self.emit(self.headers())
        self.logger._lf.generation.assert_called_once()
        generation = self.logger._lf.generation.call_args.kwargs
        self.assertEqual(generation["usage"]["total"], 5)
        self.assertEqual(generation["cost_details"]["total"], 0.01)

    def test_disabled_valve_cannot_be_overridden_by_static_header(self):
        self.headers()
        self.filter.valves.enabled = False
        headers = self.headers(config={"headers": {"x-openwebui-langfuse": '{"enabled":true}'}})
        self.assertNotIn("x-openwebui-langfuse", headers)
        self.assertNotIn("X-OpenWebUI-Langfuse", headers)
        self.emit(headers)
        self.logger._lf.generation.assert_not_called()

    def test_capture_off_keeps_metrics_without_content(self):
        self.emit(self.headers())
        generation = self.logger._lf.generation.call_args.kwargs
        self.assertNotIn("input", generation)
        self.assertNotIn("output", generation)
        self.assertNotIn("model_parameters", generation)

    def test_capture_valves_work_without_environment_override(self):
        self.filter.valves.capture_inputs = True
        self.filter.valves.capture_outputs = True
        self.emit(self.headers())
        generation = self.logger._lf.generation.call_args.kwargs
        self.assertIn("input-marker", json.dumps(generation["input"]))
        self.assertIn("output-marker", json.dumps(generation["output"]))
        self.assertNotIn("secret-args", json.dumps(generation))
        self.assertNotIn("secret-reasoning", json.dumps(generation))
        self.assertNotIn("secret-tool-output", json.dumps(generation))

    def test_policy_does_not_follow_a_different_chat(self):
        self.assertNotIn("X-OpenWebUI-Langfuse", self.headers(metadata={"chat_id": "other", "message_id": "message"}))

    def test_each_capture_direction_is_independent(self):
        for capture_inputs, capture_outputs in ((True, False), (False, True)):
            self.logger._lf.reset_mock()
            self.filter.valves.capture_inputs = capture_inputs
            self.filter.valves.capture_outputs = capture_outputs
            self.emit(self.headers())
            generation = self.logger._lf.generation.call_args.kwargs
            self.assertEqual("input" in generation, capture_inputs)
            self.assertEqual("output" in generation, capture_outputs)

    def test_stream_metrics_ignore_legacy_environment(self):
        self.headers()
        self.filter.langfuse.reset_mock()
        asyncio.run(self.filter.stream({"choices": [{"delta": {"content": "chunk"}}]}, __metadata__=self.metadata))
        asyncio.run(self.filter.stream({"done": True}, __metadata__=self.metadata))
        payload = self.filter.langfuse.start_observation.call_args.kwargs
        self.assertEqual(payload["name"], "stream")
        self.assertEqual(payload["output"]["chunks"], 1)

    def test_disabled_outlet_and_client_emit_nothing(self):
        self.filter.valves.enabled = False
        asyncio.run(self.filter.outlet(self.body, __metadata__=self.metadata))
        self.filter.langfuse.start_observation.assert_not_called()
        self.filter.langfuse = None
        with patch.object(self.filter_module, "Langfuse") as constructor:
            self.filter.set_langfuse()
        constructor.assert_not_called()

    def test_missing_or_malformed_policy_emits_nothing(self):
        for policy in (None, "invalid-json", "[]", '{"enabled":false}', '{"enabled":"true"}'):
            self.emit({"X-OpenWebUI-Langfuse": policy} if policy is not None else {})
        self.logger._lf.generation.assert_not_called()

    def test_observation_wrappers_honor_capture_off(self):
        parent = Mock()
        self.filter._create_child_and_end(parent, as_type="tool", name="tool", input="secret-args", output="secret-result")
        payload = parent.start_observation.call_args.kwargs
        self.assertNotIn("input", payload)
        self.assertNotIn("output", payload)

    def test_content_free_metrics_survive_capture_off(self):
        parent = Mock()
        self.filter._create_child_and_end(parent, as_type="guardrail", name="pii_masking", output={"total_redacted": 2}, content_free=True)
        self.assertEqual(parent.start_observation.call_args.kwargs["output"], {"total_redacted": 2})
        self.filter._create_on_trace_and_end("0" * 32, as_type="span", name="stream", output_data={"chunks": 3}, content_free=True)
        self.assertEqual(self.filter.langfuse.start_observation.call_args.kwargs["output"], {"chunks": 3})

    def test_client_initialization_uses_enabled_valve(self):
        self.filter.langfuse = None
        client = Mock()
        client._resources._score_ingestion_queue.mutex = __import__("threading").Lock()
        client._resources._media_upload_queue.mutex = __import__("threading").Lock()
        with patch.object(self.filter_module, "Langfuse", return_value=client) as constructor:
            self.filter.set_langfuse()
        constructor.assert_called_once()


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LangfuseValveTests))
    raise SystemExit(not result.wasSuccessful())