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

"""Tools that build new Search campaigns, ad groups and ads.

Multi-resource builds go through one atomic GoogleAdsService.Mutate request
with temporary resource IDs: either everything is created or nothing is.
All tools use the propose-and-confirm workflow; the dry run validates the
complete build (policy, limits, duplicates) before anything is created.
"""

from enum import Enum
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import ad_builders as ab
from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_management import EntityStatus
from ads_mcp.tools.campaign_settings import build_targeting_operations
import ads_mcp.utils as utils

campaign_builder_mcp = FastMCP("campaign_builder")

MAX_AD_GROUPS_PER_CAMPAIGN = 20
MAX_KEYWORDS_PER_AD_GROUP = 200
MAX_RSAS_PER_AD_GROUP = 3


class LocationTargeting(str, Enum):
    """Who counts as being in a targeted location."""

    PRESENCE = "PRESENCE"
    PRESENCE_OR_INTEREST = "PRESENCE_OR_INTEREST"


def _currency(ga_client: Any, cid: str) -> str:
    rows = mh.search(
        ga_client, cid, "SELECT customer.currency_code FROM customer"
    )
    return rows[0].customer.currency_code if rows else ""


def normalize_ad_group(
    ad_group: ab.AdGroupInput | dict, requires_cpc: bool
) -> dict[str, Any]:
    """Validates an ad group definition into a plain dict."""
    if isinstance(ad_group, dict):
        ad_group = ab.AdGroupInput(**ad_group)
    name = " ".join(ad_group.name.split())
    if not name:
        raise ToolError("Error: Every ad group needs a 'name'.")
    if requires_cpc and ad_group.cpc_bid is None:
        raise ToolError(
            f"Error: Ad group '{name}' needs 'cpc_bid' for MANUAL_CPC."
        )
    ads = [ab.normalize_rsa(ad) for ad in ad_group.ads]
    if not 1 <= len(ads) <= MAX_RSAS_PER_AD_GROUP:
        raise ToolError(
            f"Error: Ad group '{name}' needs 1-{MAX_RSAS_PER_AD_GROUP} "
            f"responsive search ads; got {len(ads)}."
        )
    return {
        "name": name,
        "keywords": mh.clean_texts(
            ad_group.keywords,
            f"keywords of '{name}'",
            MAX_KEYWORDS_PER_AD_GROUP,
        ),
        "match_type": mh.enum_value(
            ad_group.match_type, ab.KeywordMatchType, "match_type"
        ),
        "cpc_bid_micros": (
            mh.to_micros(ad_group.cpc_bid, "cpc_bid")
            if ad_group.cpc_bid is not None
            else None
        ),
        "negative_keywords": (
            mh.clean_texts(
                ad_group.negative_keywords,
                f"negative_keywords of '{name}'",
                mh.MAX_KEYWORDS_PER_CALL,
            )
            if ad_group.negative_keywords
            else []
        ),
        "ads": ads,
    }


def build_ad_group_operations(
    ga_client: Any,
    cid: str,
    campaign_rn: str,
    ad_group: dict[str, Any],
    temp_id: int,
    status: str,
    currency: str,
) -> tuple[list[Any], list[pac.ChangeItem]]:
    """Builds MutateOperations for an ad group, its keywords and its ads."""
    ad_group_rn = f"customers/{cid}/adGroups/{temp_id}"
    operations: list[Any] = []
    change_items: list[pac.ChangeItem] = []

    op = ga_client.get_type("MutateOperation")
    group = op.ad_group_operation.create
    group.resource_name = ad_group_rn
    group.name = ad_group["name"]
    group.campaign = campaign_rn
    group.type_ = ga_client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    group.status = getattr(ga_client.enums.AdGroupStatusEnum, status)
    if ad_group["cpc_bid_micros"]:
        group.cpc_bid_micros = ad_group["cpc_bid_micros"]
    operations.append(op)
    change_items.extend(
        pac.build_change_items(
            operation="CREATE",
            entity_readable="Ad Group",
            primary_key_readable=ad_group["name"],
            fields={
                "Status": status,
                "Default max CPC": (
                    mh.format_money(ad_group["cpc_bid_micros"], currency)
                    if ad_group["cpc_bid_micros"]
                    else None
                ),
                f"Keywords ({ad_group['match_type']})": ", ".join(
                    ad_group["keywords"]
                ),
                "Negative keywords (EXACT)": (
                    ", ".join(ad_group["negative_keywords"]) or None
                ),
            },
        )
    )

    for text in ad_group["keywords"]:
        kw_op = ga_client.get_type("MutateOperation")
        criterion = kw_op.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_rn
        criterion.status = ga_client.enums.AdGroupCriterionStatusEnum.ENABLED
        ab.fill_keyword(ga_client, criterion, text, ad_group["match_type"])
        operations.append(kw_op)
    for text in ad_group["negative_keywords"]:
        neg_op = ga_client.get_type("MutateOperation")
        criterion = neg_op.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_rn
        criterion.negative = True
        ab.fill_keyword(ga_client, criterion, text, "EXACT")
        operations.append(neg_op)
    for index, rsa in enumerate(ad_group["ads"], start=1):
        ad_op = ga_client.get_type("MutateOperation")
        ad_group_ad = ad_op.ad_group_ad_operation.create
        ad_group_ad.ad_group = ad_group_rn
        ad_group_ad.status = ga_client.enums.AdGroupAdStatusEnum.ENABLED
        ab.fill_rsa(ga_client, ad_group_ad.ad, rsa)
        operations.append(ad_op)
        change_items.extend(
            pac.build_change_items(
                operation="CREATE",
                entity_readable="Responsive Search Ad",
                primary_key_readable=f"{ad_group['name']} #{index}",
                fields=ab.rsa_change_fields(rsa),
            )
        )
    return operations, change_items


def _fetch_search_campaign(ga_client: Any, cid: str, campaign_id: str) -> Any:
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign.name, campaign.advertising_channel_type, "
        "campaign.bidding_strategy_type, customer.currency_code "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
    )
    if not rows:
        raise ToolError(
            f"Error: Campaign {campaign_id} was not found in customer {cid}."
        )
    if rows[0].campaign.advertising_channel_type.name != "SEARCH":
        raise ToolError(
            f"Error: Campaign {campaign_id} is not a Search campaign."
        )
    return rows[0]


@campaign_builder_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_search_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    bidding_strategy: ab.BiddingStrategy,
    location_ids: list[str],
    ad_groups: list[ab.AdGroupInput],
    language_ids: list[str] | None = None,
    excluded_location_ids: list[str] | None = None,
    target_cpa: float | None = None,
    target_roas_percent: float | None = None,
    max_cpc_limit: float | None = None,
    negative_keywords: list[str] | None = None,
    negative_match_type: ab.KeywordMatchType = ab.KeywordMatchType.PHRASE,
    ad_schedules: list[ab.AdScheduleInput] | None = None,
    search_partners: bool = False,
    location_targeting: LocationTargeting = LocationTargeting.PRESENCE,
    start_date: str | None = None,
    end_date: str | None = None,
    status: EntityStatus = EntityStatus.PAUSED,
    contains_eu_political_advertising: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Builds a complete Search campaign in one atomic step.

    Creates the budget, the campaign, location/language/schedule targeting,
    campaign negative keywords, and per ad group the keywords, ad-group
    negatives and responsive search ads. Everything is created or nothing.
    The campaign is PAUSED by default so it can be reviewed in Google Ads
    before it spends; the Display Network is always off.

    Before calling, gather from the user or the account: landing pages,
    locations and languages (look up IDs with the search tool on
    geo_target_constant / language_constant, e.g. 2056 = Belgium, 1010 =
    Dutch, 1002 = French), budget and bidding. Conversion-based bidding
    (MAXIMIZE_CONVERSIONS / MAXIMIZE_CONVERSION_VALUE) needs working
    conversion tracking; check the conversion_action resource first.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        name: Campaign name (must be unique in the account).
        daily_budget: Average daily budget in the account currency.
        bidding_strategy: MAXIMIZE_CONVERSIONS, MAXIMIZE_CONVERSION_VALUE,
            MAXIMIZE_CLICKS or MANUAL_CPC (then every ad group needs cpc_bid).
        location_ids: Geo target constant IDs to target (at least one).
        ad_groups: 1-20 ad groups, each with keywords and 1-3 ads.
        language_ids: Language constant IDs; empty means all languages.
        excluded_location_ids: Geo target constant IDs to exclude.
        target_cpa: Optional target CPA (MAXIMIZE_CONVERSIONS).
        target_roas_percent: Optional target ROAS in percent
            (MAXIMIZE_CONVERSION_VALUE), e.g. 400.
        max_cpc_limit: Optional max CPC limit (MAXIMIZE_CLICKS).
        negative_keywords: Campaign-level negative keywords.
        negative_match_type: Match type for campaign negatives (PHRASE).
        ad_schedules: Optional time windows, e.g. {{"day":
            "MONDAY_TO_FRIDAY", "start_time": "08:00", "end_time": "20:00"}}.
        search_partners: Also show on Google search partners (default off).
        location_targeting: PRESENCE (people in the location, default) or
            PRESENCE_OR_INTEREST.
        start_date: Optional start date YYYY-MM-DD (default: today).
        end_date: Optional end date YYYY-MM-DD.
        status: PAUSED (default) or ENABLED.
        contains_eu_political_advertising: Required EU declaration; True only
            for political ads.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaign_name = " ".join((name or "").split())
    if not campaign_name:
        raise ToolError("Error: 'name' is required.")
    budget_micros = mh.to_micros(daily_budget, "daily_budget")
    bidding = ab.resolve_bidding(
        bidding_strategy, target_cpa, target_roas_percent, max_cpc_limit
    )
    locations = mh.require_ids(location_ids, "location_ids")
    excluded = (
        mh.require_ids(excluded_location_ids, "excluded_location_ids")
        if excluded_location_ids
        else []
    )
    languages = (
        mh.require_ids(language_ids, "language_ids") if language_ids else []
    )
    schedules = ab.normalize_ad_schedules(ad_schedules)
    negatives = (
        mh.clean_texts(
            negative_keywords, "negative_keywords", mh.MAX_KEYWORDS_PER_CALL
        )
        if negative_keywords
        else []
    )
    negative_match = mh.enum_value(
        negative_match_type, ab.KeywordMatchType, "negative_match_type"
    )
    status_value = mh.enum_value(status, EntityStatus, "status")
    targeting_value = mh.enum_value(
        location_targeting, LocationTargeting, "location_targeting"
    )
    start = mh.require_date(start_date, "start_date")
    end = mh.require_date(end_date, "end_date")
    if start and end and end < start:
        raise ToolError("Error: 'end_date' must not be before 'start_date'.")
    if not ad_groups or len(ad_groups) > MAX_AD_GROUPS_PER_CAMPAIGN:
        raise ToolError(
            f"Error: Pass 1-{MAX_AD_GROUPS_PER_CAMPAIGN} ad groups."
        )
    requires_cpc = bidding["strategy"] == ab.BiddingStrategy.MANUAL_CPC.value
    groups = [normalize_ad_group(g, requires_cpc) for g in ad_groups]
    if len({g["name"].lower() for g in groups}) != len(groups):
        raise ToolError("Error: Ad group names must be unique.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency = _currency(ga_client, cid)
    geo_names = ab.lookup_constants(
        ga_client, cid, mh.dedupe(locations + excluded), "geo"
    )
    language_names = ab.lookup_constants(ga_client, cid, languages, "language")

    budget_rn = f"customers/{cid}/campaignBudgets/-1"
    campaign_rn = f"customers/{cid}/campaigns/-2"
    operations: list[Any] = []

    budget_op = ga_client.get_type("MutateOperation")
    budget = budget_op.campaign_budget_operation.create
    budget.resource_name = budget_rn
    budget.name = f"{campaign_name} budget"
    budget.amount_micros = budget_micros
    budget.delivery_method = ga_client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    operations.append(budget_op)

    campaign_op = ga_client.get_type("MutateOperation")
    campaign = campaign_op.campaign_operation.create
    campaign.resource_name = campaign_rn
    campaign.name = campaign_name
    campaign.campaign_budget = budget_rn
    campaign.advertising_channel_type = (
        ga_client.enums.AdvertisingChannelTypeEnum.SEARCH
    )
    campaign.status = getattr(ga_client.enums.CampaignStatusEnum, status_value)
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = search_partners
    campaign.network_settings.target_partner_search_network = False
    campaign.network_settings.target_content_network = False
    campaign.geo_target_type_setting.positive_geo_target_type = getattr(
        ga_client.enums.PositiveGeoTargetTypeEnum, targeting_value
    )
    eu = ga_client.enums.EuPoliticalAdvertisingStatusEnum
    campaign.contains_eu_political_advertising = (
        eu.CONTAINS_EU_POLITICAL_ADVERTISING
        if contains_eu_political_advertising
        else eu.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    if start:
        campaign.start_date_time = f"{start} 00:00:00"
    if end:
        campaign.end_date_time = f"{end} 23:59:59"
    ab.apply_bidding(ga_client, campaign, bidding)
    operations.append(campaign_op)

    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Search Campaign",
        primary_key_readable=campaign_name,
        fields={
            "Status": status_value,
            "Daily budget": mh.format_money(budget_micros, currency),
            "Bidding": ab.describe_bidding(bidding, currency),
            "Networks": "Google Search"
            + (" + search partners" if search_partners else ""),
            "Location targeting": targeting_value,
            "Languages": None if languages else "All languages",
            "Start": start,
            "End": end,
            "EU political advertising": (
                "Yes" if contains_eu_political_advertising else "No"
            ),
        },
    )

    targeting_ops, targeting_items = build_targeting_operations(
        ga_client,
        campaign_rn,
        locations,
        excluded,
        languages,
        schedules,
        geo_names,
        language_names,
    )
    operations.extend(targeting_ops)
    change_items.extend(targeting_items)

    for text in negatives:
        neg_op = ga_client.get_type("MutateOperation")
        criterion = neg_op.campaign_criterion_operation.create
        criterion.campaign = campaign_rn
        criterion.negative = True
        ab.fill_keyword(ga_client, criterion, text, negative_match)
        operations.append(neg_op)
    if negatives:
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Campaign Negative Keywords",
                campaign_name,
                f"Keywords ({negative_match})",
                ", ".join(negatives),
            )
        )

    for index, group in enumerate(groups):
        group_ops, group_items = build_ad_group_operations(
            ga_client,
            cid,
            campaign_rn,
            group,
            temp_id=-(10 + index),
            status="ENABLED",
            currency=currency,
        )
        operations.extend(group_ops)
        change_items.extend(group_items)

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_search_campaign",
            "name": campaign_name,
            "budget_micros": budget_micros,
            "bidding": bidding,
            "locations": locations,
            "excluded_locations": excluded,
            "languages": languages,
            "ad_schedules": [
                {**s, "start": list(s["start"]), "end": list(s["end"])}
                for s in schedules
            ],
            "negatives": negatives,
            "negative_match_type": negative_match,
            "search_partners": search_partners,
            "location_targeting": targeting_value,
            "start_date": start,
            "end_date": end,
            "status": status_value,
            "eu_political": contains_eu_political_advertising,
            "ad_groups": groups,
        },
        change_items=change_items,
        success_message=(
            f"Created Search campaign '{campaign_name}' ({status_value}) "
            f"with {len(groups)} ad groups."
        ),
        error_prefix="Failed to create Search campaign",
    )


@campaign_builder_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_ad_group(
    customer_id: str | int,
    campaign_id: str,
    ad_group: ab.AdGroupInput,
    status: EntityStatus = EntityStatus.ENABLED,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds a complete ad group (keywords, negatives, ads) to a Search campaign.

    Created atomically: the ad group, its keywords, ad-group negatives and
    1-3 responsive search ads succeed or fail together.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_id: The numeric ID of an existing Search campaign.
        ad_group: The ad group definition.
        status: ENABLED (default) or PAUSED.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaign_id = mh.require_simple_id(campaign_id, "campaign_id")
    status_value = mh.enum_value(status, EntityStatus, "status")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _fetch_search_campaign(ga_client, cid, campaign_id)
    requires_cpc = row.campaign.bidding_strategy_type.name == "MANUAL_CPC"
    group = normalize_ad_group(ad_group, requires_cpc)
    campaign_rn = f"customers/{cid}/campaigns/{campaign_id}"

    operations, change_items = build_ad_group_operations(
        ga_client,
        cid,
        campaign_rn,
        group,
        temp_id=-1,
        status=status_value,
        currency=row.customer.currency_code,
    )
    change_items = [
        pac.ChangeItem(
            "CREATE",
            "Target campaign",
            campaign_rn,
            "Campaign",
            row.campaign.name,
        )
    ] + change_items
    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_ad_group",
            "campaign_id": campaign_id,
            "status": status_value,
            "ad_group": group,
        },
        change_items=change_items,
        success_message=(
            f"Created ad group '{group['name']}' in campaign {campaign_id}."
        ),
        error_prefix="Failed to create ad group",
    )


@campaign_builder_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_responsive_search_ad(
    customer_id: str | int,
    ad_group_id: str,
    ad: ab.ResponsiveSearchAdInput,
    status: EntityStatus = EntityStatus.ENABLED,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds a responsive search ad to an existing ad group.

    Google allows at most 3 enabled or paused responsive search ads per ad
    group. To test new copy against an existing ad, create the new ad and
    pause the old one with update_entity_status, or edit it in place with
    update_responsive_search_ad.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        ad_group_id: The numeric ad group ID.
        ad: The ad content (3-15 headlines, 2-4 descriptions, final_url).
        status: ENABLED (default) or PAUSED.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    ad_group_id = mh.require_simple_id(ad_group_id, "ad_group_id")
    status_value = mh.enum_value(status, EntityStatus, "status")
    rsa = ab.normalize_rsa(ad)

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT ad_group.name, ad_group_ad.status, ad_group_ad.ad.type "
        "FROM ad_group_ad WHERE ad_group.id = "
        f"{ad_group_id} AND ad_group_ad.status IN ('ENABLED', 'PAUSED') "
        "AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'",
    )
    if len(rows) >= MAX_RSAS_PER_AD_GROUP:
        raise ToolError(
            f"Error: Ad group {ad_group_id} already has {len(rows)} "
            "responsive search ads (Google's maximum is 3). Edit one with "
            "update_responsive_search_ad instead."
        )
    ad_group_rn = f"customers/{cid}/adGroups/{ad_group_id}"
    group_name = rows[0].ad_group.name if rows else ad_group_rn

    op = ga_client.get_type("AdGroupAdOperation")
    op.create.ad_group = ad_group_rn
    op.create.status = getattr(
        ga_client.enums.AdGroupAdStatusEnum, status_value
    )
    ab.fill_rsa(ga_client, op.create.ad, rsa)
    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Responsive Search Ad",
        primary_key_readable=group_name,
        fields={"Status": status_value, **ab.rsa_change_fields(rsa)},
    )
    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="AdGroupAdService",
        request_type="MutateAdGroupAdsRequest",
        mutate_method="mutate_ad_group_ads",
        operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_responsive_search_ad",
            "ad_group_id": ad_group_id,
            "status": status_value,
            "ad": rsa,
        },
        change_items=change_items,
        success_message=(
            f"Created responsive search ad in ad group {ad_group_id}."
        ),
        error_prefix="Failed to create responsive search ad",
    )
