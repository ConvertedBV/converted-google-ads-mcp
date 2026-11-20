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

from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.util import get_nested_attr
from mcp.types import ToolAnnotations

from ads_mcp import propose_and_confirm as pac
from ads_mcp.ad_builders import KeywordMatchType
from ads_mcp.mutate_helpers import (
    COMPOSITE_ID_RE as _COMPOSITE_ID_RE,
    DEFAULT_MAX_INCREASE_PCT,
    MAX_ENTITIES_PER_CALL,
    MAX_INCREASE_PCT_ENV_VAR,
    MAX_KEYWORDS_PER_CALL,
    SIMPLE_ID_RE as _SIMPLE_ID_RE,
    dedupe as _dedupe,
    enforce_increase_guardrail as _enforce_increase_guardrail,
    enum_value as _enum_value,
    format_money as _format_money,
    get_max_increase_pct,
    quoted_list as _quoted_list,
    require_customer_id as _require_customer_id,
    require_simple_id as _require_simple_id,
    run_mutation as _run_mutation,
    search as _search,
    to_micros as _to_micros,
    with_workflow_doc as _with_workflow_doc,
)
from ads_mcp.safety import enforce_budget_ceiling
import ads_mcp.utils as utils

campaign_management_mcp = FastMCP("campaign_management")


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


def _normalize_entity_id(raw_id: str | int, config: _EntityConfig) -> str:
    cleaned = str(raw_id).strip()
    pattern = _COMPOSITE_ID_RE if config.composite_id else _SIMPLE_ID_RE
    if not pattern.match(cleaned):
        raise ToolError(
            f"Error: Invalid ID '{raw_id}'. Expected format "
            f"{config.id_hint}."
        )
    return cleaned


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
    to re-enable them. Permanent removal is a separate tool,
    remove_entities, for paused entities only.
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
    enforce_budget_ceiling(new_micros)

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
    request_policy_exemption: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds (positive) keywords to an ad group.

    All keywords in one call share the same match type, status and optional
    max CPC bid. The dry run surfaces policy and duplicate errors before
    anything is created. When Google flags keywords for an exemptible policy
    (common for health, finance or legal terms), the proposal names them;
    after the user agrees, call again with `request_policy_exemption=True`
    to ask Google for an exemption (Google still reviews it).
    Non-exemptible violations are always refused. BROAD match is refused
    for campaigns on manual CPC, where it wastes budget without smart bidding.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        ad_group_id: The numeric ad group ID.
        keywords: Keyword texts without match-type symbols (max 500).
        match_type: 'EXACT', 'PHRASE' or 'BROAD'.
        cpc_bid: Optional max CPC bid in the account currency.
        status: 'ENABLED' (default) or 'PAUSED'.
        request_policy_exemption: Request a policy exemption for keywords
            that hit an exemptible policy.
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
    if match_value == "BROAD":
        rows = _search(
            ga_client,
            cid,
            "SELECT campaign.bidding_strategy_type FROM ad_group "
            f"WHERE ad_group.id = {ad_group}",
        )
        if rows and rows[0].campaign.bidding_strategy_type.name == "MANUAL_CPC":
            raise ToolError(
                "Error: BROAD match on a manual CPC campaign matches far too "
                "widely without smart bidding. Use PHRASE or EXACT, or switch "
                "the campaign to a conversion-based strategy first."
            )
    match_enum = ga_client.enums.KeywordMatchTypeEnum
    status_enum = ga_client.enums.AdGroupCriterionStatusEnum

    request = ga_client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = cid
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
        request.operations.append(op)
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
    if request_policy_exemption:
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Policy exemption",
                ad_group_rn,
                "Requested",
                "Yes, for keywords with an exemptible policy violation",
            )
        )
    service = utils.get_googleads_service_from_client(
        ga_client, "AdGroupCriterionService"
    )

    def _send(validate: bool) -> Any:
        request.validate_only = validate
        return service.mutate_ad_group_criteria(request=request)

    def _validate() -> None:
        try:
            _send(True)
        except GoogleAdsException as exc:
            exemptible, blocking = policy_violations(exc)
            if not exemptible or blocking:
                raise
            if not request_policy_exemption:
                flagged = "; ".join(
                    f"'{texts[i]}' ({', '.join(k.policy_name for k in keys)})"
                    for i, keys in sorted(exemptible.items())
                )
                raise ToolError(
                    "Error: These keywords hit an exemptible Google Ads "
                    f"policy: {flagged}. If they are legitimate for this "
                    "advertiser, ask the user and call again with "
                    "request_policy_exemption=True; Google reviews the "
                    "exemption."
                ) from exc
            for index, keys in exemptible.items():
                request.operations[index].exempt_policy_violation_keys.extend(
                    keys
                )
            _send(True)

    def _commit() -> str:
        _validate()
        response = _send(False)
        ids = [utils.extract_id(r.resource_name) for r in response.results]
        return (
            f"Successfully added {len(texts)} keywords to ad group "
            f"{ad_group}. Affected IDs: {', '.join(ids)}"
        )

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "add_keywords",
            "ad_group_id": ad_group,
            "keywords": texts,
            "match_type": match_value,
            "status": status_value,
            "cpc_bid_micros": bid_micros,
            "request_policy_exemption": request_policy_exemption,
        },
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to add keywords",
    )


def policy_violations(exc: GoogleAdsException) -> tuple[dict[int, list], bool]:
    """Splits policy violations by operation index.

    Returns ({operation index: [PolicyViolationKey]} for exemptible
    violations, True if any error is not an exemptible policy violation).
    """
    exemptible: dict[int, list] = {}
    blocking = False
    for error in exc.failure.errors:
        details = error.details.policy_violation_details
        index = next(
            (
                element.index
                for element in error.location.field_path_elements
                if element.field_name == "operations"
            ),
            None,
        )
        if (
            details.is_exemptible
            and details.key.policy_name
            and index is not None
        ):
            exemptible.setdefault(index, []).append(details.key)
        else:
            blocking = True
    return exemptible, blocking


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
