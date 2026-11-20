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

"""Replace one ad text everywhere it is used: Search, PMax and Demand Gen.

Registered on the ``campaign_management`` namespace. Only the matching
headline or description changes; every other text, pin and dynamic
insertion stays exactly as it is.
"""

from collections import Counter
from enum import Enum
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import ad_builders as ab
from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_management import campaign_management_mcp
import ads_mcp.utils as utils

MAX_REPLACEMENTS_PER_CALL = 200


class AdChannel(str, Enum):
    """Placements in which ad text can be replaced."""

    SEARCH = "SEARCH"
    PERFORMANCE_MAX = "PERFORMANCE_MAX"
    DEMAND_GEN = "DEMAND_GEN"


# Ad type -> (channel, Ad field, {text list field: (label, max chars)}).
_AD_TYPES: dict[str, tuple[str, str, dict[str, tuple[str, int]]]] = {
    "RESPONSIVE_SEARCH_AD": (
        "SEARCH",
        "responsive_search_ad",
        {"headlines": ("Headline", 30), "descriptions": ("Description", 90)},
    ),
    "DEMAND_GEN_MULTI_ASSET_AD": (
        "DEMAND_GEN",
        "demand_gen_multi_asset_ad",
        {"headlines": ("Headline", 40), "descriptions": ("Description", 90)},
    ),
    "DEMAND_GEN_VIDEO_RESPONSIVE_AD": (
        "DEMAND_GEN",
        "demand_gen_video_responsive_ad",
        {
            "headlines": ("Headline", 40),
            "long_headlines": ("Long headline", 90),
            "descriptions": ("Description", 90),
        },
    ),
}

# Asset group field type -> (max chars, max assets per asset group).
_ASSET_GROUP_FIELDS = {
    "HEADLINE": (30, 15),
    "LONG_HEADLINE": (90, 5),
    "DESCRIPTION": (90, 5),
}


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _ad_query(types: list[str], campaign_filter: str) -> str:
    fields = ["campaign.name", "ad_group.name", "ad_group_ad.ad.id"]
    fields.append("ad_group_ad.ad.type")
    for _, (_, ad_field, lists) in _AD_TYPES.items():
        fields.extend(f"ad_group_ad.ad.{ad_field}.{name}" for name in lists)
    return (
        f"SELECT {', '.join(fields)} FROM ad_group_ad "
        "WHERE ad_group_ad.status != 'REMOVED' "
        f"AND ad_group_ad.ad.type IN ({mh.quoted_list(types)})"
        f"{campaign_filter}"
    )


def _asset_group_query(campaign_filter: str) -> str:
    return (
        "SELECT campaign.name, asset_group.name, "
        "asset_group_asset.resource_name, asset_group_asset.asset_group, "
        "asset_group_asset.field_type, asset.text_asset.text "
        "FROM asset_group_asset WHERE asset.type = 'TEXT' "
        "AND asset_group_asset.status != 'REMOVED' "
        "AND asset_group_asset.field_type IN "
        f"({mh.quoted_list(list(_ASSET_GROUP_FIELDS))})"
        f"{campaign_filter}"
    )


def _copy_text_asset(ga_client: Any, source: Any, text: str) -> Any:
    """Copies only the writable parts (text and pin) of an AdTextAsset."""
    asset = ga_client.get_type("AdTextAsset")
    asset.text = text
    if source.pinned_field:
        asset.pinned_field = source.pinned_field
    return asset


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def replace_ad_text(
    customer_id: str | int,
    old_text: str,
    new_text: str,
    campaign_ids: list[str] | None = None,
    channels: list[AdChannel] | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Replaces one headline or description text wherever it is used.

    Finds the exact text (case and spacing ignored) in responsive search ads,
    Performance Max asset groups (headlines, long headlines, descriptions)
    and Demand Gen ads, and replaces only that text. All other texts, pins
    and dynamic insertions such as {{KeyWord:...}} stay untouched.
    - Search and Demand Gen ads are edited in place (they go through policy
      review again but keep their statistics).
    - PMax text assets cannot be edited, so a new text asset is created,
      linked in the same field, and the old link is removed.
    Everything happens in one atomic request. If the API rejects one
    placement type, nothing changes; rerun with `channels` limited. Limits:
    Search headlines 30 characters, Demand Gen headlines 40, PMax headlines
    30, long headlines and descriptions 90; dynamic insertions count as their
    default text.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        old_text: The current text to replace.
        new_text: The replacement text.
        campaign_ids: Optional campaign IDs to limit the search to.
        channels: Optional subset of SEARCH, PERFORMANCE_MAX, DEMAND_GEN
            (default: all three).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    old_norm = _norm(old_text)
    replacement = " ".join((new_text or "").split())
    if not old_norm or not replacement:
        raise ToolError("Error: 'old_text' and 'new_text' are required.")
    if _norm(replacement) == old_norm:
        raise ToolError("Error: 'new_text' is the same as 'old_text'.")
    campaigns = (
        mh.require_ids(campaign_ids, "campaign_ids") if campaign_ids else []
    )
    selected = (
        [mh.enum_value(c, AdChannel, "channels") for c in channels]
        if channels
        else [c.value for c in AdChannel]
    )
    campaign_filter = (
        f" AND campaign.id IN ({', '.join(campaigns)})" if campaigns else ""
    )
    new_length = ab.display_length(replacement)

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    ad_ops: list[Any] = []
    asset_ops: list[Any] = []
    change_items: list[pac.ChangeItem] = []
    problems: list[str] = []
    targets: list[str] = []

    # -- Search and Demand Gen ads (edited in place) -------------------------
    ad_types = [t for t, spec in _AD_TYPES.items() if spec[0] in selected]
    if ad_types:
        for row in mh.search(
            ga_client, cid, _ad_query(ad_types, campaign_filter)
        ):
            ad = row.ad_group_ad.ad
            _, ad_field, lists = _AD_TYPES[ad.type_.name]
            current = getattr(ad, ad_field)
            touched = [
                name
                for name in lists
                if any(
                    _norm(a.text) == old_norm for a in getattr(current, name)
                )
            ]
            if not touched:
                continue
            rn = f"customers/{cid}/ads/{ad.id}"
            where = f"{row.campaign.name} > {row.ad_group.name} (ad {ad.id})"
            mutate_op = ga_client.get_type("MutateOperation")
            update = mutate_op.ad_operation.update
            update.resource_name = rn
            for name in touched:
                label, limit = lists[name]
                texts = getattr(current, name)
                if new_length > limit:
                    problems.append(
                        f"{where}: {label.lower()} max {limit} characters"
                    )
                if any(_norm(a.text) == _norm(replacement) for a in texts):
                    problems.append(f"{where}: already has '{replacement}'")
                for asset in texts:
                    is_match = _norm(asset.text) == old_norm
                    getattr(getattr(update, ad_field), name).append(
                        _copy_text_asset(
                            ga_client,
                            asset,
                            replacement if is_match else asset.text,
                        )
                    )
                    if is_match:
                        change_items.append(
                            pac.ChangeItem(
                                "UPDATE",
                                ad.type_.name.replace("_", " ").title(),
                                where,
                                label,
                                replacement,
                                asset.text,
                            )
                        )
                mutate_op.ad_operation.update_mask.paths.append(
                    f"{ad_field}.{name}"
                )
            ad_ops.append(mutate_op)
            targets.append(rn)

    # -- Performance Max asset groups (relink a new text asset) --------------
    if "PERFORMANCE_MAX" in selected:
        rows = mh.search(ga_client, cid, _asset_group_query(campaign_filter))
        counts = Counter(
            (
                r.asset_group_asset.asset_group,
                r.asset_group_asset.field_type.name,
            )
            for r in rows
        )
        matches = [
            r for r in rows if _norm(r.asset.text_asset.text) == old_norm
        ]
        if matches:
            new_asset_rn = f"customers/{cid}/assets/-1"
            create_asset = ga_client.get_type("MutateOperation")
            create_asset.asset_operation.create.resource_name = new_asset_rn
            create_asset.asset_operation.create.text_asset.text = replacement
            asset_ops.append(create_asset)
        for row in matches:
            link = row.asset_group_asset
            field = link.field_type.name
            limit, max_count = _ASSET_GROUP_FIELDS[field]
            where = f"{row.campaign.name} > {row.asset_group.name}"
            if new_length > limit:
                problems.append(
                    f"{where}: {field.lower()} max {limit} characters"
                )
            add = ga_client.get_type("MutateOperation")
            add.asset_group_asset_operation.create.asset_group = (
                link.asset_group
            )
            add.asset_group_asset_operation.create.asset = new_asset_rn
            add.asset_group_asset_operation.create.field_type = link.field_type
            remove = ga_client.get_type("MutateOperation")
            remove.asset_group_asset_operation.remove = link.resource_name
            # A full asset group must drop the old text before adding the new
            # one; otherwise add first so the group never drops below its
            # minimum.
            if counts[(link.asset_group, field)] >= max_count:
                asset_ops.extend([remove, add])
            else:
                asset_ops.extend([add, remove])
            change_items.append(
                pac.ChangeItem(
                    "UPDATE",
                    "Performance Max asset group",
                    where,
                    field.replace("_", " ").title(),
                    replacement,
                    row.asset.text_asset.text,
                )
            )
            targets.append(link.resource_name)

    if not targets:
        raise ToolError(
            f"Error: '{old_text}' was not found in the selected channels "
            f"({', '.join(selected)})"
            + (f" of campaigns {', '.join(campaigns)}." if campaigns else ".")
        )
    if len(targets) > MAX_REPLACEMENTS_PER_CALL:
        raise ToolError(
            f"Error: {len(targets)} placements match; at most "
            f"{MAX_REPLACEMENTS_PER_CALL} per call. Limit with campaign_ids."
        )
    if problems:
        raise ToolError(
            f"Error: '{replacement}' ({new_length} characters) cannot be used "
            "everywhere:\n- "
            + "\n- ".join(problems)
            + "\nUse a shorter text, or limit 'channels' / 'campaign_ids'."
        )

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=asset_ops + ad_ops,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "replace_ad_text",
            "old_text": old_norm,
            "new_text": replacement,
            "campaign_ids": campaigns,
            "channels": selected,
            "targets": sorted(targets),
        },
        change_items=change_items,
        success_message=(
            f"Replaced the text in {len(targets)} ads and asset groups."
        ),
        error_prefix="Failed to replace ad text",
    )
