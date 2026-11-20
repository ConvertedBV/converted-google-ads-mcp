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

"""Shared helpers for the Converted mutate tools.

Validation of IDs and amounts, GAQL lookups, the increase guardrail and the
glue between Google Ads mutate requests and the propose-and-confirm workflow.
"""

from collections.abc import Callable
from enum import Enum
import inspect
import os
import re
import textwrap
from typing import Any

from fastmcp.exceptions import ToolError

from ads_mcp import propose_and_confirm as pac
import ads_mcp.utils as utils

# Upper bounds per call keep proposals reviewable by a human.
MAX_ENTITIES_PER_CALL = 200
MAX_KEYWORDS_PER_CALL = 500

# Guardrail against fat-finger increases (e.g. 5000 instead of 50). A single
# call may raise a budget, bid or target CPA by at most this percentage. Set
# the variable to "off" to disable the guardrail.
MAX_INCREASE_PCT_ENV_VAR = "GOOGLE_ADS_MCP_MAX_INCREASE_PCT"
DEFAULT_MAX_INCREASE_PCT = 100.0

SIMPLE_ID_RE = re.compile(r"^\d+$")
COMPOSITE_ID_RE = re.compile(r"^\d+~\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def enum_value(value: Any, enum_cls: type[Enum], arg_name: str) -> str:
    """Normalizes an Enum member or string to an upper-case member value."""
    raw = value.value if isinstance(value, Enum) else str(value).upper()
    valid = [member.value for member in enum_cls]
    if raw not in valid:
        raise ToolError(
            f"Error: Invalid {arg_name} '{value}'. Must be one of: "
            f"{', '.join(valid)}."
        )
    return raw


def require_customer_id(customer_id: str | int) -> str:
    cleaned = utils.clean_customer_id(customer_id)
    if not cleaned:
        raise ToolError("Error: 'customer_id' must contain digits.")
    return cleaned


def require_simple_id(value: str | int | None, arg_name: str) -> str:
    cleaned = str(value).strip() if value is not None else ""
    if not SIMPLE_ID_RE.match(cleaned):
        raise ToolError(
            f"Error: '{arg_name}' must be a numeric ID (e.g. '123456')."
        )
    return cleaned


def require_composite_id(value: str | int | None, arg_name: str) -> str:
    cleaned = str(value).strip() if value is not None else ""
    if not COMPOSITE_ID_RE.match(cleaned):
        raise ToolError(
            f"Error: '{arg_name}' must be a composite ID like '456~789'."
        )
    return cleaned


def require_ids(
    values: list[str] | None,
    arg_name: str,
    *,
    composite: bool = False,
    max_items: int = MAX_ENTITIES_PER_CALL,
) -> list[str]:
    """Validates, deduplicates and bounds a list of IDs."""
    check = require_composite_id if composite else require_simple_id
    ids = dedupe([check(v, arg_name) for v in values or []])
    if not ids:
        raise ToolError(f"Error: '{arg_name}' must contain at least one ID.")
    if len(ids) > max_items:
        raise ToolError(
            f"Error: At most {max_items} items in '{arg_name}' per call; got "
            f"{len(ids)}."
        )
    return ids


def require_url(value: str | None, arg_name: str) -> str:
    cleaned = (value or "").strip()
    if not _URL_RE.match(cleaned):
        raise ToolError(
            f"Error: '{arg_name}' must be a full http(s) URL, got '{value}'."
        )
    return cleaned


def require_date(value: str | None, arg_name: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    cleaned = str(value).strip()
    if not _DATE_RE.match(cleaned):
        raise ToolError(
            f"Error: '{arg_name}' must be a date in YYYY-MM-DD format."
        )
    return cleaned


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def clean_texts(
    values: list[str] | None, arg_name: str, max_items: int
) -> list[str]:
    """Normalizes whitespace, drops empties and duplicates, bounds the list."""
    cleaned = dedupe(
        [" ".join(v.split()) for v in (values or []) if v and v.strip()]
    )
    if not cleaned:
        raise ToolError(f"Error: '{arg_name}' must contain at least one item.")
    if len(cleaned) > max_items:
        raise ToolError(
            f"Error: At most {max_items} items in '{arg_name}' per call; got "
            f"{len(cleaned)}."
        )
    return cleaned


def to_micros(amount: float, arg_name: str) -> int:
    """Converts a currency amount to micros, rounded to whole cents."""
    try:
        numeric = float(amount)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Error: '{arg_name}' must be a number.") from exc
    if numeric <= 0:
        raise ToolError(f"Error: '{arg_name}' must be greater than 0.")
    return int(round(numeric * 100)) * 10_000


def format_money(micros: int, currency: str = "") -> str:
    formatted = f"{micros / 1_000_000:.2f}"
    return f"{formatted} {currency}".strip()


def get_max_increase_pct() -> float | None:
    """Returns the configured max increase percentage, or None if disabled."""
    raw = os.environ.get(MAX_INCREASE_PCT_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_INCREASE_PCT
    if raw.strip().lower() in ("off", "none", "false", "disabled"):
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ToolError(
            f"Error: {MAX_INCREASE_PCT_ENV_VAR} must be a number or 'off', "
            f"got '{raw}'."
        ) from exc
    return value if value > 0 else None


def enforce_increase_guardrail(
    current_value: float, new_value: float, what: str
) -> None:
    """Rejects increases above the configured percentage per change."""
    max_pct = get_max_increase_pct()
    if max_pct is None or current_value <= 0:
        return
    limit = current_value * (1 + max_pct / 100)
    if new_value > limit:
        increase_pct = (new_value / current_value - 1) * 100
        raise ToolError(
            f"Error: The new {what} is {increase_pct:.0f}% higher than the "
            f"current value, which exceeds the {max_pct:.0f}% safety limit "
            f"per change. Confirm the amount with the user and apply the "
            f"increase in smaller steps, or ask the server administrator to "
            f"adjust {MAX_INCREASE_PCT_ENV_VAR}."
        )


def enforce_decrease_guardrail(
    current_value: float, new_value: float, what: str
) -> None:
    """Rejects decreases that loosen spending by more than the limit.

    For efficiency targets such as target ROAS, a lower value lets the
    campaign bid more aggressively, so the guardrail applies to decreases:
    the implied spend headroom (current / new) may grow by at most the
    configured percentage per change.
    """
    max_pct = get_max_increase_pct()
    if max_pct is None or current_value <= 0 or new_value <= 0:
        return
    if current_value / new_value > 1 + max_pct / 100:
        decrease_pct = (1 - new_value / current_value) * 100
        raise ToolError(
            f"Error: Lowering the {what} by {decrease_pct:.0f}% loosens "
            f"bidding beyond the {max_pct:.0f}% safety limit per change. "
            "Confirm with the user and lower it in smaller steps, or ask the "
            f"server administrator to adjust {MAX_INCREASE_PCT_ENV_VAR}."
        )


def search(ga_client: Any, customer_id: str, query: str) -> list[Any]:
    ga_service = utils.get_googleads_service_from_client(
        ga_client, "GoogleAdsService"
    )
    return list(ga_service.search(customer_id=customer_id, query=query))


def quoted_list(values: list[str]) -> str:
    # Callers only pass validated resource names (digits, '/', '~' and fixed
    # path segments), so quoting cannot be broken out of.
    return ", ".join(f"'{value}'" for value in values)


def run_mutation(
    *,
    ga_client: Any,
    customer_id: str,
    service_name: str,
    request_type: str,
    mutate_method: str,
    operations: list[Any],
    validate_only: bool,
    checksum: str | None,
    operation_payload: dict[str, Any],
    change_items: list[pac.ChangeItem],
    success_message: str,
    error_prefix: str,
) -> str:
    """Builds a single-service mutate request and runs propose-and-confirm."""
    service = utils.get_googleads_service_from_client(ga_client, service_name)
    request = ga_client.get_type(request_type)
    request.customer_id = customer_id
    request.operations.extend(operations)
    request.validate_only = validate_only
    mutate = getattr(service, mutate_method)

    def _validate() -> None:
        request.validate_only = True
        mutate(request=request)

    def _commit() -> str:
        request.validate_only = False
        response = mutate(request=request)
        ids = [utils.extract_id(r.resource_name) for r in response.results]
        return f"{success_message} Affected IDs: {', '.join(ids)}"

    return pac.propose_and_confirm(
        customer_id=customer_id,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix=error_prefix,
    )


def run_google_ads_mutation(
    *,
    ga_client: Any,
    customer_id: str,
    mutate_operations: list[Any],
    validate_only: bool,
    checksum: str | None,
    operation_payload: dict[str, Any],
    change_items: list[pac.ChangeItem],
    success_message: str,
    error_prefix: str,
) -> str:
    """Runs a multi-resource GoogleAdsService.Mutate atomically.

    All operations succeed or fail together, and temporary (negative) resource
    IDs can link new resources created in the same request.
    """
    service = utils.get_googleads_service_from_client(
        ga_client, "GoogleAdsService"
    )
    request = ga_client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(mutate_operations)
    request.validate_only = validate_only

    def _validate() -> None:
        request.validate_only = True
        service.mutate(request=request)

    def _commit() -> str:
        request.validate_only = False
        response = service.mutate(request=request)
        created = summarize_mutate_response(response)
        lines = [success_message]
        for kind, ids in created.items():
            lines.append(f"- {kind}: {', '.join(ids)}")
        return "\n".join(lines)

    return pac.propose_and_confirm(
        customer_id=customer_id,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix=error_prefix,
    )


def summarize_mutate_response(response: Any) -> dict[str, list[str]]:
    """Groups the resource IDs of a MutateGoogleAdsResponse by result type."""
    created: dict[str, list[str]] = {}
    for op_response in response.mutate_operation_responses:
        result_field = type(op_response).pb(op_response).WhichOneof("response")
        if not result_field:
            continue
        resource_name = getattr(op_response, result_field).resource_name
        kind = result_field.removesuffix("_result")
        created.setdefault(kind, []).append(resource_name.rsplit("/", 1)[-1])
    return created


WORKFLOW_DOC = """
    Propose & Confirm Workflow (`validate_only` and `checksum`):
    1. Call with `validate_only=True` (the default). The request is dry-run
       against the Google Ads API and the tool returns `change_items` (with
       current values where available) and a `checksum`. Nothing is changed.
    2. Present the `change_items` to the user and wait for explicit approval.
    3. Only after the user confirms, call again with `validate_only=False`,
       the exact same parameters and the returned `checksum`.
"""


def with_workflow_doc(fn: Callable[..., str]) -> Callable[..., str]:
    """Fills the shared workflow text into a tool docstring.

    Must be applied below ``@tool`` so the description FastMCP registers
    already contains the workflow instructions.
    """
    # Python 3.13+ already strips docstring indentation at compile time, so
    # normalize both texts to get the same description on every version.
    fn.__doc__ = inspect.cleandoc(fn.__doc__ or "").format(
        workflow=textwrap.dedent(WORKFLOW_DOC)
    )
    return fn
