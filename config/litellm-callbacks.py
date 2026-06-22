"""
config/litellm-callbacks.py

LiteLLM CustomLogger: correlates each LiteLLM completion with the Langfuse trace
that the OWUI in-process Langfuse filter function already created for that turn,
then appends a cost-tagged generation observation to it.

How it works
------------
The OWUI Langfuse filter (config/functions/langfuse_filter.py) creates a
deterministic trace ID using:

    trace_id = sha256(f"{chat_id}:{message_id}".encode()).digest()[:16].hex()

This mirrors Langfuse.create_trace_id(seed=...) — confirmed from langfuse==3.15.0.
This callback recomputes the same ID from OWUI's forwarded request headers and
attaches a Langfuse "generation" observation with LiteLLM's accurate cost + usage.

NOTE: The LiteLLM container ships langfuse v2 (2.57.x), not v3. This callback
uses the langfuse v2 SDK API: lf.generation(trace_id=..., usage={...}).
The langfuse v2 Usage dict supports total_cost for passing LiteLLM's cost data.

Setup checklist
---------------
1. docker-compose.prod.yaml — mount this file into owui-litellm:
       ./config/litellm-callbacks.py:/app/litellm_callbacks.py:ro

2. litellm-config.yaml — register under success_callback:
       success_callback: ["otel", "litellm_callbacks.owui_trace_correlator"]

3. OWUI Admin → Connections → (LiteLLM entry) → Headers — add:
       {"X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}"}
   (X-OpenWebUI-Chat-Id is already forwarded automatically by
    ENABLE_FORWARD_USER_INFO_HEADERS=true in docker-compose.prod.yaml.)

4. LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY must be set in
   the litellm container environment (already present in docker-compose.prod.yaml).

Result in Langfuse
------------------
Each chat-turn trace will contain TWO generation-level observations:
  • llm_response        — created by the filter outlet (input/output text, masked)
  • litellm_generation  — created by this callback (accurate cost, token counts)
They share the same trace_id so they appear together under one trace.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
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


# ── logger ────────────────────────────────────────────────────────────────────

class OWUITraceCorrelator(CustomLogger):
    """Appends a cost-enriched Langfuse generation to the per-turn trace
    that the OWUI Langfuse filter function seeded via inlet/outlet.

    Uses the langfuse v2 SDK API (lf.generation) since the LiteLLM container
    ships langfuse v2, not v3.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lf = None
        self._init_langfuse()

    def _init_langfuse(self) -> None:
        host = os.getenv("LANGFUSE_HOST")
        pub = os.getenv("LANGFUSE_PUBLIC_KEY")
        sec = os.getenv("LANGFUSE_SECRET_KEY")
        if not (host and pub and sec):
            log.warning(
                "OWUITraceCorrelator: LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY not set — disabled"
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

    # ── event handler ─────────────────────────────────────────────────────────

    async def async_log_success_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Fires after LiteLLM completes a successful LLM call.

        Uses langfuse v2 API: lf.generation(trace_id=..., usage={...})
        """
        if not self._lf:
            return
        try:
            hdrs = self._proxy_headers(kwargs)
            chat_id = self._header(hdrs, "X-OpenWebUI-Chat-Id")
            message_id = self._header(hdrs, "X-OpenWebUI-Message-Id")

            log.debug(
                "OWUITraceCorrelator: chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )

            # Skip non-OWUI calls and temporary ("local") sessions.
            if not chat_id or chat_id == "local":
                log.debug(
                    "OWUITraceCorrelator: skipping (no chat_id or local session)"
                )
                return

            trace_id = _trace_id(chat_id, message_id)
            log.debug(
                "OWUITraceCorrelator: chat_id=%s message_id=%s → trace_id=%s",
                chat_id,
                message_id,
                trace_id,
            )

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

            # ── attach generation to the filter's trace (v2 API) ──────────
            model = str(kwargs.get("model", "unknown"))
            metadata: dict[str, Any] = {
                "source": "owui_trace_correlator",
                "chat_id": chat_id,
                "message_id": message_id,
                "litellm_call_id": str(kwargs.get("litellm_call_id", "")),
            }
            custom_provider = (
                kwargs.get("litellm_params", {}).get("custom_llm_provider")
            )
            if custom_provider:
                metadata["custom_llm_provider"] = str(custom_provider)

            self._lf.generation(
                trace_id=trace_id,
                name="litellm_generation",
                model=model,
                start_time=self._to_dt(start_time),
                end_time=self._to_dt(end_time),
                usage=usage if usage else None,
                metadata=metadata,
            )
            # Flush immediately so the observation lands before the next request.
            self._lf.flush()

            log.info(
                "OWUITraceCorrelator: generation logged — trace_id=%s cost=%.6f",
                trace_id,
                cost,
            )

        except Exception as exc:
            log.warning(
                "OWUITraceCorrelator: async_log_success_event failed: %s",
                exc,
                exc_info=True,
            )


# Module-level singleton — referenced by litellm-config.yaml as:
#   success_callback: ["litellm_callbacks.owui_trace_correlator"]
owui_trace_correlator = OWUITraceCorrelator()
