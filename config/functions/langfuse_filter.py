"""
title: Langfuse Filter
author: gillson (derived from open-webui Langfuse pipeline v4)
author_url: https://github.com/jlgill
version: 2.3.0
license: MIT
required_open_webui_version: 0.11.2
requirements: langfuse==4.15.6
description: In-process Langfuse v4 tracing filter for Open WebUI. One nested trace per chat turn (root agent opened at inlet, one span per LLM step opened at request time, backdated tool/reasoning children at outlet), a `record-conversation` event that mirrors the OWUI-rendered turn (messages, collapsible tool/thinking groups, inline files) linked to those observations, the final answer as root output, LiteLLM generations nested through the X-OpenWebUI-Langfuse header, valve-controlled content capture, PII masking, bounded records and fail-open hooks.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from pydantic import BaseModel

from langfuse import Langfuse, propagate_attributes

try:  # Backdating uses the SDK's OTEL tracer directly; fall back to live timestamps if the layout changes.
    from opentelemetry import trace as _otel_trace_api
    from langfuse._client.span import LangfuseEvent as _LangfuseEvent
    from langfuse._client.span import LangfuseSpan as _LangfuseSpan
    from langfuse._client.span import _OBSERVATION_CLASS_MAP
    from langfuse._client.attributes import LangfuseOtelSpanAttributes as _LangfuseAttrs

    _BACKDATE_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the installed SDK
    _BACKDATE_AVAILABLE = False

# ---------------------------------------------------------------------------
# How this filter builds a trace (all four OWUI hooks run in ONE process for a turn)
#
#   inlet    -> root `respond-to-chat` (agent) on the deterministic trace id, as a
#               true parentless root backdated to inlet (the SDK's trace_context
#               path would hang it under a random, never-exported parent span).
#               Everything else nests under it. Publishes the policy dict OWUI
#               forwards to LiteLLM as X-OpenWebUI-Langfuse.
#   request  -> one `run-agent-step` span per LLM call (fires before every call,
#               including tool-loop follow-ups); points the policy's
#               parent_observation_id at the step so the LiteLLM correlator nests
#               its `generate-response` generation under it. The first call also
#               closes a backdated `prepare-request` span (inlet -> first call:
#               tool discovery, memory, RAG, prompt assembly) with RAG retrievers
#               nested under it.
#   stream   -> per-step metrics only (TTFT, chunk/char counts, usage chunk,
#               tool-call ids, last event = end of the model call); nothing is
#               created here.
#   outlet   -> tools / reasoning / code / compaction children backdated into the
#               step that produced them (tools use OWUI's per-call started_at/
#               ended_at stamps when present, else the stream-end -> next-request
#               window), PII guardrail, `record-conversation` event
#               (the turn as OWUI renders it: assistant messages, tool/thinking
#               groups, inline files, each linked to its observations and steps),
#               root output = final answer + final root type, per-turn `perf.*`
#               numeric scores (the only aggregatable form for custom numbers in
#               Langfuse dashboards), policy reset for background tasks.
#
# Two views of one turn: the observation tree is the EXECUTION view (where time and
# tokens went, per LLM step); `record-conversation` is the CONVERSATION view (what
# the user saw, in order). The narrative never re-parents or duplicates usage.
#
# Foreground sub-agents (delegate_task) run as a child request in this process:
# their root is created as an `agent` child of the parent's current step instead
# of a new trace. Tool-approval resumes reuse the live root (same message id).
# ---------------------------------------------------------------------------

_TRACE_VERSION = "open-webui-langfuse-function-v2.3.0"
_POLICY_VERSION = 2

# Stable observation names (dashboards, evaluators and saved views key on these).
_NAME_ROOT = "respond-to-chat"
_NAME_SUBAGENT = "run-subagent"
_NAME_PREPARE = "prepare-request"
_NAME_STEP = "run-agent-step"
_NAME_THINK = "think"
_NAME_CODE = "execute-code"
_NAME_COMPACTION = "compact-context"
_NAME_RETRIEVE_SOURCE = "retrieve-source"
_NAME_GUARDRAIL = "mask-pii"
_NAME_CONVERSATION = "record-conversation"

_LEVEL_DEFAULT = "DEFAULT"
_LEVEL_WARNING = "WARNING"
_LEVEL_ERROR = "ERROR"

_OBSERVATION_TYPE_ATTR = "langfuse.observation.type"
# Numeric scores are the only custom numbers Langfuse dashboards can average/percentile;
# per-turn phase totals live here (phase spans already give per-observation latency).
_SCORE_PREFIX = "perf."
_MEMORY_CONTEXT_RE = re.compile(r"<memory_context>(.*?)</memory_context>", re.DOTALL)

RETRIEVER_TOOL_NAMES = {
    "search_web",
    "fetch_url",
    "query_knowledge_files",
    "view_knowledge_file",
    "view_file",
    "web_search_call",
    "file_search_call",
}
OPENAI_SERVER_TOOL_NAMES = {"web_search_call", "file_search_call", "computer_call"}
_SUBAGENT_TOOL_NAME = "delegate_task"
_OWUI_DETAILS_RE = re.compile(r"<details\b[^>]*>.*?</details>", re.DOTALL | re.IGNORECASE)

# Mirrors src/lib/components/chat/Messages/structuredOutput.ts (buildOutputDisplayItems):
# consecutive items of these types render as one collapsible group between messages.
_GROUPABLE_OUTPUT_TYPES = {
    "reasoning",
    "function_call",
    "open_webui:code_interpreter",
    "web_search_call",
    "file_search_call",
    "computer_call",
}
_DONE_STATUSES = {"completed", "failed", "incomplete"}
# Tool arguments/results and reasoning in the conversation view are previews; the
# full (masked, capped) payload stays on the linked tool/think observation.
_CONVERSATION_PREVIEW_CHARS = 2000
# Per-entry fields exported in the `record-conversation` section index (never content).
_SECTION_ENTRY_FIELDS = ("type", "name", "call_id", "state", "status", "error", "duration", "chars", "fallback")

_MASK_EMAILS = True
_MASK_PHONE_NUMBERS = True
_MASK_API_KEYS = True
_MASK_REPLACEMENT = "[REDACTED]"

_MAX_FIELD_CHARS = 20000
_MAX_COLLECTION_ITEMS = 200
_STREAM_CHARS_PER_TOKEN = 4.0
# Oversized records degrade progressively (input, then output) before being dropped.
_MAX_RECORD_BYTES = 1_048_576
# Metadata keys that carry content-ish values and are dropped while capture is off.
_CONTENT_METADATA_KEYS = {"source_name"}

# Turns that never reach outlet (worker restart mid-turn, abandoned approvals) are
# closed with a warning so their spans still export.
_TURN_TTL_SECONDS = 6 * 3600
_MAX_LIVE_TURNS = 2000
_OMITTED = {"omitted": True}


def get_content_from_message(message: dict) -> Optional[str]:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in {"text", "input_text", "output_text"}:
                    text_parts.append(str(part.get("text", "")))
                elif "text" in part:
                    text_parts.append(str(part.get("text", "")))
        return "\n".join([part for part in text_parts if part])
    return None


def get_last_message_by_role(messages: List[dict], role: str) -> dict:
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == role:
            return message
    return {}


def maybe_json_loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not value.strip():
        return {}
    try:
        return json.loads(value)
    except Exception:
        return value


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _ns(seconds: float) -> int:
    return int(seconds * 1_000_000_000)


class _PinnableIdGenerator:
    """Delegating OTEL id generator whose trace id can be pinned for one start_span.

    Installed on the (process-wide, shared) Langfuse tracer so the root can be a real
    root on the deterministic trace id. Detected by attribute rather than class and
    carrying its own ContextVar, so a reloaded filter module reuses it instead of nesting.
    """

    pinnable_trace_ids = True

    def __init__(self, inner: Any):
        self.inner = inner
        self.pinned: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
            "langfuse_filter_pinned_trace_id", default=None
        )

    def generate_trace_id(self) -> int:
        pinned = self.pinned.get()
        return pinned if pinned is not None else self.inner.generate_trace_id()

    def generate_span_id(self) -> int:
        return self.inner.generate_span_id()

    def is_trace_id_random(self) -> bool:
        if self.pinned.get() is not None:
            return False
        check = getattr(self.inner, "is_trace_id_random", None)
        return bool(check()) if callable(check) else False

    def __getattr__(self, name: str) -> Any:
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)


@dataclass
class _Step:
    index: int
    obs: Any
    requested_at: float
    model: Optional[str]
    ended_at: Optional[float] = None
    first_chunk_at: Optional[float] = None
    llm_ended_at: Optional[float] = None
    # Last stream event of the call (the trailing usage chunk follows finish_reason);
    # OWUI only starts executing tools after the stream is drained.
    last_event_at: Optional[float] = None
    finish_reason: Optional[str] = None
    chunks: int = 0
    content_chars: int = 0
    reasoning_chars: int = 0
    tool_chars: int = 0
    tool_call_ids: Set[str] = field(default_factory=set)
    tool_call_names: List[str] = field(default_factory=list)
    text_parts: List[str] = field(default_factory=list)
    text_chars: int = 0
    stream_usage: Optional[Dict[str, int]] = None
    closed: bool = False


@dataclass
class _Turn:
    key: str
    trace_id: str
    root: Any
    attrs: Dict[str, Any]
    policy: Dict[str, Any]
    started_at: float
    chat_id: str
    message_id: Optional[str]
    user_id: Optional[str]
    nested: bool = False
    subagent: bool = False
    resume_pending: bool = False
    otel_trace_id: Optional[str] = None
    steps: List[_Step] = field(default_factory=list)
    message_count: int = 0
    nested_agents: int = 0
    source_count: int = 0
    resumed: int = 0

    @property
    def current(self) -> Optional[_Step]:
        if self.steps and not self.steps[-1].closed:
            return self.steps[-1]
        return None


class Filter:
    class Valves(BaseModel):
        # --- Connection (per-deployment) ---
        priority: int = 0
        secret_key: str = "your-secret-key-here"
        public_key: str = "your-public-key-here"
        host: str = "https://cloud.langfuse.com"
        enabled: bool = False
        debug: bool = False

        # --- Langfuse project context ---
        environment: str | None = None
        release: str | None = None
        sample_rate: float = 1.0

        # --- Capture toggles (privacy/cost/compliance) ---
        capture_inputs: bool = False
        capture_outputs: bool = False
        capture_tool_arguments: bool = False
        capture_tool_outputs: bool = False
        capture_retriever_outputs: bool = False
        capture_reasoning: bool = False
        capture_code_interpreter: bool = False
        capture_multimodal: bool = False

        # --- Masking ---
        enable_masking: bool = True
        mask_metadata_keys: str = (
            "authorization,cookie,set-cookie,api_key,secret_key,access_token,refresh_token,password,token,key"
        )

        # --- Structure ---
        capture_stream_metrics: bool = True
        nest_subagents: bool = True
        perf_scores: bool = True

    def __init__(self):
        self.name = "Langfuse Filter"
        self.valves = self.Valves()
        self.langfuse: Optional[Langfuse] = None
        # turn key (chat:message) -> live turn. Hooks of one turn run in one process.
        self._turns: Dict[str, _Turn] = {}
        self._last_log = float("-inf")
        self.dropped = 0
        self._last_init_attempt = 0.0
        self._init_retry_interval = 30.0

    # ------------------------------------------------------------------ logging
    def log(self, message: str, force: bool = False):
        """Debug valve prints detail; otherwise forced messages become a throttled one-liner."""
        if self.valves.debug:
            print(f"[Langfuse Filter] {message}")
            return
        if force and time.monotonic() - self._last_log >= 60:
            self._last_log = time.monotonic()
            print("[Langfuse Filter] telemetry degraded; details suppressed (enable the debug valve to diagnose)")

    # ----------------------------------------------------------------- lifecycle
    async def on_valves_updated(self):
        # Never shutdown+rebuild the client here: langfuse v4's resource manager is a
        # process-wide singleton keyed by public key and a shut-down instance is never
        # evicted, so a rebuild would silently return a dead client.
        self.log("Valves updated; connection changes require a container restart.")

    def _normalize_host(self, raw: str) -> str:
        value = (raw or "").strip().rstrip("/")
        if not value:
            return "https://cloud.langfuse.com"
        if value.startswith("http://") or value.startswith("https://"):
            return value
        return f"https://{value}"

    def _isolated_tracer_provider(self):
        """Dedicated provider so Langfuse never exports OWUI's own OTEL spans (and vice versa)."""
        try:
            from opentelemetry.sdk.trace import TracerProvider
        except Exception as e:
            self.log(f"OTEL SDK unavailable; cannot isolate tracer provider: {e}", force=True)
            return None
        try:
            from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

            rate = self.valves.sample_rate
            sampler = TraceIdRatioBased(rate) if (rate is not None and rate < 1) else None
            return TracerProvider(sampler=sampler) if sampler is not None else TracerProvider()
        except Exception:
            try:
                return TracerProvider()
            except Exception as e:
                self.log(f"Could not build isolated tracer provider: {e}", force=True)
                return None

    def set_langfuse(self):
        if self.langfuse is not None or not self.valves.enabled:
            return
        try:
            kwargs: Dict[str, Any] = {
                "secret_key": self.valves.secret_key,
                "public_key": self.valves.public_key,
                "base_url": self._normalize_host(self.valves.host),
                "debug": False,
                "sample_rate": self.valves.sample_rate,
                "timeout": 2,
                "flush_at": 32,
                "flush_interval": 1,
                "additional_headers": {"x-langfuse-ingestion-version": "4"},
                "mask": self.mask_data,
            }
            if self.valves.environment:
                kwargs["environment"] = self.valves.environment
            if self.valves.release:
                kwargs["release"] = self.valves.release
            provider = self._isolated_tracer_provider()
            if provider is None:
                return
            kwargs["tracer_provider"] = provider
            self.langfuse = Langfuse(**kwargs)
            resources = self.langfuse._resources
            for queue in (resources._score_ingestion_queue, resources._media_upload_queue):
                with queue.mutex:
                    queue.maxsize = 1024
        except Exception as e:
            self.log(f"Langfuse initialization failed: {e}", force=True)
            self.langfuse = None

    def _ensure_langfuse(self) -> bool:
        if self.langfuse is not None:
            return True
        if not self.valves.enabled:
            return False
        now = time.monotonic()
        if now - self._last_init_attempt < self._init_retry_interval:
            return False
        self._last_init_attempt = now
        self.set_langfuse()
        return self.langfuse is not None

    # ----------------------------------------------------------------- masking
    def _mask_key_segments(self) -> set[frozenset[str]]:
        raw = self.valves.mask_metadata_keys
        if getattr(self, "_mask_segments_src", None) != raw:
            self._mask_segments_src = raw
            self._mask_segments_cache = {
                frozenset(seg for seg in re.split(r"[^a-z0-9]+", entry.strip().lower()) if seg)
                for entry in raw.split(",")
                if entry.strip()
            }
        return self._mask_segments_cache

    def _should_mask_key(self, key: str) -> bool:
        """Segment-superset match: "access_token" is masked, "est_output_tokens" is not."""
        segments = {seg for seg in re.split(r"[^a-z0-9]+", key.strip().lower()) if seg}
        if not segments:
            return False
        return any(mask_seg and mask_seg.issubset(segments) for mask_seg in self._mask_key_segments())

    def _count_redactions(self, text: str) -> Dict[str, int]:
        if not isinstance(text, str) or not text:
            return {}
        counts: Dict[str, int] = {}
        if not self.valves.capture_multimodal:
            n = len(re.findall(r"data:([^;,\s]+);base64,[A-Za-z0-9+/=\r\n]+", text))
            if n:
                counts["data_uris"] = n
        if _MASK_EMAILS:
            n = len(re.findall(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", text))
            if n:
                counts["emails"] = n
        if _MASK_PHONE_NUMBERS:
            n = len(re.findall(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", text))
            if n:
                counts["phone_numbers"] = n
        if _MASK_API_KEYS:
            n = (
                len(re.findall(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", text, flags=re.I))
                + len(re.findall(r"\b(?:sk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{12,}\b", text))
                + len(re.findall(r"\b(?:sk|pk)-lf-[A-Za-z0-9_-]{12,}\b", text))
                + len(re.findall(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b", text))
            )
            if n:
                counts["api_keys"] = n
        return counts

    def _mask_string(self, value: str) -> str:
        result = value
        if not self.valves.capture_multimodal:
            result = re.sub(
                r"data:([^;,\s]+);base64,[A-Za-z0-9+/=\r\n]+",
                lambda match: f"[REDACTED {match.group(1)} DATA URI]",
                result,
            )
        if _MASK_EMAILS:
            result = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", _MASK_REPLACEMENT, result)
        if _MASK_PHONE_NUMBERS:
            result = re.sub(
                r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", _MASK_REPLACEMENT, result
            )
        if _MASK_API_KEYS:
            result = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", f"Bearer {_MASK_REPLACEMENT}", result, flags=re.I)
            result = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{12,}\b", _MASK_REPLACEMENT, result)
            result = re.sub(r"\b(?:sk|pk)-lf-[A-Za-z0-9_-]{12,}\b", _MASK_REPLACEMENT, result)
            result = re.sub(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b", _MASK_REPLACEMENT, result)
        if len(result) > _MAX_FIELD_CHARS:
            result = f"{result[:_MAX_FIELD_CHARS]}... [TRUNCATED {len(result) - _MAX_FIELD_CHARS} chars]"
        return result

    def mask_data(self, data: Any, **_: Any) -> Any:
        return self._safe_data(data)

    def _safe_data(self, data: Any, depth: int = 0) -> Any:
        if depth > 12:
            return "[MAX_DEPTH]"
        if data is None or isinstance(data, (bool, int, float)):
            return data
        if isinstance(data, str):
            return self._mask_string(data)
        if isinstance(data, dict):
            output = {}
            for index, (key, value) in enumerate(data.items()):
                if index >= _MAX_COLLECTION_ITEMS:
                    output["__truncated_items__"] = len(data) - _MAX_COLLECTION_ITEMS
                    break
                key_string = str(key)
                if self.valves.enable_masking and self._should_mask_key(key_string):
                    output[key_string] = _MASK_REPLACEMENT
                else:
                    output[key_string] = self._safe_data(value, depth + 1)
            return output
        if isinstance(data, (list, tuple, set)):
            items = list(data)
            output = [self._safe_data(value, depth + 1) for value in items[:_MAX_COLLECTION_ITEMS]]
            if len(items) > _MAX_COLLECTION_ITEMS:
                output.append({"__truncated_items__": len(items) - _MAX_COLLECTION_ITEMS})
            return output
        return self._mask_string(str(data))

    # ------------------------------------------------------------- capture gates
    def _capture(self) -> bool:
        return self.valves.capture_inputs or self.valves.capture_outputs

    def _permit_input(self, kind: str = "content") -> bool:
        if not self.valves.capture_inputs:
            return False
        if kind == "tool":
            return self.valves.capture_tool_arguments
        if kind == "code":
            return self.valves.capture_code_interpreter
        return True

    def _permit_output(self, kind: str = "content") -> bool:
        if not self.valves.capture_outputs:
            return False
        if kind == "tool":
            return self.valves.capture_tool_outputs
        if kind == "retriever":
            return self.valves.capture_tool_outputs and self.valves.capture_retriever_outputs
        if kind == "source":
            return self.valves.capture_retriever_outputs
        if kind == "reasoning":
            return self.valves.capture_reasoning
        if kind == "code":
            return self.valves.capture_code_interpreter
        return True

    @staticmethod
    def _omitted(text: Optional[str] = None, **extra: Any) -> dict:
        marker = dict(_OMITTED)
        if text is not None:
            marker["content_length"] = len(text)
        marker.update(extra)
        return marker

    @staticmethod
    def _record_size(payload: dict) -> int:
        try:
            return len(json.dumps(payload, default=str).encode())
        except Exception:
            return _MAX_RECORD_BYTES + 1

    def _finalize(self, payload: dict) -> dict:
        """Mask, apply the capture-off metadata rule and the size cap to a record."""
        payload = {key: value for key, value in payload.items() if value is not None}
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            if not self._capture():
                metadata = {k: v for k, v in metadata.items() if k not in _CONTENT_METADATA_KEYS}
            payload["metadata"] = self._safe_data(metadata)
        for key in ("input", "output"):
            if key in payload:
                payload[key] = self._safe_data(payload[key])
        if self._record_size(payload) > _MAX_RECORD_BYTES:
            for key in ("input", "output"):
                if key in payload:
                    payload[key] = self._omitted(reason="record_size")
                    if self._record_size(payload) <= _MAX_RECORD_BYTES:
                        return payload
            self.dropped += 1
            raise ValueError("Telemetry record exceeds configured size after content removal")
        return payload

    # ------------------------------------------------------------- ids / keys
    def _normalize_chat_id(self, chat_id: Optional[Any], session_id: Optional[Any] = None) -> str:
        if chat_id == "local":
            return f"temporary-session-{session_id or uuid.uuid4()}"
        if chat_id:
            return str(chat_id)
        if session_id:
            return f"temporary-session-{session_id}"
        return f"trace-{uuid.uuid4()}"

    def _ids(self, metadata: Optional[dict], body: dict) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
        """(normalized chat id, message id, socket session id, raw chat id)."""
        metadata = metadata if isinstance(metadata, dict) else {}
        raw_chat = metadata.get("chat_id") or body.get("chat_id")
        session_id = metadata.get("session_id") or body.get("session_id")
        chat_id = self._normalize_chat_id(raw_chat, session_id)
        message_id = metadata.get("message_id") or metadata.get("id") or body.get("message_id") or body.get("id")
        return (
            chat_id,
            str(message_id) if message_id else None,
            str(session_id) if session_id else None,
            str(raw_chat) if raw_chat else None,
        )

    @staticmethod
    def _turn_key(chat_id: str, message_id: Optional[str]) -> str:
        return f"{chat_id}:{message_id or '-'}"

    def _trace_id_for(self, chat_id: str, message_id: Optional[str]) -> str:
        # Same seed as the LiteLLM correlator's fallback formula; keep them identical.
        if message_id:
            return Langfuse.create_trace_id(seed=f"{chat_id}:{message_id}")
        return Langfuse.create_trace_id()

    def _turn_for(self, metadata: Optional[dict], body: Optional[dict]) -> Optional[_Turn]:
        chat_id, message_id, _, _ = self._ids(metadata, body or {})
        return self._turns.get(self._turn_key(chat_id, message_id))

    @staticmethod
    def _owui_otel_trace_id() -> Optional[str]:
        """OWUI's own OTEL trace id (FastAPI/httpx/SQL spans in Tempo) for cross-linking."""
        if not _BACKDATE_AVAILABLE:
            return None
        try:
            context = _otel_trace_api.get_current_span().get_span_context()
            if context is not None and context.is_valid:
                return format(context.trace_id, "032x")
        except Exception:
            pass
        return None

    @staticmethod
    def _tool_inventory(tools_meta: Any) -> Dict[str, Any]:
        """Count resolved tools by origin (OWUI tool_id prefixes) and distinct tool servers."""
        by_kind: Dict[str, int] = {}
        servers: Set[str] = set()
        if isinstance(tools_meta, dict):
            for tool in tools_meta.values():
                if not isinstance(tool, dict):
                    continue
                tool_id = str(tool.get("tool_id") or "")
                if tool.get("direct"):
                    kind = "direct"
                elif tool_id.startswith("builtin:"):
                    kind = "builtin"
                elif tool_id.startswith("server:mcp:"):
                    kind = "mcp"
                    servers.add(tool_id.split("|", 1)[0])
                elif tool_id.startswith("server:"):
                    kind = "openapi"
                    servers.add(tool_id.split("|", 1)[0])
                elif tool_id.startswith("terminal:"):
                    kind = "terminal"
                else:
                    kind = "local"
                by_kind[kind] = by_kind.get(kind, 0) + 1
        return {"by_kind": by_kind, "server_count": len(servers)}

    # ------------------------------------------------------- observation helpers
    @contextlib.contextmanager
    def _attrs(self, attrs: Dict[str, Any]) -> Iterator[None]:
        """Trace attributes land only on spans created inside propagate_attributes()."""
        try:
            manager = propagate_attributes(**attrs)
        except Exception as e:
            self.log(f"propagate_attributes rejected: {e}")
            yield
            return
        with manager:
            yield

    def _end(self, obs: Any, end_at: Optional[float] = None):
        if obs is None:
            return
        try:
            if end_at is not None:
                obs.end(end_time=_ns(end_at))
            else:
                obs.end()
        except Exception:
            try:
                obs.end()
            except Exception:
                pass

    def _backdated(self, turn: _Turn, parent: Any, *, as_type: str, name: str, start_at: float, payload: dict) -> Any:
        return self._open_span(turn.attrs, parent._otel_span, as_type=as_type, name=name, start_at=start_at, payload=payload)

    def _open_span(
        self,
        attrs: Dict[str, Any],
        parent_span: Any,
        *,
        as_type: str,
        name: str,
        start_at: float,
        payload: dict,
        root_trace_id: Optional[str] = None,
    ) -> Any:
        client = self.langfuse
        tracer = client._otel_tracer
        token = None
        if root_trace_id is not None:
            token = tracer.id_generator.pinned.set(int(root_trace_id, 16))
        try:
            with self._attrs(attrs):
                with _otel_trace_api.use_span(parent_span):
                    raw = tracer.start_span(name=name, start_time=_ns(start_at))
        finally:
            if token is not None:
                tracer.id_generator.pinned.reset(token)
        if root_trace_id is not None:
            raw.set_attribute(_LangfuseAttrs.AS_ROOT, True)
        if as_type == "event":
            cls = _LangfuseEvent
        else:
            cls = _OBSERVATION_CLASS_MAP.get(as_type) or _LangfuseSpan
        fields = {key: payload[key] for key in ("input", "output", "metadata", "level", "status_message") if key in payload}
        return cls(
            otel_span=raw,
            langfuse_client=client,
            environment=client._environment,
            release=client._release,
            **fields,
        )

    def _pin_trace_ids(self) -> bool:
        tracer = getattr(self.langfuse, "_otel_tracer", None)
        generator = getattr(tracer, "id_generator", None)
        if generator is None:
            return False
        if not getattr(generator, "pinnable_trace_ids", False):
            tracer.id_generator = _PinnableIdGenerator(generator)
        return True

    def _start_root(self, attrs: Dict[str, Any], trace_id: str, *, start_at: float, payload: dict) -> Any:
        """Parentless root on the deterministic trace id, backdated to inlet.

        start_observation(trace_context={"trace_id": ...}) parents the root under a
        random, never-exported span id, so Langfuse reports a missing parent and the
        root starts after the children backdated to inlet.
        """
        if _BACKDATE_AVAILABLE:
            try:
                if self._pin_trace_ids():
                    root = self._open_span(
                        attrs,
                        _otel_trace_api.INVALID_SPAN,
                        as_type="agent",
                        name=payload["name"],
                        start_at=start_at,
                        payload=payload,
                        root_trace_id=trace_id,
                    )
                    if format(root._otel_span.get_span_context().trace_id, "032x") == trace_id:
                        return root
                    self.log("pinned root got an unexpected trace id; using trace_context")
            except Exception as e:
                self.log(f"parentless root failed; using trace_context: {e}")
        with self._attrs(attrs):
            return self.langfuse.start_observation(trace_context={"trace_id": trace_id}, as_type="agent", **payload)

    def _start_nested_root(self, attrs: Dict[str, Any], anchor: Any, *, start_at: float, payload: dict) -> Any:
        if _BACKDATE_AVAILABLE:
            try:
                return self._open_span(attrs, anchor._otel_span, as_type="agent", name=payload["name"], start_at=start_at, payload=payload)
            except Exception as e:
                self.log(f"backdating nested root failed; using live timestamps: {e}")
        with self._attrs(attrs):
            return anchor.start_observation(as_type="agent", **payload)

    def _child(
        self,
        turn: _Turn,
        parent: Any,
        *,
        as_type: str,
        name: str,
        input_data: Any = None,
        output_data: Any = None,
        metadata: Optional[dict] = None,
        level: Optional[str] = None,
        status_message: Optional[str] = None,
        start_at: Optional[float] = None,
        end_at: Optional[float] = None,
        end: bool = False,
    ) -> Any:
        """Create a child of `parent` (never via trace_context). Backdated when start_at is given."""
        payload = self._finalize(
            {
                "name": name,
                "input": input_data,
                "output": output_data,
                "metadata": metadata or {},
                "level": level,
                "status_message": status_message,
            }
        )
        obs = None
        if start_at is not None and _BACKDATE_AVAILABLE:
            try:
                obs = self._backdated(turn, parent, as_type=as_type, name=name, start_at=start_at, payload=payload)
            except Exception as e:
                self.log(f"backdating {name} failed; using live timestamps: {e}")
                obs = None
        if obs is None:
            with self._attrs(turn.attrs):
                obs = parent.start_observation(as_type=as_type, **payload)
            if as_type == "event":
                return obs  # the wrapper already ended it
        if end:
            self._end(obs, end_at)
        return obs

    def _update(self, obs: Any, **fields: Any):
        try:
            payload = self._finalize(fields)
            if payload:
                obs.update(**payload)
        except Exception as e:
            self.log(f"observation update failed: {e}")

    # ------------------------------------------------------------------ turns
    def _gc_turns(self, now: float):
        stale = [key for key, turn in self._turns.items() if now - turn.started_at > _TURN_TTL_SECONDS]
        if len(self._turns) - len(stale) > _MAX_LIVE_TURNS:
            ordered = sorted(self._turns.items(), key=lambda item: item[1].started_at)
            stale.extend(key for key, _ in ordered[: len(self._turns) - _MAX_LIVE_TURNS] if key not in stale)
        for key in stale:
            turn = self._turns.pop(key, None)
            if turn is None:
                continue
            try:
                current = turn.current
                if current is not None:
                    self._close_step(turn, current, end_at=now, abandoned=True)
                self._update(turn.root, level=_LEVEL_WARNING, status_message="turn abandoned (no outlet)")
                self._end(turn.root, now)
            except Exception as e:
                self.log(f"abandoned turn cleanup failed: {e}")

    def _close_step(self, turn: _Turn, step: _Step, *, end_at: float, abandoned: bool = False):
        if step.closed:
            return
        step.closed = True
        step.ended_at = end_at
        llm_end = self._llm_end(step) or end_at
        chars = step.content_chars + step.reasoning_chars + step.tool_chars
        est_tokens = int(chars / _STREAM_CHARS_PER_TOKEN) if chars else 0
        metadata: Dict[str, Any] = {
            "step_index": step.index,
            "model": step.model,
            "step_duration_seconds": round(max(end_at - step.requested_at, 0.0), 4),
            "finish_reason": step.finish_reason,
        }
        if step.chunks:
            metadata.update(
                {
                    "ttft_seconds": round(step.first_chunk_at - step.requested_at, 4) if step.first_chunk_at else None,
                    "llm_duration_seconds": round(max(llm_end - step.requested_at, 0.0), 4),
                    "chunks": step.chunks,
                    "content_chars": step.content_chars,
                    "reasoning_chars": step.reasoning_chars,
                    "tool_call_chars": step.tool_chars,
                    "est_output_tokens": est_tokens,
                }
            )
        if step.stream_usage:
            metadata["stream_usage"] = step.stream_usage
        if abandoned:
            metadata["abandoned"] = True
        output: Dict[str, Any] = {
            "finish_reason": step.finish_reason,
            "tool_calls": list(step.tool_call_names),
            "content_chars": step.content_chars,
            "reasoning_chars": step.reasoning_chars,
        }
        if step.text_parts:
            output["text"] = "".join(step.text_parts)
        self._update(step.obs, output=output, metadata=metadata)
        self._end(step.obs, end_at)

    def _step_for(self, turn: _Turn, call_id: Optional[str], started_at: Optional[float], parser_step: int) -> Optional[_Step]:
        if not turn.steps:
            return None
        if call_id:
            for step in turn.steps:
                if call_id in step.tool_call_ids:
                    return step
        if started_at is not None:
            for index, step in enumerate(turn.steps):
                nxt = turn.steps[index + 1] if index + 1 < len(turn.steps) else None
                if step.requested_at <= started_at and (nxt is None or started_at < nxt.requested_at):
                    return step
        if 1 <= parser_step <= len(turn.steps):
            return turn.steps[parser_step - 1]
        return turn.steps[-1]

    @staticmethod
    def _llm_end(step: _Step) -> Optional[float]:
        """End of the model call: the last stream event (usage trails finish_reason)."""
        ends = [t for t in (step.llm_ended_at, step.last_event_at) if t is not None]
        return max(ends) if ends else step.first_chunk_at

    def _tool_window(self, turn: _Turn, step: _Step, now: float) -> Tuple[float, float]:
        start = self._llm_end(step) or step.requested_at
        index = turn.steps.index(step)
        nxt = turn.steps[index + 1] if index + 1 < len(turn.steps) else None
        end = nxt.requested_at if nxt is not None else (step.ended_at or now)
        if end <= start:
            end = start + 0.001
        return start, end

    # --------------------------------------------------------------- parsing
    def _extract_output_text(self, output_parts: Any) -> str:
        if isinstance(output_parts, str):
            return output_parts
        if not isinstance(output_parts, list):
            return str(output_parts) if output_parts is not None else ""
        text_parts = []
        for part in output_parts:
            if not isinstance(part, dict):
                text_parts.append(str(part))
            elif part.get("type") in {"input_text", "output_text", "text"}:
                text_parts.append(str(part.get("text", "")))
            elif "text" in part:
                text_parts.append(str(part.get("text", "")))
            elif part.get("type") in {"input_image", "image_url"}:
                image_url = part.get("image_url") or part.get("url") or ""
                text_parts.append(f"[image:{str(image_url)[:120]}]")
        return "\n".join([part for part in text_parts if part])

    def _output_item_text(self, item: dict) -> str:
        item_type = item.get("type")
        if item_type == "message":
            return self._extract_output_text(item.get("content", []))
        if item_type == "reasoning":
            return self._extract_output_text(item.get("summary") or item.get("content") or [])
        if item_type == "function_call_output":
            return self._extract_output_text(item.get("output", []))
        if item_type == "open_webui:code_interpreter":
            output = item.get("output")
            if isinstance(output, dict):
                return "\n".join(str(output.get(key, "")) for key in ["stdout", "stderr", "result"] if output.get(key))
            return str(output or "")
        return self._extract_output_text(item)

    def _is_error_output(self, item: dict, output_text: str) -> bool:
        status = str(item.get("status", "")).lower()
        if status in {"failed", "error", "incomplete"}:
            return True
        stripped = output_text.strip().lower()
        if stripped.startswith("{"):
            parsed = maybe_json_loads(output_text.strip())
            if isinstance(parsed, dict) and parsed.get("error"):
                return True
        return stripped.startswith("error:") or stripped.startswith("exception:")

    @staticmethod
    def _epoch(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        # OWUI stores time.time() seconds; tolerate millisecond timestamps.
        return float(value) / 1000.0 if value > 1e11 else float(value)

    @staticmethod
    def _parser_steps(output_items: List[Any]) -> List[int]:
        """LLM call index per output item, inferred from the reasoning -> calls -> outputs loop."""
        steps: List[int] = []
        step = 1
        step_has_output = False
        for item in output_items:
            item_type = item.get("type") if isinstance(item, dict) else None
            if step_has_output and item_type in {"reasoning", "message", "function_call"}:
                step += 1
                step_has_output = False
            steps.append(step)
            if item_type in {"function_call_output", "open_webui:code_interpreter"} or item_type in OPENAI_SERVER_TOOL_NAMES:
                step_has_output = True
        return steps

    def _parse_output_items(self, output_items: Any) -> List[dict]:
        """Flatten OWUI output items into observation specs, in original order.

        Each spec carries `kind`, `parser_step` (LLM call index inferred from the
        reasoning -> calls -> outputs loop), optional `call_id`/`started_at`/`ended_at`
        for step mapping, and already-gated input/output.
        """
        if not isinstance(output_items, list):
            return []
        calls: Dict[str, dict] = {}
        specs: List[dict] = []
        parser_steps = self._parser_steps(output_items)
        for order, item in enumerate(output_items):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            base = {
                "order": order,
                "parser_step": parser_steps[order],
                "call_id": None,
                "started_at": self._epoch(item.get("started_at")),
                "ended_at": self._epoch(item.get("ended_at")),
            }

            if item_type == "function_call":
                call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                calls[call_id] = item
                continue

            if item_type == "message":
                continue

            if item_type == "function_call_output":
                call_id = item.get("call_id") or item.get("id")
                call = calls.get(call_id, {})
                name = call.get("name") or (call.get("function") or {}).get("name") or "tool"
                arguments = call.get("arguments") or (call.get("function") or {}).get("arguments") or {}
                output_text = self._output_item_text(item)
                as_type = "retriever" if name in RETRIEVER_TOOL_NAMES else "tool"
                error = self._is_error_output(item, output_text)
                empty = not output_text.strip()
                level = _LEVEL_ERROR if error else (_LEVEL_WARNING if empty else _LEVEL_DEFAULT)
                status_message = None
                if error:
                    status_message = f"{name} returned an error-shaped result"
                elif empty:
                    status_message = f"{name} returned an empty result"
                metadata = {
                    "call_id": call_id,
                    "status": item.get("status"),
                    "tool_name": name,
                    "observation_source": "open_webui_output",
                    "has_files": bool(item.get("files")),
                    "has_embeds": bool(item.get("embeds")),
                }
                if name == _SUBAGENT_TOOL_NAME:
                    handle = maybe_json_loads(output_text.strip()) if output_text.strip().startswith("{") else None
                    if isinstance(handle, dict):
                        for key in ("chat_id", "subagent_chat_id", "delegation_id"):
                            if handle.get(key):
                                metadata[key if key != "chat_id" else "subagent_chat_id"] = str(handle[key])
                specs.append(
                    {
                        **base,
                        "kind": "tool",
                        "call_id": call_id,
                        "as_type": as_type,
                        "name": name,
                        "input": maybe_json_loads(arguments) if self._permit_input("tool") else _OMITTED,
                        "output": output_text if self._permit_output(as_type) else self._omitted(output_text),
                        "level": level,
                        "status_message": status_message,
                        "metadata": metadata,
                    }
                )
                continue

            if item_type in OPENAI_SERVER_TOOL_NAMES:
                output_text = self._output_item_text(item)
                as_type = "retriever" if item_type in RETRIEVER_TOOL_NAMES else "tool"
                error = self._is_error_output(item, output_text)
                specs.append(
                    {
                        **base,
                        "kind": "tool",
                        "as_type": as_type,
                        "name": item_type,
                        "input": (item.get("action") or item.get("queries") or _OMITTED) if self._permit_input("tool") else _OMITTED,
                        "output": output_text if self._permit_output(as_type) else self._omitted(output_text),
                        "level": _LEVEL_ERROR if error else _LEVEL_DEFAULT,
                        "status_message": f"{item_type} returned an error-shaped result" if error else None,
                        "metadata": {
                            "tool_name": item_type,
                            "status": item.get("status"),
                            "observation_source": "openai_responses_output",
                        },
                    }
                )
                continue

            if item_type == "reasoning":
                text = self._output_item_text(item)
                specs.append(
                    {
                        **base,
                        "kind": "reasoning",
                        "as_type": "span",
                        "name": _NAME_THINK,
                        "input": None,
                        "output": text if self._permit_output("reasoning") else self._omitted(text),
                        "level": _LEVEL_DEFAULT,
                        "status_message": None,
                        "metadata": {
                            "status": item.get("status"),
                            "duration": item.get("duration"),
                            "observation_source": "open_webui_reasoning_output",
                        },
                    }
                )
                continue

            if item_type == "open_webui:code_interpreter":
                output_text = self._output_item_text(item)
                error = self._is_error_output(item, output_text)
                specs.append(
                    {
                        **base,
                        "kind": "code",
                        "as_type": "tool",
                        "name": _NAME_CODE,
                        "input": (
                            {"language": item.get("lang"), "code": item.get("code")}
                            if self._permit_input("code")
                            else {**_OMITTED, "language": item.get("lang"), "code_chars": len(item.get("code") or "")}
                        ),
                        "output": output_text if self._permit_output("code") else self._omitted(output_text),
                        "level": _LEVEL_ERROR if error else _LEVEL_DEFAULT,
                        "status_message": f"{_NAME_CODE} returned an error-shaped result" if error else None,
                        "metadata": {
                            "lang": item.get("lang"),
                            "status": item.get("status"),
                            "duration": item.get("duration"),
                            "observation_source": "open_webui_code_interpreter",
                        },
                    }
                )
                continue

            if item_type == "open_webui:context_compaction":
                summary = str(item.get("summary") or "")
                specs.append(
                    {
                        **base,
                        "kind": "compaction",
                        "as_type": "event",
                        "name": _NAME_COMPACTION,
                        "input": None,
                        "output": summary if self._permit_output("content") else self._omitted(summary),
                        "level": _LEVEL_DEFAULT,
                        "status_message": None,
                        "metadata": {"summary_chars": len(summary), "observation_source": "open_webui_context_compaction"},
                    }
                )
        return specs

    def _clean_assistant_text(self, text: Optional[str]) -> Optional[str]:
        """Strip OWUI <details> tool-call markup; None when the whole content was markup."""
        if not text:
            return text
        cleaned = _OWUI_DETAILS_RE.sub("", text).strip()
        return cleaned or None

    def _final_text(self, assistant_message_obj: dict, output_items: Any) -> Optional[str]:
        text = self._clean_assistant_text(get_content_from_message(assistant_message_obj) if assistant_message_obj else None)
        if text or not isinstance(output_items, list):
            return text
        parts = [
            self._output_item_text(item)
            for item in output_items
            if isinstance(item, dict) and item.get("type") == "message"
        ]
        return "\n".join(p for p in parts if p) or None

    # ------------------------------------------------------- conversation view
    # The OWUI chat renders a turn as messages interleaved with collapsible tool /
    # thinking groups (structuredOutput.ts buildOutputDisplayItems). These helpers
    # rebuild that exact grouping so the trace can show the turn the way the user saw it.
    @staticmethod
    def _parts_text(parts: Any, skip_images: bool = False) -> str:
        """OWUI getTextFromParts / getToolResultText: part `.text` values joined with no separator."""
        if isinstance(parts, str):
            return parts
        if not isinstance(parts, list):
            return ""
        texts: List[str] = []
        for part in parts:
            if not isinstance(part, dict) or (skip_images and part.get("type") == "input_image"):
                continue
            text = part.get("text")
            if text is not None:
                texts.append(text if isinstance(text, str) else str(text))
        return "".join(texts)

    @staticmethod
    def _json_value(value: Any) -> Any:
        """OWUI parseJSONStringValue: decode while the value is still a JSON-encoded string."""
        if not isinstance(value, str):
            return value
        parsed: Any = value.strip()
        while isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except (TypeError, ValueError):
                break
        return parsed

    @staticmethod
    def _call_key(item: dict) -> str:
        """OWUI `call_id ?? id ?? ''` (nullish, not falsy, like the frontend lookup)."""
        for key in ("call_id", "id"):
            if item.get(key) is not None:
                return str(item[key])
        return ""

    @staticmethod
    def _call_fields(call: dict) -> Tuple[str, Any]:
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = call.get("name") or function.get("name") or "tool"
        return str(name), call.get("arguments") or function.get("arguments") or {}

    def _inline_file(self, call: Optional[dict], result: dict) -> Optional[dict]:
        """OWUI getInlineFileFromToolOutput: a display_file result rendered as an inline file card."""
        if not isinstance(call, dict) or call.get("name") != "display_file":
            return None
        args = self._json_value(call.get("arguments"))
        if not isinstance(args, (dict, list)):
            return None
        info = self._json_value(self._parts_text(result.get("output"), skip_images=True))
        if (
            not isinstance(info, dict)
            or info.get("type") != "file"
            or info.get("source") != "open_terminal"
            or info.get("exists") is False
            or not info.get("path")
            or not info.get("terminal_selector")
        ):
            return None
        if "page" not in info and isinstance(args, dict) and "page" in args:
            info = {**info, "page": args["page"]}
        return info

    def _detail_entry(self, item: dict, is_last: bool, output_items: List[Any], results: Dict[str, int]) -> dict:
        """One collapsible token (OWUI buildDetailToken) with the state its header shows."""
        item_type = item.get("type")
        status = "" if item.get("status") is None else str(item.get("status"))
        entry: Dict[str, Any] = {"status": item.get("status")}
        if item_type == "function_call":
            call_id = self._call_key(item)
            result_index = results.get(call_id)
            result = output_items[result_index] if result_index is not None else None
            result_text = self._output_item_text(result) if result is not None else ""
            if status == "pending":
                state = "approval_needed"
            elif result is not None or status in {"failed", "incomplete"}:
                state = "done"
            elif status == "completed":
                state = "executing"
            else:
                state = "preparing"
            entry.update(
                name=self._call_fields(item)[0],
                call_id=call_id or None,
                result_index=result_index,
                state=state,
                error=status in {"failed", "error", "incomplete"} or (result is not None and self._is_error_output(result, result_text)),
                chars=len(result_text) if result is not None else None,
            )
            return entry
        output_text = self._output_item_text(item)
        if item_type in {"reasoning", "open_webui:code_interpreter"}:
            done = status in _DONE_STATUSES or "duration" in item or not is_last
            entry.update(
                name=_NAME_THINK if item_type == "reasoning" else _NAME_CODE,
                state="done" if done else "in_progress",
                duration=item.get("duration"),
                error=item_type != "reasoning" and self._is_error_output(item, output_text),
                chars=len(output_text),
            )
            return entry
        entry.update(
            name=item_type,
            call_id=None if item.get("id") is None else str(item["id"]),
            state="done" if status in _DONE_STATUSES or not is_last else "in_progress",
            error=self._is_error_output(item, output_text),
        )
        return entry

    def _conversation_sections(self, output_items: Any) -> List[dict]:
        """Group output items into the display sections OWUI renders (buildOutputDisplayItems).

        `message` sections are assistant text; `detail_single` / `detail_group` are the
        collapsible tool / thinking / code blocks between them; `file` is an inline
        display_file card. Entries keep `item_index` (and `result_index` for tool calls)
        so each section can be linked to the observations parsed from the same items.
        """
        if not isinstance(output_items, list):
            return []
        results: Dict[str, int] = {}
        calls: Dict[str, int] = {}
        for index, item in enumerate(output_items):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call_output" and item.get("call_id"):
                results[str(item["call_id"])] = index
            elif item.get("type") == "function_call" and (item.get("call_id") or item.get("id")):
                calls[self._call_key(item)] = index
        parser_steps = self._parser_steps(output_items)
        sections: List[dict] = []
        tokens: List[dict] = []

        def push(kind: str, entries: List[dict]):
            sections.append({"index": len(sections), "kind": kind, "entries": entries})

        def flush():
            if tokens:
                push("detail_group" if len(tokens) > 1 else "detail_single", list(tokens))
                tokens.clear()

        last_index = len(output_items) - 1
        for index, item in enumerate(output_items):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            base = {
                "type": item_type,
                "item_index": index,
                "parser_step": parser_steps[index],
                "started_at": self._epoch(item.get("started_at")),
            }
            if item_type == "function_call_output":
                call_key = "" if item.get("call_id") is None else str(item["call_id"])
                call_index = calls.get(call_key)
                info = self._inline_file(output_items[call_index] if call_index is not None else None, item)
                if info is not None:
                    flush()
                    push("file", [{**base, "type": "file", "name": "display_file", "call_id": call_key or None, "result_index": index, "file": info}])
                continue
            if item_type == "function_call" and item.get("name") == "ask_user" and item.get("status") in {"pending", "in_progress"}:
                continue
            if item_type in _GROUPABLE_OUTPUT_TYPES:
                tokens.append({**base, **self._detail_entry(item, index == last_index, output_items, results)})
                continue
            text = self._parts_text(item.get("content"))
            if text.strip():
                flush()
                push("message", [{**base, "type": "message", "text": text, "chars": len(text), "fallback": item_type != "message"}])
        flush()
        return sections

    @staticmethod
    def _section_index(sections: List[dict]) -> Dict[int, List[int]]:
        """Output item index -> section indices (a tool result maps to its call's group, then its file card)."""
        index: Dict[int, List[int]] = {}
        for section in sections:
            for entry in section["entries"]:
                for key in ("item_index", "result_index"):
                    position = entry.get(key)
                    if position is not None and section["index"] not in index.setdefault(position, []):
                        index[position].append(section["index"])
        return index

    def _final_answer(self, sections: List[dict], fallback: str) -> Tuple[str, str, List[int]]:
        """What the user reads last: the trailing message sections, else the whole assistant content."""
        trailing: List[dict] = []
        for section in reversed(sections):
            if section["kind"] != "message":
                break
            trailing.insert(0, section)
        if trailing:
            text = self._clean_assistant_text("\n".join(s["entries"][0]["text"] for s in trailing).strip())
            if text:
                return text, "trailing-messages", [s["index"] for s in trailing]
        return fallback, "assistant-content", []

    def _preview(self, text: str) -> str:
        if len(text) <= _CONVERSATION_PREVIEW_CHARS:
            return text
        return f"{text[:_CONVERSATION_PREVIEW_CHARS]}... [PREVIEW of {len(text)} chars; full content on the linked observation]"

    def _tool_arguments(self, arguments: Any, permitted: bool) -> str:
        """ChatML tool-call arguments: key-masked before serialization (masking cannot see keys inside a string)."""
        if not permitted:
            return json.dumps(_OMITTED)
        safe = self._safe_data(maybe_json_loads(arguments) if isinstance(arguments, str) else arguments)
        return self._preview(safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False, default=str))

    @staticmethod
    def _tool_exchange(call_id: str, name: str, arguments: str, result: Any = None, has_result: bool = False) -> List[dict]:
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}],
            }
        ]
        if has_result:
            messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": result})
        return messages

    def _conversation_messages(self, output_items: List[Any], sections: List[dict]) -> List[dict]:
        """The turn as ChatML in OWUI display order, gated by the same valves as the observations."""
        messages: List[dict] = []
        for section in sections:
            for entry in section["entries"]:
                item = output_items[entry["item_index"]]
                kind = entry["type"]
                fallback_id = f"item-{entry['item_index']}"
                if kind == "message":
                    text = entry["text"]
                    messages.append({"role": "assistant", "content": text if self._permit_output() else self._omitted(text)})
                elif kind == "reasoning":
                    summary = item.get("summary")
                    text = self._parts_text(summary if isinstance(summary, list) and summary else item.get("content"))
                    if self._permit_output("reasoning"):
                        messages.append({"role": "assistant", "content": None, "thinking": [{"type": "thinking", "content": self._preview(text)}]})
                    else:
                        messages.append({"role": "assistant", "content": self._omitted(text, kind="reasoning")})
                elif kind == "function_call":
                    name = entry["name"]
                    result_index = entry.get("result_index")
                    result = None
                    if result_index is not None:
                        result_text = self._output_item_text(output_items[result_index])
                        gate = "retriever" if name in RETRIEVER_TOOL_NAMES else "tool"
                        result = self._preview(result_text) if self._permit_output(gate) else self._omitted(result_text)
                    arguments = self._tool_arguments(self._call_fields(item)[1], self._permit_input("tool"))
                    messages.extend(self._tool_exchange(entry.get("call_id") or fallback_id, name, arguments, result, result_index is not None))
                elif kind == "open_webui:code_interpreter":
                    code = str(item.get("code") or "")
                    if self._permit_input("code"):
                        arguments = self._tool_arguments({"language": item.get("lang"), "code": code}, True)
                    else:
                        arguments = json.dumps({**_OMITTED, "language": item.get("lang"), "code_chars": len(code)}, default=str)
                    output_text = self._output_item_text(item)
                    result = self._preview(output_text) if self._permit_output("code") else self._omitted(output_text)
                    messages.extend(self._tool_exchange(str(item.get("id") or fallback_id), _NAME_CODE, arguments, result, True))
                elif kind in OPENAI_SERVER_TOOL_NAMES:
                    output_text = self._output_item_text(item)
                    gate = "retriever" if kind in RETRIEVER_TOOL_NAMES else "tool"
                    result = self._preview(output_text) if self._permit_output(gate) else self._omitted(output_text)
                    arguments = self._tool_arguments(item.get("action") or item.get("queries") or {}, self._permit_input("tool"))
                    messages.extend(self._tool_exchange(entry.get("call_id") or fallback_id, kind, arguments, result, True))
                elif kind == "file":
                    content = f"[file: {entry['file'].get('path')}]" if self._permit_output("tool") else self._omitted(kind="file")
                    messages.append({"role": "assistant", "name": "display_file", "content": content})
        if len(messages) > _MAX_COLLECTION_ITEMS:
            # Keep the opening and the ending (final answer) intact instead of a blind cap.
            tail = 20
            head = _MAX_COLLECTION_ITEMS - tail - 1
            hidden = len(messages) - head - tail
            messages = messages[:head] + [{"role": "assistant", "content": f"[{hidden} conversation messages omitted]"}] + messages[-tail:]
        return messages

    def _emit_conversation(
        self,
        turn: _Turn,
        messages: List[dict],
        output_items: List[Any],
        sections: List[dict],
        section_observations: Dict[int, List[str]],
        answer_source: str,
        answer_sections: List[int],
        now: float,
    ) -> Optional[str]:
        """`record-conversation`: the turn as OWUI rendered it, linked to its observations and steps."""
        user_message = get_last_message_by_role(messages, "user")
        user_text = get_content_from_message(user_message) or ""
        user_content = user_message.get("content") if user_message and self._permit_input() else self._omitted(user_text)
        section_index = []
        for section in sections:
            steps = set()
            for entry in section["entries"]:
                step = self._step_for(turn, entry.get("call_id"), entry.get("started_at"), entry["parser_step"])
                if step is not None:
                    steps.add(step.index)
            section_index.append(
                {
                    "index": section["index"],
                    "kind": section["kind"],
                    "steps": sorted(steps),
                    "observation_ids": section_observations.get(section["index"], []),
                    "entries": [
                        {field: entry[field] for field in _SECTION_ENTRY_FIELDS if entry.get(field) is not None}
                        for entry in section["entries"]
                    ],
                }
            )
        kinds = [section["kind"] for section in sections]
        metadata = {
            "observation_source": "function_conversation",
            "section_count": len(sections),
            "assistant_message_count": kinds.count("message"),
            "detail_section_count": kinds.count("detail_single") + kinds.count("detail_group"),
            "file_section_count": kinds.count("file"),
            "final_answer_source": answer_source,
            "final_answer_sections": answer_sections,
            "sections": section_index,
        }
        obs = self._child(
            turn,
            turn.root,
            as_type="event",
            name=_NAME_CONVERSATION,
            # ChatML on both sides so Langfuse renders the event as a chat, capture on or off.
            input_data=[{"role": "user", "content": user_content}],
            output_data=self._conversation_messages(output_items, sections),
            metadata=metadata,
            start_at=now,
            end_at=now,
            end=True,
        )
        return getattr(obs, "id", None)

    @staticmethod
    def _usage_metadata(usage: Any) -> Optional[Dict[str, int]]:
        if not isinstance(usage, dict):
            return None
        flat: Dict[str, int] = {}
        for key, value in usage.items():
            number = _as_int(value)
            if number is not None:
                flat[str(key)] = number
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    sub_number = _as_int(sub_value)
                    if sub_number is not None:
                        flat[f"{key}.{sub_key}"] = sub_number
        return flat or None

    # ------------------------------------------------------------ stream parsing
    def _delta_sizes(self, event: Any) -> Tuple[int, int, int, List[str], List[str]]:
        """(content, reasoning, tool chars, tool call ids, tool names) carried by one raw chunk."""
        if not isinstance(event, dict):
            return 0, 0, 0, [], []
        content = reasoning = tool = 0
        call_ids: List[str] = []
        call_names: List[str] = []
        for choice in event.get("choices", []) or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content += len(delta["content"])
            r = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
            if isinstance(r, str):
                reasoning += len(r)
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                tool += len(fn.get("name") or "") + len(fn.get("arguments") or "")
                if call.get("id"):
                    call_ids.append(str(call["id"]))
                if fn.get("name"):
                    call_names.append(str(fn["name"]))
        event_type = str(event.get("type") or "")
        if event_type.endswith(".delta") and isinstance(event.get("delta"), str):
            size = len(event["delta"])
            if "reasoning" in event_type:
                reasoning += size
            elif "function_call" in event_type:
                tool += size
            else:
                content += size
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "function_call":
                if item.get("call_id") or item.get("id"):
                    call_ids.append(str(item.get("call_id") or item.get("id")))
                if item.get("name"):
                    call_names.append(str(item["name"]))
        if not (content or reasoning or tool) and isinstance(event.get("content"), str):
            content = len(event["content"])
        return content, reasoning, tool, call_ids, call_names

    @staticmethod
    def _content_delta(event: Any) -> str:
        """Visible answer text carried by one raw chunk (Chat Completions or Responses API)."""
        if not isinstance(event, dict):
            return ""
        parts: List[str] = []
        for choice in event.get("choices", []) or []:
            if isinstance(choice, dict):
                content = (choice.get("delta") or {}).get("content")
                if isinstance(content, str):
                    parts.append(content)
        if event.get("type") == "response.output_text.delta" and isinstance(event.get("delta"), str):
            parts.append(event["delta"])
        return "".join(parts)

    @staticmethod
    def _finish_reason(event: Any) -> Optional[str]:
        if not isinstance(event, dict):
            return None
        for choice in event.get("choices", []) or []:
            if isinstance(choice, dict) and choice.get("finish_reason"):
                return str(choice["finish_reason"])
        if event.get("done") is True:
            return str(event.get("done_reason") or "stop")
        event_type = event.get("type")
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            return str(event_type).split(".", 1)[1]
        return None

    @staticmethod
    def _chunk_usage(event: Any) -> Optional[Dict[str, int]]:
        if not isinstance(event, dict):
            return None
        usage = event.get("usage")
        if isinstance(usage, dict):
            flat = Filter._usage_metadata(usage)
            if flat:
                return flat
        response = event.get("response")
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            return Filter._usage_metadata(response["usage"])
        if event.get("done") is True and ("prompt_eval_count" in event or "eval_count" in event):
            return Filter._usage_metadata(
                {"prompt_tokens": event.get("prompt_eval_count"), "completion_tokens": event.get("eval_count")}
            )
        return None

    # --------------------------------------------------------------------- inlet
    async def inlet(
        self,
        body: dict,
        __event_emitter__: Any = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __model__: Optional[dict] = None,
        __request__: Any = None,
    ) -> dict:
        try:
            return await self._inlet(body, __user__, __metadata__, __request__)
        except Exception as e:
            self.log(f"inlet failed: {e}", force=True)
            return body

    async def _inlet(self, body: dict, __user__: Optional[dict], __metadata__: Optional[dict], __request__: Any) -> dict:
        state = getattr(__request__, "state", None)
        if state is not None:
            state.langfuse_tracing = None
        if not isinstance(body, dict) or not self.valves.enabled:
            return body
        if not self._ensure_langfuse():
            self.log("Langfuse client not initialized; inlet skipped.", force=True)
            return body

        now = time.time()
        self._gc_turns(now)
        metadata = __metadata__ if isinstance(__metadata__, dict) else (body.get("metadata") or {})
        chat_id, message_id, socket_session_id, raw_chat_id = self._ids(metadata, body)
        key = self._turn_key(chat_id, message_id)
        user_id = (__user__ or {}).get("id")

        existing = self._turns.get(key)
        if existing is not None:
            # Continue / tool-approval resume: same assistant message, new HTTP request.
            existing.resumed += 1
            existing.resume_pending = True
            self._update(existing.root, metadata={"resumed": existing.resumed})
            self._publish(state, existing, metadata, message_id)
            return body

        chat_meta = await self._internal_chat_meta(metadata, raw_chat_id)
        trigger = "api" if not metadata.get("session_id") else "chat"
        if chat_meta.get("type") in {"subagent", "timer", "note"}:
            trigger = str(chat_meta["type"])

        parent_turn: Optional[_Turn] = None
        if chat_meta.get("type") == "subagent" and self.valves.nest_subagents and chat_meta.get("mode") != "background":
            parent_key = self._turn_key(
                self._normalize_chat_id(chat_meta.get("parent_chat_id")), str(chat_meta.get("parent_message_id") or "") or None
            )
            parent_turn = self._turns.get(parent_key)

        user_message = get_last_message_by_role(body.get("messages", []), "user")
        user_text = get_content_from_message(user_message) or ""
        root_input = (
            {"role": "user", "content": user_message.get("content")}
            if user_message and self._permit_input()
            else self._omitted(user_text)
        )
        features = metadata.get("features") if isinstance(metadata.get("features"), dict) else (body.get("features") or {})
        enabled_features = sorted(str(k) for k, v in (features or {}).items() if v)
        tags = ["open-webui", "langfuse-function", *(f"feature:{name}" for name in enabled_features)]
        if raw_chat_id in (None, "local"):
            tags.append("temporary-chat")
        if metadata.get("internal") is True:
            tags.append("internal")
        if trigger not in {"chat", "api"}:
            tags.append(trigger)
        if chat_meta.get("type") == "subagent":
            tags.append("subagent")

        propagated: Dict[str, str] = {
            "interface": "open-webui",
            "plugin_version": _TRACE_VERSION,
            "chat_id": chat_id,
            "message_id": message_id or "",
            "model_id": str(body.get("model") or ""),
            "trigger": trigger,
        }
        otel_trace_id = self._owui_otel_trace_id()
        client_sent_at = self._epoch((metadata.get("user_message") or {}).get("timestamp")) if isinstance(metadata.get("user_message"), dict) else None
        root_metadata: Dict[str, Any] = {
            **propagated,
            "socket_session_id": socket_session_id,
            "user_id": user_id,
            "features": enabled_features,
            "file_count": len(body.get("files") or metadata.get("files") or []),
            "otel_trace_id": otel_trace_id,
            # Coarse (OWUI stores the client timestamp in whole seconds): send -> inlet queueing.
            "client_to_inlet_seconds": round(now - client_sent_at, 1) if client_sent_at and 0 <= now - client_sent_at < 3600 else None,
            "observation_source": "function_inlet",
        }
        session_id = chat_id
        trace_name = _NAME_ROOT
        root_name = _NAME_ROOT
        if chat_meta.get("type") == "subagent":
            parent_chat = self._normalize_chat_id(chat_meta.get("parent_chat_id"))
            session_id = parent_chat
            root_name = _NAME_SUBAGENT
            subagent_meta = {
                "subagent_chat_id": chat_id,
                "parent_chat_id": parent_chat,
                "parent_message_id": str(chat_meta.get("parent_message_id") or ""),
                "delegation_id": str(chat_meta.get("delegation_id") or ""),
                "mode": str(chat_meta.get("mode") or ""),
            }
            propagated.update({k: v for k, v in subagent_meta.items() if k in {"parent_chat_id", "parent_message_id", "delegation_id"}})
            root_metadata.update(subagent_meta)
            if parent_turn is None:
                trace_name = _NAME_SUBAGENT
                if chat_meta.get("parent_message_id"):
                    root_metadata["parent_trace_id"] = self._trace_id_for(parent_chat, str(chat_meta["parent_message_id"]))
        attrs: Dict[str, Any] = {
            "user_id": str(user_id) if user_id else None,
            "session_id": session_id,
            "tags": [tag[:200] for tag in dict.fromkeys(tags)],
            "metadata": {k: str(v)[:200] for k, v in propagated.items() if v not in (None, "")},
            "trace_name": trace_name,
            "version": _TRACE_VERSION,
        }
        attrs = {k: v for k, v in attrs.items() if v is not None}

        if parent_turn is not None:
            anchor = parent_turn.current.obs if parent_turn.current is not None else parent_turn.root
            trace_id = parent_turn.trace_id
            attrs["trace_name"] = parent_turn.attrs.get("trace_name", _NAME_ROOT)
            payload = self._finalize({"name": root_name, "input": root_input, "metadata": root_metadata})
            root = self._start_nested_root(attrs, anchor, start_at=now, payload=payload)
            parent_turn.nested_agents += 1
        else:
            trace_id = self._trace_id_for(chat_id, message_id)
            payload = self._finalize({"name": root_name, "input": root_input, "metadata": root_metadata})
            root = self._start_root(attrs, trace_id, start_at=now, payload=payload)

        turn = _Turn(
            key=key,
            trace_id=trace_id,
            root=root,
            attrs=attrs,
            policy={},
            started_at=now,
            chat_id=chat_id,
            message_id=message_id,
            user_id=str(user_id) if user_id else None,
            nested=parent_turn is not None,
            subagent=chat_meta.get("type") == "subagent",
            otel_trace_id=otel_trace_id,
            message_count=len(body.get("messages") or []),
        )
        turn.policy = {
            "v": _POLICY_VERSION,
            "enabled": True,
            "trace_id": trace_id,
            "parent_observation_id": getattr(root, "id", None),
            "chat_id": metadata.get("chat_id") or body.get("chat_id"),
            "message_id": metadata.get("message_id") or body.get("id"),
            "step_index": 0,
            "phase": "turn",
            "capture_inputs": self.valves.capture_inputs,
            "capture_outputs": self.valves.capture_outputs,
            "capture_tool_arguments": self.valves.capture_tool_arguments,
            "capture_tool_outputs": self.valves.capture_tool_outputs,
            "capture_retriever_outputs": self.valves.capture_retriever_outputs,
            "capture_reasoning": self.valves.capture_reasoning,
        }
        self._turns[key] = turn
        self._publish(state, turn, metadata, message_id)
        return body

    def _publish(self, state: Any, turn: _Turn, metadata: dict, message_id: Optional[str]):
        """Expose the live policy dict to OWUI's header bridge for this request."""
        if state is None:
            return
        turn.policy["chat_id"] = metadata.get("chat_id") or turn.policy.get("chat_id")
        turn.policy["message_id"] = metadata.get("message_id") or turn.policy.get("message_id")
        state.langfuse_tracing = turn.policy
        # Task payloads (query/title/tags generation, compaction) are built from
        # request.state.metadata, which lacks message_id on the UI path; without it the
        # bridge never forwards the policy for them. OWUI's own API path sets the same key.
        state_metadata = getattr(state, "metadata", None)
        if isinstance(state_metadata, dict) and message_id and not state_metadata.get("message_id"):
            state_metadata["message_id"] = metadata.get("message_id") or message_id

    async def _internal_chat_meta(self, metadata: dict, raw_chat_id: Optional[str]) -> dict:
        """Chat meta (subagent/timer/note linkage) for internal runs; empty for normal chats."""
        if metadata.get("internal") is not True or not raw_chat_id or raw_chat_id == "local":
            return {}
        try:
            from open_webui.models.chats import Chats

            chat = await Chats.get_chat_by_id(raw_chat_id)
            meta = getattr(chat, "meta", None) if chat is not None else None
            return dict(meta) if isinstance(meta, dict) else {}
        except Exception as e:
            self.log(f"internal chat lookup failed: {e}")
            return {}

    # ------------------------------------------------------------------- request
    async def request(
        self,
        body: dict,
        __event_emitter__: Any = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __model__: Optional[dict] = None,
        __request__: Any = None,
    ) -> dict:
        try:
            return await self._request(body, __metadata__, __request__)
        except Exception as e:
            self.log(f"request hook failed: {e}", force=True)
            return body

    async def _request(self, body: dict, __metadata__: Optional[dict], __request__: Any) -> dict:
        if not isinstance(body, dict) or not self.valves.enabled or self.langfuse is None:
            return body
        metadata = __metadata__ if isinstance(__metadata__, dict) else (body.get("metadata") or {})
        turn = self._turn_for(metadata, body)
        if turn is None:
            return body

        now = time.time()
        state = getattr(__request__, "state", None)
        if turn.resume_pending:
            turn.resume_pending = False
            if metadata.get("tool_approval_resume") and turn.current is not None:
                # Approved tools run before the next model call; they belong to the step
                # that requested them, so keep it open instead of opening a phantom step.
                if state is not None:
                    self._publish(state, turn, metadata, turn.message_id)
                return body
        current = turn.current
        if current is not None:
            self._close_step(turn, current, end_at=now)

        messages = body.get("messages") or []
        index = len(turn.steps) + 1
        system_message = next((m for m in messages if isinstance(m, dict) and m.get("role") == "system"), None)
        system_text = (get_content_from_message(system_message) or "") if system_message else ""
        tools = body.get("tools") or []
        tool_names = sorted(
            str((tool.get("function") or {}).get("name") or tool.get("name") or "")
            for tool in tools
            if isinstance(tool, dict)
        )
        sources = metadata.get("sources") if isinstance(metadata.get("sources"), list) else []

        if index == 1:
            user_message = get_last_message_by_role(messages, "user")
            step_input: Any = {
                "user_message": (
                    user_message.get("content") if user_message and self._permit_input() else self._omitted(get_content_from_message(user_message) or "")
                ),
                "system_prompt": system_text if self._permit_input() else self._omitted(system_text),
                "tool_names": tool_names,
            }
        else:
            new_messages = [m for m in messages[turn.message_count :] if isinstance(m, dict)]
            if self._permit_input():
                step_input = {"new_messages": [self._gate_message(m) for m in new_messages]}
            else:
                step_input = {**_OMITTED, "new_message_count": len(new_messages)}
        turn.message_count = len(messages)

        step_metadata = {
            "step_index": index,
            "model": body.get("model"),
            "message_count": len(messages),
            "tool_definition_count": len(tools),
            "system_prompt_chars": len(system_text),
            "sources_count": len(sources) if index == 1 else None,
            "observation_source": "function_request",
        }
        if index == 1:
            self._emit_prepare(turn, metadata, tools, sources, system_text, len(messages), now)
        obs = self._child(turn, turn.root, as_type="span", name=_NAME_STEP, input_data=step_input, metadata=step_metadata)
        step = _Step(index=index, obs=obs, requested_at=now, model=body.get("model"))
        turn.steps.append(step)

        turn.policy["parent_observation_id"] = getattr(obs, "id", turn.policy.get("parent_observation_id"))
        turn.policy["step_index"] = index
        turn.policy["phase"] = "turn"
        if state is not None and getattr(state, "langfuse_tracing", None) is not turn.policy:
            self._publish(state, turn, metadata, turn.message_id)
        return body

    def _emit_prepare(self, turn: _Turn, metadata: dict, tools: list, sources: list, system_text: str, message_count: int, now: float):
        """inlet -> first model call: OWUI's tool discovery, memory, RAG and prompt assembly.

        The filter cannot see the sub-phases; the span carries what was produced so slow
        turns can be filtered by shape (tool servers, sources, memory). Sub-phase timing
        lives in OWUI's own OTEL trace (`otel_trace_id`, Tempo).
        """
        inventory = self._tool_inventory(metadata.get("tools"))
        memory_match = _MEMORY_CONTEXT_RE.search(system_text or "")
        features = metadata.get("features") if isinstance(metadata.get("features"), dict) else {}
        summary: Dict[str, Any] = {
            "prepare_seconds": round(max(now - turn.started_at, 0.0), 4),
            "tool_definition_count": len(tools),
            "tools_by_kind": inventory["by_kind"],
            "tool_server_count": inventory["server_count"],
            "sources_count": len(sources),
            "source_document_count": sum(len(s.get("document") or []) for s in sources if isinstance(s, dict)),
            "system_prompt_chars": len(system_text or ""),
            "memory_context_chars": len(memory_match.group(1)) if memory_match else 0,
            "file_count": len(metadata.get("files") or []),
            "features": sorted(str(k) for k, v in (features or {}).items() if v),
            "message_count": message_count,
        }
        try:
            prepare = self._child(
                turn,
                turn.root,
                as_type="span",
                name=_NAME_PREPARE,
                output_data=summary,
                metadata={**summary, "otel_trace_id": turn.otel_trace_id, "observation_source": "function_request"},
                start_at=turn.started_at,
            )
        except Exception as e:
            self.log(f"prepare span failed: {e}")
            prepare = turn.root
        if sources:
            turn.source_count = len(sources)
            self._emit_sources(turn, prepare, sources, start_at=turn.started_at, end_at=now)
        if prepare is not turn.root:
            self._end(prepare, now)

    def _gate_message(self, message: dict) -> dict:
        """Apply the tool/reasoning capture valves to one provider message."""
        gated = dict(message)
        if gated.get("role") in {"tool", "function"} and not self._permit_output("tool"):
            gated["content"] = self._omitted(get_content_from_message(gated) or "")
        if not self.valves.capture_tool_arguments:
            for key in ("tool_calls", "function_call"):
                if key in gated:
                    gated[key] = self._omitted()
        if not self.valves.capture_reasoning:
            for key in ("reasoning", "reasoning_content", "thinking"):
                gated.pop(key, None)
        return gated

    def _emit_sources(self, turn: _Turn, parent: Any, sources: List[Any], *, start_at: float, end_at: float):
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                continue
            info = source.get("source") or {}
            documents = source.get("document") or []
            name = info.get("name") or info.get("id")
            try:
                self._child(
                    turn,
                    parent,
                    as_type="retriever",
                    name=_NAME_RETRIEVE_SOURCE,
                    input_data={"source": info} if self._permit_input() else {"source_index": index},
                    output_data=documents if self._permit_output("source") else self._omitted(document_count=len(documents)),
                    metadata={
                        "source_index": index,
                        "source_id": info.get("id"),
                        "source_name": name,
                        "source_type": info.get("type"),
                        "document_count": len(documents),
                        "metadata_count": len(source.get("metadata") or []),
                        "observation_source": "function_request_sources",
                        "timing": "window-approximate",
                    },
                    start_at=start_at,
                    end_at=end_at,
                    end=True,
                )
            except Exception as e:
                self.log(f"source observation failed: {e}")

    # -------------------------------------------------------------------- stream
    async def stream(
        self,
        event: dict,
        __event_emitter__: Any = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        try:
            return self._stream_event(event, __metadata__)
        except Exception as e:
            self.log(f"stream hook failed: {e}")
            return event

    def _stream_event(self, event: dict, __metadata__: Optional[dict]) -> dict:
        if not self.valves.capture_stream_metrics or not self.valves.enabled or self.langfuse is None:
            return event
        turn = self._turn_for(__metadata__, {})
        if turn is None:
            return event
        step = turn.current
        if step is None:
            return event
        now = time.time()
        step.last_event_at = now
        content, reasoning, tool, call_ids, call_names = self._delta_sizes(event)
        if content or reasoning or tool or call_ids:
            if step.first_chunk_at is None:
                step.first_chunk_at = now
            step.chunks += 1
            step.content_chars += content
            step.reasoning_chars += reasoning
            step.tool_chars += tool
            step.tool_call_ids.update(call_ids)
            step.tool_call_names.extend(name for name in call_names if name)
            if content and self._permit_output() and step.text_chars < _MAX_FIELD_CHARS:
                text = self._content_delta(event)
                if text:
                    step.text_parts.append(text)
                    step.text_chars += len(text)
        finish = self._finish_reason(event)
        if finish:
            step.finish_reason = finish
            step.llm_ended_at = now
        usage = self._chunk_usage(event)
        if usage:
            step.stream_usage = usage
        return event

    # -------------------------------------------------------------------- outlet
    async def outlet(
        self,
        body: dict,
        __event_emitter__: Any = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        try:
            return await self._outlet(body, __metadata__)
        except Exception as e:
            self.log(f"outlet failed: {e}", force=True)
            return body

    async def _outlet(self, body: dict, __metadata__: Optional[dict]) -> dict:
        if not isinstance(body, dict) or not self.valves.enabled or self.langfuse is None:
            return body
        metadata = __metadata__ if isinstance(__metadata__, dict) else (body.get("metadata") or {})
        turn = self._turn_for(metadata, body)
        if turn is None:
            self.log("outlet without a live turn; nothing to close")
            return body
        now = time.time()
        try:
            self._finish_turn(turn, body, now)
        finally:
            self._turns.pop(turn.key, None)
        return body

    def _finish_turn(self, turn: _Turn, body: dict, now: float):
        messages = body.get("messages") or []
        assistant = get_last_message_by_role(messages, "assistant")
        output_items = assistant.get("output") if isinstance(assistant.get("output"), list) else []
        final_text = self._final_text(assistant, output_items) or ""
        specs = self._parse_output_items(output_items)
        try:
            sections = self._conversation_sections(output_items)
            item_sections = self._section_index(sections)
        except Exception as e:
            self.log(f"conversation sections failed: {e}")
            sections, item_sections = [], {}
        section_observations: Dict[int, List[str]] = {}

        tool_names: List[str] = []
        agentic = turn.nested_agents > 0 or any(step.tool_call_ids for step in turn.steps)
        retrieval = turn.source_count > 0
        skip_delegate = turn.nested_agents > 0

        for spec in specs:
            step = self._step_for(turn, spec.get("call_id"), spec.get("started_at"), spec["parser_step"])
            if spec["kind"] == "tool":
                agentic = True
                tool_names.append(spec["name"])
                if spec["as_type"] == "retriever":
                    retrieval = True
                if spec["name"] == _SUBAGENT_TOOL_NAME and skip_delegate:
                    continue
            parent = step.obs if step is not None else turn.root
            if step is not None:
                window_start, window_end = self._tool_window(turn, step, now)
                llm_end = self._llm_end(step)
            else:
                window_start, window_end = turn.started_at, now
                llm_end = None
            if spec["kind"] == "reasoning":
                # Thinking happens inside the model call; OWUI leaves `ended_at` unset when
                # tool calls follow it, so bound it by the LLM end rather than the tool window.
                window_start, window_end = (step.requested_at if step is not None else window_start), (llm_end or window_end)
            start_at = spec.get("started_at")
            end_at = spec.get("ended_at")
            timing = "exact"
            if start_at is None:
                start_at, end_at, timing = window_start, window_end, "window-approximate"
            elif end_at is None or end_at < start_at:
                end_at, timing = max(start_at, window_end), "start-exact"
            if spec["kind"] == "compaction":
                start_at = end_at = window_end
            spec_metadata = {**spec["metadata"], "step_index": step.index if step is not None else None, "timing": timing}
            section_ids = item_sections.get(spec["order"], [])
            if section_ids:
                spec_metadata["conversation_section"] = section_ids[0]
            try:
                obs = self._child(
                    turn,
                    parent,
                    as_type=spec["as_type"],
                    name=spec["name"],
                    input_data=spec.get("input"),
                    output_data=spec.get("output"),
                    metadata=spec_metadata,
                    level=spec.get("level"),
                    status_message=spec.get("status_message"),
                    start_at=start_at,
                    end_at=end_at,
                    end=True,
                )
                obs_id = getattr(obs, "id", None)
                if obs_id:
                    for section_id in section_ids:
                        section_observations.setdefault(section_id, []).append(obs_id)
            except Exception as e:
                self.log(f"child observation {spec.get('name')} failed: {e}")

        current = turn.current
        if current is not None:
            self._close_step(turn, current, end_at=now)

        if self.valves.enable_masking:
            self._emit_guardrail(turn, messages, final_text, now)

        answer, answer_source, answer_sections = final_text, "assistant-content", []
        conversation_id = None
        if sections:
            try:
                answer, answer_source, answer_sections = self._final_answer(sections, final_text)
                # A lone message is already the root output; the narrative adds nothing.
                if len(sections) > 1 or sections[0]["kind"] != "message":
                    conversation_id = self._emit_conversation(
                        turn, messages, output_items, sections, section_observations, answer_source, answer_sections, now
                    )
            except Exception as e:
                self.log(f"{_NAME_CONVERSATION} failed: {e}")

        root_type = "agent" if agentic else ("chain" if retrieval else "span")
        if turn.nested or turn.subagent:
            root_type = "agent"
        usage_meta = self._usage_metadata(assistant.get("usage"))
        root_metadata: Dict[str, Any] = {
            "root_type": root_type,
            "step_count": len(turn.steps),
            "tool_call_count": len(tool_names),
            "tool_names": tool_names,
            "nested_agent_count": turn.nested_agents,
            "source_count": turn.source_count,
            "output_items_count": len(output_items),
            "finish_reason": assistant.get("finish_reason"),
            "request_duration_seconds": round(now - turn.started_at, 4),
            "owui_usage": usage_meta,
            "observation_source": "function_outlet",
            "final_answer_source": answer_source,
        }
        if sections:
            root_metadata["conversation_section_count"] = len(sections)
            root_metadata["assistant_message_count"] = sum(1 for section in sections if section["kind"] == "message")
        if conversation_id:
            root_metadata["conversation_observation_id"] = conversation_id
        # Root output = the answer; interim assistant messages live in `record-conversation`.
        root_output = {"role": "assistant", "content": answer} if self._permit_output() else self._omitted(answer)
        self._update(turn.root, output=root_output, metadata=root_metadata)
        try:
            # update() re-stamps the type from the wrapper class, so set it last.
            turn.root._otel_span.set_attribute(_OBSERVATION_TYPE_ATTR, root_type)
        except Exception:
            pass
        self._end(turn.root, now)
        if self.valves.perf_scores:
            self._emit_perf_scores(turn, now)

        turn.policy["parent_observation_id"] = getattr(turn.root, "id", None)
        turn.policy["step_index"] = None
        turn.policy["phase"] = "background"

    def _emit_perf_scores(self, turn: _Turn, now: float):
        """Per-turn time breakdown as numeric scores on the root (chartable in dashboards)."""
        if not turn.steps:
            return
        first = turn.steps[0]
        llm_seconds = 0.0
        tool_seconds = 0.0
        for index, step in enumerate(turn.steps):
            llm_end = self._llm_end(step) or step.ended_at or now
            llm_seconds += max(llm_end - step.requested_at, 0.0)
            nxt = turn.steps[index + 1] if index + 1 < len(turn.steps) else None
            if step.tool_call_ids or nxt is not None:
                window_end = nxt.requested_at if nxt is not None else (step.ended_at or now)
                tool_seconds += max(window_end - llm_end, 0.0)
        values = {
            "prepare_seconds": max(first.requested_at - turn.started_at, 0.0),
            "llm_seconds": llm_seconds,
            "tool_seconds": tool_seconds,
            "first_chunk_seconds": (first.first_chunk_at - turn.started_at) if first.first_chunk_at else None,
        }
        root_id = getattr(turn.root, "id", None)
        for name, value in values.items():
            if value is None:
                continue
            try:
                self.langfuse.create_score(
                    trace_id=turn.trace_id,
                    observation_id=root_id,
                    name=f"{_SCORE_PREFIX}{name}",
                    value=round(float(value), 4),
                    data_type="NUMERIC",
                    comment="filter-measured wall time",
                )
            except Exception as e:
                self.log(f"perf score {name} failed: {e}")

    def _emit_guardrail(self, turn: _Turn, messages: List[dict], final_text: str, now: float):
        user_message = get_last_message_by_role(messages, "user")
        scan_text = (get_content_from_message(user_message) or "") + "\n" + (final_text or "")
        counts = self._count_redactions(scan_text)
        if not counts:
            return
        categories = [
            cat
            for cat, active in [
                ("data_uris", not self.valves.capture_multimodal),
                ("emails", _MASK_EMAILS),
                ("phone_numbers", _MASK_PHONE_NUMBERS),
                ("api_keys", _MASK_API_KEYS),
            ]
            if active
        ]
        try:
            self._child(
                turn,
                turn.root,
                as_type="guardrail",
                name=_NAME_GUARDRAIL,
                input_data={"scanned_chars": len(scan_text), "categories_enabled": categories},
                output_data={**counts, "total_redacted": sum(counts.values())},
                level=_LEVEL_WARNING,
                metadata={"observation_source": "function_masking"},
                start_at=now,
                end_at=now,
                end=True,
            )
        except Exception as e:
            self.log(f"guardrail observation failed: {e}")
