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

"""Campaign settings tools: bidding, settings, targeting and bid modifiers.

Registered on the ``campaign_management`` namespace. All tools use the
propose-and-confirm workflow.
"""

from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import ad_builders as ab
from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_management import campaign_management_mcp
import ads_mcp.utils as utils

_CAMPAIGN_FIELDS = (
    "campaign.name, campaign.status, campaign.bidding_strategy_type, "
    "campaign.bidding_strategy, campaign.advertising_channel_type, "
    "campaign.maximize_conversions.target_cpa_micros, "
    "campaign.maximize_conversion_value.target_roas, "
    "campaign.target_cpa.target_cpa_micros, "
    "campaign.target_roas.target_roas, "
    "campaign.target_spend.cpc_bid_ceiling_micros, "
    "customer.currency_code"
)

# Maps a current (legacy or standard) strategy type to the field holding its
# target, for target-only updates that keep the strategy.
_TARGET_FIELDS = {
    "MAXIMIZE_CONVERSIONS": ("cpa", "maximize_conversions.target_cpa_micros"),
    "TARGET_CPA": ("cpa", "target_cpa.target_cpa_micros"),
    "MAXIMIZE_CONVERSION_VALUE": (
        "roas",
        "maximize_conversion_value.target_roas",
    ),
    "TARGET_ROAS": ("roas", "target_roas.target_roas"),
}


# The API reports "Maximize clicks" as TARGET_SPEND.
_FRIENDLY_STRATEGY = {"TARGET_SPEND": "MAXIMIZE_CLICKS (TARGET_SPEND)"}


def _fetch_campaign(ga_client: Any, cid: str, campaign_id: str) -> Any:
    rows = mh.search(
        ga_client,
        cid,
        f"SELECT {_CAMPAIGN_FIELDS} FROM campaign "
        f"WHERE campaign.id = {campaign_id}",
    )
    if not rows:
        raise ToolError(
            f"Error: Campaign {campaign_id} was not found in customer {cid}."
        )
    return rows[0]


def _current_target(campaign: Any, strategy_type: str) -> tuple[str, Any]:
    """Returns (kind, value) of the current target, value None when unset."""
    if strategy_type not in _TARGET_FIELDS:
        return "", None
    kind, path = _TARGET_FIELDS[strategy_type]
    obj = campaign
    for part in path.split("."):
        obj = getattr(obj, part)
    return kind, obj or None


def _set_path(message: Any, path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    for part in parents:
        message = getattr(message, part)
    setattr(message, leaf, value)


def _describe_target(kind: str, value: Any, currency: str) -> str | None:
    if value is None:
        return None
    if kind == "cpa":
        return mh.format_money(int(value), currency)
    return f"{float(value) * 100:.0f}%"


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def update_campaign_bidding(
    customer_id: str | int,
    campaign_id: str,
    bidding_strategy: ab.BiddingStrategy | None = None,
    target_cpa: float | None = None,
    target_roas_percent: float | None = None,
    max_cpc_limit: float | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Changes a campaign's bidding strategy and/or its tCPA / tROAS target.

    Two uses:
    - Target only: leave `bidding_strategy` empty and pass `target_cpa` or
      `target_roas_percent` to change the target of the current strategy.
      For a portfolio strategy this updates the portfolio itself, which
      affects every campaign using it; the proposal shows how many.
    - Switch strategy: pass `bidding_strategy` (MAXIMIZE_CONVERSIONS,
      MAXIMIZE_CONVERSION_VALUE, MAXIMIZE_CLICKS or MANUAL_CPC) with an
      optional target. Not supported for campaigns on a portfolio strategy.

    Switching strategies restarts the learning phase; raising a target CPA is
    capped by the server's safety limit (default +100% per change).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_id: The numeric campaign ID.
        bidding_strategy: Optional new strategy (see above).
        target_cpa: Target CPA in the account currency (MAXIMIZE_CONVERSIONS
            or TARGET_CPA campaigns).
        target_roas_percent: Target ROAS in percent, e.g. 400 for 400%
            (MAXIMIZE_CONVERSION_VALUE or TARGET_ROAS campaigns).
        max_cpc_limit: Max CPC limit for MAXIMIZE_CLICKS, account currency.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaign_id = mh.require_simple_id(campaign_id, "campaign_id")
    if bidding_strategy is None and not (target_cpa or target_roas_percent):
        raise ToolError(
            "Error: Pass 'bidding_strategy' and/or a target "
            "('target_cpa' or 'target_roas_percent')."
        )

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _fetch_campaign(ga_client, cid, campaign_id)
    currency = row.customer.currency_code
    current_type = row.campaign.bidding_strategy_type.name
    current_kind, current_value = _current_target(row.campaign, current_type)
    campaign_rn = f"customers/{cid}/campaigns/{campaign_id}"
    portfolio_rn = row.campaign.bidding_strategy

    fields: dict[str, Any] = {}
    original: dict[str, Any] = {}

    if bidding_strategy is not None:
        if portfolio_rn:
            raise ToolError(
                f"Error: Campaign {campaign_id} uses the portfolio strategy "
                f"{portfolio_rn}. Switching it is not supported here; change "
                "only its target or detach it in Google Ads first."
            )
        bidding = ab.resolve_bidding(
            bidding_strategy, target_cpa, target_roas_percent, max_cpc_limit
        )
        if bidding["target_cpa_micros"] and current_kind == "cpa":
            mh.enforce_increase_guardrail(
                current_value or 0, bidding["target_cpa_micros"], "target CPA"
            )
        op = ga_client.get_type("CampaignOperation")
        op.update.resource_name = campaign_rn
        op.update_mask.paths.extend(
            ab.apply_bidding(ga_client, op.update, bidding)
        )
        service, request_type, method = (
            "CampaignService",
            "MutateCampaignsRequest",
            "mutate_campaigns",
        )
        target_rn = campaign_rn
        fields["Bidding"] = ab.describe_bidding(bidding, currency)
        original["Bidding"] = _FRIENDLY_STRATEGY.get(
            current_type, current_type
        ) + (
            f", target {_describe_target(current_kind, current_value, currency)}"
            if current_value
            else ""
        )
        payload_target = bidding
    else:
        if max_cpc_limit:
            raise ToolError(
                "Error: 'max_cpc_limit' needs bidding_strategy=MAXIMIZE_CLICKS."
            )
        if target_cpa and target_roas_percent:
            raise ToolError(
                "Error: Pass either 'target_cpa' or 'target_roas_percent'."
            )
        wanted_kind = "cpa" if target_cpa else "roas"
        new_value: Any = (
            mh.to_micros(target_cpa, "target_cpa")
            if target_cpa
            else round(float(target_roas_percent) / 100, 4)
        )
        if new_value <= 0:
            raise ToolError("Error: The target must be greater than 0.")

        if portfolio_rn:
            portfolio_rows = mh.search(
                ga_client,
                cid,
                "SELECT bidding_strategy.name, bidding_strategy.type, "
                "bidding_strategy.campaign_count, "
                "bidding_strategy.maximize_conversions.target_cpa_micros, "
                "bidding_strategy.target_cpa.target_cpa_micros, "
                "bidding_strategy.maximize_conversion_value.target_roas, "
                "bidding_strategy.target_roas.target_roas "
                "FROM bidding_strategy WHERE bidding_strategy.resource_name "
                f"= '{portfolio_rn}'",
            )
            if not portfolio_rows:
                raise ToolError(
                    f"Error: Portfolio strategy {portfolio_rn} not found."
                )
            portfolio = portfolio_rows[0].bidding_strategy
            strategy_type = portfolio.type_.name
            kind, current_value = _current_target(portfolio, strategy_type)
            fields["Portfolio strategy"] = (
                f"{portfolio.name}: used by {portfolio.campaign_count} "
                "campaigns, all of which change"
            )
            message_type, service, request_type, method = (
                "BiddingStrategyOperation",
                "BiddingStrategyService",
                "MutateBiddingStrategiesRequest",
                "mutate_bidding_strategies",
            )
            target_rn = portfolio_rn
        else:
            strategy_type, kind = current_type, current_kind
            message_type, service, request_type, method = (
                "CampaignOperation",
                "CampaignService",
                "MutateCampaignsRequest",
                "mutate_campaigns",
            )
            target_rn = campaign_rn

        if kind != wanted_kind:
            raise ToolError(
                f"Error: The current strategy {strategy_type} has no "
                f"{'target CPA' if wanted_kind == 'cpa' else 'target ROAS'}. "
                "Pass 'bidding_strategy' to switch strategies."
            )
        if kind == "cpa":
            mh.enforce_increase_guardrail(
                current_value or 0, new_value, "target CPA"
            )
        path = _TARGET_FIELDS[strategy_type][1]
        op = ga_client.get_type(message_type)
        op.update.resource_name = target_rn
        _set_path(op.update, path, new_value)
        op.update_mask.paths.append(path)
        label = "Target CPA" if kind == "cpa" else "Target ROAS"
        fields[label] = _describe_target(kind, new_value, currency)
        original[label] = _describe_target(kind, current_value, currency) or (
            "not set"
        )
        payload_target = {"kind": kind, "value": new_value}

    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Campaign Bidding",
        primary_key_readable=f"{row.campaign.name} ({campaign_rn})",
        fields=fields,
        original_fields=original,
    )
    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name=service,
        request_type=request_type,
        mutate_method=method,
        operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_campaign_bidding",
            "campaign_id": campaign_id,
            "target_resource_name": target_rn,
            "change": payload_target,
        },
        change_items=change_items,
        success_message=f"Successfully updated bidding of {target_rn}.",
        error_prefix="Failed to update campaign bidding",
    )


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def update_campaign_settings(
    customer_id: str | int,
    campaign_id: str,
    name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    search_partners: bool | None = None,
    display_network: bool | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Changes a campaign's name, start/end date or network settings.

    Pass only the settings to change. Dates are YYYY-MM-DD in the account
    time zone; the campaign starts at 00:00:00 and ends at 23:59:59.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_id: The numeric campaign ID.
        name: New campaign name.
        start_date: New start date (only before the campaign has started).
        end_date: New end date.
        search_partners: Show ads on Google search partners.
        display_network: Show Search ads on the Display Network.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaign_id = mh.require_simple_id(campaign_id, "campaign_id")
    start = mh.require_date(start_date, "start_date")
    end = mh.require_date(end_date, "end_date")
    clean_name = " ".join((name or "").split()) or None
    if not any(
        v is not None
        for v in (clean_name, start, end, search_partners, display_network)
    ):
        raise ToolError("Error: Pass at least one setting to change.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign.name, campaign.start_date_time, "
        "campaign.end_date_time, "
        "campaign.network_settings.target_partner_search_network, "
        "campaign.network_settings.target_content_network "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
    )
    if not rows:
        raise ToolError(
            f"Error: Campaign {campaign_id} was not found in customer {cid}."
        )
    current = rows[0].campaign
    rn = f"customers/{cid}/campaigns/{campaign_id}"

    op = ga_client.get_type("CampaignOperation")
    op.update.resource_name = rn
    fields: dict[str, Any] = {}
    original: dict[str, Any] = {}
    network = current.network_settings
    updates = [
        ("Name", "name", clean_name, current.name),
        (
            "Start",
            "start_date_time",
            f"{start} 00:00:00" if start else None,
            current.start_date_time,
        ),
        (
            "End",
            "end_date_time",
            f"{end} 23:59:59" if end else None,
            current.end_date_time or "none",
        ),
        (
            "Search partners",
            "network_settings.target_partner_search_network",
            search_partners,
            network.target_partner_search_network,
        ),
        (
            "Display Network",
            "network_settings.target_content_network",
            display_network,
            network.target_content_network,
        ),
    ]
    for label, path, value, old in updates:
        if value is None:
            continue
        _set_path(op.update, path, value)
        op.update_mask.paths.append(path)
        fields[label] = value
        original[label] = old

    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Campaign",
        primary_key_readable=f"{current.name} ({rn})",
        fields=fields,
        original_fields=original,
    )
    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="CampaignService",
        request_type="MutateCampaignsRequest",
        mutate_method="mutate_campaigns",
        operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_campaign_settings",
            "campaign_id": campaign_id,
            "name": clean_name,
            "start_date": start,
            "end_date": end,
            "search_partners": search_partners,
            "display_network": display_network,
        },
        change_items=change_items,
        success_message=f"Successfully updated campaign {campaign_id}.",
        error_prefix="Failed to update campaign settings",
    )


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def add_campaign_targeting(
    customer_id: str | int,
    campaign_id: str,
    location_ids: list[str] | None = None,
    excluded_location_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    ad_schedules: list[ab.AdScheduleInput] | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds location, language and ad schedule targeting to a campaign.

    Location IDs are geo target constant IDs (e.g. 2056 = Belgium, 1001021
    = Ghent) and language IDs are language constant IDs (1010 = Dutch, 1002
    = French, 1000 = English). Look them up with the search tool, e.g.
    `SELECT geo_target_constant.id, geo_target_constant.canonical_name FROM
    geo_target_constant WHERE geo_target_constant.name = 'Ghent'`. Adding a
    first ad schedule limits serving to the scheduled windows only.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_id: The numeric campaign ID.
        location_ids: Geo target constant IDs to target.
        excluded_location_ids: Geo target constant IDs to exclude.
        language_ids: Language constant IDs to target.
        ad_schedules: Time windows, e.g. {{"day": "MONDAY_TO_FRIDAY",
            "start_time": "08:00", "end_time": "20:00"}}.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaign_id = mh.require_simple_id(campaign_id, "campaign_id")
    locations = (
        mh.require_ids(location_ids, "location_ids") if location_ids else []
    )
    excluded = (
        mh.require_ids(excluded_location_ids, "excluded_location_ids")
        if excluded_location_ids
        else []
    )
    languages = (
        mh.require_ids(language_ids, "language_ids") if language_ids else []
    )
    schedules = ab.normalize_ad_schedules(ad_schedules)
    if not (locations or excluded or languages or schedules):
        raise ToolError("Error: Pass at least one targeting criterion.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    geo_names = ab.lookup_constants(
        ga_client, cid, mh.dedupe(locations + excluded), "geo"
    )
    language_names = ab.lookup_constants(ga_client, cid, languages, "language")
    campaign_rn = f"customers/{cid}/campaigns/{campaign_id}"

    operations, change_items = build_targeting_operations(
        ga_client,
        campaign_rn,
        locations,
        excluded,
        languages,
        schedules,
        geo_names,
        language_names,
    )
    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="CampaignCriterionService",
        request_type="MutateCampaignCriteriaRequest",
        mutate_method="mutate_campaign_criteria",
        operations=[op.campaign_criterion_operation for op in operations],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "add_campaign_targeting",
            "campaign_id": campaign_id,
            "locations": locations,
            "excluded_locations": excluded,
            "languages": languages,
            "ad_schedules": [
                {**s, "start": list(s["start"]), "end": list(s["end"])}
                for s in schedules
            ],
        },
        change_items=change_items,
        success_message=(
            f"Successfully added {len(operations)} targeting criteria to "
            f"campaign {campaign_id}."
        ),
        error_prefix="Failed to add campaign targeting",
    )


def build_targeting_operations(
    ga_client: Any,
    campaign_rn: str,
    locations: list[str],
    excluded: list[str],
    languages: list[str],
    schedules: list[dict[str, Any]],
    geo_names: dict[str, str],
    language_names: dict[str, str],
) -> tuple[list[Any], list[pac.ChangeItem]]:
    """Builds CampaignCriterion create operations wrapped in MutateOperation.

    Shared with the campaign builder, which needs MutateOperations for its
    atomic request; single-service callers unwrap them.
    """
    operations: list[Any] = []
    change_items: list[pac.ChangeItem] = []

    def _new() -> Any:
        mutate_op = ga_client.get_type("MutateOperation")
        criterion = mutate_op.campaign_criterion_operation.create
        criterion.campaign = campaign_rn
        operations.append(mutate_op)
        return criterion

    for loc in locations:
        _new().location.geo_target_constant = f"geoTargetConstants/{loc}"
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Location target",
                campaign_rn,
                "Location",
                f"{geo_names[loc]} ({loc})",
            )
        )
    for loc in excluded:
        criterion = _new()
        criterion.negative = True
        criterion.location.geo_target_constant = f"geoTargetConstants/{loc}"
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Location exclusion",
                campaign_rn,
                "Location",
                f"{geo_names[loc]} ({loc})",
            )
        )
    for lang in languages:
        _new().language.language_constant = f"languageConstants/{lang}"
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Language target",
                campaign_rn,
                "Language",
                f"{language_names[lang]} ({lang})",
            )
        )
    for schedule in schedules:
        ab.fill_ad_schedule(ga_client, _new(), schedule)
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Ad schedule",
                campaign_rn,
                "Window",
                ab.describe_schedule(schedule),
            )
        )
    return operations, change_items


_REMOVABLE_TARGETING_TYPES = (
    "LOCATION",
    "LANGUAGE",
    "AD_SCHEDULE",
    "PROXIMITY",
)


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def remove_campaign_targeting(
    customer_id: str | int,
    campaign_id: str,
    criterion_ids: list[str],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Removes location, language, ad schedule or radius targeting.

    Only criteria of those types are removed; keywords, negative keywords and
    other criteria are refused. Find criterion IDs with the search tool on
    the campaign_criterion resource.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_id: The numeric campaign ID.
        criterion_ids: Numeric criterion IDs to remove.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaign_id = mh.require_simple_id(campaign_id, "campaign_id")
    ids = mh.require_ids(criterion_ids, "criterion_ids")
    rns = [f"customers/{cid}/campaignCriteria/{campaign_id}~{c}" for c in ids]

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign_criterion.resource_name, campaign_criterion.type, "
        "campaign_criterion.negative, campaign_criterion.display_name "
        "FROM campaign_criterion WHERE campaign_criterion.resource_name IN "
        f"({mh.quoted_list(rns)}) AND campaign_criterion.type IN "
        f"({mh.quoted_list(list(_REMOVABLE_TARGETING_TYPES))})",
    )
    found = {
        row.campaign_criterion.resource_name: row.campaign_criterion
        for row in rows
    }
    missing = [rn for rn in rns if rn not in found]
    if missing:
        raise ToolError(
            "Error: These criteria are not removable targeting criteria "
            f"({', '.join(_REMOVABLE_TARGETING_TYPES)}) of campaign "
            f"{campaign_id}, or do not exist: {', '.join(missing)}"
        )

    operations = []
    change_items: list[pac.ChangeItem] = []
    for rn in rns:
        op = ga_client.get_type("CampaignCriterionOperation")
        op.remove = rn
        operations.append(op)
        criterion = found[rn]
        kind = criterion.type_.name.replace("_", " ").title()
        if criterion.negative:
            kind += " exclusion"
        change_items.append(
            pac.ChangeItem(
                "REMOVE", kind, rn, "Criterion", criterion.display_name
            )
        )

    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="CampaignCriterionService",
        request_type="MutateCampaignCriteriaRequest",
        mutate_method="mutate_campaign_criteria",
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "remove_campaign_targeting",
            "campaign_id": campaign_id,
            "criterion_ids": ids,
        },
        change_items=change_items,
        success_message=(
            f"Successfully removed {len(ids)} targeting criteria from "
            f"campaign {campaign_id}."
        ),
        error_prefix="Failed to remove campaign targeting",
    )


_DEVICES = ("MOBILE", "TABLET", "DESKTOP")


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def update_bid_modifiers(
    customer_id: str | int,
    campaign_id: str,
    device_bid_modifiers: dict[str, float] | None = None,
    criterion_bid_modifiers: dict[str, float] | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Sets bid modifiers on a campaign's devices, locations or ad schedules.

    A modifier of 1.2 means +20%, 0.8 means -20% and 0 excludes the device
    entirely. Automated strategies such as Maximize conversions ignore most
    modifiers except a device exclusion (0).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_id: The numeric campaign ID.
        device_bid_modifiers: Map of MOBILE / TABLET / DESKTOP to a modifier.
        criterion_bid_modifiers: Map of an existing location or ad schedule
            criterion ID (from the campaign_criterion resource) to a modifier.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaign_id = mh.require_simple_id(campaign_id, "campaign_id")
    devices = {
        str(k).upper(): ab.validate_bid_modifier(v, f"device {k}")
        for k, v in (device_bid_modifiers or {}).items()
    }
    unknown = [d for d in devices if d not in _DEVICES]
    if unknown:
        raise ToolError(
            f"Error: Unknown devices {unknown}; use {', '.join(_DEVICES)}."
        )
    criteria = {
        mh.require_simple_id(k, "criterion_bid_modifiers"): (
            ab.validate_bid_modifier(v, f"criterion {k}")
        )
        for k, v in (criterion_bid_modifiers or {}).items()
    }
    if not devices and not criteria:
        raise ToolError("Error: Pass at least one bid modifier.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign_criterion.resource_name, "
        "campaign_criterion.criterion_id, campaign_criterion.type, "
        "campaign_criterion.device.type, campaign_criterion.bid_modifier, "
        "campaign_criterion.display_name FROM campaign_criterion "
        f"WHERE campaign.id = {campaign_id} AND campaign_criterion.type IN "
        "('DEVICE', 'LOCATION', 'AD_SCHEDULE') "
        "AND campaign_criterion.negative = FALSE",
    )
    by_device = {}
    by_id = {}
    for row in rows:
        criterion = row.campaign_criterion
        by_id[str(criterion.criterion_id)] = criterion
        if criterion.type_.name == "DEVICE":
            by_device[criterion.device.type_.name] = criterion

    targets: list[tuple[Any, float]] = []
    for device, modifier in devices.items():
        if device not in by_device:
            raise ToolError(
                f"Error: No {device} device criterion found on campaign "
                f"{campaign_id}."
            )
        targets.append((by_device[device], modifier))
    for criterion_id, modifier in criteria.items():
        if criterion_id not in by_id:
            raise ToolError(
                f"Error: Criterion {criterion_id} is not a location or ad "
                f"schedule criterion of campaign {campaign_id}."
            )
        targets.append((by_id[criterion_id], modifier))

    operations = []
    change_items: list[pac.ChangeItem] = []
    for criterion, modifier in targets:
        op = ga_client.get_type("CampaignCriterionOperation")
        op.update.resource_name = criterion.resource_name
        op.update.bid_modifier = modifier
        op.update_mask.paths.append("bid_modifier")
        operations.append(op)
        change_items.extend(
            pac.build_change_items(
                operation="UPDATE",
                entity_readable=criterion.type_.name.replace("_", " ").title(),
                primary_key_readable=(
                    f"{criterion.display_name} ({criterion.resource_name})"
                ),
                fields={"Bid modifier": modifier},
                original_fields={
                    "Bid modifier": criterion.bid_modifier or "none (1.0)"
                },
            )
        )

    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="CampaignCriterionService",
        request_type="MutateCampaignCriteriaRequest",
        mutate_method="mutate_campaign_criteria",
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_bid_modifiers",
            "campaign_id": campaign_id,
            "devices": devices,
            "criteria": criteria,
        },
        change_items=change_items,
        success_message=(
            f"Successfully updated {len(operations)} bid modifiers on "
            f"campaign {campaign_id}."
        ),
        error_prefix="Failed to update bid modifiers",
    )
