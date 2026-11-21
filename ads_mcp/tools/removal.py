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

"""Guarded removal of entities and undo of recent changes.

Registered on the ``campaign_management`` namespace. Removal is permanent in
Google Ads, so campaigns, ad groups and asset groups must be paused first and
the proposal shows their last 30 days of spend and conversions. Undo restores
the previous values of an UPDATE from the change history, but only when the
entity still has the values that change set.
"""

from dataclasses import dataclass
import datetime
from enum import Enum
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.safety import enforce_budget_ceiling
from ads_mcp.tools.campaign_management import campaign_management_mcp
import ads_mcp.utils as utils


class RemovableType(str, Enum):
    CAMPAIGN = "CAMPAIGN"
    AD_GROUP = "AD_GROUP"
    ASSET_GROUP = "ASSET_GROUP"
    AD = "AD"
    KEYWORD = "KEYWORD"


@dataclass(frozen=True)
class _Removable:
    resource: str
    prefix: str
    composite: bool
    name_field: str
    must_be_paused: bool
    extra_filter: str = ""


_REMOVABLE = {
    "CAMPAIGN": _Removable(
        "campaign", "campaigns", False, "campaign.name", True
    ),
    "AD_GROUP": _Removable(
        "ad_group", "adGroups", False, "ad_group.name", True
    ),
    "ASSET_GROUP": _Removable(
        "asset_group", "assetGroups", False, "asset_group.name", True
    ),
    "AD": _Removable("ad_group_ad", "adGroupAds", True, "ad_group.name", False),
    "KEYWORD": _Removable(
        "ad_group_criterion",
        "adGroupCriteria",
        True,
        "ad_group_criterion.keyword.text",
        False,
        " AND ad_group_criterion.type = 'KEYWORD'"
        " AND ad_group_criterion.negative = FALSE",
    ),
}


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def remove_entities(
    customer_id: str | int,
    entity_type: RemovableType,
    entity_ids: list[str],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Permanently removes campaigns, ad groups, asset groups, ads or keywords.

    Removal cannot be undone (removed entities cannot be re-enabled). Prefer
    pausing with update_entity_status. Campaigns, ad groups and asset groups
    must be PAUSED first; the proposal lists each entity's spend and
    conversions over the last 30 days so the user can confirm deliberately.
    Negative keywords are removed with manage_negative_keywords.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        entity_type: CAMPAIGN, AD_GROUP, ASSET_GROUP, AD or KEYWORD.
        entity_ids: IDs; ads and keywords use '{{ad_group_id}}~{{id}}'.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    kind = mh.enum_value(entity_type, RemovableType, "entity_type")
    config = _REMOVABLE[kind]
    ids = mh.require_ids(entity_ids, "entity_ids", composite=config.composite)
    rns = [f"customers/{cid}/{config.prefix}/{i}" for i in ids]
    res = config.resource

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    where = f"WHERE {res}.resource_name IN ({mh.quoted_list(rns)})"
    rows = mh.search(
        ga_client,
        cid,
        f"SELECT {res}.resource_name, {res}.status, {config.name_field}, "
        f"customer.currency_code FROM {res} {where}{config.extra_filter}",
    )
    found = {_get(row, f"{res}.resource_name"): row for row in rows}
    missing = [rn for rn in rns if rn not in found]
    if missing:
        raise ToolError(
            f"Error: Not found as removable {kind.lower()}: "
            f"{', '.join(missing)}"
        )
    if config.must_be_paused:
        active = [
            rn
            for rn in rns
            if mh.enum_name(_get(found[rn], f"{res}.status")) != "PAUSED"
        ]
        if active:
            raise ToolError(
                "Error: Pause these first with update_entity_status and "
                f"confirm with the user before removing: {', '.join(active)}"
            )
    metric_rows = mh.search(
        ga_client,
        cid,
        f"SELECT {res}.resource_name, metrics.cost_micros, "
        f"metrics.conversions FROM {res} {where} "
        "AND segments.date DURING LAST_30_DAYS",
    )
    spend: dict[str, list[float]] = {}
    for row in metric_rows:
        totals = spend.setdefault(_get(row, f"{res}.resource_name"), [0.0, 0.0])
        totals[0] += row.metrics.cost_micros
        totals[1] += row.metrics.conversions

    currency = rows[0].customer.currency_code
    operations = []
    change_items = []
    for rn in rns:
        mutate_op = ga_client.get_type("MutateOperation")
        getattr(mutate_op, f"{res}_operation").remove = rn
        operations.append(mutate_op)
        cost, conversions = spend.get(rn, [0.0, 0.0])
        name = _get(found[rn], config.name_field) or ""
        change_items.append(
            pac.ChangeItem(
                "REMOVE",
                kind.replace("_", " ").title(),
                f"{name} ({rn})".strip(),
                "Last 30 days",
                f"{mh.format_money(int(cost), currency)} spend, "
                f"{conversions:g} conversions. Permanent.",
            )
        )

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "remove_entities",
            "entity_type": kind,
            "entity_ids": ids,
        },
        change_items=change_items,
        success_message=f"Removed {len(ids)} {kind.lower()}(s).",
        error_prefix=f"Failed to remove {kind.lower()}",
    )


def _get(message: Any, path: str) -> Any:
    for part in path.split("."):
        message = getattr(message, part if part != "type" else "type_")
    return message


def _set(message: Any, path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    for part in parents:
        message = getattr(message, part)
    setattr(message, leaf, list(value) if _is_repeated(value) else value)


def _is_repeated(value: Any) -> bool:
    return (
        hasattr(value, "__iter__")
        and not isinstance(value, (str, bytes, dict))
        and not hasattr(value, "_pb")
    )


def _plain(value: Any) -> Any:
    """Comparable, readable form of a proto field value."""
    if hasattr(value, "name") and not hasattr(value, "_pb"):
        return value.name
    if hasattr(value, "_pb"):
        return type(value).to_dict(value)
    if _is_repeated(value):
        return [_plain(v) for v in value]
    return value


# ChangeEvent resource type -> (changed resource field, GAQL resource,
# MutateOperation field).
_REVERTIBLE = {
    "CAMPAIGN": ("campaign", "campaign", "campaign_operation"),
    "AD_GROUP": ("ad_group", "ad_group", "ad_group_operation"),
    "AD_GROUP_AD": ("ad_group_ad", "ad_group_ad", "ad_group_ad_operation"),
    "AD_GROUP_CRITERION": (
        "ad_group_criterion",
        "ad_group_criterion",
        "ad_group_criterion_operation",
    ),
    "CAMPAIGN_CRITERION": (
        "campaign_criterion",
        "campaign_criterion",
        "campaign_criterion_operation",
    ),
    "CAMPAIGN_BUDGET": (
        "campaign_budget",
        "campaign_budget",
        "campaign_budget_operation",
    ),
    "AD_GROUP_BID_MODIFIER": (
        "ad_group_bid_modifier",
        "ad_group_bid_modifier",
        "ad_group_bid_modifier_operation",
    ),
}

# Fields whose higher value means more spend (raising them is capped).
_INCREASE_GUARDED = (
    "amount_micros",
    "cpc_bid_micros",
    "target_cpa_micros",
    "cpc_bid_ceiling_micros",
    "cpm_bid_micros",
    "bid_modifier",
)
# Fields whose lower value means more aggressive bidding.
_DECREASE_GUARDED = ("target_roas",)
# Limits whose removal loosens bidding; restoring "no limit" is refused.
_LIMIT_LEAVES = ("target_cpa_micros", "target_roas", "cpc_bid_ceiling_micros")

_COMMON = ("status", "name", "tracking_url_template", "final_url_suffix")
# Only these fields can be restored; everything else that affects spend
# (budget swaps, strategy switches, total budgets) has its own guarded tool.
_ALLOWED_PATHS = {
    "CAMPAIGN": _COMMON
    + (
        "start_date_time",
        "end_date_time",
        "network_settings.target_search_network",
        "network_settings.target_partner_search_network",
        "network_settings.target_content_network",
        "maximize_conversions.target_cpa_micros",
        "maximize_conversion_value.target_roas",
        "target_cpa.target_cpa_micros",
        "target_roas.target_roas",
        "target_spend.cpc_bid_ceiling_micros",
        "geo_target_type_setting.positive_geo_target_type",
        "geo_target_type_setting.negative_geo_target_type",
    ),
    "AD_GROUP": _COMMON
    + ("cpc_bid_micros", "cpm_bid_micros", "target_cpa_micros", "target_roas"),
    "AD_GROUP_AD": ("status",),
    "AD_GROUP_CRITERION": (
        "status",
        "cpc_bid_micros",
        "final_urls",
        "tracking_url_template",
        "final_url_suffix",
    ),
    "CAMPAIGN_CRITERION": ("bid_modifier",),
    "CAMPAIGN_BUDGET": ("amount_micros", "name"),
    "AD_GROUP_BID_MODIFIER": ("bid_modifier",),
}


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def revert_change(
    customer_id: str | int,
    change_id: str,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Undoes a recent setting change by restoring the previous values.

    Takes a `change_id` from get_change_history (last 30 days). Works for
    UPDATE changes to campaigns, ad groups, ads (status), keywords and other
    criteria, budgets and bid modifiers: status, budgets, bids, targets,
    names, URLs. Refused when the entity was changed again since (so a later
    change is never silently overwritten), for created entities (pause or
    remove them instead) and for removals (cannot be restored). Restoring a
    higher budget, bid or looser target is capped by the safety limit.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        change_id: The change_id from get_change_history, e.g.
            customers/123/changeEvents/1700000000000000~0~0.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    change = (change_id or "").strip()
    prefix = f"customers/{cid}/changeEvents/"
    if not change.startswith(prefix) or "'" in change:
        raise ToolError(
            f"Error: 'change_id' must look like {prefix}<id> (from "
            "get_change_history)."
        )
    today = datetime.date.today()
    start = today - datetime.timedelta(days=29)
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT change_event.resource_name, change_event.change_date_time, "
        "change_event.change_resource_type, "
        "change_event.change_resource_name, "
        "change_event.resource_change_operation, "
        "change_event.changed_fields, change_event.old_resource, "
        "change_event.new_resource FROM change_event "
        f"WHERE change_event.resource_name = '{change}' "
        f"AND change_event.change_date_time >= '{start.isoformat()}' "
        f"AND change_event.change_date_time <= '{today.isoformat()} 23:59:59' "
        "LIMIT 1",
    )
    if not rows:
        raise ToolError(
            f"Error: Change {change} was not found in the last 30 days."
        )
    event = rows[0].change_event
    operation = mh.enum_name(event.resource_change_operation)
    resource_type = mh.enum_name(event.change_resource_type)
    if operation == "CREATE":
        raise ToolError(
            "Error: This change created the entity. Pause it with "
            "update_entity_status or remove it with remove_entities instead."
        )
    if operation != "UPDATE":
        raise ToolError(
            "Error: Removed entities cannot be restored in Google Ads; "
            "recreate them instead."
        )
    if resource_type not in _REVERTIBLE:
        raise ToolError(
            f"Error: Undo is not supported for {resource_type} changes. "
            f"Supported: {', '.join(_REVERTIBLE)}."
        )
    field, gaql, op_field = _REVERTIBLE[resource_type]
    old = getattr(event.old_resource, field)
    new = getattr(event.new_resource, field)
    paths = [p for p in event.changed_fields.paths if p != "resource_name"]
    if not paths:
        raise ToolError("Error: This change has no fields to restore.")
    target_rn = event.change_resource_name
    unsupported = [p for p in paths if p not in _ALLOWED_PATHS[resource_type]]
    if unsupported:
        raise ToolError(
            "Error: Undo does not restore these fields: "
            f"{', '.join(unsupported)}. Use the specific tool (budget, "
            "bidding, targeting), which applies its own safety checks."
        )

    try:
        current_rows = mh.search(
            ga_client,
            cid,
            f"SELECT {', '.join(f'{gaql}.{p}' for p in paths)} FROM {gaql} "
            f"WHERE {gaql}.resource_name = '{target_rn}'",
        )
    except Exception as exc:  # GAQL cannot select every changed field.
        raise ToolError(
            "Error: The current values of these fields cannot be read, so "
            "the undo cannot be checked safely. Restore them with the "
            f"specific tool instead. Fields: {', '.join(paths)}"
        ) from exc
    if not current_rows:
        raise ToolError(f"Error: {target_rn} no longer exists.")
    current = getattr(current_rows[0], gaql)

    mutate_op = ga_client.get_type("MutateOperation")
    op = getattr(mutate_op, op_field)
    op.update.resource_name = target_rn
    fields: dict[str, Any] = {}
    original: dict[str, Any] = {}
    changed_since = []
    for path in paths:
        now_value = _plain(_get(current, path))
        set_value = _plain(_get(new, path))
        if now_value != set_value:
            changed_since.append(path)
            continue
        old_value = _get(old, path)
        leaf = path.rsplit(".", 1)[-1]
        if leaf in _LIMIT_LEAVES and now_value and not old_value:
            raise ToolError(
                f"Error: Undoing this would remove the {leaf} limit. Change "
                "bidding with update_campaign_bidding instead."
            )
        if (
            leaf in _INCREASE_GUARDED + _DECREASE_GUARDED
            and leaf != "bid_modifier"
            and not now_value
            and old_value
        ):
            # 0 = unset: the entity uses an inherited (campaign) value, so
            # the guardrail has no baseline; the specific tool has one.
            raise ToolError(
                f"Error: {leaf} is 0 now; set it with the specific tool "
                "instead of an undo."
            )
        if leaf == "bid_modifier":
            # An excluded segment (0) counts as neutral (1.0) as baseline.
            now_value = now_value or 1.0
        if leaf in _INCREASE_GUARDED and isinstance(now_value, (int, float)):
            mh.enforce_increase_guardrail(
                float(now_value or 0), float(old_value or 0), leaf
            )
        if leaf in _DECREASE_GUARDED and isinstance(now_value, (int, float)):
            mh.enforce_decrease_guardrail(
                float(now_value or 0), float(old_value or 0), leaf
            )
        if gaql == "campaign_budget" and leaf == "amount_micros":
            enforce_budget_ceiling(int(old_value or 0))
        _set(op.update, path, old_value)
        op.update_mask.paths.append(path)
        fields[path] = _plain(old_value)
        original[path] = now_value
    if changed_since:
        raise ToolError(
            f"Error: {target_rn} was changed again after this change "
            f"({', '.join(changed_since)}). Undo the later change first or "
            "set the values with the specific tool."
        )

    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable=f"Undo {resource_type.replace('_', ' ').lower()}",
        primary_key_readable=f"{target_rn} (change of "
        f"{event.change_date_time})",
        fields=fields,
        original_fields=original,
    )
    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=[mutate_op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "revert_change",
            "change_id": change,
            "fields": {k: str(v) for k, v in fields.items()},
        },
        change_items=change_items,
        success_message=f"Restored {', '.join(paths)} on {target_rn}.",
        error_prefix="Failed to undo the change",
    )
