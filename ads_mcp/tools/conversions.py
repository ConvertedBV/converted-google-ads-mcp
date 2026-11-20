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

"""Conversion tracking tools: setup overview, conversion actions and goals.

Smart bidding optimizes towards the primary conversion actions of the
biddable conversion goals, so check `get_conversion_setup` before choosing
or changing a conversion-based bidding strategy.
"""

from collections import defaultdict
import hashlib
from enum import Enum
import json
import re
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
import ads_mcp.utils as utils

conversions_mcp = FastMCP("conversions")

_LEAD_CATEGORIES = {
    "SUBMIT_LEAD_FORM",
    "REQUEST_QUOTE",
    "BOOK_APPOINTMENT",
    "CONTACT",
    "SIGNUP",
    "PHONE_CALL_LEAD",
    "IMPORTED_LEAD",
    "QUALIFIED_LEAD",
    "CONVERTED_LEAD",
    "MESSAGE_LEAD",
}
_SEND_TO_RE = re.compile(r"send_to'?\"?\s*:\s*['\"](AW-[^'\"]+)['\"]")


class Period(str, Enum):
    LAST_7_DAYS = "LAST_7_DAYS"
    LAST_14_DAYS = "LAST_14_DAYS"
    LAST_30_DAYS = "LAST_30_DAYS"


class ConversionCategory(str, Enum):
    """Common conversion categories (the goal a conversion counts towards)."""

    PURCHASE = "PURCHASE"
    SUBMIT_LEAD_FORM = "SUBMIT_LEAD_FORM"
    REQUEST_QUOTE = "REQUEST_QUOTE"
    BOOK_APPOINTMENT = "BOOK_APPOINTMENT"
    CONTACT = "CONTACT"
    PHONE_CALL_LEAD = "PHONE_CALL_LEAD"
    SIGNUP = "SIGNUP"
    QUALIFIED_LEAD = "QUALIFIED_LEAD"
    CONVERTED_LEAD = "CONVERTED_LEAD"
    IMPORTED_LEAD = "IMPORTED_LEAD"
    ADD_TO_CART = "ADD_TO_CART"
    BEGIN_CHECKOUT = "BEGIN_CHECKOUT"
    PAGE_VIEW = "PAGE_VIEW"
    DOWNLOAD = "DOWNLOAD"
    OUTBOUND_CLICK = "OUTBOUND_CLICK"
    ENGAGEMENT = "ENGAGEMENT"
    DEFAULT = "DEFAULT"


class ConversionType(str, Enum):
    """Conversion sources that can be created here."""

    WEBPAGE = "WEBPAGE"
    UPLOAD_CLICKS = "UPLOAD_CLICKS"
    UPLOAD_CALLS = "UPLOAD_CALLS"


class CountingType(str, Enum):
    ONE_PER_CLICK = "ONE_PER_CLICK"
    MANY_PER_CLICK = "MANY_PER_CLICK"


class AttributionModel(str, Enum):
    DATA_DRIVEN = "DATA_DRIVEN"
    LAST_CLICK = "LAST_CLICK"


_ATTRIBUTION = {
    "DATA_DRIVEN": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
    "LAST_CLICK": "GOOGLE_ADS_LAST_CLICK",
}


class ConversionActionStatus(str, Enum):
    ENABLED = "ENABLED"
    HIDDEN = "HIDDEN"


class GoalSetting(BaseModel):
    """Whether a conversion goal (category + origin) is used for bidding."""

    category: str = Field(description="e.g. SUBMIT_LEAD_FORM or PURCHASE.")
    origin: str = Field(
        default="WEBSITE",
        description="e.g. WEBSITE, CALL_FROM_ADS, GOOGLE_HOSTED or APP.",
    )
    biddable: bool = Field(description="True to optimize bidding towards it.")


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@conversions_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_conversion_setup(
    customer_id: str | int,
    period: Period = Period.LAST_30_DAYS,
    login_customer_id: str | int | None = None,
) -> str:
    """Summarizes conversion tracking and flags setups that mislead bidding.

    Lists every active conversion action (category, source, primary or
    secondary, counting, value, attribution, conversions in the period) and
    the account's conversion goals, and adds warnings such as: no primary
    actions, primary actions without conversions (tracking may be broken),
    lead actions counted many-per-click, or biddable goals without a primary
    action. Use it before choosing or changing smart bidding.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        period: LAST_7_DAYS, LAST_14_DAYS or LAST_30_DAYS (default).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    period_value = mh.enum_value(period, Period, "period")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)

    actions = mh.search(
        ga_client,
        cid,
        "SELECT conversion_action.resource_name, conversion_action.id, "
        "conversion_action.name, conversion_action.status, "
        "conversion_action.type, conversion_action.category, "
        "conversion_action.origin, conversion_action.primary_for_goal, "
        "conversion_action.counting_type, "
        "conversion_action.value_settings.default_value, "
        "conversion_action.value_settings.always_use_default_value, "
        "conversion_action.attribution_model_settings.attribution_model, "
        "conversion_action.click_through_lookback_window_days "
        "FROM conversion_action WHERE conversion_action.status = 'ENABLED'",
    )
    stats = mh.search(
        ga_client,
        cid,
        "SELECT segments.conversion_action, metrics.all_conversions, "
        "metrics.all_conversions_value FROM customer "
        f"WHERE segments.date DURING {period_value}",
    )
    goals = mh.search(
        ga_client,
        cid,
        "SELECT customer_conversion_goal.category, "
        "customer_conversion_goal.origin, customer_conversion_goal.biddable "
        "FROM customer_conversion_goal",
    )

    counts: dict[str, float] = defaultdict(float)
    for row in stats:
        counts[row.segments.conversion_action] += row.metrics.all_conversions

    # Smart bidding only uses primary actions whose goal is biddable, so only
    # those can mislead it; other findings would be noise.
    biddable_goals = {
        (
            row.customer_conversion_goal.category.name,
            row.customer_conversion_goal.origin.name,
        )
        for row in goals
        if row.customer_conversion_goal.biddable
    }

    result_actions = []
    warnings: list[str] = []
    primary_goals: set[tuple[str, str]] = set()
    for row in actions:
        action = row.conversion_action
        category = action.category.name
        origin = action.origin.name
        conversions = round(counts.get(action.resource_name, 0.0), 1)
        used_for_bidding = bool(
            action.primary_for_goal and (category, origin) in biddable_goals
        )
        if action.primary_for_goal:
            primary_goals.add((category, origin))
        result_actions.append(
            {
                "id": action.id,
                "name": action.name,
                "category": category,
                "origin": origin,
                "type": action.type_.name,
                "primary": action.primary_for_goal,
                "used_for_bidding": used_for_bidding,
                "counting": action.counting_type.name,
                "default_value": action.value_settings.default_value or None,
                "always_use_default_value": (
                    action.value_settings.always_use_default_value
                ),
                "attribution": (
                    action.attribution_model_settings.attribution_model.name
                ),
                "click_lookback_days": (
                    action.click_through_lookback_window_days or None
                ),
                f"conversions_{period_value.lower()}": conversions,
            }
        )
        if used_for_bidding and conversions == 0:
            warnings.append(
                f"Action '{action.name}' is used for bidding but recorded no "
                f"conversions in {period_value}: check the tag or import."
            )
        if (
            used_for_bidding
            and category in _LEAD_CATEGORIES
            and action.counting_type.name == "MANY_PER_CLICK"
        ):
            warnings.append(
                f"Lead action '{action.name}' is used for bidding and counts "
                "every conversion (MANY_PER_CLICK); leads are usually "
                "ONE_PER_CLICK."
            )

    result_goals = []
    for row in goals:
        goal = row.customer_conversion_goal
        key = (goal.category.name, goal.origin.name)
        result_goals.append(
            {
                "category": key[0],
                "origin": key[1],
                "biddable": goal.biddable,
                "has_primary_action": key in primary_goals,
            }
        )
        if goal.biddable and key not in primary_goals:
            warnings.append(
                f"Goal {key[0]} / {key[1]} is used for bidding but has no "
                "enabled primary action."
            )
    if not primary_goals:
        warnings.append(
            "No enabled primary conversion actions: conversion-based bidding "
            "has nothing to optimize towards."
        )

    return json.dumps(
        {
            "period": period_value,
            "conversion_actions": result_actions,
            "account_goals": result_goals,
            "warnings": warnings,
        },
        indent=2,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Conversion actions
# ---------------------------------------------------------------------------


def _lookback(value: int | None, arg_name: str, max_days: int) -> int | None:
    if value is None:
        return None
    if not 1 <= int(value) <= max_days:
        raise ToolError(f"Error: '{arg_name}' must be 1-{max_days} days.")
    return int(value)


def _value(value: float | None) -> float | None:
    if value is None:
        return None
    if float(value) < 0:
        raise ToolError("Error: 'default_value' must not be negative.")
    return round(float(value), 2)


@conversions_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_conversion_action(
    customer_id: str | int,
    name: str,
    category: ConversionCategory,
    conversion_type: ConversionType = ConversionType.WEBPAGE,
    counting_type: CountingType = CountingType.ONE_PER_CLICK,
    primary_for_goal: bool = True,
    default_value: float | None = None,
    always_use_default_value: bool = False,
    attribution_model: AttributionModel = AttributionModel.DATA_DRIVEN,
    click_through_lookback_days: int | None = None,
    view_through_lookback_days: int | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a conversion action.

    WEBPAGE actions need a tag on the website: after creation the result
    contains the `send_to` value (AW-.../label) for Google Tag Manager or the
    gtag event snippet. UPLOAD_CLICKS / UPLOAD_CALLS are for offline imports
    (e.g. qualified leads from a CRM). Leads should normally count
    ONE_PER_CLICK, purchases MANY_PER_CLICK. A new primary action starts to
    influence smart bidding of campaigns that bid on its goal.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        name: Unique conversion action name.
        category: The goal category, e.g. SUBMIT_LEAD_FORM or PURCHASE.
        conversion_type: WEBPAGE (default), UPLOAD_CLICKS or UPLOAD_CALLS.
        counting_type: ONE_PER_CLICK (default) or MANY_PER_CLICK.
        primary_for_goal: True (default) to use it for bidding.
        default_value: Optional value per conversion (account currency).
        always_use_default_value: Ignore dynamic values sent by the tag.
        attribution_model: DATA_DRIVEN (default) or LAST_CLICK.
        click_through_lookback_days: 1-90 (Google default 30).
        view_through_lookback_days: 1-30 (Google default 1).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    clean_name = " ".join((name or "").split())
    if not clean_name:
        raise ToolError("Error: 'name' is required.")
    settings = {
        "name": clean_name,
        "category": mh.enum_value(category, ConversionCategory, "category"),
        "type": mh.enum_value(conversion_type, ConversionType, "type"),
        "counting": mh.enum_value(counting_type, CountingType, "counting"),
        "primary": bool(primary_for_goal),
        "default_value": _value(default_value),
        "always_use_default_value": bool(always_use_default_value),
        "attribution": mh.enum_value(
            attribution_model, AttributionModel, "attribution_model"
        ),
        "click_lookback": _lookback(
            click_through_lookback_days, "click_through_lookback_days", 90
        ),
        "view_lookback": _lookback(
            view_through_lookback_days, "view_through_lookback_days", 30
        ),
    }

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client, cid, "SELECT customer.currency_code FROM customer"
    )
    currency = rows[0].customer.currency_code if rows else ""
    enums = ga_client.enums

    op = ga_client.get_type("ConversionActionOperation")
    action = op.create
    action.name = settings["name"]
    action.category = getattr(
        enums.ConversionActionCategoryEnum, settings["category"]
    )
    action.type_ = getattr(enums.ConversionActionTypeEnum, settings["type"])
    action.status = enums.ConversionActionStatusEnum.ENABLED
    action.counting_type = getattr(
        enums.ConversionActionCountingTypeEnum, settings["counting"]
    )
    action.primary_for_goal = settings["primary"]
    if settings["default_value"] is not None:
        action.value_settings.default_value = settings["default_value"]
        action.value_settings.default_currency_code = currency
    action.value_settings.always_use_default_value = settings[
        "always_use_default_value"
    ]
    action.attribution_model_settings.attribution_model = getattr(
        enums.AttributionModelEnum, _ATTRIBUTION[settings["attribution"]]
    )
    if settings["click_lookback"]:
        action.click_through_lookback_window_days = settings["click_lookback"]
    if settings["view_lookback"]:
        action.view_through_lookback_window_days = settings["view_lookback"]

    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Conversion Action",
        primary_key_readable=clean_name,
        fields={
            "Category": settings["category"],
            "Source": settings["type"],
            "Counting": settings["counting"],
            "Primary (used for bidding)": (
                "Yes" if settings["primary"] else "No"
            ),
            "Default value": (
                f"{settings['default_value']:.2f} {currency}"
                if settings["default_value"] is not None
                else None
            ),
            "Attribution": settings["attribution"],
            "Click lookback (days)": settings["click_lookback"],
            "View lookback (days)": settings["view_lookback"],
        },
    )

    service = utils.get_googleads_service_from_client(
        ga_client, "ConversionActionService"
    )
    request = ga_client.get_type("MutateConversionActionsRequest")
    request.customer_id = cid
    request.operations.append(op)

    def _validate() -> None:
        request.validate_only = True
        service.mutate_conversion_actions(request=request)

    def _commit() -> str:
        request.validate_only = False
        response = service.mutate_conversion_actions(request=request)
        resource_name = response.results[0].resource_name
        message = (
            f"Created conversion action '{clean_name}' "
            f"(ID {utils.extract_id(resource_name)})."
        )
        if settings["type"] == "WEBPAGE":
            message += _tag_instructions(ga_client, cid, resource_name)
        return message

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={"tool": "create_conversion_action", **settings},
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to create conversion action",
    )


def _tag_instructions(ga_client: Any, cid: str, resource_name: str) -> str:
    """Returns the send_to value for the tag, best effort."""
    try:
        rows = mh.search(
            ga_client,
            cid,
            "SELECT conversion_action.tag_snippets FROM conversion_action "
            f"WHERE conversion_action.resource_name = '{resource_name}'",
        )
    except Exception:  # The action exists; tag lookup is only a convenience.
        rows = []
    for row in rows:
        for snippet in row.conversion_action.tag_snippets:
            match = _SEND_TO_RE.search(snippet.event_snippet or "")
            if match:
                conversion_id, _, label = match.group(1).partition("/")
                return (
                    f"\nTag setup: send_to = {match.group(1)} (Google Tag "
                    f"Manager: Conversion ID {conversion_id.removeprefix('AW-')}"
                    f", Conversion Label {label})."
                )
    return (
        "\nThe tag details are available in Google Ads under Goals > "
        "Conversions > this action > Tag setup."
    )


@conversions_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def update_conversion_action(
    customer_id: str | int,
    conversion_action_id: str,
    name: str | None = None,
    primary_for_goal: bool | None = None,
    status: ConversionActionStatus | None = None,
    counting_type: CountingType | None = None,
    default_value: float | None = None,
    always_use_default_value: bool | None = None,
    click_through_lookback_days: int | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Changes a conversion action: primary/secondary, counting, value, ...

    Making an action primary or secondary changes what smart bidding
    optimizes towards for every campaign bidding on its goal. HIDDEN hides
    the action from reports; removing is intentionally not supported.
    Actions imported from GA4 or other systems allow only some changes; the
    dry run reports what is not allowed.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        conversion_action_id: The numeric conversion action ID.
        name: New name.
        primary_for_goal: True = primary (bidding), False = secondary.
        status: ENABLED or HIDDEN.
        counting_type: ONE_PER_CLICK or MANY_PER_CLICK.
        default_value: Default value per conversion (account currency).
        always_use_default_value: Ignore dynamic values sent by the tag.
        click_through_lookback_days: 1-90.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    action_id = mh.require_simple_id(
        conversion_action_id, "conversion_action_id"
    )
    changes = {
        "name": " ".join(name.split()) if name else None,
        "primary_for_goal": primary_for_goal,
        "status": (
            mh.enum_value(status, ConversionActionStatus, "status")
            if status
            else None
        ),
        "counting_type": (
            mh.enum_value(counting_type, CountingType, "counting_type")
            if counting_type
            else None
        ),
        "value_settings.default_value": _value(default_value),
        "value_settings.always_use_default_value": always_use_default_value,
        "click_through_lookback_window_days": _lookback(
            click_through_lookback_days, "click_through_lookback_days", 90
        ),
    }
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        raise ToolError("Error: Pass at least one setting to change.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rn = f"customers/{cid}/conversionActions/{action_id}"
    rows = mh.search(
        ga_client,
        cid,
        "SELECT conversion_action.name, conversion_action.status, "
        "conversion_action.primary_for_goal, "
        "conversion_action.counting_type, "
        "conversion_action.value_settings.default_value, "
        "conversion_action.value_settings.always_use_default_value, "
        "conversion_action.click_through_lookback_window_days "
        f"FROM conversion_action WHERE conversion_action.id = {action_id}",
    )
    if not rows:
        raise ToolError(
            f"Error: Conversion action {action_id} was not found in {cid}."
        )
    current = rows[0].conversion_action
    enums = ga_client.enums

    op = ga_client.get_type("ConversionActionOperation")
    op.update.resource_name = rn
    fields: dict[str, Any] = {}
    original: dict[str, Any] = {}
    for path, value in changes.items():
        target = op.update
        old = current
        *parents, leaf = path.split(".")
        for part in parents:
            target = getattr(target, part)
            old = getattr(old, part)
        old_value = getattr(old, leaf)
        if path == "status":
            value_to_set = getattr(enums.ConversionActionStatusEnum, value)
            old_value = old_value.name
        elif path == "counting_type":
            value_to_set = getattr(
                enums.ConversionActionCountingTypeEnum, value
            )
            old_value = old_value.name
        else:
            value_to_set = value
        setattr(target, leaf, value_to_set)
        op.update_mask.paths.append(path)
        label = path.rsplit(".", 1)[-1].replace("_", " ").capitalize()
        fields[label] = value
        original[label] = old_value

    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Conversion Action",
        primary_key_readable=f"{current.name} ({rn})",
        fields=fields,
        original_fields=original,
    )
    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="ConversionActionService",
        request_type="MutateConversionActionsRequest",
        mutate_method="mutate_conversion_actions",
        operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_conversion_action",
            "conversion_action_id": action_id,
            "changes": changes,
        },
        change_items=change_items,
        success_message=f"Updated conversion action {action_id}.",
        error_prefix="Failed to update conversion action",
    )


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


@conversions_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def set_conversion_goals(
    customer_id: str | int,
    goals: list[GoalSetting],
    campaign_id: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Chooses which conversion goals smart bidding optimizes towards.

    Without `campaign_id` this changes the account default goals, which
    apply to every campaign that uses account-level goals. With
    `campaign_id` it changes that campaign's goals only. A goal is a
    category + origin pair (e.g. SUBMIT_LEAD_FORM + WEBSITE); see
    get_conversion_setup for the existing goals.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        goals: Goals to change, each {{"category", "origin", "biddable"}}.
        campaign_id: Optional campaign ID for campaign-specific goals.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaign = (
        mh.require_simple_id(campaign_id, "campaign_id")
        if campaign_id
        else None
    )
    if not goals:
        raise ToolError("Error: Pass at least one goal.")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    valid_categories = {e.name for e in enums.ConversionActionCategoryEnum}
    valid_origins = {e.name for e in enums.ConversionOriginEnum}

    wanted: dict[tuple[str, str], bool] = {}
    for raw in goals:
        goal = GoalSetting(**raw) if isinstance(raw, dict) else raw
        key = (goal.category.strip().upper(), goal.origin.strip().upper())
        if key[0] not in valid_categories or key[1] not in valid_origins:
            raise ToolError(f"Error: Unknown goal {key[0]} / {key[1]}.")
        wanted[key] = bool(goal.biddable)

    if campaign:
        resource = "campaign_conversion_goal"
        rows = mh.search(
            ga_client,
            cid,
            f"SELECT {resource}.category, {resource}.origin, "
            f"{resource}.biddable, campaign.name FROM {resource} "
            f"WHERE campaign.id = {campaign}",
        )
        scope = f"campaign {rows[0].campaign.name}" if rows else ""
        prefix = f"customers/{cid}/campaignConversionGoals/{campaign}~"
        service, request_type, method, op_type = (
            "CampaignConversionGoalService",
            "MutateCampaignConversionGoalsRequest",
            "mutate_campaign_conversion_goals",
            "CampaignConversionGoalOperation",
        )
    else:
        resource = "customer_conversion_goal"
        rows = mh.search(
            ga_client,
            cid,
            f"SELECT {resource}.category, {resource}.origin, "
            f"{resource}.biddable FROM {resource}",
        )
        scope = "account default goals"
        prefix = f"customers/{cid}/customerConversionGoals/"
        service, request_type, method, op_type = (
            "CustomerConversionGoalService",
            "MutateCustomerConversionGoalsRequest",
            "mutate_customer_conversion_goals",
            "CustomerConversionGoalOperation",
        )

    current = {
        (
            getattr(row, resource).category.name,
            getattr(row, resource).origin.name,
        ): getattr(row, resource).biddable
        for row in rows
    }
    missing = [f"{c} / {o}" for c, o in wanted if (c, o) not in current]
    if missing:
        raise ToolError(
            f"Error: These goals do not exist for {scope or 'this campaign'}: "
            f"{', '.join(missing)}. Create a conversion action in that "
            "category first."
        )

    operations = []
    change_items: list[pac.ChangeItem] = []
    for (category, origin), biddable in sorted(wanted.items()):
        op = ga_client.get_type(op_type)
        op.update.resource_name = f"{prefix}{category}~{origin}"
        op.update.biddable = biddable
        op.update_mask.paths.append("biddable")
        operations.append(op)
        change_items.append(
            pac.ChangeItem(
                "UPDATE",
                "Conversion goal",
                f"{category} / {origin} ({scope})",
                "Used for bidding",
                "Yes" if biddable else "No",
                "Yes" if current[(category, origin)] else "No",
            )
        )

    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name=service,
        request_type=request_type,
        mutate_method=method,
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "set_conversion_goals",
            "campaign_id": campaign,
            "goals": {f"{c}~{o}": b for (c, o), b in sorted(wanted.items())},
        },
        change_items=change_items,
        success_message=f"Updated {len(operations)} conversion goals ({scope}).",
        error_prefix="Failed to update conversion goals",
    )


# ---------------------------------------------------------------------------
# Offline conversion uploads
# ---------------------------------------------------------------------------

MAX_UPLOAD_ROWS = 200
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_CLICK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{10,}$")


class ConsentChoice(str, Enum):
    """Whether the person consented to sending their data to Google
    (required for EEA uploads)."""

    GRANTED = "GRANTED"
    DENIED = "DENIED"


class OfflineConversionInput(BaseModel):
    """One offline conversion, e.g. a qualified lead from the CRM."""

    conversion_time: str = Field(
        description="'YYYY-MM-DD HH:MM:SS+01:00' (with time zone offset)."
    )
    gclid: str | None = Field(default=None, description="Google click ID.")
    gbraid: str | None = Field(default=None, description="iOS app click ID.")
    wbraid: str | None = Field(default=None, description="iOS web click ID.")
    email: str | None = Field(
        default=None,
        description="Email for enhanced conversions; hashed on the server.",
    )
    phone_number: str | None = Field(
        default=None,
        description="Phone in E.164 (+32...) format; hashed on the server.",
    )
    value: float | None = Field(default=None, description="Conversion value.")
    currency: str | None = Field(
        default=None, description="ISO currency, e.g. EUR (default: account)."
    )
    order_id: str | None = Field(
        default=None, description="Optional unique ID to prevent duplicates."
    )


def normalize_email_hash(email: str) -> str:
    """SHA-256 of the email, normalized the way Google Ads expects."""
    cleaned = email.strip().lower()
    if not _EMAIL_RE.match(cleaned):
        raise ToolError("Error: An email address is not valid.")
    local, domain = cleaned.rsplit("@", 1)
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
    return hashlib.sha256(f"{local}@{domain}".encode()).hexdigest()


def normalize_phone_hash(phone: str) -> str:
    """SHA-256 of an E.164 phone number."""
    cleaned = re.sub(r"[\s\-().]", "", phone.strip())
    if not _PHONE_RE.match(cleaned):
        raise ToolError(
            "Error: Phone numbers must be in E.164 format with country code, "
            "e.g. +32470123456."
        )
    return hashlib.sha256(cleaned.encode()).hexdigest()


def _normalize_row(row: OfflineConversionInput | dict, index: int) -> dict:
    if isinstance(row, dict):
        row = OfflineConversionInput(**row)
    when = row.conversion_time.strip().replace("T", " ")
    if not _TIME_RE.match(when):
        raise ToolError(
            f"Error: Row {index}: conversion_time must look like "
            "'2026-11-20 14:30:00+01:00'."
        )
    click_ids = {}
    for key in ("gclid", "gbraid", "wbraid"):
        value = (getattr(row, key) or "").strip()
        if value:
            if not _CLICK_ID_RE.match(value):
                raise ToolError(f"Error: Row {index}: invalid {key}.")
            click_ids[key] = value
    if len(click_ids) > 1:
        raise ToolError(f"Error: Row {index}: pass only one click ID.")
    hashed_email = normalize_email_hash(row.email) if row.email else None
    hashed_phone = (
        normalize_phone_hash(row.phone_number) if row.phone_number else None
    )
    if not click_ids and not (hashed_email or hashed_phone):
        raise ToolError(
            f"Error: Row {index} needs a gclid/gbraid/wbraid or an email or "
            "phone number."
        )
    if row.value is not None and float(row.value) < 0:
        raise ToolError(f"Error: Row {index}: value must not be negative.")
    return {
        "time": when,
        **click_ids,
        "hashed_email": hashed_email,
        "hashed_phone": hashed_phone,
        "value": round(float(row.value), 2) if row.value is not None else None,
        "currency": (row.currency or "").strip().upper() or None,
        "order_id": (row.order_id or "").strip() or None,
    }


def _row_errors(ga_client: Any, status: Any) -> dict[int, str]:
    """Maps row index to error message from a partial failure status."""
    errors: dict[int, str] = {}
    if not status or not status.code:
        return errors
    failure_type = type(ga_client.get_type("GoogleAdsFailure"))
    for detail in status.details:
        failure = failure_type.deserialize(detail.value)
        for error in failure.errors:
            index = next(
                (
                    element.index
                    for element in error.location.field_path_elements
                    if element.field_name == "conversions"
                ),
                -1,
            )
            errors[index] = error.message
    if not errors:
        errors[-1] = status.message
    return errors


def _describe_row(row: dict) -> str:
    ids = []
    for key in ("gclid", "gbraid", "wbraid"):
        if row.get(key):
            ids.append(f"{key} {row[key][:6]}…")
    if row["hashed_email"]:
        ids.append("email (hashed)")
    if row["hashed_phone"]:
        ids.append("phone (hashed)")
    value = f", value {row['value']:.2f}" if row["value"] is not None else ""
    return f"{row['time']}: {' + '.join(ids)}{value}"


@conversions_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def upload_offline_conversions(
    customer_id: str | int,
    conversion_action_id: str,
    conversions: list[OfflineConversionInput],
    ad_user_data_consent: ConsentChoice,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Uploads offline conversions such as qualified or closed leads.

    Use an UPLOAD_CLICKS conversion action (create one with
    create_conversion_action). Each row needs a click ID (gclid, gbraid or
    wbraid) and/or an email or phone number (enhanced conversions for
    leads); emails and phone numbers are normalized and SHA-256 hashed on
    the server and never sent in plain text. `ad_user_data_consent` must
    reflect the consent you collected (required for EEA users). Use
    order_id to avoid duplicates. Check results later with the search tool
    on offline_conversion_upload_conversion_action_summary.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        conversion_action_id: The numeric UPLOAD_CLICKS conversion action ID.
        conversions: 1-200 conversions.
        ad_user_data_consent: GRANTED or DENIED.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    action_id = mh.require_simple_id(
        conversion_action_id, "conversion_action_id"
    )
    consent = mh.enum_value(ad_user_data_consent, ConsentChoice, "consent")
    if not conversions or len(conversions) > MAX_UPLOAD_ROWS:
        raise ToolError(f"Error: Pass 1-{MAX_UPLOAD_ROWS} conversions.")
    rows = [_normalize_row(r, i) for i, r in enumerate(conversions)]

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    found = mh.search(
        ga_client,
        cid,
        "SELECT conversion_action.name, conversion_action.type, "
        "conversion_action.status, customer.currency_code "
        f"FROM conversion_action WHERE conversion_action.id = {action_id}",
    )
    if not found:
        raise ToolError(f"Error: Conversion action {action_id} was not found.")
    action = found[0].conversion_action
    if action.type_.name != "UPLOAD_CLICKS" or action.status.name != "ENABLED":
        raise ToolError(
            f"Error: '{action.name}' is a {action.type_.name} action "
            f"({action.status.name}); uploads need an enabled UPLOAD_CLICKS "
            "action."
        )
    currency = found[0].customer.currency_code
    action_rn = f"customers/{cid}/conversionActions/{action_id}"
    enums = ga_client.enums

    request = ga_client.get_type("UploadClickConversionsRequest")
    request.customer_id = cid
    request.partial_failure = True
    for row in rows:
        conversion = ga_client.get_type("ClickConversion")
        conversion.conversion_action = action_rn
        conversion.conversion_date_time = row["time"]
        for key in ("gclid", "gbraid", "wbraid"):
            if row.get(key):
                setattr(conversion, key, row[key])
        if row["hashed_email"]:
            identifier = ga_client.get_type("UserIdentifier")
            identifier.hashed_email = row["hashed_email"]
            conversion.user_identifiers.append(identifier)
        if row["hashed_phone"]:
            identifier = ga_client.get_type("UserIdentifier")
            identifier.hashed_phone_number = row["hashed_phone"]
            conversion.user_identifiers.append(identifier)
        if row["value"] is not None:
            conversion.conversion_value = row["value"]
            conversion.currency_code = row["currency"] or currency
        if row["order_id"]:
            conversion.order_id = row["order_id"]
        conversion.consent.ad_user_data = getattr(
            enums.ConsentStatusEnum, consent
        )
        request.conversions.append(conversion)

    service = utils.get_googleads_service_from_client(
        ga_client, "ConversionUploadService"
    )

    def _format(errors: dict[int, str]) -> str:
        return "\n- ".join(
            f"row {i}: {message}" if i >= 0 else message
            for i, message in sorted(errors.items())[:20]
        )

    def _validate() -> None:
        request.validate_only = True
        response = service.upload_click_conversions(request=request)
        errors = _row_errors(ga_client, response.partial_failure_error)
        if errors:
            raise ToolError(
                "Error: Google rejected conversions in the dry run:\n- "
                + _format(errors)
            )

    def _commit() -> str:
        request.validate_only = False
        response = service.upload_click_conversions(request=request)
        errors = _row_errors(ga_client, response.partial_failure_error)
        accepted = len(rows) - len([i for i in errors if i >= 0])
        message = (
            f"Uploaded {accepted} of {len(rows)} conversions to "
            f"'{action.name}'. Google processes uploads with a delay of a "
            "few hours."
        )
        if errors:
            message += "\nRejected:\n- " + _format(errors)
        return message

    total_value = sum(r["value"] or 0 for r in rows)
    change_items = [
        pac.ChangeItem(
            "CREATE",
            "Offline conversion upload",
            f"{action.name} ({action_id})",
            "Summary",
            f"{len(rows)} conversions, total value {total_value:.2f} "
            f"{currency}, consent {consent}",
        )
    ] + [
        pac.ChangeItem(
            "CREATE",
            "Offline conversion",
            action.name,
            f"Row {i}",
            _describe_row(r),
        )
        for i, r in enumerate(rows[:10])
    ]
    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "upload_offline_conversions",
            "conversion_action_id": action_id,
            "consent": consent,
            "rows": rows,
        },
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to upload conversions",
    )
