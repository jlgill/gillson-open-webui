"""Temporary diagnostic callback — logs incoming request headers to stdout.

Mounted as /app/litellm_diagnostic.py. Registered in litellm-config.yaml
success_callback to capture the exact headers VS Code (and other clients)
send to LiteLLM. Remove after header discovery is complete.
"""

from __future__ import annotations

import json
import sys
from typing import Any


class HeaderDiagnosticLogger:
    """LiteLLM CustomLogger that prints request headers to stdout."""

    def __init__(self) -> None:
        print("HeaderDiagnosticLogger: initialized", flush=True)

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        """Makes _is_async_callable() return True → routes to async callback."""
        pass

    async def async_log_success_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        try:
            litellm_params = kwargs.get("litellm_params", {})
            proxy_req = litellm_params.get("proxy_server_request", {})
            headers = proxy_req.get("headers", {})
            model = kwargs.get("model", "unknown")
            metadata = litellm_params.get("metadata", {})

            # Extract key metadata fields
            user_api_key = metadata.get("user_api_key", "N/A")
            user_api_key_alias = metadata.get("user_api_key_alias", "N/A")
            user_api_key_user_id = metadata.get("user_api_key_user_id", "N/A")

            # Print headers as JSON for easy parsing
            print(
                "HEADER_DIAGNOSTIC_START\n"
                + json.dumps(
                    {
                        "model": model,
                        "user_api_key": user_api_key,
                        "user_api_key_alias": user_api_key_alias,
                        "user_api_key_user_id": user_api_key_user_id,
                        "headers": dict(headers) if headers else {},
                    },
                    indent=2,
                    default=str,
                )
                + "\nHEADER_DIAGNOSTIC_END",
                flush=True,
            )
        except Exception as exc:
            print(f"HeaderDiagnosticLogger: error — {exc}", file=sys.stderr, flush=True)


# Module-level singleton
header_diagnostic_logger = HeaderDiagnosticLogger()