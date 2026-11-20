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

"""Deployment-level safety settings.

- GOOGLE_ADS_MCP_ACCOUNT_IDS: comma-separated customer IDs this server may
  touch (include the manager ID used as login-customer-id). Every tool call
  with another customer_id or login_customer_id is refused.
- GOOGLE_ADS_MCP_BLOCKED_TOOLS: comma-separated tool names (with or without
  namespace prefix) that are not exposed at all.
- GOOGLE_ADS_MCP_MAX_DAILY_BUDGET: absolute ceiling for any campaign's daily
  budget set through this server, in the account currency. Complements the
  per-change increase cap, which alone allows step-by-step escalation.
"""

import os
import re
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp import types as mcp_types

ACCOUNT_IDS_ENV_VAR = "GOOGLE_ADS_MCP_ACCOUNT_IDS"
BLOCKED_TOOLS_ENV_VAR = "GOOGLE_ADS_MCP_BLOCKED_TOOLS"
MAX_DAILY_BUDGET_ENV_VAR = "GOOGLE_ADS_MCP_MAX_DAILY_BUDGET"

_ACCOUNT_ARGUMENTS = ("customer_id", "login_customer_id")


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value))


def allowed_accounts() -> frozenset[str] | None:
    """Returns the allowed customer IDs, or None when unrestricted."""
    raw = os.environ.get(ACCOUNT_IDS_ENV_VAR, "")
    ids = frozenset(_digits(v) for v in raw.split(",") if _digits(v))
    return ids or None


def blocked_tools() -> frozenset[str]:
    raw = os.environ.get(BLOCKED_TOOLS_ENV_VAR, "")
    return frozenset(v.strip() for v in raw.split(",") if v.strip())


def is_blocked(tool_name: str, namespace: str | None) -> bool:
    blocked = blocked_tools()
    return tool_name in blocked or (
        namespace is not None and f"{namespace}_{tool_name}" in blocked
    )


def max_daily_budget_micros() -> int | None:
    raw = os.environ.get(MAX_DAILY_BUDGET_ENV_VAR, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ToolError(
            f"Error: {MAX_DAILY_BUDGET_ENV_VAR} must be a number, got '{raw}'."
        ) from exc
    return int(value * 1_000_000) if value > 0 else None


def enforce_budget_ceiling(budget_micros: int) -> None:
    """Refuses daily budgets above the deployment's absolute ceiling."""
    ceiling = max_daily_budget_micros()
    if ceiling is not None and budget_micros > ceiling:
        raise ToolError(
            f"Error: A daily budget of {budget_micros / 1_000_000:.2f} exceeds "
            f"this server's ceiling of {ceiling / 1_000_000:.2f} "
            f"({MAX_DAILY_BUDGET_ENV_VAR}). Change it in Google Ads directly "
            "or ask the server administrator."
        )


class AccountScopeMiddleware(Middleware):
    """Refuses tool calls for accounts outside GOOGLE_ADS_MCP_ACCOUNT_IDS."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, Any],
    ) -> Any:
        allowed = allowed_accounts()
        if allowed is not None:
            arguments = context.message.arguments or {}
            for key in _ACCOUNT_ARGUMENTS:
                value = arguments.get(key)
                if value in (None, ""):
                    continue
                if _digits(value) not in allowed:
                    raise ToolError(
                        f"Error: Account {_digits(value)} ({key}) is not "
                        "allowed on this server. Allowed accounts are set "
                        f"in {ACCOUNT_IDS_ENV_VAR}."
                    )
        return await call_next(context)
