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

"""Builders for asset-based campaigns: Performance Max and Demand Gen.

Registered on the ``campaign_builder`` namespace. Each build is one atomic
GoogleAdsService.Mutate request (budget, campaign, targeting, assets,
asset groups or ad groups and ads): everything is created or nothing.
"""

from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ads_mcp import ad_builders as ab
from ads_mcp import image_assets as ia
from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_builder import (
    NEXT_STEP,
    LocationTargeting,
    campaign_builder_mcp,
)
from ads_mcp.tools.campaign_management import EntityStatus
from ads_mcp.tools.campaign_settings import build_targeting_operations
from ads_mcp.safety import enforce_budget_ceiling
import ads_mcp.utils as utils

MAX_ASSET_GROUPS = 10
MAX_DG_AD_GROUPS = 10
MAX_IMAGES_PER_ROLE = 20
MAX_SEARCH_THEMES = 25


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class AssetGroupInput(BaseModel):
    """A Performance Max asset group."""

    name: str = Field(description="Asset group name.")
    final_url: str = Field(description="Landing page URL.")
    headlines: list[str] = Field(description="3-15 headlines, max 30 chars.")
    long_headlines: list[str] = Field(
        description="1-5 long headlines, max 90 chars."
    )
    descriptions: list[str] = Field(
        description="2-5 descriptions, max 90 chars; one must be max 60."
    )
    marketing_images: list[str] = Field(
        description="Landscape 1.91:1 images: asset IDs or https URLs (1-20)."
    )
    square_marketing_images: list[str] = Field(
        description="Square 1:1 images: asset IDs or https URLs (1-20)."
    )
    portrait_marketing_images: list[str] = Field(
        default_factory=list,
        description="Optional portrait 4:5 images (asset IDs or URLs).",
    )
    youtube_video_ids: list[str] = Field(
        default_factory=list, description="Optional YouTube video IDs (max 5)."
    )
    search_themes: list[str] = Field(
        default_factory=list,
        description="Optional search themes as audience signal (max 25).",
    )
    audience_ids: list[str] = Field(
        default_factory=list,
        description="Optional audience IDs (audience resource) as signal.",
    )
    path1: str | None = Field(default=None, description="Display path 1.")
    path2: str | None = Field(default=None, description="Display path 2.")


class DemandGenAdInput(BaseModel):
    """A Demand Gen multi-asset (image) ad."""

    name: str = Field(description="Ad name.")
    final_url: str = Field(description="Landing page URL.")
    headlines: list[str] = Field(description="1-5 headlines, max 40 chars.")
    descriptions: list[str] = Field(
        description="1-5 descriptions, max 90 chars."
    )
    business_name: str = Field(description="Business name, max 25 chars.")
    logo_images: list[str] = Field(
        description="Square 1:1 logos: asset IDs or https URLs (1-5)."
    )
    marketing_images: list[str] = Field(
        default_factory=list,
        description="Landscape 1.91:1 images (asset IDs or URLs).",
    )
    square_marketing_images: list[str] = Field(
        default_factory=list,
        description="Square 1:1 images (asset IDs or URLs).",
    )
    portrait_marketing_images: list[str] = Field(
        default_factory=list,
        description="Portrait 4:5 images (asset IDs or URLs).",
    )
    call_to_action: str | None = Field(
        default=None,
        description="Optional call to action text, e.g. 'Learn more'.",
    )


class DemandGenAdGroupInput(BaseModel):
    """A Demand Gen ad group with its own targeting and ads."""

    name: str = Field(description="Ad group name.")
    location_ids: list[str] = Field(
        description="Geo target constant IDs (at least one)."
    )
    language_ids: list[str] = Field(
        default_factory=list, description="Language constant IDs."
    )
    audience_id: str | None = Field(
        default=None,
        description=(
            "Optional audience ID; without one Google's optimized targeting "
            "finds the audience."
        ),
    )
    ads: list[DemandGenAdInput] = Field(description="1-5 multi-asset ads.")


class ImageInput(BaseModel):
    """An image to upload as an asset."""

    url: str = Field(description="Public https URL of a JPEG, PNG or GIF.")
    name: str = Field(description="Asset name.")
    role: ia.ImageRole | None = Field(
        default=None,
        description=(
            "Optional intended use to validate the aspect ratio: MARKETING, "
            "SQUARE, PORTRAIT, LOGO or LANDSCAPE_LOGO."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refs(values: list[str], label: str, min_items: int) -> list[str]:
    refs = mh.dedupe([str(v).strip() for v in values or [] if str(v).strip()])
    if len(refs) < min_items or len(refs) > MAX_IMAGES_PER_ROLE:
        raise ToolError(
            f"Error: '{label}' needs {min_items}-{MAX_IMAGES_PER_ROLE} images; "
            f"got {len(refs)}."
        )
    return refs


def normalize_asset_group(group: AssetGroupInput | dict) -> dict[str, Any]:
    if isinstance(group, dict):
        group = AssetGroupInput(**group)
    name = " ".join(group.name.split())
    if not name:
        raise ToolError("Error: Every asset group needs a 'name'.")
    descriptions = ab.normalize_plain_texts(
        group.descriptions, "description", 2, 5, 90
    )
    if not any(ab.display_length(d) <= 60 for d in descriptions):
        raise ToolError(
            f"Error: Asset group '{name}' needs at least one description of "
            "max 60 characters."
        )
    videos = mh.dedupe(
        [v.strip() for v in group.youtube_video_ids if v.strip()]
    )
    if len(videos) > 5:
        raise ToolError("Error: At most 5 YouTube videos per asset group.")
    themes = mh.dedupe(
        [" ".join(t.split()) for t in group.search_themes if t.strip()]
    )
    if len(themes) > MAX_SEARCH_THEMES:
        raise ToolError(
            f"Error: At most {MAX_SEARCH_THEMES} search themes per asset group."
        )
    path1 = (group.path1 or "").strip() or None
    path2 = (group.path2 or "").strip() or None
    if any(p and len(p) > ab.RSA_PATH_MAX_CHARS for p in (path1, path2)):
        raise ToolError(
            f"Error: Display paths may have at most {ab.RSA_PATH_MAX_CHARS} "
            "characters."
        )
    if path2 and not path1:
        raise ToolError("Error: 'path2' requires 'path1'.")
    return {
        "name": name,
        "final_url": mh.require_url(group.final_url, "final_url"),
        "path1": path1,
        "path2": path2,
        "headlines": ab.normalize_plain_texts(
            group.headlines, "headline", 3, 15, 30
        ),
        "long_headlines": ab.normalize_plain_texts(
            group.long_headlines, "long headline", 1, 5, 90
        ),
        "descriptions": descriptions,
        "marketing_images": _refs(
            group.marketing_images, "marketing_images", 1
        ),
        "square_marketing_images": _refs(
            group.square_marketing_images, "square_marketing_images", 1
        ),
        "portrait_marketing_images": _refs(
            group.portrait_marketing_images, "portrait_marketing_images", 0
        ),
        "youtube_video_ids": videos,
        "search_themes": themes,
        "audience_ids": (
            mh.require_ids(group.audience_ids, "audience_ids")
            if group.audience_ids
            else []
        ),
    }


_ASSET_GROUP_FIELDS = (
    ("headlines", "HEADLINE", None),
    ("long_headlines", "LONG_HEADLINE", None),
    ("descriptions", "DESCRIPTION", None),
    ("marketing_images", "MARKETING_IMAGE", "MARKETING"),
    ("square_marketing_images", "SQUARE_MARKETING_IMAGE", "SQUARE"),
    ("portrait_marketing_images", "PORTRAIT_MARKETING_IMAGE", "PORTRAIT"),
)


def build_asset_group_operations(
    ga_client: Any,
    cid: str,
    campaign_rn: str,
    group: dict[str, Any],
    ids: ia.TempIds,
    pool: ia.AssetPool,
    status: str,
    retail: bool = False,
) -> tuple[list[Any], list[pac.ChangeItem]]:
    """Builds the asset group, its asset links and its signals.

    With `retail`, adds the root listing group filter that includes all
    Merchant Center products, which retail asset groups need to serve them.
    """
    enums = ga_client.enums
    operations: list[Any] = []
    group_rn = ids.next("assetGroups")

    op = ga_client.get_type("MutateOperation")
    asset_group = op.asset_group_operation.create
    asset_group.resource_name = group_rn
    asset_group.name = group["name"]
    asset_group.campaign = campaign_rn
    asset_group.final_urls.append(group["final_url"])
    asset_group.status = getattr(enums.AssetGroupStatusEnum, status)
    if group["path1"]:
        asset_group.path1 = group["path1"]
    if group["path2"]:
        asset_group.path2 = group["path2"]
    operations.append(op)

    def _link(asset_rn: str, field_type: str) -> None:
        link_op = ga_client.get_type("MutateOperation")
        link = link_op.asset_group_asset_operation.create
        link.asset_group = group_rn
        link.asset = asset_rn
        link.field_type = getattr(enums.AssetFieldTypeEnum, field_type)
        operations.append(link_op)

    for key, field_type, role in _ASSET_GROUP_FIELDS:
        for index, value in enumerate(group[key], start=1):
            if role:
                asset_rn = pool.image(
                    value, role, f"{group['name']} {field_type.lower()} {index}"
                )
            else:
                asset_rn = pool.text(value)
            _link(asset_rn, field_type)
    for video_id in group["youtube_video_ids"]:
        _link(pool.youtube(video_id), "YOUTUBE_VIDEO")

    for theme in group["search_themes"]:
        signal_op = ga_client.get_type("MutateOperation")
        signal = signal_op.asset_group_signal_operation.create
        signal.asset_group = group_rn
        signal.search_theme.text = theme
        operations.append(signal_op)
    for audience_id in group["audience_ids"]:
        signal_op = ga_client.get_type("MutateOperation")
        signal = signal_op.asset_group_signal_operation.create
        signal.asset_group = group_rn
        signal.audience.audience = f"customers/{cid}/audiences/{audience_id}"
        operations.append(signal_op)
    if retail:
        filter_op = ga_client.get_type("MutateOperation")
        listing = filter_op.asset_group_listing_group_filter_operation.create
        listing.asset_group = group_rn
        listing.type_ = enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
        listing.listing_source = (
            enums.ListingGroupFilterListingSourceEnum.SHOPPING
        )
        operations.append(filter_op)

    def _images(key: str) -> str | None:
        return f"{len(group[key])} image(s)" if group[key] else None

    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Asset Group",
        primary_key_readable=group["name"],
        fields={
            "Status": status,
            "Final URL": group["final_url"],
            "Headlines": " | ".join(group["headlines"]),
            "Long headlines": " | ".join(group["long_headlines"]),
            "Descriptions": " | ".join(group["descriptions"]),
            "Landscape images": _images("marketing_images"),
            "Square images": _images("square_marketing_images"),
            "Portrait images": _images("portrait_marketing_images"),
            "YouTube videos": ", ".join(group["youtube_video_ids"]) or None,
            "Search themes": ", ".join(group["search_themes"]) or None,
            "Audience signals": ", ".join(group["audience_ids"]) or None,
        },
    )
    return operations, change_items


def _campaign_shell(
    ga_client: Any,
    ids: ia.TempIds,
    *,
    name: str,
    channel: str,
    budget_micros: int,
    bidding: dict[str, Any],
    status: str,
    start: str | None,
    end: str | None,
    eu_political: bool,
) -> tuple[str, list[Any], Any]:
    """Creates budget + campaign operations; returns (campaign rn, ops, msg)."""
    enums = ga_client.enums
    budget_rn = ids.next("campaignBudgets")
    campaign_rn = ids.next("campaigns")

    budget_op = ga_client.get_type("MutateOperation")
    budget = budget_op.campaign_budget_operation.create
    budget.resource_name = budget_rn
    budget.name = f"{name} budget"
    budget.amount_micros = budget_micros
    budget.delivery_method = enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False

    campaign_op = ga_client.get_type("MutateOperation")
    campaign = campaign_op.campaign_operation.create
    campaign.resource_name = campaign_rn
    campaign.name = name
    campaign.campaign_budget = budget_rn
    campaign.advertising_channel_type = getattr(
        enums.AdvertisingChannelTypeEnum, channel
    )
    campaign.status = getattr(enums.CampaignStatusEnum, status)
    eu = enums.EuPoliticalAdvertisingStatusEnum
    campaign.contains_eu_political_advertising = (
        eu.CONTAINS_EU_POLITICAL_ADVERTISING
        if eu_political
        else eu.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    if start:
        campaign.start_date_time = f"{start} 00:00:00"
    if end:
        campaign.end_date_time = f"{end} 23:59:59"
    ab.apply_bidding(ga_client, campaign, bidding)
    return campaign_rn, [budget_op, campaign_op], campaign


def _common_inputs(
    name: str,
    daily_budget: float,
    bidding_strategy: Any,
    allowed: tuple[str, ...],
    target_cpa: float | None,
    target_roas_percent: float | None,
    max_cpc_limit: float | None,
    start_date: str | None,
    end_date: str | None,
    status: Any,
) -> dict[str, Any]:
    campaign_name = " ".join((name or "").split())
    if not campaign_name:
        raise ToolError("Error: 'name' is required.")
    bidding = ab.resolve_bidding(
        bidding_strategy, target_cpa, target_roas_percent, max_cpc_limit
    )
    if bidding["strategy"] not in allowed:
        raise ToolError(
            f"Error: This campaign type supports {', '.join(allowed)} bidding."
        )
    start = mh.require_date(start_date, "start_date")
    end = mh.require_date(end_date, "end_date")
    if start and end and end < start:
        raise ToolError("Error: 'end_date' must not be before 'start_date'.")
    budget_micros = mh.to_micros(daily_budget, "daily_budget")
    enforce_budget_ceiling(budget_micros)
    return {
        "name": campaign_name,
        "budget_micros": budget_micros,
        "bidding": bidding,
        "start": start,
        "end": end,
        "status": mh.enum_value(status, EntityStatus, "status"),
    }


def _currency(ga_client: Any, cid: str) -> str:
    rows = mh.search(
        ga_client, cid, "SELECT customer.currency_code FROM customer"
    )
    return rows[0].customer.currency_code if rows else ""


def _campaign_change_items(
    kind: str, base: dict[str, Any], currency: str, extra: dict[str, Any]
) -> list[pac.ChangeItem]:
    return pac.build_change_items(
        operation="CREATE",
        entity_readable=kind,
        primary_key_readable=base["name"],
        fields={
            "Status": base["status"],
            "Daily budget": mh.format_money(base["budget_micros"], currency),
            "Bidding": ab.describe_bidding(base["bidding"], currency),
            "Start": base["start"],
            "End": base["end"],
            **extra,
        },
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@campaign_builder_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_image_assets(
    customer_id: str | int,
    images: list[ImageInput],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Uploads images from public https URLs as image assets.

    Use this to prepare images (e.g. from the client's website) before
    building or for reuse; the PMax and Demand Gen builders also accept URLs
    directly. With a `role`, the aspect ratio and minimum size are checked
    (MARKETING 1.91:1, SQUARE 1:1, PORTRAIT 4:5, LOGO 1:1, LANDSCAPE_LOGO
    4:1). JPEG, PNG and GIF up to 5 MB, max 10 per call.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        images: Images, each {{"url", "name", "role"}}.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    inputs = [ImageInput(**i) if isinstance(i, dict) else i for i in images]
    if not 1 <= len(inputs) <= 10:
        raise ToolError("Error: Pass 1-10 images.")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    operations = []
    change_items: list[pac.ChangeItem] = []
    digests = []
    for item in inputs:
        name = " ".join(item.name.split())
        if not name:
            raise ToolError("Error: Every image needs a 'name'.")
        image = ia.fetch_image(item.url)
        role = (
            mh.enum_value(item.role, ia.ImageRole, "role")
            if item.role
            else None
        )
        if role:
            ia.check_role(image.width, image.height, role, item.url)
        op = ga_client.get_type("AssetOperation")
        op.create.name = name
        op.create.image_asset.data = image.data
        op.create.image_asset.mime_type = getattr(
            ga_client.enums.MimeTypeEnum, image.mime
        )
        operations.append(op)
        digests.append({"url": item.url, "name": name, "sha256": image.sha256})
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Image Asset",
                name,
                "Image",
                f"{image.width}x{image.height} {image.mime.removeprefix('IMAGE_')}"
                + (f" ({role})" if role else ""),
            )
        )
    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="AssetService",
        request_type="MutateAssetsRequest",
        mutate_method="mutate_assets",
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={"tool": "create_image_assets", "images": digests},
        change_items=change_items,
        success_message=(
            f"Uploaded {len(operations)} images (asset IDs in input order)."
        ),
        error_prefix="Failed to upload images",
    )


@campaign_builder_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_performance_max_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    bidding_strategy: ab.BiddingStrategy,
    location_ids: list[str],
    business_name: str,
    logo_image: str,
    asset_groups: list[AssetGroupInput],
    language_ids: list[str] | None = None,
    excluded_location_ids: list[str] | None = None,
    target_cpa: float | None = None,
    target_roas_percent: float | None = None,
    landscape_logo_image: str | None = None,
    merchant_center_id: str | None = None,
    feed_label: str | None = None,
    location_targeting: LocationTargeting = LocationTargeting.PRESENCE,
    start_date: str | None = None,
    end_date: str | None = None,
    status: EntityStatus = EntityStatus.PAUSED,
    contains_eu_political_advertising: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Builds a complete Performance Max campaign in one atomic step.

    Creates the budget, the campaign (brand guidelines on: business name and
    logo at campaign level), location/language targeting, and 1-10 asset
    groups with texts, images, optional YouTube videos and audience signals
    (search themes, audiences). PAUSED by default for review.

    Images are asset IDs or public https URLs (fetched and checked: landscape
    1.91:1, square 1:1, portrait 4:5, logo 1:1). Check get_conversion_setup
    first: PMax only supports MAXIMIZE_CONVERSIONS (optional target CPA) and
    MAXIMIZE_CONVERSION_VALUE (optional target ROAS).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        name: Campaign name.
        daily_budget: Average daily budget in the account currency.
        bidding_strategy: MAXIMIZE_CONVERSIONS or MAXIMIZE_CONVERSION_VALUE.
        location_ids: Geo target constant IDs (at least one).
        business_name: Business name (max 25 characters).
        logo_image: Square logo: asset ID or https URL.
        asset_groups: 1-10 asset groups.
        language_ids: Language constant IDs; empty = all languages.
        excluded_location_ids: Geo target constant IDs to exclude.
        target_cpa: Optional target CPA (MAXIMIZE_CONVERSIONS).
        target_roas_percent: Optional target ROAS in percent.
        landscape_logo_image: Optional 4:1 logo: asset ID or https URL.
        merchant_center_id: Optional linked Merchant Center ID for a retail
            (shopping) PMax campaign; every asset group then includes all
            products.
        feed_label: Optional Merchant Center feed label (e.g. BE).
        location_targeting: PRESENCE (default) or PRESENCE_OR_INTEREST.
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
        ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"),
        target_cpa,
        target_roas_percent,
        None,
        start_date,
        end_date,
        status,
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
    brand = ab.normalize_plain_texts(
        [business_name], "business name", 1, 1, 25
    )[0]
    merchant = (
        mh.require_simple_id(merchant_center_id, "merchant_center_id")
        if merchant_center_id
        else None
    )
    label = (feed_label or "").strip() or None
    if label and not merchant:
        raise ToolError("Error: 'feed_label' needs 'merchant_center_id'.")
    targeting_value = mh.enum_value(
        location_targeting, LocationTargeting, "location_targeting"
    )
    if not asset_groups or len(asset_groups) > MAX_ASSET_GROUPS:
        raise ToolError(f"Error: Pass 1-{MAX_ASSET_GROUPS} asset groups.")
    groups = [normalize_asset_group(g) for g in asset_groups]
    if len({g["name"].lower() for g in groups}) != len(groups):
        raise ToolError("Error: Asset group names must be unique.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency = _currency(ga_client, cid)
    geo_names = ab.lookup_constants(
        ga_client, cid, mh.dedupe(locations + excluded), "geo"
    )
    language_names = ab.lookup_constants(ga_client, cid, languages, "language")

    ids = ia.TempIds(cid)
    pool = ia.AssetPool(ga_client, cid, ids)
    campaign_rn, shell_ops, campaign = _campaign_shell(
        ga_client,
        ids,
        name=base["name"],
        channel="PERFORMANCE_MAX",
        budget_micros=base["budget_micros"],
        bidding=base["bidding"],
        status=base["status"],
        start=base["start"],
        end=base["end"],
        eu_political=contains_eu_political_advertising,
    )
    campaign.brand_guidelines_enabled = True
    if merchant:
        campaign.shopping_setting.merchant_id = int(merchant)
        if label:
            campaign.shopping_setting.feed_label = label
    campaign.geo_target_type_setting.positive_geo_target_type = getattr(
        ga_client.enums.PositiveGeoTargetTypeEnum, targeting_value
    )

    targeting_ops, targeting_items = build_targeting_operations(
        ga_client,
        campaign_rn,
        locations,
        excluded,
        languages,
        [],
        geo_names,
        language_names,
    )

    # Brand guidelines: business name and logos are campaign-level assets.
    brand_assets = [
        (pool.text(brand), "BUSINESS_NAME"),
        (pool.image(logo_image, "LOGO", f"{brand} logo"), "LOGO"),
    ]
    if landscape_logo_image:
        brand_assets.append(
            (
                pool.image(
                    landscape_logo_image,
                    "LANDSCAPE_LOGO",
                    f"{brand} landscape logo",
                ),
                "LANDSCAPE_LOGO",
            )
        )
    brand_links = []
    for asset_rn, field_type in brand_assets:
        link_op = ga_client.get_type("MutateOperation")
        link = link_op.campaign_asset_operation.create
        link.campaign = campaign_rn
        link.asset = asset_rn
        link.field_type = getattr(
            ga_client.enums.AssetFieldTypeEnum, field_type
        )
        brand_links.append(link_op)

    group_ops: list[Any] = []
    change_items = _campaign_change_items(
        "Performance Max Campaign",
        base,
        currency,
        {
            "Business name": brand,
            "Merchant Center": (
                f"{merchant}" + (f" (feed {label})" if label else "")
                if merchant
                else None
            ),
            "Logo": logo_image,
            "Location targeting": targeting_value,
            "Languages": None if languages else "All languages",
            "EU political advertising": (
                "Yes" if contains_eu_political_advertising else "No"
            ),
        },
    )
    change_items.extend(targeting_items)
    for group in groups:
        ops, items = build_asset_group_operations(
            ga_client,
            cid,
            campaign_rn,
            group,
            ids,
            pool,
            "ENABLED",
            retail=bool(merchant),
        )
        group_ops.extend(ops)
        change_items.extend(items)

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=(
            shell_ops
            + targeting_ops
            + pool.operations
            + brand_links
            + group_ops
        ),
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_performance_max_campaign",
            **base,
            "locations": locations,
            "excluded_locations": excluded,
            "languages": languages,
            "business_name": brand,
            "merchant_center_id": merchant,
            "feed_label": label,
            "logo": logo_image,
            "landscape_logo": landscape_logo_image,
            "location_targeting": targeting_value,
            "eu_political": contains_eu_political_advertising,
            "asset_groups": groups,
            "image_digests": pool.image_digests,
        },
        change_items=change_items,
        success_message=(
            f"Created Performance Max campaign '{base['name']}' "
            f"({base['status']}) with {len(groups)} asset groups."
            + (NEXT_STEP if base["status"] == "PAUSED" else "")
        ),
        error_prefix="Failed to create Performance Max campaign",
    )


@campaign_builder_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_asset_group(
    customer_id: str | int,
    campaign_id: str,
    asset_group: AssetGroupInput,
    status: EntityStatus = EntityStatus.ENABLED,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds an asset group (texts, images, videos, signals) to a PMax campaign.

    Created atomically with all its assets. Images are asset IDs or public
    https URLs (checked for the right aspect ratio).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_id: The numeric ID of a Performance Max campaign.
        asset_group: The asset group definition.
        status: ENABLED (default) or PAUSED.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaign_id = mh.require_simple_id(campaign_id, "campaign_id")
    status_value = mh.enum_value(status, EntityStatus, "status")
    group = normalize_asset_group(asset_group)

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign.name, campaign.advertising_channel_type, "
        "campaign.shopping_setting.merchant_id FROM campaign "
        f"WHERE campaign.id = {campaign_id}",
    )
    if not rows:
        raise ToolError(f"Error: Campaign {campaign_id} was not found.")
    if rows[0].campaign.advertising_channel_type.name != "PERFORMANCE_MAX":
        raise ToolError(
            f"Error: Campaign {campaign_id} is not a Performance Max campaign."
        )
    campaign_rn = f"customers/{cid}/campaigns/{campaign_id}"
    ids = ia.TempIds(cid)
    pool = ia.AssetPool(ga_client, cid, ids)
    group_ops, change_items = build_asset_group_operations(
        ga_client,
        cid,
        campaign_rn,
        group,
        ids,
        pool,
        status_value,
        retail=bool(rows[0].campaign.shopping_setting.merchant_id),
    )
    change_items.insert(
        0,
        pac.ChangeItem(
            "CREATE",
            "Target campaign",
            campaign_rn,
            "Campaign",
            rows[0].campaign.name,
        ),
    )
    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=pool.operations + group_ops,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_asset_group",
            "campaign_id": campaign_id,
            "status": status_value,
            "asset_group": group,
            "image_digests": pool.image_digests,
        },
        change_items=change_items,
        success_message=(
            f"Created asset group '{group['name']}' in campaign {campaign_id}."
        ),
        error_prefix="Failed to create asset group",
    )


def _normalize_dg_ad(ad: DemandGenAdInput | dict) -> dict[str, Any]:
    if isinstance(ad, dict):
        ad = DemandGenAdInput(**ad)
    name = " ".join(ad.name.split())
    if not name:
        raise ToolError("Error: Every Demand Gen ad needs a 'name'.")
    images = {
        "marketing_images": _refs(ad.marketing_images, "marketing_images", 0),
        "square_marketing_images": _refs(
            ad.square_marketing_images, "square_marketing_images", 0
        ),
        "portrait_marketing_images": _refs(
            ad.portrait_marketing_images, "portrait_marketing_images", 0
        ),
    }
    if not (images["marketing_images"] or images["square_marketing_images"]):
        raise ToolError(
            f"Error: Ad '{name}' needs at least one landscape or square image."
        )
    logos = _refs(ad.logo_images, "logo_images", 1)
    if len(logos) > 5:
        raise ToolError("Error: At most 5 logos per ad.")
    return {
        "name": name,
        "final_url": mh.require_url(ad.final_url, "final_url"),
        "headlines": ab.normalize_plain_texts(
            ad.headlines, "headline", 1, 5, 40
        ),
        "descriptions": ab.normalize_plain_texts(
            ad.descriptions, "description", 1, 5, 90
        ),
        "business_name": ab.normalize_plain_texts(
            [ad.business_name], "business name", 1, 1, 25
        )[0],
        "logo_images": logos,
        "call_to_action": (ad.call_to_action or "").strip() or None,
        **images,
    }


def _normalize_dg_group(group: DemandGenAdGroupInput | dict) -> dict[str, Any]:
    if isinstance(group, dict):
        group = DemandGenAdGroupInput(**group)
    name = " ".join(group.name.split())
    if not name:
        raise ToolError("Error: Every ad group needs a 'name'.")
    if not 1 <= len(group.ads) <= 5:
        raise ToolError(f"Error: Ad group '{name}' needs 1-5 ads.")
    return {
        "name": name,
        "location_ids": mh.require_ids(group.location_ids, "location_ids"),
        "language_ids": (
            mh.require_ids(group.language_ids, "language_ids")
            if group.language_ids
            else []
        ),
        "audience_id": (
            mh.require_simple_id(group.audience_id, "audience_id")
            if group.audience_id
            else None
        ),
        "ads": [_normalize_dg_ad(ad) for ad in group.ads],
    }


_DG_IMAGE_FIELDS = (
    ("marketing_images", "marketing_images", "MARKETING"),
    ("square_marketing_images", "square_marketing_images", "SQUARE"),
    ("portrait_marketing_images", "portrait_marketing_images", "PORTRAIT"),
    ("logo_images", "logo_images", "LOGO"),
)


@campaign_builder_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_demand_gen_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    bidding_strategy: ab.BiddingStrategy,
    ad_groups: list[DemandGenAdGroupInput],
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
    """Builds a complete Demand Gen campaign with image ads in one step.

    Creates the budget, the campaign (ad-group-level targeting enabled), and
    1-10 ad groups, each with its own locations/languages, an optional
    audience (without one, optimized targeting is used) and 1-5 multi-asset
    image ads. PAUSED by default. Images are asset IDs or public https URLs
    (checked: landscape 1.91:1, square 1:1, portrait 4:5, logo 1:1).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        name: Campaign name.
        daily_budget: Average daily budget in the account currency.
        bidding_strategy: MAXIMIZE_CONVERSIONS, MAXIMIZE_CONVERSION_VALUE or
            MAXIMIZE_CLICKS.
        ad_groups: 1-10 ad groups with targeting and ads.
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
        ),
        target_cpa,
        target_roas_percent,
        max_cpc_limit,
        start_date,
        end_date,
        status,
    )
    if not ad_groups or len(ad_groups) > MAX_DG_AD_GROUPS:
        raise ToolError(f"Error: Pass 1-{MAX_DG_AD_GROUPS} ad groups.")
    groups = [_normalize_dg_group(g) for g in ad_groups]
    if len({g["name"].lower() for g in groups}) != len(groups):
        raise ToolError("Error: Ad group names must be unique.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    currency = _currency(ga_client, cid)
    geo_names = ab.lookup_constants(
        ga_client,
        cid,
        mh.dedupe([g for group in groups for g in group["location_ids"]]),
        "geo",
    )
    language_names = ab.lookup_constants(
        ga_client,
        cid,
        mh.dedupe([lang for group in groups for lang in group["language_ids"]]),
        "language",
    )

    ids = ia.TempIds(cid)
    pool = ia.AssetPool(ga_client, cid, ids)
    campaign_rn, shell_ops, campaign = _campaign_shell(
        ga_client,
        ids,
        name=base["name"],
        channel="DEMAND_GEN",
        budget_micros=base["budget_micros"],
        bidding=base["bidding"],
        status=base["status"],
        start=base["start"],
        end=base["end"],
        eu_political=contains_eu_political_advertising,
    )
    campaign.demand_gen_campaign_settings.upgraded_targeting = True

    change_items = _campaign_change_items(
        "Demand Gen Campaign",
        base,
        currency,
        {
            "EU political advertising": (
                "Yes" if contains_eu_political_advertising else "No"
            )
        },
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
        group_ops.append(op)

        def _criterion() -> Any:
            crit_op = ga_client.get_type("MutateOperation")
            criterion = crit_op.ad_group_criterion_operation.create
            criterion.ad_group = group_rn
            group_ops.append(crit_op)
            return criterion

        for loc in group["location_ids"]:
            _criterion().location.geo_target_constant = (
                f"geoTargetConstants/{loc}"
            )
        for lang in group["language_ids"]:
            _criterion().language.language_constant = (
                f"languageConstants/{lang}"
            )
        if group["audience_id"]:
            _criterion().audience.audience = (
                f"customers/{cid}/audiences/{group['audience_id']}"
            )
        change_items.extend(
            pac.build_change_items(
                operation="CREATE",
                entity_readable="Demand Gen Ad Group",
                primary_key_readable=group["name"],
                fields={
                    "Locations": ", ".join(
                        f"{geo_names[g]} ({g})" for g in group["location_ids"]
                    ),
                    "Languages": ", ".join(
                        f"{language_names[g]} ({g})"
                        for g in group["language_ids"]
                    )
                    or "All languages",
                    "Audience": group["audience_id"] or "Optimized targeting",
                },
            )
        )

        for ad in group["ads"]:
            ad_op = ga_client.get_type("MutateOperation")
            ad_group_ad = ad_op.ad_group_ad_operation.create
            ad_group_ad.ad_group = group_rn
            ad_group_ad.status = enums.AdGroupAdStatusEnum.ENABLED
            ad_message = ad_group_ad.ad
            ad_message.name = ad["name"]
            ad_message.final_urls.append(ad["final_url"])
            info = ad_message.demand_gen_multi_asset_ad
            for text in ad["headlines"]:
                headline = ga_client.get_type("AdTextAsset")
                headline.text = text
                info.headlines.append(headline)
            for text in ad["descriptions"]:
                description = ga_client.get_type("AdTextAsset")
                description.text = text
                info.descriptions.append(description)
            info.business_name = ad["business_name"]
            if ad["call_to_action"]:
                info.call_to_action_text = ad["call_to_action"]
            for key, field, role in _DG_IMAGE_FIELDS:
                for index, ref in enumerate(ad[key], start=1):
                    image = ga_client.get_type("AdImageAsset")
                    image.asset = pool.image(
                        ref, role, f"{ad['name']} {role.lower()} {index}"
                    )
                    getattr(info, field).append(image)
            group_ops.append(ad_op)
            change_items.extend(
                pac.build_change_items(
                    operation="CREATE",
                    entity_readable="Demand Gen Image Ad",
                    primary_key_readable=f"{group['name']} > {ad['name']}",
                    fields={
                        "Final URL": ad["final_url"],
                        "Headlines": " | ".join(ad["headlines"]),
                        "Descriptions": " | ".join(ad["descriptions"]),
                        "Business name": ad["business_name"],
                        "Call to action": ad["call_to_action"],
                        "Images": (
                            f"{len(ad['marketing_images'])} landscape, "
                            f"{len(ad['square_marketing_images'])} square, "
                            f"{len(ad['portrait_marketing_images'])} portrait, "
                            f"{len(ad['logo_images'])} logo"
                        ),
                    },
                )
            )

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=shell_ops + pool.operations + group_ops,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_demand_gen_campaign",
            **base,
            "eu_political": contains_eu_political_advertising,
            "ad_groups": groups,
            "image_digests": pool.image_digests,
        },
        change_items=change_items,
        success_message=(
            f"Created Demand Gen campaign '{base['name']}' ({base['status']}) "
            f"with {len(groups)} ad groups."
            + (NEXT_STEP if base["status"] == "PAUSED" else "")
        ),
        error_prefix="Failed to create Demand Gen campaign",
    )
