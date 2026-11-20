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

"""Builders for Display and standard Shopping campaigns.

Registered on the ``campaign_builder`` namespace; same atomic,
propose-and-confirm, PAUSED-by-default approach as the other builders.
"""

from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ads_mcp import ad_builders as ab
from ads_mcp import image_assets as ia
from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.asset_campaigns import (
    _campaign_change_items,
    _campaign_shell,
    _common_inputs,
    _currency,
    _refs,
)
from ads_mcp.tools.campaign_builder import NEXT_STEP, campaign_builder_mcp
from ads_mcp.tools.campaign_management import EntityStatus
from ads_mcp.tools.campaign_settings import build_targeting_operations
import ads_mcp.utils as utils

MAX_DISPLAY_AD_GROUPS = 10


class ResponsiveDisplayAdInput(BaseModel):
    """A responsive display ad."""

    final_url: str = Field(description="Landing page URL.")
    headlines: list[str] = Field(description="1-5 short headlines, max 30.")
    long_headline: str = Field(description="Long headline, max 90 chars.")
    descriptions: list[str] = Field(description="1-5 descriptions, max 90.")
    business_name: str = Field(description="Business name, max 25 chars.")
    marketing_images: list[str] = Field(
        description="Landscape 1.91:1 images: asset IDs or https URLs (1-15)."
    )
    square_marketing_images: list[str] = Field(
        description="Square 1:1 images: asset IDs or https URLs (1-15)."
    )
    logo_images: list[str] = Field(
        default_factory=list, description="Optional square logos (max 5)."
    )
    call_to_action: str | None = Field(
        default=None, description="Optional call to action, e.g. 'Learn more'."
    )


class DisplayAdGroupInput(BaseModel):
    """A Display ad group with its targeting and ads."""

    name: str = Field(description="Ad group name.")
    cpc_bid: float | None = Field(
        default=None, description="Default max CPC (MANUAL_CPC only)."
    )
    user_interest_ids: list[str] = Field(
        default_factory=list,
        description="Affinity / in-market segment IDs to target.",
    )
    user_list_ids: list[str] = Field(
        default_factory=list, description="Remarketing list IDs to target."
    )
    placements: list[str] = Field(
        default_factory=list, description="Websites to target, e.g. example.be."
    )
    topic_ids: list[str] = Field(
        default_factory=list, description="Topic constant IDs to target."
    )
    ads: list[ResponsiveDisplayAdInput] = Field(
        description="1-5 responsive display ads."
    )


def _normalize_display_ad(ad: ResponsiveDisplayAdInput | dict) -> dict:
    if isinstance(ad, dict):
        ad = ResponsiveDisplayAdInput(**ad)
    logos = _refs(ad.logo_images, "logo_images", 0)
    if len(logos) > 5:
        raise ToolError("Error: At most 5 logos per display ad.")
    return {
        "final_url": mh.require_url(ad.final_url, "final_url"),
        "headlines": ab.normalize_plain_texts(
            ad.headlines, "headline", 1, 5, 30
        ),
        "long_headline": ab.normalize_plain_texts(
            [ad.long_headline], "long headline", 1, 1, 90
        )[0],
        "descriptions": ab.normalize_plain_texts(
            ad.descriptions, "description", 1, 5, 90
        ),
        "business_name": ab.normalize_plain_texts(
            [ad.business_name], "business name", 1, 1, 25
        )[0],
        "marketing_images": _refs(ad.marketing_images, "marketing_images", 1),
        "square_marketing_images": _refs(
            ad.square_marketing_images, "square_marketing_images", 1
        ),
        "logo_images": logos,
        "call_to_action": (ad.call_to_action or "").strip() or None,
    }


def _normalize_display_group(
    group: DisplayAdGroupInput | dict, manual_cpc: bool
) -> dict:
    if isinstance(group, dict):
        group = DisplayAdGroupInput(**group)
    name = " ".join(group.name.split())
    if not name:
        raise ToolError("Error: Every ad group needs a 'name'.")
    if manual_cpc and group.cpc_bid is None:
        raise ToolError(
            f"Error: Ad group '{name}' needs 'cpc_bid' for MANUAL_CPC."
        )
    if not 1 <= len(group.ads) <= 5:
        raise ToolError(f"Error: Ad group '{name}' needs 1-5 ads.")
    placements = []
    for raw in group.placements:
        site = str(raw).strip().lower()
        site = site.removeprefix("https://").removeprefix("http://")
        if not site or " " in site or "." not in site:
            raise ToolError(f"Error: Invalid placement '{raw}'.")
        placements.append(site)

    def ids(values: list[str], label: str) -> list[str]:
        return mh.require_ids(values, label) if values else []

    return {
        "name": name,
        "cpc_bid_micros": (
            mh.to_micros(group.cpc_bid, "cpc_bid")
            if group.cpc_bid is not None
            else None
        ),
        "user_interest_ids": ids(group.user_interest_ids, "user_interest_ids"),
        "user_list_ids": ids(group.user_list_ids, "user_list_ids"),
        "placements": mh.dedupe(placements),
        "topic_ids": ids(group.topic_ids, "topic_ids"),
        "ads": [_normalize_display_ad(ad) for ad in group.ads],
    }


def _text_assets(ga_client: Any, texts: list[str]) -> list[Any]:
    assets = []
    for text in texts:
        asset = ga_client.get_type("AdTextAsset")
        asset.text = text
        assets.append(asset)
    return assets


@campaign_builder_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_display_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    bidding_strategy: ab.BiddingStrategy,
    location_ids: list[str],
    ad_groups: list[DisplayAdGroupInput],
    language_ids: list[str] | None = None,
    target_cpa: float | None = None,
    target_roas_percent: float | None = None,
    max_cpc_limit: float | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    status: EntityStatus = EntityStatus.PAUSED,
    contains_eu_political_advertising: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Builds a complete Display campaign with responsive display ads.

    Creates the budget, the campaign, location/language targeting and 1-10
    ad groups, each with its targeting (segments from find_audience_segments,
    remarketing lists, placements, topics; none means Google's optimized
    targeting) and 1-5 responsive display ads. PAUSED by default. Images
    are asset IDs or https URLs (checked: landscape 1.91:1, square 1:1).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        name: Campaign name.
        daily_budget: Average daily budget in the account currency.
        bidding_strategy: MAXIMIZE_CONVERSIONS, MAXIMIZE_CONVERSION_VALUE,
            MAXIMIZE_CLICKS or MANUAL_CPC.
        location_ids: Geo target constant IDs (at least one).
        ad_groups: 1-10 ad groups with targeting and ads.
        language_ids: Language constant IDs; empty = all languages.
        target_cpa: Optional target CPA (MAXIMIZE_CONVERSIONS).
        target_roas_percent: Optional target ROAS (MAXIMIZE_CONVERSION_VALUE).
        max_cpc_limit: Optional max CPC (MAXIMIZE_CLICKS).
        start_date: Optional start date YYYY-MM-DD.
        end_date: Optional end date YYYY-MM-DD.
        status: PAUSED (default) or ENABLED.
        contains_eu_political_advertising: Required EU declaration.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    base = _common_inputs(
        name,
        daily_budget,
        bidding_strategy,
        (
            "MAXIMIZE_CONVERSIONS",
            "MAXIMIZE_CONVERSION_VALUE",
            "MAXIMIZE_CLICKS",
            "MANUAL_CPC",
        ),
        target_cpa,
        target_roas_percent,
        max_cpc_limit,
        start_date,
        end_date,
        status,
    )
    locations = mh.require_ids(location_ids, "location_ids")
    languages = (
        mh.require_ids(language_ids, "language_ids") if language_ids else []
    )
    if not ad_groups or len(ad_groups) > MAX_DISPLAY_AD_GROUPS:
        raise ToolError(f"Error: Pass 1-{MAX_DISPLAY_AD_GROUPS} ad groups.")
    manual = base["bidding"]["strategy"] == "MANUAL_CPC"
    groups = [_normalize_display_group(g, manual) for g in ad_groups]
    if len({g["name"].lower() for g in groups}) != len(groups):
        raise ToolError("Error: Ad group names must be unique.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    currency = _currency(ga_client, cid)
    geo_names = ab.lookup_constants(ga_client, cid, locations, "geo")
    language_names = ab.lookup_constants(ga_client, cid, languages, "language")
    ids = ia.TempIds(cid)
    pool = ia.AssetPool(ga_client, cid, ids)
    campaign_rn, shell_ops, campaign = _campaign_shell(
        ga_client,
        ids,
        name=base["name"],
        channel="DISPLAY",
        budget_micros=base["budget_micros"],
        bidding=base["bidding"],
        status=base["status"],
        start=base["start"],
        end=base["end"],
        eu_political=contains_eu_political_advertising,
    )
    targeting_ops, targeting_items = build_targeting_operations(
        ga_client,
        campaign_rn,
        locations,
        [],
        languages,
        [],
        geo_names,
        language_names,
    )
    change_items = (
        _campaign_change_items("Display Campaign", base, currency, {})
        + targeting_items
    )

    group_ops: list[Any] = []
    for group in groups:
        group_rn = ids.next("adGroups")
        op = ga_client.get_type("MutateOperation")
        ad_group = op.ad_group_operation.create
        ad_group.resource_name = group_rn
        ad_group.name = group["name"]
        ad_group.campaign = campaign_rn
        ad_group.status = enums.AdGroupStatusEnum.ENABLED
        ad_group.type_ = enums.AdGroupTypeEnum.DISPLAY_STANDARD
        if group["cpc_bid_micros"]:
            ad_group.cpc_bid_micros = group["cpc_bid_micros"]
        group_ops.append(op)

        def _criterion() -> Any:
            crit_op = ga_client.get_type("MutateOperation")
            criterion = crit_op.ad_group_criterion_operation.create
            criterion.ad_group = group_rn
            group_ops.append(crit_op)
            return criterion

        for interest in group["user_interest_ids"]:
            _criterion().user_interest.user_interest_category = (
                f"customers/{cid}/userInterests/{interest}"
            )
        for user_list in group["user_list_ids"]:
            _criterion().user_list.user_list = (
                f"customers/{cid}/userLists/{user_list}"
            )
        for site in group["placements"]:
            _criterion().placement.url = site
        for topic in group["topic_ids"]:
            _criterion().topic.topic_constant = f"topicConstants/{topic}"

        for index, ad in enumerate(group["ads"], start=1):
            ad_op = ga_client.get_type("MutateOperation")
            ad_group_ad = ad_op.ad_group_ad_operation.create
            ad_group_ad.ad_group = group_rn
            ad_group_ad.status = enums.AdGroupAdStatusEnum.ENABLED
            ad_group_ad.ad.final_urls.append(ad["final_url"])
            info = ad_group_ad.ad.responsive_display_ad
            info.headlines.extend(_text_assets(ga_client, ad["headlines"]))
            info.long_headline.text = ad["long_headline"]
            info.descriptions.extend(
                _text_assets(ga_client, ad["descriptions"])
            )
            info.business_name = ad["business_name"]
            if ad["call_to_action"]:
                info.call_to_action_text = ad["call_to_action"]
            for key, role, field in (
                ("marketing_images", "MARKETING", "marketing_images"),
                (
                    "square_marketing_images",
                    "SQUARE",
                    "square_marketing_images",
                ),
                ("logo_images", "LOGO", "square_logo_images"),
            ):
                for n, ref in enumerate(ad[key], start=1):
                    image = ga_client.get_type("AdImageAsset")
                    image.asset = pool.image(
                        ref,
                        role,
                        f"{group['name']} ad {index} {role.lower()} {n}",
                    )
                    getattr(info, field).append(image)
            group_ops.append(ad_op)

        targeting = [
            (
                f"{len(group['user_interest_ids'])} segments"
                if group["user_interest_ids"]
                else None
            ),
            (
                f"{len(group['user_list_ids'])} remarketing lists"
                if group["user_list_ids"]
                else None
            ),
            (
                f"placements: {', '.join(group['placements'])}"
                if group["placements"]
                else None
            ),
            f"{len(group['topic_ids'])} topics" if group["topic_ids"] else None,
        ]
        change_items.extend(
            pac.build_change_items(
                operation="CREATE",
                entity_readable="Display Ad Group",
                primary_key_readable=group["name"],
                fields={
                    "Targeting": "; ".join(t for t in targeting if t)
                    or "Optimized targeting",
                    "Ads": " | ".join(a["long_headline"] for a in group["ads"]),
                },
            )
        )

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=shell_ops
        + targeting_ops
        + pool.operations
        + group_ops,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_display_campaign",
            **base,
            "locations": locations,
            "languages": languages,
            "eu_political": contains_eu_political_advertising,
            "ad_groups": groups,
            "image_digests": pool.image_digests,
        },
        change_items=change_items,
        success_message=(
            f"Created Display campaign '{base['name']}' ({base['status']}) "
            f"with {len(groups)} ad groups."
            + (NEXT_STEP if base["status"] == "PAUSED" else "")
        ),
        error_prefix="Failed to create Display campaign",
    )


@campaign_builder_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_shopping_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    merchant_center_id: str,
    location_ids: list[str],
    default_cpc: float,
    bidding_strategy: ab.BiddingStrategy = ab.BiddingStrategy.MANUAL_CPC,
    max_cpc_limit: float | None = None,
    feed_label: str | None = None,
    campaign_priority: int = 0,
    search_partners: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    status: EntityStatus = EntityStatus.PAUSED,
    contains_eu_political_advertising: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Builds a standard Shopping campaign for a linked Merchant Center.

    Creates the budget, the campaign (Merchant Center, feed label, priority),
    location targeting, a product ad group with a product ad, and one product
    group that includes all products at `default_cpc`. For automated retail
    bidding across channels, prefer create_performance_max_campaign with a
    Merchant Center ID. PAUSED by default.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        name: Campaign name.
        daily_budget: Average daily budget in the account currency.
        merchant_center_id: Linked Merchant Center account ID.
        location_ids: Geo target constant IDs (at least one).
        default_cpc: Max CPC for all products (account currency).
        bidding_strategy: MANUAL_CPC (default) or MAXIMIZE_CLICKS.
        max_cpc_limit: Optional max CPC limit (MAXIMIZE_CLICKS).
        feed_label: Optional Merchant Center feed label (e.g. BE).
        campaign_priority: 0 (low, default), 1 or 2 (high).
        search_partners: Also show on search partners (default off).
        start_date: Optional start date YYYY-MM-DD.
        end_date: Optional end date YYYY-MM-DD.
        status: PAUSED (default) or ENABLED.
        contains_eu_political_advertising: Required EU declaration.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    base = _common_inputs(
        name,
        daily_budget,
        bidding_strategy,
        ("MANUAL_CPC", "MAXIMIZE_CLICKS"),
        None,
        None,
        max_cpc_limit,
        start_date,
        end_date,
        status,
    )
    merchant = mh.require_simple_id(merchant_center_id, "merchant_center_id")
    locations = mh.require_ids(location_ids, "location_ids")
    cpc_micros = mh.to_micros(default_cpc, "default_cpc")
    if campaign_priority not in (0, 1, 2):
        raise ToolError("Error: 'campaign_priority' must be 0, 1 or 2.")
    label = (feed_label or "").strip() or None

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    currency = _currency(ga_client, cid)
    geo_names = ab.lookup_constants(ga_client, cid, locations, "geo")
    ids = ia.TempIds(cid)
    campaign_rn, shell_ops, campaign = _campaign_shell(
        ga_client,
        ids,
        name=base["name"],
        channel="SHOPPING",
        budget_micros=base["budget_micros"],
        bidding=base["bidding"],
        status=base["status"],
        start=base["start"],
        end=base["end"],
        eu_political=contains_eu_political_advertising,
    )
    campaign.shopping_setting.merchant_id = int(merchant)
    campaign.shopping_setting.campaign_priority = campaign_priority
    if label:
        campaign.shopping_setting.feed_label = label
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = search_partners
    campaign.network_settings.target_content_network = False

    targeting_ops, targeting_items = build_targeting_operations(
        ga_client, campaign_rn, locations, [], [], [], geo_names, {}
    )
    group_rn = ids.next("adGroups")
    group_op = ga_client.get_type("MutateOperation")
    ad_group = group_op.ad_group_operation.create
    ad_group.resource_name = group_rn
    ad_group.name = f"{base['name']} - all products"
    ad_group.campaign = campaign_rn
    ad_group.status = enums.AdGroupStatusEnum.ENABLED
    ad_group.type_ = enums.AdGroupTypeEnum.SHOPPING_PRODUCT_ADS
    ad_group.cpc_bid_micros = cpc_micros

    ad_op = ga_client.get_type("MutateOperation")
    ad_group_ad = ad_op.ad_group_ad_operation.create
    ad_group_ad.ad_group = group_rn
    ad_group_ad.status = enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad.shopping_product_ad = ga_client.get_type(
        "ShoppingProductAdInfo"
    )

    listing_op = ga_client.get_type("MutateOperation")
    listing = listing_op.ad_group_criterion_operation.create
    listing.ad_group = group_rn
    listing.status = enums.AdGroupCriterionStatusEnum.ENABLED
    listing.listing_group.type_ = enums.ListingGroupTypeEnum.UNIT
    listing.cpc_bid_micros = cpc_micros

    change_items = (
        _campaign_change_items(
            "Shopping Campaign",
            base,
            currency,
            {
                "Merchant Center": merchant
                + (f" (feed {label})" if label else ""),
                "Priority": campaign_priority,
                "Products": f"All products at {mh.format_money(cpc_micros, currency)}",
                "Search partners": "Yes" if search_partners else "No",
            },
        )
        + targeting_items
    )
    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=shell_ops
        + targeting_ops
        + [group_op, ad_op, listing_op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_shopping_campaign",
            **base,
            "merchant": merchant,
            "feed_label": label,
            "locations": locations,
            "cpc_micros": cpc_micros,
            "priority": campaign_priority,
            "search_partners": search_partners,
            "eu_political": contains_eu_political_advertising,
        },
        change_items=change_items,
        success_message=(
            f"Created Shopping campaign '{base['name']}' ({base['status']})."
            + (NEXT_STEP if base["status"] == "PAUSED" else "")
        ),
        error_prefix="Failed to create Shopping campaign",
    )
