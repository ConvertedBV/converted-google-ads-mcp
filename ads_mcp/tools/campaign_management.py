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

"""Day-to-day campaign management tools (status, budgets, bids, keywords).

Every tool in this module mutates the account and therefore follows the
two-phase propose-and-confirm workflow from ``ads_mcp.propose_and_confirm``:
the first call (``validate_only=True``) runs a server-side dry run and returns
human-readable ``change_items`` (with current values where they exist) plus a
``checksum``; only a second call with ``validate_only=False`` and that exact
checksum commits the change.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import os
import re
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.util import get_nested_attr
from mcp.types import ToolAnnotations

from ads_mcp import propose_and_confirm as pac
import ads_mcp.utils as utils

campaign_management_mcp = FastMCP("campaign_management")

# Upper bound on entities per call keeps proposals reviewable by a human.
MAX_ENTITIES_PER_CALL = 200
MAX_KEYWORDS_PER_CALL = 500

# Guardrail against fat-finger increases (e.g. 5000 instead of 50). A single
# call may raise a budget or bid by at most this percentage. Set the variable
# to "off" to disable the guardrail.
MAX_INCREASE_PCT_ENV_VAR = "GOOGLE_ADS_MCP_MAX_INCREASE_PCT"
DEFAULT_MAX_INCREASE_PCT = 100.0

_SIMPLE_ID_RE = re.compile(r"^\d+$")
_COMPOSITE_ID_RE = re.compile(r"^\d+~\d+$")


class EntityType(str, Enum):
    """Entities whose serving status can be changed."""

    CAMPAIGN = "CAMPAIGN"
    AD_GROUP = "AD_GROUP"
    AD = "AD"
    KEYWORD = "KEYWORD"


class EntityStatus(str, Enum):
    """Serving statuses that can be set. REMOVED is deliberately excluded."""

    ENABLED = "ENABLED"
    PAUSED = "PAUSED"


class BidEntityType(str, Enum):
    """Entities that carry a manual CPC bid."""

    AD_GROUP = "AD_GROUP"
    KEYWORD = "KEYWORD"


class KeywordMatchType(str, Enum):
    """Keyword match types."""

    EXACT = "EXACT"
    PHRASE = "PHRASE"
    BROAD = "BROAD"


class NegativeKeywordLevel(str, Enum):
    """Levels at which negative keywords can be managed."""

    CAMPAIGN = "CAMPAIGN"
    AD_GROUP = "AD_GROUP"


class NegativeKeywordOperation(str, Enum):
    """Supported operations for negative keywords."""

    CREATE = "CREATE"
    REMOVE = "REMOVE"


@dataclass(frozen=True)
class _EntityConfig:
    service: str
    operation: str
    request: str
    mutate_method: str
    resource_prefix: str
    status_enum: str
    gaql_resource: str
    name_field: str
    composite_id: bool
    id_hint: str


_ENTITY_CONFIGS: dict[str, _EntityConfig] = {
    EntityType.CAMPAIGN.value: _EntityConfig(
        service="CampaignService",
        operation="CampaignOperation",
        request="MutateCampaignsRequest",
        mutate_method="mutate_campaigns",
        resource_prefix="campaigns",
        status_enum="CampaignStatusEnum",
        gaql_resource="campaign",
        name_field="campaign.name",
        composite_id=False,
        id_hint="'{campaign_id}', e.g. '123456'",
    ),
    EntityType.AD_GROUP.value: _EntityConfig(
        service="AdGroupService",
        operation="AdGroupOperation",
        request="MutateAdGroupsRequest",
        mutate_method="mutate_ad_groups",
        resource_prefix="adGroups",
        status_enum="AdGroupStatusEnum",
        gaql_resource="ad_group",
        name_field="ad_group.name",
        composite_id=False,
        id_hint="'{ad_group_id}', e.g. '123456'",
    ),
    EntityType.AD.value: _EntityConfig(
        service="AdGroupAdService",
        operation="AdGroupAdOperation",
        request="MutateAdGroupAdsRequest",
        mutate_method="mutate_ad_group_ads",
        resource_prefix="adGroupAds",
        status_enum="AdGroupAdStatusEnum",
        gaql_resource="ad_group_ad",
        name_field="ad_group.name",
        composite_id=True,
        id_hint="'{ad_group_id}~{ad_id}', e.g. '456~789'",
    ),
    EntityType.KEYWORD.value: _EntityConfig(
        service="AdGroupCriterionService",
        operation="AdGroupCriterionOperation",
        request="MutateAdGroupCriteriaRequest",
        mutate_method="mutate_ad_group_criteria",
        resource_prefix="adGroupCriteria",
        status_enum="AdGroupCriterionStatusEnum",
        gaql_resource="ad_group_criterion",
        name_field="ad_group_criterion.keyword.text",
        composite_id=True,
        id_hint="'{ad_group_id}~{criterion_id}', e.g. '456~789'",
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enum_value(value: Any, enum_cls: type[Enum], arg_name: str) -> str:
    """Normalizes an Enum member or string to an upper-case member value."""
    raw = value.value if isinstance(value, Enum) else str(value).upper()
    valid = [member.value for member in enum_cls]
    if raw not in valid:
        raise ToolError(
            f"Error: Invalid {arg_name} '{value}'. Must be one of: "
            f"{', '.join(valid)}."
        )
    return raw


def _require_customer_id(customer_id: str | int) -> str:
    cleaned = utils.clean_customer_id(customer_id)
    if not cleaned:
        raise ToolError("Error: 'customer_id' must contain digits.")
    return cleaned


def _require_simple_id(value: str | int | None, arg_name: str) -> str:
    cleaned = str(value).strip() if value is not None else ""
    if not _SIMPLE_ID_RE.match(cleaned):
        raise ToolError(
            f"Error: '{arg_name}' must be a numeric ID (e.g. '123456')."
        )
    return cleaned


def _normalize_entity_id(raw_id: str | int, config: _EntityConfig) -> str:
    cleaned = str(raw_id).strip()
    pattern = _COMPOSITE_ID_RE if config.composite_id else _SIMPLE_ID_RE
    if not pattern.match(cleaned):
        raise ToolError(
            f"Error: Invalid ID '{raw_id}'. Expected format "
            f"{config.id_hint}."
        )
    return cleaned


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _to_micros(amount: float, arg_name: str) -> int:
    """Converts a currency amount to micros, rounded to whole cents."""
    try:
        numeric = float(amount)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Error: '{arg_name}' must be a number.") from exc
    if numeric <= 0:
        raise ToolError(f"Error: '{arg_name}' must be greater than 0.")
    return int(round(numeric * 100)) * 10_000


def _format_money(micros: int, currency: str = "") -> str:
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


def _enforce_increase_guardrail(
    current_micros: int, new_micros: int, what: str
) -> None:
    max_pct = get_max_increase_pct()
    if max_pct is None or current_micros <= 0:
        return
    limit = current_micros * (1 + max_pct / 100)
    if new_micros > limit:
        increase_pct = (new_micros / current_micros - 1) * 100
        raise ToolError(
            f"Error: The new {what} is {increase_pct:.0f}% higher than the "
            f"current value, which exceeds the {max_pct:.0f}% safety limit "
            f"per change. Confirm the amount with the user and apply the "
            f"increase in smaller steps, or ask the server administrator to "
            f"adjust {MAX_INCREASE_PCT_ENV_VAR}."
        )


def _search(ga_client: Any, customer_id: str, query: str) -> list[Any]:
    ga_service = utils.get_googleads_service_from_client(
        ga_client, "GoogleAdsService"
    )
    return list(ga_service.search(customer_id=customer_id, query=query))


def _quoted_list(values: list[str]) -> str:
    # Values are validated resource names (digits, '/', '~' and fixed path
    # segments only), so quoting cannot be broken out of.
    return ", ".join(f"'{value}'" for value in values)


def _run_mutation(
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
    """Builds a mutate request and runs it through propose-and-confirm."""
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


_WORKFLOW_DOC = """
    Propose & Confirm Workflow (`validate_only` and `checksum`):
    1. Call with `validate_only=True` (the default). The request is dry-run
       against the Google Ads API and the tool returns `change_items` (with
       current values where available) and a `checksum`. Nothing is changed.
    2. Present the `change_items` to the user and wait for explicit approval.
    3. Only after the user confirms, call again with `validate_only=False`,
       the exact same parameters and the returned `checksum`.
"""


def _with_workflow_doc(fn: Callable[..., str]) -> Callable[..., str]:
    """Fills the shared workflow text into a tool docstring.

    Must be applied below ``@tool`` so the description FastMCP registers
    already contains the workflow instructions.
    """
    fn.__doc__ = (fn.__doc__ or "").format(workflow=_WORKFLOW_DOC)
    return fn


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@_with_workflow_doc
def update_entity_status(
    customer_id: str | int,
    entity_type: EntityType,
    entity_ids: list[str],
    status: EntityStatus,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Pauses or enables campaigns, ad groups, ads or keywords.

    Use this to pause underperforming campaigns, ad groups, ads or keywords, or
    to re-enable them. Removing entities is intentionally not supported.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        entity_type: 'CAMPAIGN', 'AD_GROUP', 'AD' or 'KEYWORD'.
        entity_ids: IDs of the entities to update (max 200 per call). Use
            '{{campaign_id}}' for CAMPAIGN, '{{ad_group_id}}' for AD_GROUP,
            '{{ad_group_id}}~{{ad_id}}' for AD and
            '{{ad_group_id}}~{{criterion_id}}' for KEYWORD.
        status: The new status: 'ENABLED' or 'PAUSED'.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = _require_customer_id(customer_id)
    type_value = _enum_value(entity_type, EntityType, "entity_type")
    status_value = _enum_value(status, EntityStatus, "status")
    config = _ENTITY_CONFIGS[type_value]

    if not entity_ids:
        raise ToolError("Error: 'entity_ids' must contain at least one ID.")
    ids = _dedupe([_normalize_entity_id(i, config) for i in entity_ids])
    if len(ids) > MAX_ENTITIES_PER_CALL:
        raise ToolError(
            f"Error: At most {MAX_ENTITIES_PER_CALL} entities can be updated "
            f"per call; got {len(ids)}."
        )

    resource_names = [
        f"customers/{cid}/{config.resource_prefix}/{entity_id}"
        for entity_id in ids
    ]

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)

    rows = _search(
        ga_client,
        cid,
        f"SELECT {config.gaql_resource}.resource_name, "
        f"{config.gaql_resource}.status, {config.name_field} "
        f"FROM {config.gaql_resource} "
        f"WHERE {config.gaql_resource}.resource_name IN "
        f"({_quoted_list(resource_names)})",
    )
    current: dict[str, tuple[str, str]] = {}
    for row in rows:
        rn = get_nested_attr(row, f"{config.gaql_resource}.resource_name")
        current[rn] = (
            str(get_nested_attr(row, config.name_field) or ""),
            get_nested_attr(row, f"{config.gaql_resource}.status").name,
        )
    missing = [rn for rn in resource_names if rn not in current]
    if missing:
        raise ToolError(
            "Error: The following entities were not found in customer "
            f"{cid}: {', '.join(missing)}"
        )

    status_enum = getattr(ga_client.enums, config.status_enum)
    operations = []
    change_items: list[pac.ChangeItem] = []
    for rn in resource_names:
        op = ga_client.get_type(config.operation)
        op.update.resource_name = rn
        op.update.status = getattr(status_enum, status_value)
        op.update_mask.paths.append("status")
        operations.append(op)

        name, current_status = current[rn]
        change_items.extend(
            pac.build_change_items(
                operation="UPDATE",
                entity_readable=type_value.replace("_", " ").title(),
                primary_key_readable=f"{name} ({rn})" if name else rn,
                fields={"Status": status_value},
                original_fields={"Status": current_status},
            )
        )

    return _run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name=config.service,
        request_type=config.request,
        mutate_method=config.mutate_method,
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_entity_status",
            "entity_type": type_value,
            "entity_ids": ids,
            "status": status_value,
        },
        change_items=change_items,
        success_message=(
            f"Successfully set {len(ids)} {type_value} entities to "
            f"{status_value}."
        ),
        error_prefix=f"Failed to update {type_value} status",
    )


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@_with_workflow_doc
def update_campaign_budget(
    customer_id: str | int,
    campaign_id: str,
    daily_budget: float,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Sets the average daily budget of the budget attached to a campaign.

    The amount is in the account currency (e.g. 45.50 for EUR 45.50 per day).
    If the budget is shared, the change applies to every campaign using it;
    the proposal says so. Increases above the server's safety limit (default
    +100% per change) are rejected and must be done in smaller steps.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_id: The numeric campaign ID.
        daily_budget: New average daily budget in the account currency.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = _require_customer_id(customer_id)
    campaign = _require_simple_id(campaign_id, "campaign_id")
    new_micros = _to_micros(daily_budget, "daily_budget")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = _search(
        ga_client,
        cid,
        "SELECT campaign.name, campaign_budget.resource_name, "
        "campaign_budget.amount_micros, campaign_budget.explicitly_shared, "
        "campaign_budget.reference_count, customer.currency_code "
        f"FROM campaign WHERE campaign.id = {campaign}",
    )
    if not rows:
        raise ToolError(
            f"Error: Campaign {campaign} was not found in customer {cid}."
        )
    row = rows[0]
    budget_rn = row.campaign_budget.resource_name
    current_micros = int(row.campaign_budget.amount_micros or 0)
    currency = row.customer.currency_code
    if not budget_rn or current_micros <= 0:
        raise ToolError(
            f"Error: Campaign {campaign} has no daily budget that can be "
            "changed (it may use a total/campaign-total budget)."
        )

    _enforce_increase_guardrail(current_micros, new_micros, "daily budget")

    op = ga_client.get_type("CampaignBudgetOperation")
    op.update.resource_name = budget_rn
    op.update.amount_micros = new_micros
    op.update_mask.paths.append("amount_micros")

    fields: dict[str, Any] = {
        "Daily budget": _format_money(new_micros, currency)
    }
    original: dict[str, Any] = {
        "Daily budget": _format_money(current_micros, currency)
    }
    if row.campaign_budget.explicitly_shared:
        fields["Shared budget"] = (
            "Yes: applies to all "
            f"{row.campaign_budget.reference_count} campaigns using it"
        )

    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Campaign Budget",
        primary_key_readable=f"{row.campaign.name} ({budget_rn})",
        fields=fields,
        original_fields=original,
    )

    return _run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="CampaignBudgetService",
        request_type="MutateCampaignBudgetsRequest",
        mutate_method="mutate_campaign_budgets",
        operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_campaign_budget",
            "campaign_id": campaign,
            "budget_resource_name": budget_rn,
            "amount_micros": new_micros,
        },
        change_items=change_items,
        success_message=(
            f"Successfully set the daily budget of campaign {campaign} to "
            f"{_format_money(new_micros, currency)}."
        ),
        error_prefix="Failed to update campaign budget",
    )


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@_with_workflow_doc
def update_cpc_bid(
    customer_id: str | int,
    entity_type: BidEntityType,
    entity_id: str,
    cpc_bid: float,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Sets the manual max CPC bid of an ad group or keyword.

    Only meaningful for campaigns using manual (or enhanced) CPC bidding; the
    dry run reports an error for automated bidding strategies. Increases above
    the server's safety limit (default +100% per change) are rejected.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        entity_type: 'AD_GROUP' or 'KEYWORD'.
        entity_id: '{{ad_group_id}}' for AD_GROUP or
            '{{ad_group_id}}~{{criterion_id}}' for KEYWORD.
        cpc_bid: New max CPC bid in the account currency (e.g. 1.25).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = _require_customer_id(customer_id)
    type_value = _enum_value(entity_type, BidEntityType, "entity_type")
    config = _ENTITY_CONFIGS[type_value]
    normalized_id = _normalize_entity_id(entity_id, config)
    new_micros = _to_micros(cpc_bid, "cpc_bid")
    rn = f"customers/{cid}/{config.resource_prefix}/{normalized_id}"

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = _search(
        ga_client,
        cid,
        f"SELECT {config.name_field}, {config.gaql_resource}.cpc_bid_micros, "
        f"customer.currency_code FROM {config.gaql_resource} "
        f"WHERE {config.gaql_resource}.resource_name = '{rn}'",
    )
    if not rows:
        raise ToolError(f"Error: {rn} was not found in customer {cid}.")
    row = rows[0]
    name = str(get_nested_attr(row, config.name_field) or "")
    current_micros = int(
        get_nested_attr(row, f"{config.gaql_resource}.cpc_bid_micros") or 0
    )
    currency = row.customer.currency_code

    _enforce_increase_guardrail(current_micros, new_micros, "CPC bid")

    op = ga_client.get_type(config.operation)
    op.update.resource_name = rn
    op.update.cpc_bid_micros = new_micros
    op.update_mask.paths.append("cpc_bid_micros")

    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable=type_value.replace("_", " ").title(),
        primary_key_readable=f"{name} ({rn})" if name else rn,
        fields={"Max CPC": _format_money(new_micros, currency)},
        original_fields={
            "Max CPC": (
                _format_money(current_micros, currency)
                if current_micros
                else "not set (inherits from ad group)"
            )
        },
    )

    return _run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name=config.service,
        request_type=config.request,
        mutate_method=config.mutate_method,
        operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_cpc_bid",
            "entity_type": type_value,
            "entity_id": normalized_id,
            "cpc_bid_micros": new_micros,
        },
        change_items=change_items,
        success_message=(
            f"Successfully set the max CPC of {type_value} {normalized_id} to "
            f"{_format_money(new_micros, currency)}."
        ),
        error_prefix=f"Failed to update {type_value} CPC bid",
    )


def _clean_keywords(keywords: list[str] | None) -> list[str]:
    cleaned = _dedupe(
        [" ".join(k.split()) for k in (keywords or []) if k and k.strip()]
    )
    if not cleaned:
        raise ToolError("Error: 'keywords' must contain at least one keyword.")
    if len(cleaned) > MAX_KEYWORDS_PER_CALL:
        raise ToolError(
            f"Error: At most {MAX_KEYWORDS_PER_CALL} keywords per call; got "
            f"{len(cleaned)}."
        )
    return cleaned


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@_with_workflow_doc
def add_keywords(
    customer_id: str | int,
    ad_group_id: str,
    keywords: list[str],
    match_type: KeywordMatchType,
    cpc_bid: float | None = None,
    status: EntityStatus = EntityStatus.ENABLED,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds (positive) keywords to an ad group.

    All keywords in one call share the same match type, status and optional
    max CPC bid. The dry run surfaces policy and duplicate errors before
    anything is created.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        ad_group_id: The numeric ad group ID.
        keywords: Keyword texts without match-type symbols (max 500).
        match_type: 'EXACT', 'PHRASE' or 'BROAD'.
        cpc_bid: Optional max CPC bid in the account currency.
        status: 'ENABLED' (default) or 'PAUSED'.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = _require_customer_id(customer_id)
    ad_group = _require_simple_id(ad_group_id, "ad_group_id")
    match_value = _enum_value(match_type, KeywordMatchType, "match_type")
    status_value = _enum_value(status, EntityStatus, "status")
    texts = _clean_keywords(keywords)
    bid_micros = _to_micros(cpc_bid, "cpc_bid") if cpc_bid is not None else None
    ad_group_rn = f"customers/{cid}/adGroups/{ad_group}"

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    match_enum = ga_client.enums.KeywordMatchTypeEnum
    status_enum = ga_client.enums.AdGroupCriterionStatusEnum

    operations = []
    change_items: list[pac.ChangeItem] = []
    for text in texts:
        op = ga_client.get_type("AdGroupCriterionOperation")
        criterion = op.create
        criterion.ad_group = ad_group_rn
        criterion.status = getattr(status_enum, status_value)
        criterion.keyword.text = text
        criterion.keyword.match_type = getattr(match_enum, match_value)
        if bid_micros is not None:
            criterion.cpc_bid_micros = bid_micros
        operations.append(op)
        change_items.extend(
            pac.build_change_items(
                operation="CREATE",
                entity_readable="Keyword",
                primary_key_readable=ad_group_rn,
                fields={
                    "Keyword": text,
                    "Match type": match_value,
                    "Status": status_value,
                    "Max CPC": (
                        _format_money(bid_micros)
                        if bid_micros is not None
                        else None
                    ),
                },
            )
        )

    return _run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="AdGroupCriterionService",
        request_type="MutateAdGroupCriteriaRequest",
        mutate_method="mutate_ad_group_criteria",
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "add_keywords",
            "ad_group_id": ad_group,
            "keywords": texts,
            "match_type": match_value,
            "status": status_value,
            "cpc_bid_micros": bid_micros,
        },
        change_items=change_items,
        success_message=(
            f"Successfully added {len(texts)} keywords to ad group "
            f"{ad_group}."
        ),
        error_prefix="Failed to add keywords",
    )


_NEGATIVE_CONFIGS: dict[str, dict[str, str]] = {
    NegativeKeywordLevel.CAMPAIGN.value: {
        "service": "CampaignCriterionService",
        "operation": "CampaignCriterionOperation",
        "request": "MutateCampaignCriteriaRequest",
        "mutate_method": "mutate_campaign_criteria",
        "parent_field": "campaign",
        "parent_prefix": "campaigns",
        "criterion_prefix": "campaignCriteria",
        "gaql_resource": "campaign_criterion",
    },
    NegativeKeywordLevel.AD_GROUP.value: {
        "service": "AdGroupCriterionService",
        "operation": "AdGroupCriterionOperation",
        "request": "MutateAdGroupCriteriaRequest",
        "mutate_method": "mutate_ad_group_criteria",
        "parent_field": "ad_group",
        "parent_prefix": "adGroups",
        "criterion_prefix": "adGroupCriteria",
        "gaql_resource": "ad_group_criterion",
    },
}


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@_with_workflow_doc
def manage_negative_keywords(
    customer_id: str | int,
    level: NegativeKeywordLevel,
    parent_id: str,
    operation: NegativeKeywordOperation,
    keywords: list[str] | None = None,
    match_type: KeywordMatchType = KeywordMatchType.EXACT,
    criterion_ids: list[str] | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds or removes negative keywords on a campaign or ad group.

    Use CREATE with `keywords` (e.g. irrelevant search terms found in the
    search terms report) and `match_type`. Use REMOVE with `criterion_ids`;
    REMOVE only touches criteria verified to be negative keywords, so it can
    never delete a positive keyword.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        level: 'CAMPAIGN' or 'AD_GROUP'.
        parent_id: The numeric campaign ID or ad group ID.
        operation: 'CREATE' or 'REMOVE'.
        keywords: Negative keyword texts (CREATE only, max 500).
        match_type: 'EXACT' (default), 'PHRASE' or 'BROAD' (CREATE only).
        criterion_ids: Numeric criterion IDs to remove (REMOVE only).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = _require_customer_id(customer_id)
    level_value = _enum_value(level, NegativeKeywordLevel, "level")
    op_value = _enum_value(operation, NegativeKeywordOperation, "operation")
    parent = _require_simple_id(parent_id, "parent_id")
    config = _NEGATIVE_CONFIGS[level_value]
    parent_rn = f"customers/{cid}/{config['parent_prefix']}/{parent}"

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    operations = []
    change_items: list[pac.ChangeItem] = []

    if op_value == "CREATE":
        if criterion_ids:
            raise ToolError("Error: 'criterion_ids' is only used for REMOVE.")
        match_value = _enum_value(match_type, KeywordMatchType, "match_type")
        texts = _clean_keywords(keywords)
        match_enum = ga_client.enums.KeywordMatchTypeEnum
        for text in texts:
            op = ga_client.get_type(config["operation"])
            setattr(op.create, config["parent_field"], parent_rn)
            op.create.negative = True
            op.create.keyword.text = text
            op.create.keyword.match_type = getattr(match_enum, match_value)
            operations.append(op)
            change_items.extend(
                pac.build_change_items(
                    operation="CREATE",
                    entity_readable=f"{level_value.title()} Negative Keyword",
                    primary_key_readable=parent_rn,
                    fields={"Keyword": text, "Match type": match_value},
                )
            )
        payload: dict[str, Any] = {
            "keywords": texts,
            "match_type": match_value,
        }
    else:
        if keywords:
            raise ToolError(
                "Error: Use 'criterion_ids' (not 'keywords') for REMOVE."
            )
        ids = _dedupe(
            [
                _require_simple_id(c, "criterion_ids")
                for c in criterion_ids or []
            ]
        )
        if not ids:
            raise ToolError(
                "Error: 'criterion_ids' must contain at least one ID."
            )
        if len(ids) > MAX_KEYWORDS_PER_CALL:
            raise ToolError(
                f"Error: At most {MAX_KEYWORDS_PER_CALL} criteria per call; "
                f"got {len(ids)}."
            )
        target_rns = [
            f"customers/{cid}/{config['criterion_prefix']}/{parent}~{c}"
            for c in ids
        ]
        resource = config["gaql_resource"]
        rows = _search(
            ga_client,
            cid,
            f"SELECT {resource}.resource_name, {resource}.keyword.text, "
            f"{resource}.keyword.match_type FROM {resource} "
            f"WHERE {resource}.resource_name IN ({_quoted_list(target_rns)}) "
            f"AND {resource}.negative = TRUE "
            f"AND {resource}.type = 'KEYWORD'",
        )
        found = {}
        for row in rows:
            rn = get_nested_attr(row, f"{resource}.resource_name")
            found[rn] = (
                get_nested_attr(row, f"{resource}.keyword.text"),
                get_nested_attr(row, f"{resource}.keyword.match_type").name,
            )
        missing = [rn for rn in target_rns if rn not in found]
        if missing:
            raise ToolError(
                "Error: These criteria are not negative keywords of "
                f"{parent_rn} (or do not exist): {', '.join(missing)}"
            )
        for rn in target_rns:
            op = ga_client.get_type(config["operation"])
            op.remove = rn
            operations.append(op)
            text, match_value = found[rn]
            change_items.extend(
                pac.build_change_items(
                    operation="REMOVE",
                    entity_readable=f"{level_value.title()} Negative Keyword",
                    primary_key_readable=rn,
                    fields={"Keyword": text, "Match type": match_value},
                )
            )
        payload = {"criterion_ids": ids}

    return _run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name=config["service"],
        request_type=config["request"],
        mutate_method=config["mutate_method"],
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_negative_keywords",
            "level": level_value,
            "parent_id": parent,
            "operation": op_value,
            **payload,
        },
        change_items=change_items,
        success_message=(
            f"Successfully {'added' if op_value == 'CREATE' else 'removed'} "
            f"{len(operations)} negative keywords on {parent_rn}."
        ),
        error_prefix=f"Failed to {op_value.lower()} negative keywords",
    )
