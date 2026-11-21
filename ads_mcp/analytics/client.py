# Copyright 2026 Converted BV.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Google Analytics 4 clients, scopes, IDs and output formatting.

The GA4 tools reuse the server's Google OAuth login: when the server runs
behind the Google OAuth provider, the user's access token (which then also
carries the Analytics scopes) is used for the Admin and Data APIs. Locally,
Application Default Credentials with the Analytics scopes are used.

GOOGLE_ADS_MCP_ANALYTICS controls the feature:
  - unset / "off": no Analytics tools, no extra OAuth scopes (default).
  - "read": read-only Analytics tools, scope analytics.readonly.
  - "full": all Analytics tools including propose & confirm changes, scopes
    analytics.readonly, analytics.edit and analytics.manage.users.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from fastmcp.exceptions import ToolError

ANALYTICS_MODE_ENV_VAR = "GOOGLE_ADS_MCP_ANALYTICS"

SCOPE_READONLY = "https://www.googleapis.com/auth/analytics.readonly"
SCOPE_EDIT = "https://www.googleapis.com/auth/analytics.edit"
SCOPE_MANAGE_USERS = "https://www.googleapis.com/auth/analytics.manage.users"

# Tool namespaces (FastMCP sub-server names) that belong to GA4.
READ_NAMESPACES = (
    "analytics_reporting",
    "analytics_insights",
    "analytics_admin",
)
WRITE_NAMESPACES = ("analytics_management",)
ALL_NAMESPACES = READ_NAMESPACES + WRITE_NAMESPACES

# Upper bound on the JSON a single tool returns, to protect the context window.
MAX_OUTPUT_CHARS = 120_000


def analytics_mode() -> str:
    """Returns 'off', 'read' or 'full'."""
    raw = os.environ.get(ANALYTICS_MODE_ENV_VAR, "").strip().lower()
    if raw in ("full", "write", "all", "true", "1", "yes", "on"):
        return "full"
    if raw in ("read", "readonly", "read-only", "read_only"):
        return "read"
    return "off"


def analytics_scopes() -> list[str]:
    """OAuth scopes the GA4 tools need in the current mode."""
    mode = analytics_mode()
    if mode == "full":
        return [SCOPE_READONLY, SCOPE_EDIT, SCOPE_MANAGE_USERS]
    if mode == "read":
        return [SCOPE_READONLY]
    return []


def is_namespace_allowed(category: str) -> bool:
    """Decides whether an Analytics namespace may be mounted in this mode.

    Non-Analytics namespaces are always allowed (this gate does not touch
    them).
    """
    if category not in ALL_NAMESPACES:
        return True
    mode = analytics_mode()
    if mode == "full":
        return True
    if mode == "read":
        return category in READ_NAMESPACES
    return False


# --------------------------------------------------------------------------
# Credentials and clients
# --------------------------------------------------------------------------


def _credentials():
    """User OAuth token when served remotely, otherwise ADC."""
    from fastmcp.server.dependencies import get_access_token
    from google.oauth2.credentials import Credentials

    token_obj = get_access_token()
    if token_obj and token_obj.token:
        return Credentials(token=token_obj.token)

    import google.auth
    from ads_mcp.utils import prevent_stdio_inheritance

    scopes = analytics_scopes() or [SCOPE_READONLY]
    with prevent_stdio_inheritance():
        credentials, _ = google.auth.default(scopes=scopes)
    return credentials


def admin_client():
    """Analytics Admin API client (v1alpha, a superset of v1beta)."""
    from google.analytics.admin_v1alpha import AnalyticsAdminServiceClient

    return AnalyticsAdminServiceClient(credentials=_credentials())


def data_client():
    """Analytics Data API client (v1beta)."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    return BetaAnalyticsDataClient(credentials=_credentials())


def data_alpha_client():
    """Analytics Data API client (v1alpha, funnels and quota snapshots)."""
    from google.analytics.data_v1alpha import AlphaAnalyticsDataClient

    return AlphaAnalyticsDataClient(credentials=_credentials())


# --------------------------------------------------------------------------
# IDs
# --------------------------------------------------------------------------


def clean_numeric_id(value: Any, label: str) -> str:
    """Accepts '123', 'properties/123', 'accounts/123' or 'p123'."""
    text = str(value or "").strip()
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    digits = re.sub(r"\D", "", text)
    if not digits:
        raise ToolError(
            f"Error: '{label}' must contain a numeric ID, got '{value}'."
        )
    return digits


def property_name(property_id: Any) -> str:
    return f"properties/{clean_numeric_id(property_id, 'property_id')}"


def account_name(account_id: Any) -> str:
    return f"accounts/{clean_numeric_id(account_id, 'account_id')}"


def data_stream_name(property_id: Any, data_stream_id: Any) -> str:
    return (
        f"{property_name(property_id)}/dataStreams/"
        f"{clean_numeric_id(data_stream_id, 'data_stream_id')}"
    )


# --------------------------------------------------------------------------
# Output formatting
# --------------------------------------------------------------------------


def message_to_dict(message: Any, include_defaults: bool = False) -> Any:
    """Converts proto-plus / protobuf messages into plain JSON-able dicts.

    include_defaults=True also prints fields at their default value (False,
    0, ''), which matters for settings objects where 'off' is the default.
    """
    import proto
    from google.protobuf.json_format import MessageToDict
    from google.protobuf.message import Message as PbMessage

    if message is None:
        return None
    if isinstance(message, proto.Message):
        return type(message).to_dict(
            message,
            use_integers_for_enums=False,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=include_defaults,
        )
    if isinstance(message, PbMessage):
        return MessageToDict(message, preserving_proto_field_name=True)
    if isinstance(message, (list, tuple)):
        return [message_to_dict(m, include_defaults) for m in message]
    return message


def to_json(payload: Any, *, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Serializes a tool result, trimming lists when the output is too big."""
    text = json.dumps(payload, indent=1, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    # Trim the longest list fields until the result fits.
    if isinstance(payload, dict):
        trimmed = dict(payload)
        for _ in range(20):
            list_keys = [
                k
                for k, v in trimmed.items()
                if isinstance(v, list) and len(v) > 1
            ]
            if not list_keys:
                break
            key = max(list_keys, key=lambda k: len(json.dumps(trimmed[k])))
            original = trimmed[key]
            trimmed[key] = original[: max(1, len(original) // 2)]
            trimmed.setdefault("truncated", {})[key] = (
                f"showing {len(trimmed[key])} of {len(payload[key])} items; "
                "narrow the request (filters, fewer dimensions, limit) "
                "to see the rest"
            )
            text = json.dumps(
                trimmed, indent=1, ensure_ascii=False, default=str
            )
            if len(text) <= limit:
                return text
    return text[:limit] + "\n... [output truncated]"


def format_api_error(exc: Exception) -> str:
    """Readable message for Google API errors (Admin and Data API)."""
    from google.api_core import exceptions as gexc

    if isinstance(exc, gexc.PermissionDenied):
        return (
            f"Permission denied by Google Analytics: {exc.message}. Check that "
            "the signed-in user has access to this property (Viewer for "
            "reading, Editor for changes, Administrator for user management) "
            "and that the Analytics Admin and Data APIs are enabled in the "
            "server's Google Cloud project."
        )
    if isinstance(exc, gexc.Unauthenticated):
        return (
            "Google Analytics rejected the login. Reconnect the MCP server so "
            "the Analytics scopes are granted."
        )
    if isinstance(exc, gexc.GoogleAPICallError):
        return f"Google Analytics API error ({exc.code}): {exc.message}"
    return f"{type(exc).__name__}: {exc}"


def call_api(fn, *args, **kwargs):
    """Runs an API call and turns Google errors into ToolErrors."""
    try:
        return fn(*args, **kwargs)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced to the agent as text
        raise ToolError(format_api_error(exc)) from exc


def parse_json_arg(value: Any, label: str) -> Any:
    """Accepts a dict/list or a JSON string for structured arguments."""
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"Error: '{label}' must be valid JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})."
            ) from exc
    raise ToolError(f"Error: '{label}' must be a JSON object or array.")
