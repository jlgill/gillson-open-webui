"""
config/litellm-callbacks.py

LiteLLM CustomLogger: correlates each LiteLLM completion with the Langfuse trace
that the OWUI in-process Langfuse filter function (config/functions/langfuse_filter.py)
opened for that turn, then appends a fully-enriched generation observation to it,
nested under the filter's current `run-agent-step` span.

How it works
------------
The OWUI filter publishes a per-request policy dict that OWUI's header bridge
(utils/headers.py) forwards on every outgoing LiteLLM call as the
`X-OpenWebUI-Langfuse` header (JSON):

    {"v": 2, "enabled": true, "trace_id": "<32 hex>",
     "parent_observation_id": "<16 hex>", "chat_id": ..., "message_id": ...,
     "step_index": 2, "phase": "turn" | "background", "capture_*": bool}

`trace_id` is used verbatim when present and valid; otherwise the legacy
formula is recomputed from chat_id/message_id:

    trace_id = sha256(f"{chat_id}:{message_id}".encode()).digest()[:16].hex()

which mirrors Langfuse.create_trace_id(seed=...) in langfuse==4.15.6.
`parent_observation_id` nests the generation under the filter's step (or the
root for task calls such as title/tags/query generation).

The generation is named after the action, never the transport:
  * `generate-response` for chat turns,
  * `generate-title`, `generate-tags`, `generate-queries`, ... for OWUI tasks,
    derived from the `X-OpenWebUI-Task` connection header (`{{TASK}}` template).

NOTE: The LiteLLM container ships langfuse v2 (2.57.x), not v4. This callback
uses the langfuse v2 SDK API: lf.generation(trace_id=..., parent_observation_id=..., ...).

v2.2.0 (honest cost):
- `cost_status` metadata on every generation: `litellm` (priced by deployment
  pricing or the model map), `cache_hit`, `unavailable` (LiteLLM could not cost
  the call) or `unpriced`.
- `unpriced` = response_cost 0 for a call that used tokens on a model with no
  deployment pricing and no model-map entry. Such generations carry NO
  cost_details instead of an asserted $0, so Langfuse can apply its own model
  definitions and spend dashboards do not under-report. Fix the root cause by
  adding input/output_cost_per_token to the LiteLLM deployment.

v2.1.0 (nested per-step generations + gateway timing):
- Policy-provided trace_id + parent_observation_id (validated hex) with the
  sha256 fallback; task-aware generation names; step_index/phase/task metadata.
- LiteLLM timing: native completion_start_time (Langfuse TTFT), gateway pre-call /
  provider TTFT / overhead / retries metadata, `perf.litellm_overhead_ms` score.
- Retains v1.3.x hardening: no per-request flush(); bounded queues (1024);
  atexit shutdown flush; 1MB record cap with progressive degradation; ERROR
  generations for failures with generic text when capture is off; enablement
  and capture follow the OWUI filter valves forwarded in the header.

Setup checklist
---------------
1. docker-compose.prod.yaml — mount this file into owui-litellm:
       ./config/litellm-callbacks.py:/app/litellm_callbacks.py:ro

2. litellm-config.yaml — register under success_callback AND failure_callback:
       success_callback: ["litellm_callbacks.owui_trace_correlator"]
       failure_callback: ["litellm_callbacks.owui_trace_correlator"]

3. Install the OWUI filter (v2.0.0+) and keep the backend header bridge. OWUI
   forwards X-OpenWebUI-Langfuse automatically for requests the filter traces.

4. Admin -> Settings -> Connections -> LiteLLM connection -> Headers: add
       X-OpenWebUI-Task: {{TASK}}
   so background/in-turn task calls get task-specific generation names.

5. LANGFUSE_HOST / OWUI_LANGFUSE_PUBLIC_KEY / OWUI_LANGFUSE_SECRET_KEY must be set
   in the litellm container environment (already present in docker-compose.prod.yaml).

Result in Langfuse
------------------
Each chat-turn trace holds the filter's `respond-to-chat` root with one
`run-agent-step` span per model call; this callback adds the `generate-response`
generation (per-call input/output, tool calls, reasoning, TTFT, model params,
accurate cost + token counts) inside the matching step, and task generations
under the root. The filter (v2.2.0+) also records a `record-conversation` event
under the root that replays the turn in OWUI display order; it carries no usage,
so cost and tokens stay solely on these generations.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("owui_trace_correlator")
log.setLevel(logging.WARNING)

# Per-record export cap; oversized records degrade progressively before dropping.
_MAX_RECORD_BYTES = 1_048_576

_HEX_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_HEX_OBSERVATION_ID = re.compile(r"^[0-9a-f]{16}$")

# Action-oriented generation names; OWUI task names arrive via X-OpenWebUI-Task.
_GENERATION_NAME_DEFAULT = "generate-response"
_GENERATION_NAME_BY_TASK = {
    "title_generation": "generate-title",
    "tags_generation": "generate-tags",
    "follow_up_generation": "generate-follow-ups",
    "query_generation": "generate-queries",
    "emoji_generation": "generate-emoji",
    "image_prompt_generation": "generate-image-prompt",
    "autocomplete_generation": "generate-autocomplete",
    "function_calling": "call-functions",
    "moa_response_generation": "generate-moa-response",
}
_GENERATION_NAME_TASK_FALLBACK = "run-task"


def _debug() -> bool:
    return os.getenv("OWUI_TRACE_CORRELATOR_DEBUG", "false").lower() == "true"


# ── trace-ID formula ──────────────────────────────────────────────────────────

def _trace_id(chat_id: str, message_id: Optional[str]) -> str:
    """Legacy fallback reproducing Langfuse.create_trace_id(seed=…) from langfuse==4.15.6.

    Source (confirmed in the pinned Open WebUI SDK):
        return sha256(seed.encode("utf-8")).digest()[:16].hex()

    Seed mirrors langfuse_filter.py -> Filter._trace_id_for():
        f"{chat_id}:{message_id}"  if message_id is present
        f"{chat_id}"               fallback (whole-chat trace)

    Policies from filter v2.0.0+ carry the trace id explicitly; this is only used
    when that field is absent or malformed.
    """
    seed = f"{chat_id}:{message_id}" if message_id else chat_id
    return hashlib.sha256(seed.encode("utf-8")).digest()[:16].hex()


def _generation_name(task: Optional[str]) -> str:
    task = (task or "").strip()
    if not task:
        return _GENERATION_NAME_DEFAULT
    return _GENERATION_NAME_BY_TASK.get(task, _GENERATION_NAME_TASK_FALLBACK)


# ── masking / truncation (ported from langfuse_filter.py) ─────────────────────

_MASK_REPLACEMENT = "[REDACTED]"
_MAX_FIELD_CHARS = 20000
_MAX_COLLECTION_ITEMS = 200
_MASK_METADATA_KEYS = (
    "authorization,cookie,set-cookie,api_key,secret_key,access_token,"
    "refresh_token,password,token,key"
)
# Matches Open WebUI <details> HTML tool-call blocks embedded in assistant content.
_OWUI_DETAILS_RE = re.compile(r"<details\b[^>]*>.*?</details>", re.DOTALL | re.IGNORECASE)


def _mask_string(value: str) -> str:
    """PII/secret masking, ported from the filter's _mask_string (minus valves)."""
    result = value
    # data URIs (multimodal payloads)
    result = re.sub(
        r"data:([^;,\s]+);base64,[A-Za-z0-9+/=\r\n]+",
        lambda m: f"[REDACTED {m.group(1)} DATA URI]",
        result,
    )
    # emails
    result = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", _MASK_REPLACEMENT, result)
    # phone numbers
    result = re.sub(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", _MASK_REPLACEMENT, result)
    # API keys / bearer tokens
    result = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", f"Bearer {_MASK_REPLACEMENT}", result, flags=re.I)
    result = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{12,}\b", _MASK_REPLACEMENT, result)
    result = re.sub(r"\b(?:sk|pk)-lf-[A-Za-z0-9_-]{12,}\b", _MASK_REPLACEMENT, result)
    result = re.sub(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b", _MASK_REPLACEMENT, result)
    if len(result) > _MAX_FIELD_CHARS:
        result = f"{result[:_MAX_FIELD_CHARS]}... [TRUNCATED {len(result) - _MAX_FIELD_CHARS} chars]"
    return result


def _mask_key_segments() -> set[frozenset[str]]:
    return {
        frozenset(seg for seg in re.split(r"[^a-z0-9]+", entry.strip().lower()) if seg)
        for entry in _MASK_METADATA_KEYS.split(",")
        if entry.strip()
    }


def _should_mask_key(key: str) -> bool:
    """Segment-superset matching (mirrors the filter's fixed _should_mask_key):
    "access_token" -> {access, token} contains {token} -> masked;
    "est_output_tokens" -> {est, output, tokens} -> NOT masked.
    """
    segments = {seg for seg in re.split(r"[^a-z0-9]+", key.strip().lower()) if seg}
    if not segments:
        return False
    return any(mask_seg and mask_seg.issubset(segments) for mask_seg in _mask_key_segments())


def _safe_data(data: Any, depth: int = 0) -> Any:
    """Recursive masking + truncation, ported from the filter's _safe_data."""
    if depth > 12:
        return "[MAX_DEPTH]"
    if data is None or isinstance(data, (bool, int, float)):
        return data
    if isinstance(data, str):
        return _mask_string(data)
    if isinstance(data, dict):
        output = {}
        for index, (key, value) in enumerate(data.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                output["__truncated_items__"] = len(data) - _MAX_COLLECTION_ITEMS
                break
            key_string = str(key)
            if _should_mask_key(key_string):
                output[key_string] = _MASK_REPLACEMENT
            else:
                output[key_string] = _safe_data(value, depth + 1)
        return output
    if isinstance(data, (list, tuple, set)):
        items = list(data)
        output = [_safe_data(value, depth + 1) for value in items[:_MAX_COLLECTION_ITEMS]]
        if len(items) > _MAX_COLLECTION_ITEMS:
            output.append({"__truncated_items__": len(items) - _MAX_COLLECTION_ITEMS})
        return output
    return _mask_string(str(data))


# ── payload extraction helpers ─────────────────────────────────────────────────

def _extract_output_text(response_obj: Any) -> Optional[Any]:
    """Extract the completion text from a LiteLLM response object (chat or responses)."""
    if response_obj is None:
        return None
    # Chat Completions shape
    choices = getattr(response_obj, "choices", None)
    if choices:
        first = choices[0]
        message = getattr(first, "message", None)
        if message is not None:
            content = getattr(message, "content", None)
            if isinstance(content, str) and content:
                return content
        text = getattr(first, "text", None)
        if isinstance(text, str) and text:
            return text
    # Responses API shape
    output_items = getattr(response_obj, "output", None)
    if isinstance(output_items, list) and output_items:
        parts = []
        for item in output_items:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for part in getattr(item, "content", []) or []:
                    part_text = getattr(part, "text", None)
                    if part_text:
                        parts.append(str(part_text))
            elif item_type == "reasoning":
                for part in getattr(item, "summary", []) or []:
                    part_text = getattr(part, "text", None)
                    if part_text:
                        parts.append(str(part_text))
        if parts:
            return "\n".join(parts)
    return None


def _extract_tool_calls(response_obj: Any) -> Optional[list]:
    """Extract tool calls from a Chat Completions response (function_call shape)."""
    choices = getattr(response_obj, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    if message is None:
        return None
    tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls:
        return None
    extracted = []
    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        extracted.append(
            {
                "name": getattr(fn, "name", None),
                "arguments": getattr(fn, "arguments", None),
            }
        )
    return extracted or None


def _extract_reasoning(response_obj: Any) -> Optional[str]:
    """Extract reasoning content (OpenAI-style reasoning_content / Anthropic thinking)."""
    choices = getattr(response_obj, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    if message is None:
        return None
    reasoning = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None)
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    return None


def _extract_model_parameters(kwargs: dict) -> Optional[dict]:
    """Model parameters from the standard logging payload (modelParameters)."""
    slo = kwargs.get("standard_logging_object") or {}
    params = slo.get("modelParameters") if isinstance(slo, dict) else None
    if isinstance(params, dict) and params:
        return {str(k): v for k, v in params.items() if v is not None}
    return None


def _epoch_seconds(value: Any) -> Optional[float]:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _extract_completion_start(kwargs: dict) -> Optional[float]:
    """Epoch seconds of the first streamed token as measured by LiteLLM.

    LiteLLM stores it on model_call_details as `completion_start_time` (datetime) and on
    the standard logging payload as `completionStartTime` (float); non-streaming calls
    report end_time there, which is still the moment the response became available.
    """
    direct = _epoch_seconds(kwargs.get("completion_start_time"))
    if direct is not None:
        return direct
    slo = kwargs.get("standard_logging_object") or {}
    if isinstance(slo, dict):
        return _epoch_seconds(slo.get("completionStartTime"))
    return None


def _extract_gateway_timing(kwargs: dict, start_time: Any, completion_start: Optional[float]) -> dict[str, Any]:
    """Where the gateway spent time: pre-call overhead, provider hand-off, TTFT, retries.

    Sources (LiteLLM model_call_details / standard_logging_object):
      first_api_call_start_time  first provider hand-off (set once, excludes retry loops)
      llm_api_duration_ms        provider round trip
      hidden_params.litellm_overhead_time_ms  total - provider time (gateway overhead)
      hidden_params.additional_headers        provider rate-limit headroom
      litellm_params.metadata.attempted_retries / attempted_fallbacks
      timing_*_ms                opt-in per-phase split (LITELLM_DETAILED_TIMING=true)
    """
    timing: dict[str, Any] = {}
    start = _epoch_seconds(start_time)
    handoff = _epoch_seconds(kwargs.get("first_api_call_start_time") or kwargs.get("api_call_start_time"))
    if start is not None and handoff is not None and handoff >= start:
        timing["gateway_precall_seconds"] = round(handoff - start, 4)
    if start is not None and completion_start is not None and completion_start >= start:
        timing["ttft_seconds"] = round(completion_start - start, 4)
    if handoff is not None and completion_start is not None and completion_start >= handoff:
        timing["provider_ttft_seconds"] = round(completion_start - handoff, 4)
    llm_api_ms = kwargs.get("llm_api_duration_ms")
    if isinstance(llm_api_ms, (int, float)) and not isinstance(llm_api_ms, bool):
        timing["llm_api_duration_ms"] = round(float(llm_api_ms), 3)
    slo = kwargs.get("standard_logging_object") or {}
    hidden = slo.get("hidden_params") if isinstance(slo, dict) else None
    if isinstance(hidden, dict):
        overhead = hidden.get("litellm_overhead_time_ms")
        if isinstance(overhead, (int, float)) and not isinstance(overhead, bool):
            timing["litellm_overhead_ms"] = round(float(overhead), 3)
        for key in ("_response_ms", "timing_pre_processing_ms", "timing_llm_api_ms", "timing_post_processing_ms", "callback_duration_ms"):
            value = hidden.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                timing[key.lstrip("_")] = round(float(value), 3)
        if hidden.get("litellm_model_name"):
            timing["provider_model"] = str(hidden["litellm_model_name"])
        headers = hidden.get("additional_headers")
        if isinstance(headers, dict):
            for key in ("x_ratelimit_remaining_requests", "x_ratelimit_remaining_tokens"):
                if headers.get(key) is not None:
                    timing[key.replace("x_", "")] = headers[key]
    litellm_meta = (kwargs.get("litellm_params") or {}).get("metadata") if isinstance(kwargs.get("litellm_params"), dict) else None
    if isinstance(litellm_meta, dict):
        for key in ("attempted_retries", "attempted_fallbacks", "original_model_group"):
            if litellm_meta.get(key) not in (None, "", 0):
                timing[key] = litellm_meta[key]
    slo_meta = slo.get("metadata") if isinstance(slo, dict) else None
    if isinstance(slo_meta, dict) and isinstance(slo_meta.get("routing_decision"), dict):
        timing["routing_decision"] = slo_meta["routing_decision"]
    return timing


def _extract_call_type(kwargs: dict) -> Optional[str]:
    call_type = kwargs.get("call_type")
    if isinstance(call_type, str) and call_type:
        return call_type
    std = kwargs.get("standard_logging_object") or {}
    if isinstance(std, dict):
        ct = std.get("call_type")
        if isinstance(ct, str) and ct:
            return ct
    return None


def _extract_stream_mode(kwargs: dict) -> Optional[bool]:
    litellm_params = kwargs.get("litellm_params") or {}
    if isinstance(litellm_params, dict):
        return bool(litellm_params.get("stream"))
    return None


# Mirrors litellm's use_custom_pricing_for_model (deployment pricing) plus the model map.
_PRICING_KEYS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "input_cost_per_second",
    "output_cost_per_second",
    "input_cost_per_character",
    "output_cost_per_character",
    "tiered_pricing",
)


def _has_pricing(source: Any) -> bool:
    return isinstance(source, dict) and any(source.get(key) is not None for key in _PRICING_KEYS)


def _cost_status(kwargs: dict, total_tokens: int) -> str:
    """How far LiteLLM's response_cost can be trusted.

    `unpriced` = 0 for a call that used tokens on a model with no deployment pricing
    and no model-map entry: an artifact, not a free call. Reporting it as $0 would
    hide real spend, so the generation carries no cost and Langfuse can fall back to
    its own model definitions. A deliberate $0 price (e.g. local Ollama) stays `litellm`.
    """
    response_cost = kwargs.get("response_cost")
    if kwargs.get("cache_hit") is True:
        return "cache_hit"
    if response_cost is None:
        return "unavailable"
    try:
        if float(response_cost) > 0 or total_tokens <= 0:
            return "litellm"
    except (TypeError, ValueError):
        return "unavailable"
    params = kwargs.get("litellm_params") or {}
    if isinstance(params, dict):
        if _has_pricing(params):
            return "litellm"
        for key in ("metadata", "litellm_metadata"):
            container = params.get(key)
            if isinstance(container, dict) and _has_pricing(container.get("model_info")):
                return "litellm"
    std = kwargs.get("standard_logging_object") or {}
    model_map = std.get("model_map_information") if isinstance(std, dict) else None
    if isinstance(model_map, dict) and _has_pricing(model_map.get("model_map_value")):
        return "litellm"
    return "unpriced"


def _extract_input_messages(kwargs: dict) -> Optional[Any]:
    """The exact messages array sent to the provider for THIS call."""
    messages = kwargs.get("messages")
    if messages:
        return messages
    std = kwargs.get("standard_logging_object") or {}
    if isinstance(std, dict):
        return std.get("messages")
    return None


def _extract_error_info(kwargs: dict) -> Optional[dict]:
    """Error details from the standard logging payload (failure events)."""
    std = kwargs.get("error_information") or kwargs.get("standard_logging_object") or {}
    if isinstance(std, dict):
        info = std.get("error_information")
        if isinstance(info, dict):
            return info
        if "error_str" in std or "error_class" in std:
            return {k: std[k] for k in ("error_str", "error_class", "error_code", "llm_provider") if k in std}
    return None


# ── logger ────────────────────────────────────────────────────────────────────

class OWUITraceCorrelator(CustomLogger):
    """Appends a fully-enriched Langfuse generation to the per-turn trace
    that the OWUI Langfuse filter function seeded via inlet/outlet.

    Uses the langfuse v2 SDK API (lf.generation) since the LiteLLM container
    ships langfuse v2, not v4.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lf = None
        self._last_init = float("-inf")
        self.failures = 0
        self.dropped = 0
        self._last_log = float("-inf")
        atexit.register(self._shutdown_flush)

    def _throttled_log(self, message: str) -> None:
        """Detail-free, throttled (60s) degradation notice; full detail only when
        OWUI_TRACE_CORRELATOR_DEBUG=true (container logs must stay content-free)."""
        if _debug():
            log.warning("OWUITraceCorrelator: %s", message)
            return
        if time.monotonic() - self._last_log >= 60:
            self._last_log = time.monotonic()
            log.warning("OWUITraceCorrelator: telemetry degraded; details suppressed")

    def _shutdown_flush(self) -> None:
        """Bounded final flush at process exit (LiteLLM never calls a shutdown hook
        on custom loggers; without this, up to flush_interval of buffered events
        would be lost on every restart)."""
        if self._lf is None:
            return
        flush_thread = threading.Thread(target=self._lf.flush, daemon=True)
        flush_thread.start()
        flush_thread.join(timeout=5)

    def _init_langfuse(self) -> None:
        # Use OWUI-project-specific keys for correlated chat observations.
        host = os.getenv("LANGFUSE_HOST")
        pub = os.getenv("OWUI_LANGFUSE_PUBLIC_KEY") or os.getenv("LANGFUSE_PUBLIC_KEY")
        sec = os.getenv("OWUI_LANGFUSE_SECRET_KEY") or os.getenv("LANGFUSE_SECRET_KEY")
        if not (host and pub and sec):
            log.warning(
                "OWUITraceCorrelator: LANGFUSE_HOST/OWUI_LANGFUSE_PUBLIC_KEY/OWUI_LANGFUSE_SECRET_KEY not set — disabled"
            )
            return
        try:
            from langfuse import Langfuse  # noqa: PLC0415

            # langfuse v2 constructor — no tracer_provider; v2 uses HTTP API, not OTEL.
            client = Langfuse(host=host, public_key=pub, secret_key=sec,
                              timeout=2, max_retries=2, threads=1,
                              flush_at=32, flush_interval=1, debug=False)
            for queue in (client.task_manager._ingestion_queue,
                          client.task_manager._media_upload_queue):
                with queue.mutex:
                    queue.maxsize = 1024
            self._lf = client
        except Exception as exc:
            self.failures += 1
            self._throttled_log(f"initialization failed: {exc}")

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _proxy_headers(kwargs: dict) -> dict:
        """Extract the HTTP request headers LiteLLM received from OWUI."""
        try:
            return (
                kwargs.get("litellm_params", {})
                .get("proxy_server_request", {})
                .get("headers", {})
            ) or {}
        except Exception:
            return {}

    @staticmethod
    def _header(headers: dict, name: str) -> Optional[str]:
        """Case-insensitive header lookup (HTTP/2 lowercases all header names)."""
        lo = name.lower()
        for k, v in headers.items():
            if k.lower() == lo and v:
                return str(v)
        return None

    @staticmethod
    def _to_dt(v: Any) -> Optional[datetime]:
        """Normalise a LiteLLM start/end time value to datetime or None."""
        if isinstance(v, datetime):
            return v
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=timezone.utc)
        return None

    # ── async callable shim ───────────────────────────────────────────────────
    # LiteLLM's logging_callback_manager.add_litellm_success_callback routes
    # callbacks to _async_success_callback only when _is_async_callable(obj) is
    # True. That check inspects obj.__call__ for iscoroutinefunction. Adding an
    # async __call__ makes the instance route to the async list so that
    # async_log_success_event is actually invoked.

    async def __call__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        """Makes _is_async_callable() return True → routes to _async_success_callback."""
        pass

    # ── shared generation builder ──────────────────────────────────────────────

    def _emit_generation(
        self,
        *,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        level: str = "DEFAULT",
        status_message: Optional[str] = None,
    ) -> None:
        """Build and post the litellm_generation observation (success or failure)."""
        try:
            hdrs = self._proxy_headers(kwargs)
            raw_policy = self._header(hdrs, "X-OpenWebUI-Langfuse")
            if not raw_policy:
                return
            policy = json.loads(raw_policy)
            if not isinstance(policy, dict) or policy.get("enabled") is not True:
                return
            chat_id = policy.get("chat_id")
            message_id = policy.get("message_id")
            if not isinstance(chat_id, str) or not chat_id or not isinstance(message_id, str) or not message_id:
                return
            if any("{{" in value or len(value) > 200 for value in (chat_id, message_id)):
                return
            if self._lf is None and time.monotonic() - self._last_init >= 30:
                self._last_init = time.monotonic()
                self._init_langfuse()
            if self._lf is None:
                return

            policy_trace_id = policy.get("trace_id")
            trace_id = (
                policy_trace_id
                if isinstance(policy_trace_id, str) and _HEX_TRACE_ID.match(policy_trace_id)
                else _trace_id(chat_id, message_id)
            )
            parent_observation_id = policy.get("parent_observation_id")
            if not (isinstance(parent_observation_id, str) and _HEX_OBSERVATION_ID.match(parent_observation_id)):
                parent_observation_id = None
            task = self._header(hdrs, "X-OpenWebUI-Task")
            if task and ("{{" in task or len(task) > 100):
                task = None
            capture_inputs = policy.get("capture_inputs") is True
            capture_outputs = policy.get("capture_outputs") is True
            capture = capture_inputs or capture_outputs

            # ── usage dict (langfuse v2 accepts plain dict) ────────────────
            usage: dict[str, Any] = {}
            u = getattr(response_obj, "usage", None)
            if u:
                in_t = int(
                    getattr(u, "prompt_tokens", None)
                    or getattr(u, "input_tokens", None)
                    or 0
                )
                out_t = int(
                    getattr(u, "completion_tokens", None)
                    or getattr(u, "output_tokens", None)
                    or 0
                )
                total_t = in_t + out_t
                usage["input"] = in_t
                usage["output"] = out_t
                usage["total"] = total_t
                usage["unit"] = "TOKENS"

            # ── cost ───────────────────────────────────────────────────────
            cost = float(kwargs.get("response_cost") or 0)
            cost_status = _cost_status(kwargs, int(usage.get("total") or 0))

            # ── per-call content (masked + truncated) ─────────────────────
            input_messages = _extract_input_messages(kwargs)
            output_text = _extract_output_text(response_obj)
            tool_calls = _extract_tool_calls(response_obj)
            reasoning = _extract_reasoning(response_obj)
            completion_start = _extract_completion_start(kwargs)
            gateway_timing = _extract_gateway_timing(kwargs, start_time, completion_start)
            model_parameters = _extract_model_parameters(kwargs)
            call_type = _extract_call_type(kwargs)
            stream_mode = _extract_stream_mode(kwargs)

            model = str(kwargs.get("model", "unknown"))
            metadata: dict[str, Any] = {
                "source": "owui_trace_correlator",
                "chat_id": chat_id,
                "message_id": message_id,
                "litellm_call_id": str(kwargs.get("litellm_call_id", "")),
                "call_type": call_type,
                "stream": stream_mode,
                "tool_call_count": len(tool_calls) if tool_calls else 0,
                "task": task or None,
                "step_index": policy.get("step_index"),
                "phase": policy.get("phase"),
                "policy_version": policy.get("v"),
                "cost_status": cost_status,
                **gateway_timing,
            }
            custom_provider = (
                kwargs.get("litellm_params", {}).get("custom_llm_provider")
            )
            if custom_provider:
                metadata["custom_llm_provider"] = str(custom_provider)

            # ── attach generation to the filter's trace (v2 API) ──────────
            gen_kwargs: dict[str, Any] = {
                "trace_id": trace_id,
                "parent_observation_id": parent_observation_id,
                "name": _generation_name(task),
                "model": model,
                "start_time": self._to_dt(start_time),
                "end_time": self._to_dt(end_time),
                "usage": usage if usage else None,
                "metadata": _safe_data(metadata),
                "level": level,
                "status_message": _mask_string(status_message) if capture and status_message else ("LLM call failed" if level == "ERROR" else None),
            }
            if cost_status in {"litellm", "cache_hit"}:
                # Keep cost in the dedicated field; Langfuse v3 persists this as
                # total_cost, whereas a generic Usage.totalCost is not surfaced
                # by the v3 observation API.
                gen_kwargs["cost_details"] = {"total": cost}
            call_id = kwargs.get("litellm_call_id")
            if call_id:
                gen_kwargs["id"] = hashlib.sha256(f"{trace_id}:{call_id}".encode()).hexdigest()[:16]

            # Per-call input: the exact messages sent to the provider.
            if capture_inputs and input_messages is not None:
                captured_messages = []
                for message in input_messages:
                    if isinstance(message, dict):
                        if message.get("role") in {"tool", "function"}:
                            if policy.get("capture_tool_outputs") is not True:
                                continue
                            tool_name = message.get("name")
                            if tool_name in {"search_web", "fetch_url", "query_knowledge_files", "view_knowledge_file", "view_file"}:
                                if policy.get("capture_retriever_outputs") is not True:
                                    continue
                        message = dict(message)
                        if policy.get("capture_tool_arguments") is not True:
                            message.pop("tool_calls", None)
                            message.pop("function_call", None)
                        if policy.get("capture_reasoning") is not True:
                            message.pop("reasoning", None)
                            message.pop("reasoning_content", None)
                    captured_messages.append(message)
                gen_kwargs["input"] = _safe_data(captured_messages)

            # Per-call output: completion text, tool calls, reasoning.
            output_payload: dict[str, Any] = {}
            if output_text is not None:
                output_payload["text"] = output_text
            if tool_calls and policy.get("capture_tool_arguments") is True:
                output_payload["tool_calls"] = tool_calls
            if reasoning and policy.get("capture_reasoning") is True:
                output_payload["reasoning"] = reasoning
            if capture_outputs and output_payload:
                gen_kwargs["output"] = _safe_data(output_payload)

            # LiteLLM's first-token instant -> Langfuse's native TTFT (timeToFirstToken,
            # chartable per model in the latency dashboard).
            if completion_start is not None:
                start_dt = self._to_dt(start_time)
                completion_dt = datetime.fromtimestamp(completion_start, tz=timezone.utc)
                if start_dt is None or completion_dt >= start_dt.astimezone(timezone.utc):
                    gen_kwargs["completion_start_time"] = completion_dt

            if capture and model_parameters:
                gen_kwargs["model_parameters"] = _safe_data(model_parameters)

            if len(json.dumps(gen_kwargs, default=str).encode()) > _MAX_RECORD_BYTES:
                # Progressive degradation: drop input first, then output, before
                # dropping the record. Long agentic turns keep output/usage/cost.
                if gen_kwargs.get("input") is not None:
                    gen_kwargs["input"] = None
                    if len(json.dumps(gen_kwargs, default=str).encode()) <= _MAX_RECORD_BYTES:
                        self._post_generation(gen_kwargs, gateway_timing)
                        return
                if gen_kwargs.get("output") is not None:
                    gen_kwargs["output"] = None
                    if len(json.dumps(gen_kwargs, default=str).encode()) <= _MAX_RECORD_BYTES:
                        self._post_generation(gen_kwargs, gateway_timing)
                        return
                self.dropped += 1
                self._throttled_log("record exceeds size cap after content removal")
                return
            self._post_generation(gen_kwargs, gateway_timing)

        except Exception as exc:
            self.failures += 1
            self._throttled_log(f"_emit_generation failed: {exc}")

    def _post_generation(self, gen_kwargs: dict, gateway_timing: Optional[dict] = None) -> None:
        self._lf.generation(**{key: value for key, value in gen_kwargs.items() if value is not None})
        # Gateway overhead as a numeric score: dashboards can average it per model;
        # metadata is filter-only.
        overhead = (gateway_timing or {}).get("litellm_overhead_ms")
        if gen_kwargs.get("id") and isinstance(overhead, (int, float)):
            try:
                self._lf.score(
                    trace_id=gen_kwargs["trace_id"],
                    observation_id=gen_kwargs["id"],
                    name="perf.litellm_overhead_ms",
                    value=float(overhead),
                    data_type="NUMERIC",
                    comment="LiteLLM total minus provider time",
                )
            except Exception as exc:
                self._throttled_log(f"overhead score failed: {exc}")
        if _debug():
            log.info(
                "OWUITraceCorrelator: generation logged — trace_id=%s parent=%s name=%s",
                gen_kwargs.get("trace_id"),
                gen_kwargs.get("parent_observation_id"),
                gen_kwargs.get("name"),
            )

    # ── event handlers ─────────────────────────────────────────────────────────

    async def async_log_success_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Fires after LiteLLM completes a successful LLM call."""
        self._emit_generation(
            kwargs=kwargs,
            response_obj=response_obj,
            start_time=start_time,
            end_time=end_time,
        )

    async def async_log_failure_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Fires when an LLM call FAILS — emit an ERROR-level generation so
        failed calls are visible in the trace instead of vanishing."""
        error_info = _extract_error_info(kwargs) or {}
        status_message = str(
            error_info.get("error_str")
            or kwargs.get("exception")
            or "LLM call failed"
        )[:500]
        self._emit_generation(
            kwargs=kwargs,
            response_obj=response_obj,
            start_time=start_time,
            end_time=end_time,
            level="ERROR",
            status_message=status_message,
        )


# Module-level singleton — referenced by config/litellm-config.yaml as:
#   success_callback / failure_callback: ["litellm_callbacks.owui_trace_correlator"]
owui_trace_correlator = OWUITraceCorrelator()