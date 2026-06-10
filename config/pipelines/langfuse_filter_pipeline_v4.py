"""
title: Langfuse Filter Pipeline v4
author: open-webui
date: 2026-06-04
version: 0.1.0
license: MIT
description: A self-contained Langfuse v4 proposal filter for Open WebUI Pipeline inlet/outlet payloads.
requirements: langfuse>=3.15.0
"""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import nullcontext
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

try:
    from langfuse import Langfuse, propagate_attributes
except ImportError:
    from langfuse import Langfuse

    propagate_attributes = None


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


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


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
        if message.get("role") == role:
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


class Pipeline:
    class Valves(BaseModel):
        pipelines: list[str] = Field(default_factory=lambda: ["*"])
        priority: int = 0

        secret_key: str = os.getenv("LANGFUSE_SECRET_KEY", "your-secret-key-here")
        public_key: str = os.getenv("LANGFUSE_PUBLIC_KEY", "your-public-key-here")
        host: str = os.getenv("LANGFUSE_HOST", os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"))

        enabled: bool = env_bool("LANGFUSE_PIPELINE_ENABLED", True)
        debug: bool = env_bool("DEBUG_MODE", False)
        insert_tags: bool = True
        use_model_name_instead_of_id_for_generation: bool = env_bool("USE_MODEL_NAME", False)

        trace_version: str = "open-webui-langfuse-pipeline-v4"
        environment: str | None = os.getenv("LANGFUSE_TRACING_ENVIRONMENT")
        release: str | None = os.getenv("LANGFUSE_RELEASE")
        sample_rate: float = env_float("LANGFUSE_SAMPLE_RATE", 1.0)

        root_observation_type: str = "agent"
        default_level: str = "DEFAULT"
        user_event_level: str = "DEFAULT"
        tool_level: str = "DEFAULT"
        retriever_level: str = "DEFAULT"
        reasoning_level: str = "DEBUG"
        error_level: str = "ERROR"

        capture_inputs: bool = True
        capture_outputs: bool = True
        capture_tool_arguments: bool = True
        capture_tool_outputs: bool = True
        capture_retriever_outputs: bool = True
        capture_reasoning: bool = True
        capture_code_interpreter: bool = True
        capture_multimodal: bool = False

        enable_masking: bool = True
        mask_emails: bool = True
        mask_phone_numbers: bool = True
        mask_api_keys: bool = True
        mask_metadata_keys: str = (
            "authorization,cookie,set-cookie,api_key,secret_key,access_token," "refresh_token,password,token,key"
        )
        mask_replacement: str = "[REDACTED]"
        max_field_chars: int = env_int("LANGFUSE_PIPELINE_MAX_FIELD_CHARS", 20000)
        max_collection_items: int = env_int("LANGFUSE_PIPELINE_MAX_COLLECTION_ITEMS", 200)
        flush_on_outlet: bool = True

    def __init__(self):
        self.type = "filter"
        self.name = "Langfuse Filter v4"
        self.valves = self.Valves()

        self.langfuse = None
        self.active_traces: Dict[str, dict] = {}
        self.chat_aliases: Dict[str, str] = {}
        self.model_names: Dict[str, dict] = {}
        self.suppressed_logs = set()

    def log(self, message: str, suppress_repeats: bool = False, force: bool = False):
        if suppress_repeats:
            if message in self.suppressed_logs:
                return
            self.suppressed_logs.add(message)
        if self.valves.debug or force:
            print(f"[Langfuse v4] {message}")

    async def on_startup(self):
        self.log(f"on_startup triggered for {__name__}")
        self.set_langfuse()

    async def on_shutdown(self):
        self.log(f"on_shutdown triggered for {__name__}")
        if self.langfuse:
            for key, entry in list(self.active_traces.items()):
                try:
                    root = entry.get("root")
                    if root is not None:
                        root.end()
                except Exception as e:
                    self.log(f"Failed to end active trace {key}: {e}", force=True)

            self.active_traces.clear()
            self.chat_aliases.clear()

            try:
                self.langfuse.flush()
            except Exception as e:
                self.log(f"Failed to flush Langfuse data on shutdown: {e}", force=True)

    async def on_valves_updated(self):
        self.log("Valves updated, resetting Langfuse client.")
        self.set_langfuse()

    def set_langfuse(self):
        if not self.valves.enabled:
            self.langfuse = None
            self.log("Langfuse pipeline disabled by valve.")
            return

        try:
            kwargs = {
                "secret_key": self.valves.secret_key,
                "public_key": self.valves.public_key,
                "host": self.valves.host,
                "debug": self.valves.debug,
                "sample_rate": self.valves.sample_rate,
            }
            if self.valves.environment:
                kwargs["environment"] = self.valves.environment
            if self.valves.release:
                kwargs["release"] = self.valves.release
            if self.valves.enable_masking:
                kwargs["mask"] = self.mask_data

            try:
                self.langfuse = Langfuse(**kwargs)
            except TypeError:
                kwargs.pop("mask", None)
                kwargs.pop("sample_rate", None)
                kwargs.pop("environment", None)
                kwargs.pop("release", None)
                self.langfuse = Langfuse(**kwargs)

            try:
                self.langfuse.auth_check()
                self.log(f"Langfuse client initialized for host: {self.valves.host}", force=True)
            except Exception as e:
                self.log(f"Langfuse auth check failed for host {self.valves.host}: {e}", force=True)
                self.langfuse = None
        except Exception as e:
            self.log(f"Langfuse initialization failed: {e}", force=True)
            self.langfuse = None

    def _mask_keys(self) -> set[str]:
        return {key.strip().lower() for key in self.valves.mask_metadata_keys.split(",") if key.strip()}

    def _should_mask_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(mask_key in lowered for mask_key in self._mask_keys())

    def _mask_string(self, value: str) -> str:
        replacement = self.valves.mask_replacement
        result = value

        if not self.valves.capture_multimodal:
            result = re.sub(
                r"data:([^;,\s]+);base64,[A-Za-z0-9+/=\r\n]+",
                lambda match: f"[REDACTED {match.group(1)} DATA URI]",
                result,
            )

        if self.valves.mask_emails:
            result = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", replacement, result)

        if self.valves.mask_phone_numbers:
            result = re.sub(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", replacement, result)

        if self.valves.mask_api_keys:
            result = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", f"Bearer {replacement}", result, flags=re.I)
            result = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{12,}\b", replacement, result)
            result = re.sub(r"\b(?:sk|pk)-lf-[A-Za-z0-9_-]{12,}\b", replacement, result)
            result = re.sub(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b", replacement, result)

        max_chars = max(self.valves.max_field_chars, 0)
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
                if index >= self.valves.max_collection_items:
                    output["__truncated_items__"] = len(data) - self.valves.max_collection_items
                    break
                key_string = str(key)
                if self.valves.enable_masking and self._should_mask_key(key_string):
                    output[key_string] = self.valves.mask_replacement
                else:
                    output[key_string] = self._safe_data(value, depth + 1)
            return output

        if isinstance(data, (list, tuple, set)):
            items = list(data)
            output = [self._safe_data(value, depth + 1) for value in items[: self.valves.max_collection_items]]
            if len(items) > self.valves.max_collection_items:
                output.append({"__truncated_items__": len(items) - self.valves.max_collection_items})
            return output

        return self._mask_string(str(data))

    def _build_tags(self, task_name: str, extra_tags: Optional[List[str]] = None) -> List[str]:
        tags = []
        if self.valves.insert_tags:
            tags.extend(["open-webui", "langfuse-v4"])
            if task_name and task_name not in {"user_response", "llm_response"}:
                tags.append(task_name)
        if extra_tags:
            tags.extend(extra_tags)

        deduped = []
        for tag in tags:
            if tag and tag not in deduped:
                deduped.append(str(tag)[:200])
        return deduped

    def _normalize_chat_id(self, chat_id: Optional[Any], session_id: Optional[Any] = None) -> str:
        if chat_id == "local":
            return f"temporary-session-{session_id or uuid.uuid4()}"
        if chat_id:
            return str(chat_id)
        return f"trace-{uuid.uuid4()}"

    def _turn_key(self, chat_id: str, message_id: Optional[Any] = None) -> str:
        return f"{chat_id}:{message_id}" if message_id else chat_id

    def _model_value(self, chat_id: str, body_model: Optional[Any] = None) -> Optional[str]:
        model_id = self.model_names.get(chat_id, {}).get("id", body_model)
        model_name = self.model_names.get(chat_id, {}).get("name")
        if self.valves.use_model_name_instead_of_id_for_generation and model_name:
            return str(model_name)
        return str(model_id) if model_id is not None else None

    def _store_model_info(self, chat_id: str, body: dict):
        model_id = body.get("model")
        metadata_model = body.get("metadata", {}).get("model", {})

        model_info = self.model_names.setdefault(chat_id, {})
        if model_id is not None:
            model_info["id"] = model_id
        if isinstance(metadata_model, dict) and metadata_model.get("name"):
            model_info["name"] = metadata_model["name"]

    def _trace_metadata(
        self,
        *,
        body: dict,
        chat_id: str,
        message_id: Optional[Any],
        task_name: str,
        user: Optional[dict],
    ) -> dict:
        metadata = body.get("metadata", {}) if isinstance(body.get("metadata"), dict) else {}
        model_info = self.model_names.get(chat_id, {})
        return {
            "interface": "open-webui",
            "pipeline": "langfuse-filter-v4",
            "pipeline_version": self.valves.trace_version,
            "chat_id": chat_id,
            "session_id": metadata.get("session_id") or body.get("session_id") or chat_id,
            "message_id": message_id,
            "task": task_name,
            "model_id": model_info.get("id", body.get("model")),
            "model_name": model_info.get("name"),
            "user_id": user.get("id") if user else None,
            "user_email": user.get("email") if user else None,
        }

    def _trace_name(self, chat_id: str, task_name: str, message_id: Optional[Any] = None) -> str:
        if task_name and task_name not in {"user_response", "llm_response"}:
            return f"open-webui:{task_name}:{chat_id}"
        if message_id:
            return f"open-webui:chat:{chat_id}:{message_id}"
        return f"open-webui:chat:{chat_id}"

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

        if hasattr(root, "set_trace_io") and (input_data is not None or output_data is not None):
            try:
                root.set_trace_io(
                    input=self._safe_data(input_data) if input_data is not None else None,
                    output=self._safe_data(output_data) if output_data is not None else None,
                )
            except Exception:
                pass

    def _start_root_observation(
        self,
        *,
        name: str,
        input_data: Optional[Any],
        metadata: dict,
        level: str,
    ) -> Any:
        kwargs = {
            "name": name,
            "input": self._safe_data(input_data) if input_data is not None else None,
            "metadata": self._safe_data(metadata),
            "level": level,
        }

        if hasattr(self.langfuse, "start_observation"):
            try:
                return self.langfuse.start_observation(
                    as_type=self.valves.root_observation_type,
                    **kwargs,
                )
            except TypeError:
                try:
                    return self.langfuse.start_observation(as_type="span", **kwargs)
                except TypeError:
                    pass

        if hasattr(self.langfuse, "start_span"):
            try:
                return self.langfuse.start_span(**kwargs)
            except TypeError:
                legacy_kwargs = {key: value for key, value in kwargs.items() if key in {"name", "input", "metadata"}}
                return self.langfuse.start_span(**legacy_kwargs)

        if hasattr(self.langfuse, "span"):
            try:
                return self.langfuse.span(**kwargs)
            except TypeError:
                legacy_kwargs = {key: value for key, value in kwargs.items() if key in {"name", "input", "metadata"}}
                return self.langfuse.span(**legacy_kwargs)

        raise RuntimeError("Installed Langfuse SDK does not expose a supported span API")

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
        kwargs = {key: value for key, value in kwargs.items() if value is not None}

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
            observation = self._start_child_observation(parent, **child_kwargs)
            self._end_observation(observation)
        except Exception as e:
            self.log(f"Failed to create observation {child_kwargs.get('name')}: {e}")

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

    def _extract_usage_details(self, usage: Optional[dict]) -> Tuple[Optional[dict], Optional[dict]]:
        if not isinstance(usage, dict) or not usage:
            return None, None

        input_tokens = first_present(
            usage,
            ["input", "input_tokens", "prompt_tokens", "prompt_eval_count", "prompt_n"],
        )
        output_tokens = first_present(
            usage,
            ["output", "output_tokens", "completion_tokens", "eval_count", "predicted_n"],
        )
        total_tokens = first_present(usage, ["total", "total_tokens"])

        usage_details = {}
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

        for key, value in usage.items():
            if isinstance(value, int) and ("token" in key or key.endswith("_count")):
                usage_details.setdefault(key, value)

        cost_details = {}
        cost = first_present(usage, ["cost", "total_cost", "response_cost", "litellm_response_cost"])
        if cost is not None:
            try:
                cost_details["total"] = float(cost)
            except (TypeError, ValueError):
                pass

        return usage_details or None, cost_details or None

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
                            self.valves.error_level
                            if self._is_error_output(item, output_text)
                            else (
                                self.valves.retriever_level
                                if observation_type == "retriever"
                                else self.valves.tool_level
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
                            self.valves.error_level
                            if self._is_error_output(item, output_text)
                            else self.valves.tool_level
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
                        "as_type": "chain",
                        "input": item.get("attributes") or {},
                        "output": reasoning_text,
                        "level": self.valves.reasoning_level,
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
                            self.valves.error_level
                            if self._is_error_output(item, output_text)
                            else self.valves.tool_level
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
                    "level": self.valves.retriever_level,
                    "metadata": {
                        "source_index": index,
                        "source_id": source_info.get("id"),
                        "source_name": source_info.get("name"),
                        "metadata_count": len(source.get("metadata") or []),
                    },
                }
            )
        return observations

    def _register_trace(
        self,
        *,
        key: str,
        chat_id: str,
        message_id: Optional[Any],
        root: Any,
        trace_name: str,
        trace_metadata: dict,
        tags: List[str],
        input_body: dict,
        model_parameters: dict,
    ):
        self.active_traces[key] = {
            "root": root,
            "chat_id": chat_id,
            "message_id": message_id,
            "trace_name": trace_name,
            "trace_metadata": trace_metadata,
            "tags": tags,
            "input_body": self._safe_data(input_body),
            "model_parameters": model_parameters,
        }
        self.chat_aliases[chat_id] = key

    def _get_trace_entry(self, chat_id: str, message_id: Optional[Any]) -> Tuple[Optional[str], Optional[dict]]:
        key = self._turn_key(chat_id, message_id)
        if key in self.active_traces:
            return key, self.active_traces[key]

        alias = self.chat_aliases.get(chat_id)
        if alias and alias in self.active_traces:
            return alias, self.active_traces[alias]

        if chat_id in self.active_traces:
            return chat_id, self.active_traces[chat_id]

        return None, None

    async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        self.log("INLET called")
        if not self.langfuse:
            self.log("Langfuse client not initialized; inlet skipped.", suppress_repeats=True)
            return body

        required_keys = ["model", "messages"]
        missing_keys = [key for key in required_keys if key not in body]
        if missing_keys:
            raise ValueError(f"Missing keys in request body: {', '.join(missing_keys)}")

        metadata = body.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        chat_id = self._normalize_chat_id(
            metadata.get("chat_id") or body.get("chat_id"),
            metadata.get("session_id") or body.get("session_id"),
        )
        message_id = metadata.get("message_id") or metadata.get("id") or body.get("message_id") or body.get("id")
        task_name = metadata.get("task", "user_response")

        metadata["chat_id"] = chat_id
        body["metadata"] = metadata

        self._store_model_info(chat_id, body)

        key = self._turn_key(chat_id, message_id)
        trace_name = self._trace_name(chat_id, task_name, message_id)
        trace_metadata = self._trace_metadata(
            body=body,
            chat_id=chat_id,
            message_id=message_id,
            task_name=task_name,
            user=user,
        )
        tags = self._build_tags(task_name)

        input_body = body if self.valves.capture_inputs else {"omitted": True}

        try:
            context = (
                propagate_attributes(
                    user_id=str(user.get("email") or user.get("id")) if user else None,
                    session_id=chat_id,
                    metadata={
                        "interface": "open-webui",
                        "pipeline": "langfuse-filter-v4",
                        "task": str(task_name),
                    },
                    tags=tags,
                    trace_name=trace_name,
                )
                if propagate_attributes
                else nullcontext()
            )
            with context:
                root = self._start_root_observation(
                    name=trace_name,
                    input_data=input_body,
                    metadata=trace_metadata,
                    level=self.valves.default_level,
                )
        except Exception as e:
            self.log(f"Failed to create root trace: {e}", force=True)
            return body

        self._apply_trace_attributes(
            root,
            name=trace_name,
            user_id=str(user.get("email") or user.get("id")) if user else None,
            session_id=chat_id,
            tags=tags,
            metadata=trace_metadata,
            input_data=input_body,
        )

        self._register_trace(
            key=key,
            chat_id=chat_id,
            message_id=message_id,
            root=root,
            trace_name=trace_name,
            trace_metadata=trace_metadata,
            tags=tags,
            input_body=input_body,
            model_parameters=self._model_parameters(body),
        )

        user_message = get_last_message_by_role(body.get("messages", []), "user")
        if user_message:
            self._create_child_and_end(
                root,
                as_type="event",
                name="user_input",
                input_data=user_message,
                metadata={
                    **trace_metadata,
                    "observation_source": "pipeline_inlet",
                },
                level=self.valves.user_event_level,
            )

        return body

    async def outlet(self, body: dict, user: Optional[dict] = None) -> dict:
        self.log("OUTLET called")
        if not self.langfuse:
            self.log("Langfuse client not initialized; outlet skipped.", suppress_repeats=True)
            return body

        chat_id = self._normalize_chat_id(body.get("chat_id"), body.get("session_id"))
        message_id = body.get("id") or body.get("message_id")
        task_name = (
            body.get("metadata", {}).get("task", "llm_response")
            if isinstance(body.get("metadata"), dict)
            else "llm_response"
        )
        key, entry = self._get_trace_entry(chat_id, message_id)

        if not entry:
            self.log(f"No active inlet trace for chat_id={chat_id}; creating outlet-only trace.")
            fallback_metadata = {
                "chat_id": chat_id,
                "session_id": body.get("session_id") or chat_id,
                "message_id": message_id,
                "task": task_name,
                "interface": "open-webui",
                "pipeline": "langfuse-filter-v4",
            }
            trace_name = self._trace_name(chat_id, task_name, message_id)
            try:
                root = self._start_root_observation(
                    name=trace_name,
                    input_data=None,
                    metadata=fallback_metadata,
                    level=self.valves.default_level,
                )
            except Exception as e:
                self.log(f"Failed to create outlet-only trace: {e}", force=True)
                return body
            key = self._turn_key(chat_id, message_id)
            entry = {
                "root": root,
                "chat_id": chat_id,
                "message_id": message_id,
                "trace_name": trace_name,
                "trace_metadata": fallback_metadata,
                "tags": self._build_tags(task_name),
                "input_body": None,
                "model_parameters": {},
            }
            self.active_traces[key] = entry

        root = entry["root"]
        messages = body.get("messages", [])
        assistant_message_obj = get_last_message_by_role(messages, "assistant")
        assistant_message = get_last_assistant_message(messages)
        output_items = assistant_message_obj.get("output") if assistant_message_obj else None
        usage_raw = assistant_message_obj.get("usage") if assistant_message_obj else None
        usage_details, cost_details = self._extract_usage_details(usage_raw)

        trace_metadata = {
            **entry.get("trace_metadata", {}),
            "task": task_name,
            "output_items_count": len(output_items) if isinstance(output_items, list) else 0,
            "has_usage": bool(usage_details),
        }
        tags = entry.get("tags") or self._build_tags(task_name)

        self._apply_trace_attributes(
            root,
            name=entry.get("trace_name") or self._trace_name(chat_id, task_name, message_id),
            user_id=str(user.get("email") or user.get("id")) if user else trace_metadata.get("user_email"),
            session_id=chat_id,
            tags=tags,
            metadata=trace_metadata,
            input_data=entry.get("input_body"),
            output_data=assistant_message if self.valves.capture_outputs else {"omitted": True},
        )

        tool_observations, reasoning_observations, code_observations, server_tool_observations = (
            self._parse_output_items(output_items)
        )
        source_observations = self._source_observations(assistant_message_obj)

        for observation in source_observations:
            self._create_child_and_end(root, **observation)

        for observation in reasoning_observations:
            self._create_child_and_end(root, **observation)

        for observation in [*tool_observations, *server_tool_observations, *code_observations]:
            status_message = None
            if observation.get("level") == self.valves.error_level:
                status_message = f"{observation.get('name')} returned an error-shaped result"
            self._create_child_and_end(root, **observation, status_message=status_message)

        tool_names = [obs.get("name") for obs in [*tool_observations, *server_tool_observations, *code_observations]]
        generation_metadata = {
            **trace_metadata,
            "type": "llm_response",
            "model_id": self.model_names.get(chat_id, {}).get("id", body.get("model")),
            "model_name": self.model_names.get(chat_id, {}).get("name"),
            "tool_call_count": len(tool_names),
            "tool_names": tool_names,
            "usage_raw": usage_raw,
        }

        try:
            generation = self._start_child_observation(
                root,
                as_type="generation",
                name="llm_response",
                model=self._model_value(chat_id, body.get("model")),
                input_data=(
                    self._messages_before_last_assistant(messages) if self.valves.capture_inputs else {"omitted": True}
                ),
                output_data=assistant_message if self.valves.capture_outputs else {"omitted": True},
                metadata=generation_metadata,
                level=self.valves.default_level,
                model_parameters=entry.get("model_parameters") or {},
                usage_details=usage_details,
                cost_details=cost_details,
            )
            self._update_generation_usage(generation, usage_details, cost_details)
            self._end_observation(generation)
        except Exception as e:
            self.log(f"Failed to create LLM generation: {e}", force=True)

        try:
            root.end()
        except Exception as e:
            self.log(f"Failed to end root span: {e}", force=True)

        if key:
            self.active_traces.pop(key, None)
        if self.chat_aliases.get(chat_id) == key:
            self.chat_aliases.pop(chat_id, None)

        if self.valves.flush_on_outlet:
            try:
                self.langfuse.flush()
            except Exception as e:
                self.log(f"Failed to flush Langfuse data: {e}", force=True)

        return body
