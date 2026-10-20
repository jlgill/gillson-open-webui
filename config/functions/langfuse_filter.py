"""
title: Langfuse Filter
author: gillson (derived from open-webui Langfuse pipeline v4)
author_url: https://github.com/jlgill
version: 1.3.1
license: MIT
required_open_webui_version: 0.10.0
requirements: langfuse==4.15.6
description: In-process Langfuse v4 tracing filter for Open WebUI. Valve-controlled tracing and content capture, deterministic per-turn trace correlation, PII masking, bounded telemetry records, and failure-tolerant lifecycle hooks.
"""

from __future__ import annotations

import json
import asyncio
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from langfuse import Langfuse


TRACE_ATTR_NAME = "langfuse.trace.name"
TRACE_ATTR_USER_ID = "user.id"
TRACE_ATTR_SESSION_ID = "session.id"
TRACE_ATTR_TAGS = "langfuse.trace.tags"
TRACE_ATTR_METADATA = "langfuse.trace.metadata"
TRACE_ATTR_INPUT = "langfuse.trace.input"
TRACE_ATTR_OUTPUT = "langfuse.trace.output"

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
# Compiled once: matches Open WebUI <details> HTML tool-call blocks embedded in assistant messages.
_OWUI_DETAILS_RE = re.compile(r"<details\b[^>]*>.*?</details>", re.DOTALL | re.IGNORECASE)

# ---------------------------------------------------------------------------
# Hardcoded constants (previously valves).
#
# These values almost never change between deployments. They live here as
# module-level constants so they're easy to find and adjust, and trivial to
# re-promote to a valve later if a real per-deployment need arises. See the
# "Valves vs constants" section of the changelog for the rationale.
# ---------------------------------------------------------------------------

# Trace identity — tracks the code version; do NOT expose as a valve or it
# will drift from the actual code.
_TRACE_VERSION = "open-webui-langfuse-function-v1.3.1"

# Langfuse observation levels. Map to DEBUG/DEFAULT/WARNING/ERROR.
_LEVEL_DEFAULT = "DEFAULT"
_LEVEL_USER_EVENT = "DEFAULT"
_LEVEL_TOOL = "DEFAULT"
_LEVEL_RETRIEVER = "DEFAULT"
_LEVEL_REASONING = "DEBUG"
_LEVEL_ERROR = "ERROR"
_LEVEL_GUARDRAIL = "WARNING"

# Root observation types (Langfuse observation *types*, not free-form labels).
_ROOT_TYPE_AGENT = "agent"
_ROOT_TYPE_RAG = "chain"
_ROOT_TYPE_UTILITY = "span"
_ROOT_TYPE_CHAT = "span"

# Masking — granular toggles collapsed under the master `enable_masking` valve.
_MASK_EMAILS = True
_MASK_PHONE_NUMBERS = True
_MASK_API_KEYS = True
_MASK_REPLACEMENT = "[REDACTED]"

# Size/throughput guards.
_MAX_FIELD_CHARS = 20000
_MAX_COLLECTION_ITEMS = 200
_STREAM_CHARS_PER_TOKEN = 4.0
# Per-observation export cap. Oversized records degrade progressively (input
# dropped first, then output) before the record itself is dropped — long
# agentic turns keep their output/usage instead of vanishing entirely.
_MAX_RECORD_BYTES = 1_048_576

# Structural toggles — the whole point of the observation structure.
_INSERT_TAGS = True
_GROUP_RAG_AS_CHAIN = True
_EMIT_GUARDRAIL_OBSERVATION = True

# Background-task names that should be classified as utility (span) roots.
_BACKGROUND_TASKS = (
    "title_generation,follow_up_generation,tags_generation,emoji_generation,"
    "query_generation,image_prompt_generation,autocomplete_generation,"
    "function_calling,moa_response_generation"
)


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
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == role:
            return message
    return {}


def get_last_assistant_message(messages: List[dict]) -> Optional[str]:
    message = get_last_message_by_role(messages, "assistant")
    return get_content_from_message(message) if message else None


def maybe_json_loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not value.strip():
        return {}
    try:
        return json.loads(value)
    except Exception:
        return value


def first_present(data: dict, keys: List[str]) -> Optional[Any]:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
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

        # --- Langfuse project context (varies per deploy) ---
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

        # --- Ergonomics / perf escape hatches ---
        use_model_name_instead_of_id_for_generation: bool = False
        capture_stream_metrics: bool = True

        # --- Background-task root-type list (changes as OWUI adds tasks) ---
        background_tasks: str = _BACKGROUND_TASKS

    def __init__(self):
        self.name = "Langfuse Filter"
        self.valves = self.Valves()

        # Hardcoded constants (see module-level definitions). Exposed as instance
        # attributes so the rest of the class reads `self._LEVEL_*` etc., matching
        # the pre-refactor pattern. Re-promote to a valve only if a real per-deployment
        # need arises.
        self._trace_version = _TRACE_VERSION
        self._level_default = _LEVEL_DEFAULT
        self._level_user_event = _LEVEL_USER_EVENT
        self._level_tool = _LEVEL_TOOL
        self._level_retriever = _LEVEL_RETRIEVER
        self._level_reasoning = _LEVEL_REASONING
        self._level_error = _LEVEL_ERROR
        self._level_guardrail = _LEVEL_GUARDRAIL
        self._root_type_agent = _ROOT_TYPE_AGENT
        self._root_type_rag = _ROOT_TYPE_RAG
        self._root_type_utility = _ROOT_TYPE_UTILITY
        self._root_type_chat = _ROOT_TYPE_CHAT
        self._mask_emails = _MASK_EMAILS
        self._mask_phone_numbers = _MASK_PHONE_NUMBERS
        self._mask_api_keys = _MASK_API_KEYS
        self._mask_replacement = _MASK_REPLACEMENT
        self._max_field_chars = _MAX_FIELD_CHARS
        self._max_collection_items = _MAX_COLLECTION_ITEMS
        self._stream_chars_per_token = _STREAM_CHARS_PER_TOKEN
        self._insert_tags = _INSERT_TAGS
        self._group_rag_as_chain = _GROUP_RAG_AS_CHAIN
        self._emit_guardrail_observation = _EMIT_GUARDRAIL_OBSERVATION

        self.langfuse: Optional[Langfuse] = None
        # trace_id -> perf_counter() captured at inlet (TTFT base + end-to-end latency
        # when outlet runs on the same worker). Best-effort only; correctness never
        # depends on it because correlation comes from the deterministic trace_id.
        self._start_ts: Dict[str, float] = {}
        # trace_id -> {first,last,chunks,chars} accumulated during stream().
        self._stream: Dict[str, dict] = {}
        # trace_id -> per-turn counter for stream_call_N span naming (agentic
        # turns stream once per LLM call; indexing keeps the spans distinguishable).
        self._stream_seq: Dict[str, int] = {}
        self._last_log = float("-inf")
        self.dropped = 0
        # Throttled lazy re-init so tracing self-heals after a startup race without a restart.
        self._last_init_attempt = 0.0
        self._init_retry_interval = 30.0

    def _capture(self) -> bool:
        return self.valves.capture_inputs or self.valves.capture_outputs

    def _payload(self, payload: dict, as_type: str, content: bool = True) -> dict:
        """Apply the capture contract + size guard to an observation payload.

        content=True  -> input/output are user content: gated by the capture valves.
        content=False -> payload carries only counts/metrics (stream spans, guardrail
        tallies): input/output flow regardless of capture since they hold no user
        content. The capture-off metadata allowlist still applies either way.
        """
        if content:
            permit_input = self._capture() and self.valves.capture_inputs
            permit_output = self._capture() and self.valves.capture_outputs
            if as_type == "tool":
                permit_input = permit_input and self.valves.capture_tool_arguments
                permit_output = permit_output and self.valves.capture_tool_outputs
            if as_type == "retriever":
                permit_output = permit_output and self.valves.capture_retriever_outputs
            if not permit_input:
                payload["input"] = None
            if not permit_output:
                payload["output"] = None
        if not self._capture():
            payload["model_parameters"] = None
            payload["status_message"] = "Operation failed" if payload.get("level") == "ERROR" else None
            if as_type in {"tool", "retriever"}:
                payload["name"] = as_type
            allowed = {
                "interface", "plugin", "plugin_version", "chat_id", "session_id", "socket_session_id",
                "message_id", "task", "user_id", "root_type", "observation_source", "source_index",
                "metadata_count", "has_files", "has_embeds", "has_usage", "output_items_count",
                "request_duration_seconds", "usage_total_reconciled", "stream_call_index",
                "tool_call_count", "rag_source_count",
            }
            payload["metadata"] = {key: value for key, value in payload.get("metadata", {}).items() if key in allowed}
        if self._record_size(payload) > _MAX_RECORD_BYTES:
            if payload.get("input") is not None:
                payload["input"] = None
                if self._record_size(payload) <= _MAX_RECORD_BYTES:
                    return payload
            if payload.get("output") is not None:
                payload["output"] = None
                if self._record_size(payload) <= _MAX_RECORD_BYTES:
                    return payload
            self.dropped += 1
            raise ValueError("Telemetry record exceeds configured size after content removal")
        return payload

    @staticmethod
    def _record_size(payload: dict) -> int:
        try:
            return len(json.dumps(payload, default=str).encode())
        except Exception:
            return _MAX_RECORD_BYTES + 1

    # ------------------------------------------------------------------ logging
    def log(self, message: str, force: bool = False):
        """Debug valve prints full detail; otherwise forced messages surface as a
        throttled, detail-free one-liner (no hostnames/payloads in container logs)."""
        if self.valves.debug:
            print(f"[Langfuse Filter] {message}")
            return
        if force and time.monotonic() - self._last_log >= 60:
            self._last_log = time.monotonic()
            print("[Langfuse Filter] telemetry degraded; details suppressed (enable the debug valve to diagnose)")

    # ----------------------------------------------------------------- lifecycle
    async def on_startup(self):
        self.log(f"on_startup for {__name__}")
        self.set_langfuse()

    async def on_shutdown(self):
        if self.langfuse:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.langfuse.shutdown), timeout=5)
            except Exception:
                self.log("shutdown incomplete", force=True)

    async def on_valves_updated(self):
        # NOTE: do NOT shutdown+rebuild the client here. langfuse v4's
        # LangfuseResourceManager is a process-wide singleton keyed by public
        # key; a shutdown() marks it dead (_shutdown=True, consumer threads
        # joined) and it is NEVER removed from _instances — a rebuild with the
        # same key silently returns the dead singleton and tracing stops
        # until restart. Valve edits to host/keys therefore require a
        # container restart; non-connection valves are read live from
        # self.valves on every request.
        self.log("Valves updated; connection changes require a container restart.")

    def _normalize_host(self, raw: str) -> str:
        value = (raw or "").strip().rstrip("/")
        if not value:
            return "https://cloud.langfuse.com"
        if value.startswith("http://") or value.startswith("https://"):
            return value
        return f"https://{value}"

    def _isolated_tracer_provider(self):
        """Build a dedicated provider so Langfuse does not export OWUI infrastructure spans."""
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
            kwargs = {
                "secret_key": self.valves.secret_key,
                "public_key": self.valves.public_key,
                "base_url": self._normalize_host(self.valves.host),
                "debug": False,
                "sample_rate": self.valves.sample_rate,
                "timeout": 2,
                "flush_at": 32,
                "flush_interval": 1,
                "additional_headers": {"x-langfuse-ingestion-version": "4"},
            }
            if self.valves.environment:
                kwargs["environment"] = self.valves.environment
            if self.valves.release:
                kwargs["release"] = self.valves.release
            kwargs["mask"] = self.mask_data

            # Isolate langfuse's tracer provider from OWUI's global OTEL provider so
            # OWUI app/infra spans don't leak into Langfuse (and vice-versa).
            isolated_provider = self._isolated_tracer_provider()
            if isolated_provider is None:
                return
            kwargs["tracer_provider"] = isolated_provider
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

    # ----------------------------------------------------------- root type logic
    def _resolve_root_type(self, task_name: str, body: dict) -> str:
        background = {t.strip() for t in self.valves.background_tasks.split(",") if t.strip()}
        if task_name in background:
            return self._root_type_utility
        if body.get("tools"):
            return self._root_type_agent
        metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        if body.get("files") or metadata.get("folder_knowledge"):
            return self._root_type_rag
        return self._root_type_chat

    # ----------------------------------------------------------------- masking
    def _mask_key_segments(self) -> set[frozenset[str]]:
        """Configured mask keys normalized to segment sets, cached per valve value.

        "api_key" -> {api, key}; "set-cookie" -> {set, cookie}.
        """
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
        """Segment-superset matching, NOT substring matching.

        The old substring check redacted innocent keys: "token" matched inside
        "est_output_tokens"/"prompt_tokens" (stream spans + usage metadata came
        out as "[REDACTED]"), "key" inside "monkey". A key is masked only when
        its alphanumeric segments contain ALL segments of a configured mask key
        ("access_token" -> {access, token} contains {token} -> masked;
        "est_output_tokens" -> {est, output, tokens} -> not masked).
        """
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

        if self._mask_emails:
            n = len(re.findall(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", text))
            if n:
                counts["emails"] = n

        if self._mask_phone_numbers:
            n = len(re.findall(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", text))
            if n:
                counts["phone_numbers"] = n

        if self._mask_api_keys:
            api_key_count = (
                len(re.findall(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", text, flags=re.I))
                + len(re.findall(r"\b(?:sk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{12,}\b", text))
                + len(re.findall(r"\b(?:sk|pk)-lf-[A-Za-z0-9_-]{12,}\b", text))
                + len(re.findall(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b", text))
            )
            if api_key_count:
                counts["api_keys"] = api_key_count

        return counts

    def _mask_string(self, value: str) -> str:
        replacement = self._mask_replacement
        result = value

        if not self.valves.capture_multimodal:
            result = re.sub(
                r"data:([^;,\s]+);base64,[A-Za-z0-9+/=\r\n]+",
                lambda match: f"[REDACTED {match.group(1)} DATA URI]",
                result,
            )

        if self._mask_emails:
            result = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", replacement, result)

        if self._mask_phone_numbers:
            result = re.sub(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", replacement, result)

        if self._mask_api_keys:
            result = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", f"Bearer {replacement}", result, flags=re.I)
            result = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{12,}\b", replacement, result)
            result = re.sub(r"\b(?:sk|pk)-lf-[A-Za-z0-9_-]{12,}\b", replacement, result)
            result = re.sub(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b", replacement, result)

        max_chars = max(self._max_field_chars, 0)
        if max_chars and len(result) > max_chars:
            result = f"{result[:max_chars]}... [TRUNCATED {len(result) - max_chars} chars]"

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
                if index >= self._max_collection_items:
                    output["__truncated_items__"] = len(data) - self._max_collection_items
                    break
                key_string = str(key)
                if self.valves.enable_masking and self._should_mask_key(key_string):
                    output[key_string] = self._mask_replacement
                else:
                    output[key_string] = self._safe_data(value, depth + 1)
            return output

        if isinstance(data, (list, tuple, set)):
            items = list(data)
            output = [self._safe_data(value, depth + 1) for value in items[: self._max_collection_items]]
            if len(items) > self._max_collection_items:
                output.append({"__truncated_items__": len(items) - self._max_collection_items})
            return output

        return self._mask_string(str(data))

    # --------------------------------------------------------------- id helpers
    def _build_tags(self, task_name: str, extra_tags: Optional[List[str]] = None) -> List[str]:
        tags: List[str] = []
        if self._insert_tags:
            tags.extend(["open-webui", "langfuse-function"])
            if task_name and task_name not in {"user_response", "llm_response"}:
                tags.append(task_name)
        if extra_tags:
            tags.extend(extra_tags)

        deduped: List[str] = []
        for tag in tags:
            if tag and tag not in deduped:
                deduped.append(str(tag)[:200])
        return deduped

    def _normalize_chat_id(self, chat_id: Optional[Any], session_id: Optional[Any] = None) -> str:
        if chat_id == "local":
            return f"temporary-session-{session_id or uuid.uuid4()}"
        if chat_id:
            return str(chat_id)
        if session_id:
            return f"temporary-session-{session_id}"
        return f"trace-{uuid.uuid4()}"

    def _ids(self, metadata: Optional[dict], body: dict) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
        metadata = metadata if isinstance(metadata, dict) else {}
        raw_chat = metadata.get("chat_id") or body.get("chat_id")
        session_id = metadata.get("session_id") or body.get("session_id")
        chat_id = self._normalize_chat_id(raw_chat, session_id)
        message_id = (
            metadata.get("message_id")
            or metadata.get("id")
            or body.get("message_id")
            or body.get("id")
        )
        # Only an EXPLICIT task participates in the trace seed (its value is identical
        # in inlet and outlet). The defaulted task name ("user_response"/"llm_response")
        # must never enter the seed or inlet/outlet would diverge.
        explicit_task = metadata.get("task")
        return chat_id, (str(message_id) if message_id else None), (str(session_id) if session_id else None), (
            str(explicit_task) if explicit_task else None
        )

    def _trace_id_for(self, chat_id: str, message_id: Optional[str], explicit_task: Optional[str]) -> Optional[str]:
        # Mirrors the proven v4 turn-key semantics so inlet/stream/outlet converge with
        # no shared memory. Per-turn (chat:msg) preserves one-trace-PER-TURN topology.
        if message_id:
            seed = f"{chat_id}:{message_id}"
        elif explicit_task:
            seed = f"{chat_id}:{explicit_task}"
        else:
            seed = str(chat_id)
        try:
            return Langfuse.create_trace_id(seed=seed)
        except Exception as e:
            self.log(f"create_trace_id failed: {e}", force=True)
            return None

    def _gc_dicts(self):
        for d in (self._start_ts, self._stream, self._stream_seq):
            if len(d) > 2000:
                for key in list(d.keys())[: len(d) - 1000]:
                    d.pop(key, None)

    def _model_value(self, model_id: Optional[str], model_name: Optional[str]) -> str:
        if self.valves.use_model_name_instead_of_id_for_generation:
            return str(model_name or model_id or "unknown")
        return str(model_id or model_name or "unknown")

    def _extract_model_info(self, body: dict) -> Tuple[Optional[str], Optional[str]]:
        model_id: Optional[str] = None
        model_name: Optional[str] = None

        model_item = body.get("model_item")
        if isinstance(model_item, dict):
            if isinstance(model_item.get("id"), str) and model_item["id"]:
                model_id = model_item["id"]
            if isinstance(model_item.get("name"), str) and model_item["name"]:
                model_name = model_item["name"]

        raw_model = body.get("model")
        if isinstance(raw_model, str) and raw_model and not model_id:
            model_id = raw_model

        metadata = body.get("metadata") or {}
        meta_model = metadata.get("model")
        if isinstance(meta_model, dict):
            if isinstance(meta_model.get("name"), str) and meta_model["name"] and not model_name:
                model_name = meta_model["name"]
            if isinstance(meta_model.get("id"), str) and meta_model["id"] and not model_id:
                model_id = meta_model["id"]
                if not model_name:
                    model_name = meta_model["id"]

        # Fallback: background-task payloads nest the real model under task_body.model.
        if not model_id or not model_name:
            task_body = metadata.get("task_body")
            if isinstance(task_body, dict):
                tb_model = task_body.get("model")
                if isinstance(tb_model, str) and tb_model:
                    model_id = model_id or tb_model
                    model_name = model_name or tb_model
                elif isinstance(tb_model, dict):
                    if isinstance(tb_model.get("id"), str) and tb_model["id"] and not model_id:
                        model_id = tb_model["id"]
                    if isinstance(tb_model.get("name"), str) and tb_model["name"] and not model_name:
                        model_name = tb_model["name"]

        return model_id, model_name

    def _infer_task_name_from_assistant_message(self, message: Optional[str]) -> Optional[str]:
        if not message:
            return None
        try:
            parsed = json.loads(message.strip())
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        if "queries" in parsed:
            return "query_generation"
        if "title" in parsed:
            return "title_generation"
        if isinstance(parsed.get("tags"), list):
            return "tags_generation"
        return None

    def _safe_input(self, body: dict) -> dict:
        return {"model": body.get("model"), "messages": body.get("messages")}

    def _trace_metadata(
        self,
        *,
        body: dict,
        chat_id: str,
        message_id: Optional[str],
        session_id: Optional[str],
        task_name: str,
        user: Optional[dict],
        model_id: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        metadata = body.get("metadata", {}) if isinstance(body.get("metadata"), dict) else {}
        return {
            "interface": "open-webui",
            "plugin": "langfuse-filter-function",
            "plugin_version": self._trace_version,
            "chat_id": chat_id,
            # Langfuse session = one persistent OWUI chat (chat_id), so every turn in a
            # chat groups into a single session. OWUI's `session_id` is the Socket.IO
            # CONNECTION id (shared across multiple chats in the same browser tab) and is
            # kept separately for reference only - never used as the Langfuse session key.
            "session_id": chat_id,
            "socket_session_id": session_id,
            "message_id": message_id,
            "task": task_name,
            "model_id": model_id if model_id is not None else body.get("model"),
            "model_name": model_name,
            "user_id": user.get("id") if user else None,
        }

    def _trace_name(self, chat_id: str, task_name: str, message_id: Optional[str] = None) -> str:
        if task_name and task_name not in {"user_response", "llm_response"}:
            return f"open-webui:{task_name}:{chat_id}"
        if message_id:
            return f"open-webui:chat:{chat_id}:{message_id}"
        return f"open-webui:chat:{chat_id}"

    # --------------------------------------------------------- trace attributes
    def _set_otel_attribute(self, observation: Any, key: str, value: Any):
        if value is None:
            return
        otel_span = getattr(observation, "_otel_span", None)
        if otel_span is None:
            return
        try:
            otel_span.set_attribute(key, value)
        except Exception:
            pass

    def _apply_trace_attributes(
        self,
        root: Any,
        *,
        name: str,
        user_id: Optional[str],
        session_id: str,
        tags: List[str],
        metadata: dict,
        input_data: Optional[Any] = None,
        output_data: Optional[Any] = None,
    ):
        metadata = self._payload({"metadata": self._safe_data(metadata)}, "span")["metadata"]
        input_data = input_data if self._capture() and self.valves.capture_inputs else None
        output_data = output_data if self._capture() and self.valves.capture_outputs else None
        safe_metadata = {
            str(key): str(value)[:200]
            for key, value in metadata.items()
            if value is not None and isinstance(value, (str, int, float, bool))
        }

        if hasattr(root, "update_trace"):
            try:
                root.update_trace(
                    name=name,
                    user_id=user_id,
                    session_id=session_id,
                    tags=tags if tags else None,
                    metadata=self._safe_data(metadata),
                    input=self._safe_data(input_data) if input_data is not None else None,
                    output=self._safe_data(output_data) if output_data is not None else None,
                )
                return
            except TypeError:
                pass
            except Exception as e:
                self.log(f"update_trace failed: {e}")

        self._set_otel_attribute(root, TRACE_ATTR_NAME, name)
        self._set_otel_attribute(root, TRACE_ATTR_USER_ID, user_id)
        self._set_otel_attribute(root, TRACE_ATTR_SESSION_ID, session_id)
        if tags:
            self._set_otel_attribute(root, TRACE_ATTR_TAGS, tags)
        for key, value in safe_metadata.items():
            self._set_otel_attribute(root, f"{TRACE_ATTR_METADATA}.{key}", value)
        if input_data is not None:
            self._set_otel_attribute(root, TRACE_ATTR_INPUT, json.dumps(self._safe_data(input_data), default=str))
        if output_data is not None:
            self._set_otel_attribute(root, TRACE_ATTR_OUTPUT, json.dumps(self._safe_data(output_data), default=str))

    # ------------------------------------------------------ observation helpers
    def _start_on_trace(
        self,
        trace_id: str,
        *,
        as_type: str,
        name: str,
        input_data: Optional[Any] = None,
        output_data: Optional[Any] = None,
        metadata: Optional[dict] = None,
        level: Optional[str] = None,
        status_message: Optional[str] = None,
        model: Optional[str] = None,
        model_parameters: Optional[dict] = None,
        usage_details: Optional[dict] = None,
        cost_details: Optional[dict] = None,
        content: bool = True,
    ) -> Any:
        """Create an observation attached to a DETERMINISTIC trace by id."""
        kwargs = {
            "name": name,
            "input": self._safe_data(input_data) if input_data is not None else None,
            "output": self._safe_data(output_data) if output_data is not None else None,
            "metadata": self._safe_data(metadata or {}),
            "level": level,
            "status_message": status_message,
            "model": model,
            "model_parameters": self._safe_data(model_parameters) if model_parameters else None,
            "usage_details": usage_details,
            "cost_details": cost_details,
            "trace_context": {"trace_id": trace_id},
        }
        kwargs = {key: value for key, value in self._payload(kwargs, as_type, content=content).items() if value is not None}

        if hasattr(self.langfuse, "start_observation"):
            try:
                return self.langfuse.start_observation(as_type=as_type, **kwargs)
            except TypeError:
                legacy = dict(kwargs)
                legacy.pop("usage_details", None)
                legacy.pop("cost_details", None)
                try:
                    return self.langfuse.start_observation(as_type=as_type, **legacy)
                except TypeError:
                    try:
                        return self.langfuse.start_observation(as_type="span", **legacy)
                    except TypeError:
                        pass

        if as_type == "generation" and hasattr(self.langfuse, "start_generation"):
            gen_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key
                in {"name", "input", "output", "metadata", "level", "status_message", "model",
                    "model_parameters", "trace_context"}
            }
            try:
                return self.langfuse.start_generation(**gen_kwargs)
            except TypeError:
                pass

        if hasattr(self.langfuse, "start_span"):
            span_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key in {"name", "input", "output", "metadata", "level", "status_message", "trace_context"}
            }
            try:
                return self.langfuse.start_span(**span_kwargs)
            except TypeError:
                legacy = {k: v for k, v in span_kwargs.items() if k in {"name", "input", "metadata", "trace_context"}}
                return self.langfuse.start_span(**legacy)

        raise RuntimeError("Installed Langfuse SDK does not expose a supported observation API")

    def _start_child_observation(
        self,
        parent: Any,
        *,
        as_type: str,
        name: str,
        input_data: Optional[Any] = None,
        output_data: Optional[Any] = None,
        metadata: Optional[dict] = None,
        level: Optional[str] = None,
        status_message: Optional[str] = None,
        model: Optional[str] = None,
        model_parameters: Optional[dict] = None,
        usage_details: Optional[dict] = None,
        cost_details: Optional[dict] = None,
        content: bool = True,
    ) -> Any:
        kwargs = {
            "name": name,
            "input": self._safe_data(input_data) if input_data is not None else None,
            "output": self._safe_data(output_data) if output_data is not None else None,
            "metadata": self._safe_data(metadata or {}),
            "level": level,
            "status_message": status_message,
            "model": model,
            "model_parameters": self._safe_data(model_parameters) if model_parameters else None,
            "usage_details": usage_details,
            "cost_details": cost_details,
        }
        kwargs = {key: value for key, value in self._payload(kwargs, as_type, content=content).items() if value is not None}

        if hasattr(parent, "start_observation"):
            try:
                return parent.start_observation(as_type=as_type, **kwargs)
            except TypeError:
                legacy_kwargs = dict(kwargs)
                legacy_kwargs.pop("usage_details", None)
                legacy_kwargs.pop("cost_details", None)
                try:
                    return parent.start_observation(as_type=as_type, **legacy_kwargs)
                except TypeError:
                    pass

        if as_type == "event" and hasattr(parent, "create_event"):
            event_kwargs = {key: value for key, value in kwargs.items() if key not in {"model", "model_parameters"}}
            return parent.create_event(**event_kwargs)

        if as_type == "generation" and hasattr(parent, "start_generation"):
            generation_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key
                in {"name", "input", "output", "metadata", "level", "status_message", "model", "model_parameters"}
            }
            try:
                return parent.start_generation(**generation_kwargs)
            except TypeError:
                legacy_kwargs = {
                    key: value
                    for key, value in generation_kwargs.items()
                    if key in {"name", "input", "output", "metadata", "model"}
                }
                return parent.start_generation(**legacy_kwargs)

        if hasattr(parent, "start_span"):
            span_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key in {"name", "input", "output", "metadata", "level", "status_message"}
            }
            try:
                return parent.start_span(**span_kwargs)
            except TypeError:
                legacy_kwargs = {
                    key: value for key, value in span_kwargs.items() if key in {"name", "input", "metadata"}
                }
                return parent.start_span(**legacy_kwargs)

        raise RuntimeError(f"Unable to create child observation {name} ({as_type})")

    def _end_observation(self, observation: Any):
        if observation is None:
            return
        try:
            observation.end()
        except Exception:
            pass

    def _update_generation_usage(self, generation: Any, usage_details: Optional[dict], cost_details: Optional[dict]):
        if not generation or not usage_details:
            return
        try:
            generation.update(usage_details=usage_details, cost_details=cost_details)
            return
        except TypeError:
            pass
        except Exception as e:
            self.log(f"Failed to update generation usage_details: {e}")

        try:
            generation.update(usage=usage_details)
        except Exception as e:
            self.log(f"Failed to update generation legacy usage: {e}")

    def _create_child_and_end(self, parent: Any, **kwargs: Any):
        child_kwargs = dict(kwargs)
        content = not bool(child_kwargs.pop("content_free", False))
        if "input" in child_kwargs and "input_data" not in child_kwargs:
            child_kwargs["input_data"] = child_kwargs.pop("input")
        if "output" in child_kwargs and "output_data" not in child_kwargs:
            child_kwargs["output_data"] = child_kwargs.pop("output")

        accepted_keys = {
            "as_type",
            "name",
            "input_data",
            "output_data",
            "metadata",
            "level",
            "status_message",
            "model",
            "model_parameters",
            "usage_details",
            "cost_details",
        }
        extra = {key: child_kwargs.pop(key) for key in list(child_kwargs.keys()) if key not in accepted_keys}
        if extra:
            metadata = child_kwargs.get("metadata") if isinstance(child_kwargs.get("metadata"), dict) else {}
            child_kwargs["metadata"] = {**metadata, "observation_extra": extra}

        try:
            observation = self._start_child_observation(parent, content=content, **child_kwargs)
            self._end_observation(observation)
        except Exception as e:
            self.log(f"Failed to create observation {child_kwargs.get('name')}: {e}")

    def _create_on_trace_and_end(self, trace_id: str, **kwargs: Any):
        try:
            observation = self._start_on_trace(trace_id, content=not bool(kwargs.pop("content_free", False)), **kwargs)
            self._end_observation(observation)
        except Exception as e:
            self.log(f"Failed to create trace-level observation {kwargs.get('name')}: {e}")

    # ----------------------------------------------------------- usage / output
    def _model_parameters(self, body: dict) -> dict:
        parameter_keys = [
            "temperature",
            "top_p",
            "top_k",
            "max_tokens",
            "max_completion_tokens",
            "stop",
            "seed",
            "frequency_penalty",
            "presence_penalty",
            "reasoning_effort",
        ]
        return {key: body[key] for key in parameter_keys if key in body and body[key] is not None}

    def _extract_usage_details(self, usage: Optional[dict]) -> Tuple[Optional[dict], Optional[dict], bool]:
        if not isinstance(usage, dict) or not usage:
            return None, None, False

        input_tokens = first_present(
            usage,
            ["input", "input_tokens", "prompt_tokens", "prompt_eval_count", "prompt_n"],
        )
        output_tokens = first_present(
            usage,
            ["output", "output_tokens", "completion_tokens", "eval_count", "predicted_n"],
        )
        total_tokens = first_present(usage, ["total", "total_tokens"])

        usage_details: Dict[str, int] = {}
        for key, value in {
            "input": input_tokens,
            "output": output_tokens,
            "total": total_tokens,
        }.items():
            if value is not None:
                try:
                    usage_details[key] = int(value)
                except (TypeError, ValueError):
                    pass

        if "total" not in usage_details and ("input" in usage_details or "output" in usage_details):
            usage_details["total"] = usage_details.get("input", 0) + usage_details.get("output", 0)

        # OWUI's aggregated usage for multi-call (agentic) turns is unreliable —
        # observed input=205836/output=4354/total=75241 on a 6-call turn where
        # the per-call sums were 73478/1763. When total != input + output,
        # recompute it and flag the generation metadata so the discrepancy is
        # visible (accurate per-call numbers live on litellm_generation obs).
        usage_total_reconciled = False
        if "input" in usage_details and "output" in usage_details and "total" in usage_details:
            expected_total = usage_details["input"] + usage_details["output"]
            if usage_details["total"] != expected_total:
                usage_details["total"] = expected_total
                usage_total_reconciled = True

        for details_key, prefix in [
            ("prompt_tokens_details", "input"),
            ("completion_tokens_details", "output"),
            ("input_tokens_details", "input"),
            ("output_tokens_details", "output"),
        ]:
            details = usage.get(details_key)
            if isinstance(details, dict):
                for key, value in details.items():
                    if value is None:
                        continue
                    try:
                        usage_details[f"{prefix}_{key}"] = int(value)
                    except (TypeError, ValueError):
                        pass

        primary_token_aliases = {
            "input", "input_tokens", "prompt_tokens", "prompt_eval_count", "prompt_n",
            "output", "output_tokens", "completion_tokens", "eval_count", "predicted_n",
            "total", "total_tokens",
        }
        for key, value in usage.items():
            if key in primary_token_aliases:
                continue
            if isinstance(value, int) and ("token" in key or key.endswith("_count")):
                usage_details.setdefault(key, value)

        cost_details: Dict[str, float] = {}
        cost = first_present(usage, ["cost", "total_cost", "response_cost", "litellm_response_cost"])
        if cost is not None:
            try:
                cost_details["total"] = float(cost)
            except (TypeError, ValueError):
                pass

        return usage_details or None, cost_details or None, usage_total_reconciled

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
                text_parts.append(f"[image:{image_url[:120]}]")
        return "\n".join([part for part in text_parts if part])

    def _output_item_text(self, item: dict) -> str:
        if item.get("type") == "message":
            return self._extract_output_text(item.get("content", []))
        if item.get("type") == "reasoning":
            return self._extract_output_text(item.get("summary") or item.get("content") or [])
        if item.get("type") == "function_call_output":
            return self._extract_output_text(item.get("output", []))
        if item.get("type") == "open_webui:code_interpreter":
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
        return stripped.startswith("error:") or stripped.startswith("exception:")

    def _parse_output_items(self, output_items: Any) -> Tuple[List[dict], List[dict], List[dict], List[dict]]:
        if not isinstance(output_items, list):
            return [], [], [], []

        calls: Dict[str, dict] = {}
        tool_observations: List[dict] = []
        reasoning_observations: List[dict] = []
        code_observations: List[dict] = []
        server_tool_observations: List[dict] = []

        for item in output_items:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")

            if item_type == "function_call":
                call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                calls[call_id] = item
                continue

            if item_type == "function_call_output":
                call_id = item.get("call_id") or item.get("id")
                call = calls.get(call_id, {})
                name = call.get("name") or call.get("function", {}).get("name") or "tool"
                arguments = call.get("arguments") or call.get("function", {}).get("arguments") or {}
                parsed_arguments = maybe_json_loads(arguments)
                output_text = self._output_item_text(item)
                observation_type = "retriever" if name in RETRIEVER_TOOL_NAMES else "tool"
                tool_observations.append(
                    {
                        "name": name,
                        "call_id": call_id,
                        "as_type": observation_type,
                        "input": parsed_arguments if self.valves.capture_tool_arguments else {"omitted": True},
                        "output": (
                            output_text
                            if self.valves.capture_tool_outputs
                            else {"omitted": True, "content_length": len(output_text)}
                        ),
                        "level": (
                            self._level_error
                            if self._is_error_output(item, output_text)
                            else (
                                self._level_retriever
                                if observation_type == "retriever"
                                else self._level_tool
                            )
                        ),
                        "metadata": {
                            "call_id": call_id,
                            "status": item.get("status"),
                            "tool_name": name,
                            "observation_source": "open_webui_output",
                            "has_files": bool(item.get("files")),
                            "has_embeds": bool(item.get("embeds")),
                        },
                    }
                )
                continue

            if item_type in OPENAI_SERVER_TOOL_NAMES:
                output_text = self._output_item_text(item)
                server_tool_observations.append(
                    {
                        "name": item_type,
                        "as_type": "retriever" if item_type in RETRIEVER_TOOL_NAMES else "tool",
                        "input": item.get("action") or item.get("queries") or item,
                        "output": output_text,
                        "level": (
                            self._level_error
                            if self._is_error_output(item, output_text)
                            else self._level_tool
                        ),
                        "metadata": {
                            "tool_name": item_type,
                            "status": item.get("status"),
                            "observation_source": "openai_responses_output",
                        },
                    }
                )
                continue

            if item_type == "reasoning" and self.valves.capture_reasoning:
                reasoning_text = self._output_item_text(item)
                reasoning_observations.append(
                    {
                        "name": item.get("type") or "reasoning",
                        "as_type": "span",
                        "input": item.get("attributes") or {},
                        "output": reasoning_text,
                        "level": self._level_reasoning,
                        "metadata": {
                            "status": item.get("status"),
                            "duration": item.get("duration"),
                            "observation_source": "open_webui_reasoning_output",
                        },
                    }
                )
                continue

            if item_type == "open_webui:code_interpreter" and self.valves.capture_code_interpreter:
                output_text = self._output_item_text(item)
                code_observations.append(
                    {
                        "name": "code_interpreter",
                        "as_type": "tool",
                        "input": {
                            "language": item.get("lang"),
                            "code": item.get("code"),
                        },
                        "output": output_text,
                        "level": (
                            self._level_error
                            if self._is_error_output(item, output_text)
                            else self._level_tool
                        ),
                        "metadata": {
                            "status": item.get("status"),
                            "duration": item.get("duration"),
                            "observation_source": "open_webui_code_interpreter",
                        },
                    }
                )

        return tool_observations, reasoning_observations, code_observations, server_tool_observations

    def _messages_before_last_assistant(self, messages: List[dict]) -> List[dict]:
        if not messages:
            return []
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") == "assistant":
                return messages[:index]
        return messages

    def _clean_assistant_text(self, text: Optional[str]) -> Optional[str]:
        """Strip Open WebUI <details> HTML tool-call markup from assistant content.

        Open WebUI embeds tool-call context as ``<details type="tool_calls">…</details>``
        blocks inside the assistant message content string. These are UI display artefacts;
        the structured tool observations are captured separately via _parse_output_items.
        Stripping them here keeps the generation output readable in Langfuse.
        Returns None when the entire content was markup (tool-calls-only turn).
        """
        if not text:
            return text
        cleaned = _OWUI_DETAILS_RE.sub("", text).strip()
        return cleaned or None

    def _clean_messages(self, messages: List[dict]) -> List[dict]:
        """Return a shallow copy of ``messages`` with Open WebUI ``<details>`` tool-call
        HTML stripped from every assistant message's string content.

        Conversation history fed to the LLM (and shown as the trace/generation input)
        accumulates prior assistant turns, each potentially carrying its own tool-call
        markup. Cleaning the whole list keeps multi-turn inputs readable in Langfuse.
        Non-assistant messages and non-string content are passed through untouched.
        """
        if not messages:
            return messages
        cleaned: List[dict] = []
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and isinstance(message.get("content"), str)
            ):
                cleaned.append({**message, "content": _OWUI_DETAILS_RE.sub("", message["content"]).strip()})
            else:
                cleaned.append(message)
        return cleaned

    def _source_observations(self, assistant_message_obj: dict) -> List[dict]:
        sources = assistant_message_obj.get("sources") or []
        if not isinstance(sources, list):
            return []

        observations = []
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                continue
            source_info = source.get("source") or {}
            documents = source.get("document") or []
            observations.append(
                {
                    "name": source_info.get("name") or source_info.get("id") or f"source-{index + 1}",
                    "as_type": "retriever",
                    "input": {"source": source_info},
                    "output": (
                        documents
                        if self.valves.capture_retriever_outputs
                        else {"omitted": True, "document_count": len(documents)}
                    ),
                    "level": self._level_retriever,
                    "metadata": {
                        "source_index": index,
                        "source_id": source_info.get("id"),
                        "source_name": source_info.get("name"),
                        "metadata_count": len(source.get("metadata") or []),
                    },
                }
            )
        return observations

    # ------------------------------------------------------------- stream() bits
    def _delta_text(self, event: Any) -> str:
        if isinstance(event, str):
            return "" if event.strip() == "[DONE]" else event
        if not isinstance(event, dict):
            return ""
        parts: List[str] = []
        for choice in event.get("choices", []) or []:
            if isinstance(choice, dict):
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str):
                    parts.append(content)
        if not parts:
            content = event.get("content")
            if isinstance(content, str):
                parts.append(content)
        return "".join(parts)

    def _is_stream_terminal(self, event: Any) -> bool:
        if isinstance(event, str):
            return event.strip() == "[DONE]"
        if not isinstance(event, dict):
            return False
        if event.get("done") is True:
            return True
        for choice in event.get("choices", []) or []:
            if isinstance(choice, dict) and choice.get("finish_reason"):
                return True
        return False

    def _emit_stream_span(self, trace_id: str, state: dict):
        start = self._start_ts.get(trace_id)
        ttft = (state["first"] - start) if start else None
        duration = max(state.get("last", state["first"]) - state["first"], 0.0)
        chars = int(state.get("chars", 0))
        divisor = self._stream_chars_per_token or 4.0
        est_tokens = int(chars / divisor) if divisor else None

        # Agentic turns stream once per LLM call. Index the spans so each call's
        # metrics are distinguishable in the trace tree (stream_call_1, _2, ...).
        seq = self._stream_seq.get(trace_id, 0) + 1
        self._stream_seq[trace_id] = seq
        span_name = "stream" if seq == 1 else f"stream_call_{seq}"

        output: Dict[str, Any] = {
            "stream_call_index": seq,
            "ttft_seconds": round(ttft, 4) if ttft is not None else None,
            "stream_duration_seconds": round(duration, 4),
            "chunks": int(state.get("chunks", 0)),
            "chars": chars,
            "est_output_tokens": est_tokens,
        }
        if est_tokens and duration > 0:
            output["est_tokens_per_second"] = round(est_tokens / duration, 2)

        self._create_on_trace_and_end(
            trace_id,
            as_type="span",
            name=span_name,
            output_data=output,
            metadata={"observation_source": "function_stream", "stream_call_index": seq},
            level=self._level_default,
            content_free=True,
        )

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
            return await self._inlet(body, __event_emitter__, __user__, __metadata__, __model__, __request__)
        except Exception:
            self.log("inlet failed", force=True)
            return body

    async def _inlet(
        self,
        body: dict,
        __event_emitter__: Any = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __model__: Optional[dict] = None,
        __request__: Any = None,
    ) -> dict:
        self.log("INLET called")
        if __request__ is not None:
            __request__.state.langfuse_tracing = None
        if not isinstance(body, dict):
            return body
        if not self.valves.enabled:
            return body
        if not self._ensure_langfuse():
            self.log("Langfuse client not initialized; inlet skipped.", force=True)
            return body

        try:
            metadata = __metadata__ if isinstance(__metadata__, dict) else (body.get("metadata") or {})
            chat_id, message_id, session_id, explicit_task = self._ids(metadata, body)
            task_name = explicit_task or "user_response"
            trace_id = self._trace_id_for(chat_id, message_id, explicit_task)
            if not trace_id:
                return body

            if __request__ is not None:
                __request__.state.langfuse_tracing = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "enabled": self.valves.enabled,
                    "capture_inputs": self.valves.capture_inputs,
                    "capture_outputs": self.valves.capture_outputs,
                    "capture_tool_arguments": self.valves.capture_tool_arguments,
                    "capture_tool_outputs": self.valves.capture_tool_outputs,
                    "capture_retriever_outputs": self.valves.capture_retriever_outputs,
                    "capture_reasoning": self.valves.capture_reasoning,
                }

            self._start_ts[trace_id] = time.perf_counter()
            self._gc_dicts()

            model_id, model_name = self._extract_model_info(body)
            user_email = (__user__ or {}).get("id")
            trace_metadata = self._trace_metadata(
                body=body,
                chat_id=chat_id,
                message_id=message_id,
                session_id=session_id,
                task_name=task_name,
                user=__user__,
                model_id=model_id,
                model_name=model_name,
            )
            trace_metadata["root_type"] = self._resolve_root_type(task_name, body)
            tags = self._build_tags(task_name)
            safe_input = self._safe_input(body) if self.valves.capture_inputs else {"omitted": True}
            trace_name = self._trace_name(chat_id, task_name, message_id)

            user_message = get_last_message_by_role(body.get("messages", []), "user")
            event = self._start_on_trace(
                trace_id,
                as_type="event",
                name="user_input",
                input_data=user_message if user_message and self.valves.capture_inputs else None,
                metadata={**trace_metadata, "observation_source": "function_inlet"},
                level=self._level_user_event,
            )
            # Seed trace-level attributes early so the trace is usable even if outlet
            # never runs (e.g. direct API callers that skip /api/chat/completed).
            self._apply_trace_attributes(
                event,
                name=trace_name,
                user_id=user_email,
                session_id=chat_id,
                tags=tags,
                metadata=trace_metadata,
                input_data=safe_input,
            )
            self._end_observation(event)
        except Exception as e:
            self.log(f"inlet trace seed failed: {e}", force=True)

        return body

    # -------------------------------------------------------------------- stream
    async def stream(
        self,
        event: dict,
        __event_emitter__: Any = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        try:
            return await self._stream_event(event, __event_emitter__, __metadata__)
        except Exception:
            self.log("stream failed", force=True)
            return event

    async def _stream_event(
        self,
        event: dict,
        __event_emitter__: Any = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        if not self.valves.capture_stream_metrics or not self.valves.enabled:
            return event
        if not self._ensure_langfuse():
            return event

        try:
            metadata = __metadata__ if isinstance(__metadata__, dict) else {}
            chat_id, message_id, _session_id, explicit_task = self._ids(metadata, {})
            trace_id = self._trace_id_for(chat_id, message_id, explicit_task)
            if not trace_id or trace_id not in self._start_ts:
                return event

            now = time.perf_counter()
            text = self._delta_text(event)
            terminal = self._is_stream_terminal(event)

            if text:
                state = self._stream.get(trace_id)
                if state is None:
                    state = self._stream[trace_id] = {"first": now, "last": now, "chunks": 0, "chars": 0}
                state["last"] = now
                state["chunks"] += 1
                state["chars"] += len(text)

            if terminal:
                state = self._stream.pop(trace_id, None)
                if state is not None:
                    self._emit_stream_span(trace_id, state)
        except Exception as e:
            self.log(f"stream() error: {e}")

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
            return await self._outlet(body, __event_emitter__, __user__, __metadata__)
        except Exception:
            self.log("outlet failed", force=True)
            return body

    async def _outlet(
        self,
        body: dict,
        __event_emitter__: Any = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        self.log("OUTLET called")
        if not isinstance(body, dict):
            return body
        if not self.valves.enabled:
            return body
        if not self._ensure_langfuse():
            self.log("Langfuse client not initialized; outlet skipped.", force=True)
            return body

        metadata = __metadata__ if isinstance(__metadata__, dict) else (body.get("metadata") or {})
        chat_id, message_id, session_id, explicit_task = self._ids(metadata, body)
        trace_id = self._trace_id_for(chat_id, message_id, explicit_task)
        if not trace_id:
            return body

        messages = body.get("messages", []) or []
        assistant_message_obj = get_last_message_by_role(messages, "assistant")
        assistant_message = get_last_assistant_message(messages)
        # Strip <details type="tool_calls"> HTML from the text used as Langfuse output;
        # the raw content is still used below for PII scanning.
        clean_assistant_message = self._clean_assistant_text(assistant_message)
        output_items = assistant_message_obj.get("output") if assistant_message_obj else None
        # OWUI 0.10.x saves only structured `output` (not string `content`) to the DB for
        # assistant turns, so `message["content"]` comes back as "" for persistent chats.
        # Fall back to reconstructing the visible text from the `message`-type items in
        # `output_items` so the generation observation's output is populated.
        if not clean_assistant_message and isinstance(output_items, list):
            text_parts = [
                self._output_item_text(item)
                for item in output_items
                if isinstance(item, dict) and item.get("type") == "message"
            ]
            clean_assistant_message = "\n".join(p for p in text_parts if p) or None
        usage_raw = assistant_message_obj.get("usage") if assistant_message_obj else None
        usage_details, cost_details, usage_total_reconciled = self._extract_usage_details(usage_raw)

        # Task: explicit metadata first, then infer from response shape (background tasks).
        task_name = explicit_task or self._infer_task_name_from_assistant_message(assistant_message) or "llm_response"

        model_id, model_name = self._extract_model_info(body)

        tool_observations, reasoning_observations, code_observations, server_tool_observations = (
            self._parse_output_items(output_items)
        )
        source_observations = self._source_observations(assistant_message_obj or {})

        # Root type: prefer response-derived signals, then request signals.
        has_tool_calls = bool(tool_observations or server_tool_observations or code_observations)
        has_sources = bool(source_observations)
        if task_name in {t.strip() for t in self.valves.background_tasks.split(",") if t.strip()}:
            root_type = self._root_type_utility
        elif has_tool_calls:
            root_type = self._root_type_agent
        elif has_sources:
            root_type = self._root_type_rag
        else:
            root_type = self._resolve_root_type(task_name, body)

        trace_metadata = self._trace_metadata(
            body=body,
            chat_id=chat_id,
            message_id=message_id,
            session_id=session_id,
            task_name=task_name,
            user=__user__,
            model_id=model_id,
            model_name=model_name,
        )
        trace_metadata["root_type"] = root_type
        trace_metadata["output_items_count"] = len(output_items) if isinstance(output_items, list) else 0
        trace_metadata["has_usage"] = bool(usage_details)
        start = self._start_ts.pop(trace_id, None)
        if start is not None:
            trace_metadata["request_duration_seconds"] = round(time.perf_counter() - start, 4)

        tags = self._build_tags(task_name)
        user_id = (__user__ or {}).get("id")
        # Trace input = cleaned prior conversation context only. The final assistant turn
        # belongs in `output`, not `input`, so it is excluded here (and its <details>
        # markup is stripped from any earlier assistant turns).
        if self.valves.capture_inputs:
            input_messages = self._clean_messages(self._messages_before_last_assistant(messages))
            safe_input = {"model": body.get("model"), "messages": input_messages}
        else:
            safe_input = {"omitted": True}
        trace_name = self._trace_name(chat_id, task_name, message_id)

        try:
            root = self._start_on_trace(
                trace_id,
                as_type=root_type,
                name=trace_name,
                input_data=safe_input,
                metadata=trace_metadata,
                level=self._level_default,
            )
        except Exception as e:
            self.log(f"Failed to create root observation: {e}", force=True)
            return body

        self._apply_trace_attributes(
            root,
            name=trace_name,
            user_id=user_id,
            session_id=chat_id,
            tags=tags,
            metadata=trace_metadata,
            input_data=safe_input,
            output_data=clean_assistant_message if self.valves.capture_outputs else {"omitted": True},
        )

        # Reasoning spans: directly under root.
        for observation in reasoning_observations:
            self._create_child_and_end(root, **observation)

        # Tools / server-tools / code: directly under root.
        for observation in [*tool_observations, *server_tool_observations, *code_observations]:
            status_message = None
            if observation.get("level") == self._level_error:
                status_message = f"{observation.get('name')} returned an error-shaped result"
            self._create_child_and_end(root, **observation, status_message=status_message)

        # RAG chain grouping: retrievers (+ generation) under an inner chain unless the
        # root is already a chain. Never stack two chain layers.
        rag_chain = None
        rag_parent: Any = root
        if source_observations:
            if root_type == self._root_type_rag:
                for observation in source_observations:
                    self._create_child_and_end(root, **observation)
            elif self._group_rag_as_chain:
                user_msg = get_last_message_by_role(messages, "user")
                user_query = (get_content_from_message(user_msg) or "")[:500]
                try:
                    rag_chain = self._start_child_observation(
                        root,
                        as_type="chain",
                        name="rag",
                        input_data={"query": user_query} if user_query and self.valves.capture_inputs else None,
                        metadata={
                            **trace_metadata,
                            "rag_source_count": len(source_observations),
                            "observation_source": "function_rag_grouping",
                        },
                        level=self._level_default,
                    )
                    rag_parent = rag_chain
                except Exception as e:
                    self.log(f"Failed to create rag chain wrapper: {e}")
                for observation in source_observations:
                    self._create_child_and_end(rag_parent, **observation)
            else:
                for observation in source_observations:
                    self._create_child_and_end(root, **observation)

        tool_names = [obs.get("name") for obs in [*tool_observations, *server_tool_observations, *code_observations]]
        generation_metadata = {
            **trace_metadata,
            "type": "llm_response",
            "model_id": model_id if model_id is not None else body.get("model"),
            "model_name": model_name,
            "tool_call_count": len(tool_names),
            "tool_names": tool_names,
            "usage_raw": None,
            "usage_total_reconciled": usage_total_reconciled,
        }

        try:
            generation = self._start_child_observation(
                rag_parent,
                as_type="generation",
                name="llm_response",
                model=self._model_value(model_id, model_name),
                input_data=(
                    self._clean_messages(self._messages_before_last_assistant(messages))
                    if self.valves.capture_inputs
                    else {"omitted": True}
                ),
                output_data=clean_assistant_message if self.valves.capture_outputs else {"omitted": True},
                metadata=generation_metadata,
                level=self._level_default,
                model_parameters=self._model_parameters(body),
                # Usage always flows (metrics, not content): the trace keeps
                # token counts even when the litellm correlator is disabled or
                # gated off. OWUI's aggregated usage for multi-call agentic
                # turns is unreliable — _extract_usage_details reconciles
                # total = input + output and flags it in metadata; accurate
                # per-call numbers live on the litellm_generation observations.
                usage_details=usage_details,
                cost_details=cost_details,
            )
            self._end_observation(generation)
        except Exception as e:
            self.log(f"Failed to create LLM generation: {e}", force=True)

        if rag_chain is not None:
            self._end_observation(rag_chain)

        # Guardrail observation: per-category COUNTS of PII/secret redactions only
        # (never the matched strings), so it is itself safe to store.
        if self.valves.enable_masking and self._emit_guardrail_observation:
            user_msg = get_last_message_by_role(messages, "user")
            # Scan the reconstructed assistant text (clean_assistant_message), not the raw
            # `assistant_message`, because OWUI 0.10.x leaves `content` empty for persistent
            # chats — scanning the raw value would miss PII in the assistant's response.
            scan_text = (get_content_from_message(user_msg) or "") + "\n" + (clean_assistant_message or "")
            redaction_counts = self._count_redactions(scan_text)
            if redaction_counts:
                categories_enabled = [
                    cat
                    for cat, active in [
                        ("data_uris", not self.valves.capture_multimodal),
                        ("emails", self._mask_emails),
                        ("phone_numbers", self._mask_phone_numbers),
                        ("api_keys", self._mask_api_keys),
                    ]
                    if active
                ]
                self._create_child_and_end(
                    root,
                    as_type="guardrail",
                    name="pii_masking",
                    input_data={
                        "scanned_chars": len(scan_text),
                        "categories_enabled": categories_enabled,
                    },
                    output_data={
                        **redaction_counts,
                        "total_redacted": sum(redaction_counts.values()),
                    },
                    level=self._level_guardrail,
                    metadata={**trace_metadata, "observation_source": "function_masking"},
                    content_free=True,
                )

        try:
            root.end()
        except Exception as e:
            self.log(f"Failed to end root observation: {e}", force=True)

        # Best-effort: emit a stream span if a terminal chunk was never observed.
        leftover = self._stream.pop(trace_id, None)
        if leftover is not None:
            try:
                self._emit_stream_span(trace_id, leftover)
            except Exception:
                pass
        # Turn complete — clear the per-turn stream counter.
        self._stream_seq.pop(trace_id, None)

        return body