"""
config/litellm-callbacks.py

LiteLLM CustomLogger: correlates each LiteLLM completion with the Langfuse trace
that the OWUI in-process Langfuse filter function already created for that turn,
then appends a fully-enriched generation observation to it.

How it works
------------
The OWUI Langfuse filter (config/functions/langfuse_filter.py) creates a
deterministic trace ID using:

    trace_id = sha256(f"{chat_id}:{message_id}".encode()).digest()[:16].hex()

This mirrors Langfuse.create_trace_id(seed=...) — confirmed from langfuse==3.15.0.
This callback recomputes the same ID from OWUI's forwarded request headers and
attaches a Langfuse "generation" observation with LiteLLM's accurate cost + usage
PLUS the per-call input messages, completion output, tool calls, reasoning,
gateway TTFT (completion_start_time), and model parameters.

NOTE: The LiteLLM container ships langfuse v2 (2.57.x), not v3. This callback
uses the langfuse v2 SDK API: lf.generation(trace_id=..., usage={...}).
The langfuse v2 Usage dict supports total_cost for passing LiteLLM's cost data.

Setup checklist
---------------
1. docker-compose.prod.yaml — mount this file into owui-litellm:
       ./config/litellm-callbacks.py:/app/litellm_callbacks.py:ro

2. litellm-config.yaml — register under success_callback:
       success_callback: ["langfuse", "litellm_callbacks.owui_trace_correlator"]

3. OWUI Admin → Connections → (LiteLLM entry) → Headers — add:
       {"X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}"}
   (X-OpenWebUI-Chat-Id is already forwarded automatically by
    ENABLE_FORWARD_USER_INFO_HEADERS=true in docker-compose.prod.yaml.)

4. LANGFUSE_HOST / OWUI_LANGFUSE_PUBLIC_KEY / OWUI_LANGFUSE_SECRET_KEY must be set
   in the litellm container environment (already present in docker-compose.prod.yaml).

Result in Langfuse
------------------
Each chat-turn trace will contain TWO generation-level observations:
  • llm_response        — created by the filter outlet (input/output text, masked)
  • litellm_generation  — created by this callback (per-call input/output, tool calls,
                          reasoning, TTFT, model params, accurate cost + token counts)
They share the same trace_id so they appear together under one trace.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("owui_trace_correlator")
log.setLevel(logging.DEBUG)


# ── trace-ID formula ──────────────────────────────────────────────────────────

def _trace_id(chat_id: str, message_id: Optional[str]) -> str:
    """Reproduces Langfuse.create_trace_id(seed=…) from langfuse==3.15.0.

    Source (confirmed in running owui-app container):
        return sha256(seed.encode("utf-8")).digest()[:16].hex()

    Seed mirrors langfuse_filter.py → Filter._trace_id_for():
        f"{chat_id}:{message_id}"  if message_id is present
        f"{chat_id}"               fallback (whole-chat trace)
    """
    seed = f"{chat_id}:{message_id}" if message_id else chat_id
    return hashlib.sha256(seed.encode("utf-8")).digest()[:16].hex()


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


def _extract_ttft_seconds(kwargs: dict) -> Optional[float]:
    """Gateway-measured time-to-first-token from the standard logging payload."""
    slo = kwargs.get("measured_time_to_first_token") or kwargs.get("time_to_first_token")
    if isinstance(slo, (int, float)):
        return round(float(slo), 4)
    std = kwargs.get("standard_logging_object") or {}
    if isinstance(std, dict):
        ttft = std.get("time_to_first_token")
        if isinstance(ttft, (int, float)):
            return round(float(ttft), 4)
    return None


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
    ships langfuse v2, not v3.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lf = None
        self._init_langfuse()

    def _init_langfuse(self) -> None:
        # Use OWUI-project-specific keys so this correlator always posts cost observations
        # into the OWUI Langfuse project, even when the native 'langfuse' callback is
        # configured with different (proxy-project) keys via LANGFUSE_PUBLIC/SECRET_KEY.
        host = os.getenv("LANGFUSE_HOST")
        pub = os.getenv("OWUI_LANGFUSE_PUBLIC_KEY")
        sec = os.getenv("OWUI_LANGFUSE_SECRET_KEY")
        if not (host and pub and sec):
            log.warning(
                "OWUITraceCorrelator: LANGFUSE_HOST/OWUI_LANGFUSE_PUBLIC_KEY/OWUI_LANGFUSE_SECRET_KEY not set — disabled"
            )
            return
        try:
            from langfuse import Langfuse  # noqa: PLC0415

            # langfuse v2 constructor — no tracer_provider; v2 uses HTTP API, not OTEL.
            self._lf = Langfuse(host=host, public_key=pub, secret_key=sec)
            log.info(
                "OWUITraceCorrelator: Langfuse v2 client initialized (host=%s)", host
            )
        except Exception as exc:
            log.error("OWUITraceCorrelator: Langfuse init failed — %s", exc)

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
        if not self._lf:
            return
        try:
            hdrs = self._proxy_headers(kwargs)
            chat_id = self._header(hdrs, "X-OpenWebUI-Chat-Id")
            message_id = self._header(hdrs, "X-OpenWebUI-Message-Id")

            # Skip non-OWUI calls and temporary ("local") sessions.
            if not chat_id or chat_id == "local":
                return

            trace_id = _trace_id(chat_id, message_id)

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
                total_t = int(getattr(u, "total_tokens", None) or (in_t + out_t))
                usage["input"] = in_t
                usage["output"] = out_t
                usage["total"] = total_t
                usage["unit"] = "TOKENS"

            # ── cost — langfuse v2 Usage dict supports total_cost ──────────
            cost = float(kwargs.get("response_cost") or 0)
            if cost:
                usage["total_cost"] = cost

            # ── per-call content (masked + truncated) ─────────────────────
            input_messages = _extract_input_messages(kwargs)
            output_text = _extract_output_text(response_obj)
            tool_calls = _extract_tool_calls(response_obj)
            reasoning = _extract_reasoning(response_obj)
            ttft_seconds = _extract_ttft_seconds(kwargs)
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
                "ttft_seconds": ttft_seconds,
                "tool_call_count": len(tool_calls) if tool_calls else 0,
            }
            custom_provider = (
                kwargs.get("litellm_params", {}).get("custom_llm_provider")
            )
            if custom_provider:
                metadata["custom_llm_provider"] = str(custom_provider)

            # ── attach generation to the filter's trace (v2 API) ──────────
            gen_kwargs: dict[str, Any] = {
                "trace_id": trace_id,
                "name": "litellm_generation",
                "model": model,
                "start_time": self._to_dt(start_time),
                "end_time": self._to_dt(end_time),
                "usage": usage if usage else None,
                "metadata": _safe_data(metadata),
                "level": level,
                "status_message": status_message,
            }

            # Per-call input: the exact messages sent to the provider.
            if input_messages is not None:
                gen_kwargs["input"] = _safe_data(input_messages)

            # Per-call output: completion text, tool calls, reasoning.
            output_payload: dict[str, Any] = {}
            if output_text is not None:
                output_payload["text"] = output_text
            if tool_calls:
                output_payload["tool_calls"] = tool_calls
            if reasoning:
                output_payload["reasoning"] = reasoning
            if output_payload:
                gen_kwargs["output"] = _safe_data(output_payload)

            # Gateway TTFT → Langfuse's native completion_start_time (renders
            # as the TTFT bar in the generation timeline UI).
            if ttft_seconds is not None:
                start_dt = self._to_dt(start_time)
                if start_dt is not None:
                    gen_kwargs["completion_start_time"] = start_dt + timedelta(seconds=ttft_seconds)

            if model_parameters:
                gen_kwargs["model_parameters"] = _safe_data(model_parameters)

            self._lf.generation(**gen_kwargs)
            # Flush immediately so the observation lands before the next request.
            self._lf.flush()

            log.info(
                "OWUITraceCorrelator: generation logged — trace_id=%s cost=%.6f",
                trace_id,
                cost,
            )

        except Exception as exc:
            log.warning(
                "OWUITraceCorrelator: _emit_generation failed: %s",
                exc,
                exc_info=True,
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


# Module-level singleton — referenced by litellm-config.yaml as:
#   success_callback: ["litellm_callbacks.owui_trace_correlator"]
owui_trace_correlator = OWUITraceCorrelator()
