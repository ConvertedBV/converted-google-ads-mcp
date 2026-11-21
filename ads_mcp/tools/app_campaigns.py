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

"""App campaigns: installs or in-app actions across Search, Play, YouTube,
Discover and the Display Network.

Registered on the ``campaign_builder`` namespace; same atomic,
propose-and-confirm, PAUSED-by-default approach as the other builders.
"""

from enum import Enum
import re
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import ad_builders as ab
from ads_mcp import image_assets as ia
from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.safety import enforce_budget_ceiling
from ads_mcp.tools.asset_campaigns import _currency
from ads_mcp.tools.campaign_builder import NEXT_STEP, campaign_builder_mcp
from ads_mcp.tools.campaign_management import EntityStatus
from ads_mcp.tools.campaign_settings import build_targeting_operations
import ads_mcp.utils as utils

_ANDROID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_IOS_RE = re.compile(r"^\d{6,12}$")
_VIDEO_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class AppStore(str, Enum):
    GOOGLE_APP_STORE = "GOOGLE_APP_STORE"
    APPLE_APP_STORE = "APPLE_APP_STORE"


class AppGoal(str, Enum):
    """INSTALLS optimizes for installs (optional target cost per install);
    IN_APP_ACTIONS optimizes for in-app conversions (target CPA required)."""

    INSTALLS = "INSTALLS"
    IN_APP_ACTIONS = "IN_APP_ACTIONS"


@campaign_builder_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_app_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    app_id: str,
    app_store: AppStore,
    location_ids: list[str],
    headlines: list[str],
    descriptions: list[str],
    goal: AppGoal = AppGoal.INSTALLS,
    target_cost: float | None = None,
    conversion_action_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    landscape_images: list[str] | None = None,
    square_images: list[str] | None = None,
    portrait_images: list[str] | None = None,
    youtube_video_ids: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    status: EntityStatus = EntityStatus.PAUSED,
    contains_eu_political_advertising: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Builds an App campaign (installs or in-app actions) in one step.

    Creates the budget, the campaign (app, store, goal, bidding), location
    and language targeting and one ad group with an app ad: 2-5 headlines
    (max 30), 1-5 descriptions (max 90), and optional images (asset IDs or
    https URLs: landscape 1.91:1, square 1:1, portrait 4:5) and YouTube
    videos. Google picks the placements. INSTALLS can take a target cost
    per install; IN_APP_ACTIONS needs a target CPA and the in-app
    conversion actions to optimize for (from get_conversion_setup). The
    app must be published in the store. PAUSED by default.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        name: Campaign name.
        daily_budget: Average daily budget in the account currency.
        app_id: Android package name (e.g. com.example.app) or Apple App
            Store ID (digits).
        app_store: GOOGLE_APP_STORE or APPLE_APP_STORE.
        location_ids: Geo target constant IDs (at least one).
        headlines: 2-5 headlines, max 30 characters.
        descriptions: 1-5 descriptions, max 90 characters.
        goal: INSTALLS (default) or IN_APP_ACTIONS.
        target_cost: Target cost per install (INSTALLS, optional) or per
            in-app action (IN_APP_ACTIONS, required).
        conversion_action_ids: In-app conversion actions (IN_APP_ACTIONS).
        language_ids: Language constant IDs; empty = all languages.
        landscape_images: Landscape 1.91:1 images (max 20 images in total).
        square_images: Square 1:1 images.
        portrait_images: Portrait 4:5 images.
        youtube_video_ids: Up to 20 YouTube video IDs.
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
    campaign_name = " ".join((name or "").split())
    if not campaign_name:
        raise ToolError("Error: 'name' is required.")
    store = mh.enum_value(app_store, AppStore, "app_store")
    app = (app_id or "").strip()
    if store == "GOOGLE_APP_STORE" and not _ANDROID_RE.match(app):
        raise ToolError("Error: 'app_id' must be a package name like com.x.y.")
    if store == "APPLE_APP_STORE" and not _IOS_RE.match(app):
        raise ToolError("Error: 'app_id' must be the numeric App Store ID.")
    goal_value = mh.enum_value(goal, AppGoal, "goal")
    target_micros = (
        mh.to_micros(target_cost, "target_cost") if target_cost else None
    )
    actions = (
        mh.require_ids(conversion_action_ids, "conversion_action_ids")
        if conversion_action_ids
        else []
    )
    if goal_value == "IN_APP_ACTIONS" and not (target_micros and actions):
        raise ToolError(
            "Error: IN_APP_ACTIONS needs 'target_cost' and "
            "'conversion_action_ids'."
        )
    if goal_value == "INSTALLS" and actions:
        raise ToolError(
            "Error: 'conversion_action_ids' only apply to IN_APP_ACTIONS."
        )
    budget_micros = mh.to_micros(daily_budget, "daily_budget")
    enforce_budget_ceiling(budget_micros)
    start = mh.require_date(start_date, "start_date")
    end = mh.require_date(end_date, "end_date")
    if start and end and end < start:
        raise ToolError("Error: 'end_date' must not be before 'start_date'.")
    status_value = mh.enum_value(status, EntityStatus, "status")
    locations = mh.require_ids(location_ids, "location_ids")
    languages = (
        mh.require_ids(language_ids, "language_ids") if language_ids else []
    )
    heads = ab.normalize_plain_texts(headlines, "headline", 2, 5, 30)
    descs = ab.normalize_plain_texts(descriptions, "description", 1, 5, 90)
    images = {
        "MARKETING": [
            str(r).strip() for r in landscape_images or [] if str(r).strip()
        ],
        "SQUARE": [
            str(r).strip() for r in square_images or [] if str(r).strip()
        ],
        "PORTRAIT": [
            str(r).strip() for r in portrait_images or [] if str(r).strip()
        ],
    }
    if sum(len(v) for v in images.values()) > 20:
        raise ToolError("Error: At most 20 images per app ad.")
    videos = mh.dedupe(
        [v.strip() for v in youtube_video_ids or [] if v.strip()]
    )
    if len(videos) > 20 or any(not _VIDEO_RE.match(v) for v in videos):
        raise ToolError("Error: Up to 20 YouTube video IDs of 11 characters.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    currency = _currency(ga_client, cid)
    if actions:
        found = mh.search(
            ga_client,
            cid,
            "SELECT conversion_action.id, conversion_action.name "
            "FROM conversion_action WHERE conversion_action.id IN "
            f"({', '.join(actions)})",
        )
        found_ids = {str(r.conversion_action.id) for r in found}
        missing = [a for a in actions if a not in found_ids]
        if missing:
            raise ToolError(
                f"Error: Conversion actions not found: {', '.join(missing)}"
            )
    geo_names = ab.lookup_constants(ga_client, cid, locations, "geo")
    language_names = ab.lookup_constants(ga_client, cid, languages, "language")
    ids = ia.TempIds(cid)
    pool = ia.AssetPool(ga_client, cid, ids)

    budget_rn = ids.next("campaignBudgets")
    campaign_rn = ids.next("campaigns")
    budget_op = ga_client.get_type("MutateOperation")
    budget = budget_op.campaign_budget_operation.create
    budget.resource_name = budget_rn
    budget.name = f"{campaign_name} budget"
    budget.amount_micros = budget_micros
    budget.delivery_method = enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False

    campaign_op = ga_client.get_type("MutateOperation")
    campaign = campaign_op.campaign_operation.create
    campaign.resource_name = campaign_rn
    campaign.name = campaign_name
    campaign.campaign_budget = budget_rn
    campaign.status = getattr(enums.CampaignStatusEnum, status_value)
    campaign.advertising_channel_type = (
        enums.AdvertisingChannelTypeEnum.MULTI_CHANNEL
    )
    campaign.advertising_channel_sub_type = (
        enums.AdvertisingChannelSubTypeEnum.APP_CAMPAIGN
    )
    eu = enums.EuPoliticalAdvertisingStatusEnum
    campaign.contains_eu_political_advertising = (
        eu.CONTAINS_EU_POLITICAL_ADVERTISING
        if contains_eu_political_advertising
        else eu.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    campaign.app_campaign_setting.app_id = app
    campaign.app_campaign_setting.app_store = getattr(
        enums.AppCampaignAppStoreEnum, store
    )
    goals = enums.AppCampaignBiddingStrategyGoalTypeEnum
    if goal_value == "INSTALLS" and target_micros:
        campaign.app_campaign_setting.bidding_strategy_goal_type = (
            goals.OPTIMIZE_INSTALLS_TARGET_INSTALL_COST
        )
        campaign.target_cpa.target_cpa_micros = target_micros
    elif goal_value == "INSTALLS":
        campaign.app_campaign_setting.bidding_strategy_goal_type = (
            goals.OPTIMIZE_INSTALLS_WITHOUT_TARGET_INSTALL_COST
        )
        campaign.maximize_conversions = ga_client.get_type(
            "MaximizeConversions"
        )
    else:
        campaign.app_campaign_setting.bidding_strategy_goal_type = (
            goals.OPTIMIZE_IN_APP_CONVERSIONS_TARGET_CONVERSION_COST
        )
        campaign.target_cpa.target_cpa_micros = target_micros
        campaign.selective_optimization.conversion_actions.extend(
            f"customers/{cid}/conversionActions/{a}" for a in actions
        )
    if start:
        campaign.start_date_time = f"{start} 00:00:00"
    if end:
        campaign.end_date_time = f"{end} 23:59:59"

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

    group_rn = ids.next("adGroups")
    group_op = ga_client.get_type("MutateOperation")
    ad_group = group_op.ad_group_operation.create
    ad_group.resource_name = group_rn
    ad_group.name = f"{campaign_name} - app"
    ad_group.campaign = campaign_rn
    ad_group.status = enums.AdGroupStatusEnum.ENABLED

    ad_op = ga_client.get_type("MutateOperation")
    ad_group_ad = ad_op.ad_group_ad_operation.create
    ad_group_ad.ad_group = group_rn
    ad_group_ad.status = enums.AdGroupAdStatusEnum.ENABLED
    info = ad_group_ad.ad.app_ad
    for text in heads:
        asset = ga_client.get_type("AdTextAsset")
        asset.text = text
        info.headlines.append(asset)
    for text in descs:
        asset = ga_client.get_type("AdTextAsset")
        asset.text = text
        info.descriptions.append(asset)
    for role, refs in images.items():
        for n, ref in enumerate(mh.dedupe(refs), start=1):
            image = ga_client.get_type("AdImageAsset")
            image.asset = pool.image(
                ref, role, f"{campaign_name} {role.lower()} {n}"
            )
            info.images.append(image)
    for video_id in videos:
        video = ga_client.get_type("AdVideoAsset")
        video.asset = pool.youtube(video_id)
        info.youtube_videos.append(video)

    target_text = (
        mh.format_money(target_micros, currency) if target_micros else "none"
    )
    change_items = (
        pac.build_change_items(
            operation="CREATE",
            entity_readable="App Campaign",
            primary_key_readable=campaign_name,
            fields={
                "Status": status_value,
                "Daily budget": mh.format_money(budget_micros, currency),
                "App": f"{app} ({store})",
                "Goal": goal_value,
                "Target cost": target_text,
                "Conversion actions": ", ".join(actions) or None,
                "Start": start,
                "End": end,
            },
        )
        + targeting_items
        + pac.build_change_items(
            operation="CREATE",
            entity_readable="App Ad",
            primary_key_readable=f"{campaign_name} - app",
            fields={
                "Headlines": " | ".join(heads),
                "Descriptions": " | ".join(descs),
                "Images": sum(len(v) for v in images.values()),
                "YouTube videos": ", ".join(videos) or None,
            },
        )
    )
    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=[budget_op, campaign_op]
        + targeting_ops
        + pool.operations
        + [group_op, ad_op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_app_campaign",
            "name": campaign_name,
            "budget_micros": budget_micros,
            "app_id": app,
            "app_store": store,
            "goal": goal_value,
            "target_micros": target_micros,
            "conversion_actions": actions,
            "locations": locations,
            "languages": languages,
            "headlines": heads,
            "descriptions": descs,
            "images": images,
            "videos": videos,
            "start": start,
            "end": end,
            "status": status_value,
            "eu_political": contains_eu_political_advertising,
            "image_digests": pool.image_digests,
        },
        change_items=change_items,
        success_message=(
            f"Created App campaign '{campaign_name}' ({status_value})."
            + (NEXT_STEP if status_value == "PAUSED" else "")
        ),
        error_prefix="Failed to create App campaign",
    )
