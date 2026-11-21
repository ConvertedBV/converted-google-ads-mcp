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

"""Conversion adjustments (retractions, restatements) and value rules.

Registered on the ``conversions`` namespace.
"""

from enum import Enum
import re
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.conversions import conversions_mcp
import ads_mcp.utils as utils

MAX_ADJUSTMENTS = 200
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
_CLICK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


class AdjustmentType(str, Enum):
    RETRACTION = "RETRACTION"
    RESTATEMENT = "RESTATEMENT"


class ConversionAdjustmentInput(BaseModel):
    """One conversion to retract or restate, found by order ID or click."""

    adjustment_type: AdjustmentType = Field(
        description="RETRACTION (cancel) or RESTATEMENT (new value)."
    )
    adjustment_time: str = Field(
        description="When it changed, e.g. '2026-11-21 10:00:00+01:00'."
    )
    order_id: str | None = Field(
        default=None, description="Order / transaction ID of the conversion."
    )
    gclid: str | None = Field(
        default=None, description="Click ID (with conversion_time)."
    )
    conversion_time: str | None = Field(
        default=None, description="Original conversion time (with gclid)."
    )
    restated_value: float | None = Field(
        default=None, description="New total value (RESTATEMENT)."
    )


def _time(value: str | None, label: str, index: int) -> str:
    text = (value or "").strip().replace("T", " ")
    if not _TIME_RE.match(text):
        raise ToolError(
            f"Error: Row {index}: {label} must look like "
            "'2026-11-21 10:00:00+01:00'."
        )
    return text


def _normalize(row: ConversionAdjustmentInput | dict, index: int) -> dict:
    if isinstance(row, dict):
        row = ConversionAdjustmentInput(**row)
    kind = mh.enum_value(row.adjustment_type, AdjustmentType, "adjustment_type")
    order_id = (row.order_id or "").strip() or None
    gclid = (row.gclid or "").strip() or None
    if bool(order_id) == bool(gclid):
        raise ToolError(
            f"Error: Row {index}: pass either order_id or gclid + "
            "conversion_time."
        )
    result: dict[str, Any] = {
        "type": kind,
        "time": _time(row.adjustment_time, "adjustment_time", index),
        "order_id": order_id,
    }
    if gclid:
        if not _CLICK_ID_RE.match(gclid):
            raise ToolError(f"Error: Row {index}: invalid gclid.")
        result["gclid"] = gclid
        result["conversion_time"] = _time(
            row.conversion_time, "conversion_time", index
        )
    if kind == "RESTATEMENT":
        if row.restated_value is None or float(row.restated_value) < 0:
            raise ToolError(
                f"Error: Row {index}: a restatement needs restated_value >= 0."
            )
        result["value"] = round(float(row.restated_value), 2)
    elif row.restated_value is not None:
        raise ToolError(f"Error: Row {index}: a retraction has no value.")
    return result


def _row_errors(ga_client: Any, status: Any) -> dict[int, str]:
    errors: dict[int, str] = {}
    if not status or not status.code:
        return errors
    failure_type = type(ga_client.get_type("GoogleAdsFailure"))
    for detail in status.details:
        failure = failure_type.deserialize(detail.value)
        for error in failure.errors:
            index = next(
                (
                    e.index
                    for e in error.location.field_path_elements
                    if e.field_name == "conversion_adjustments"
                ),
                -1,
            )
            errors[index] = error.message
    return errors or {-1: status.message}


@conversions_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def upload_conversion_adjustments(
    customer_id: str | int,
    conversion_action_id: str,
    adjustments: list[ConversionAdjustmentInput],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Retracts or restates conversions that were already reported.

    Use it when a lead turned out to be spam or a sale was refunded
    (RETRACTION), or the real value differs from the reported one
    (RESTATEMENT with the new total value). Find conversions by order_id
    (best) or gclid + original conversion_time. Works for website and
    uploaded conversions with order IDs, within 55 days.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        conversion_action_id: The conversion action of these conversions.
        adjustments: 1-200 adjustments.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    action_id = mh.require_simple_id(
        conversion_action_id, "conversion_action_id"
    )
    if not adjustments or len(adjustments) > MAX_ADJUSTMENTS:
        raise ToolError(f"Error: Pass 1-{MAX_ADJUSTMENTS} adjustments.")
    rows = [_normalize(r, i) for i, r in enumerate(adjustments)]
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    found = mh.search(
        ga_client,
        cid,
        "SELECT conversion_action.name, customer.currency_code "
        f"FROM conversion_action WHERE conversion_action.id = {action_id}",
    )
    if not found:
        raise ToolError(f"Error: Conversion action {action_id} was not found.")
    action_name = found[0].conversion_action.name
    currency = found[0].customer.currency_code
    action_rn = f"customers/{cid}/conversionActions/{action_id}"
    enums = ga_client.enums

    request = ga_client.get_type("UploadConversionAdjustmentsRequest")
    request.customer_id = cid
    request.partial_failure = True
    for row in rows:
        adjustment = ga_client.get_type("ConversionAdjustment")
        adjustment.conversion_action = action_rn
        adjustment.adjustment_type = getattr(
            enums.ConversionAdjustmentTypeEnum, row["type"]
        )
        adjustment.adjustment_date_time = row["time"]
        if row["order_id"]:
            adjustment.order_id = row["order_id"]
        else:
            adjustment.gclid_date_time_pair.gclid = row["gclid"]
            adjustment.gclid_date_time_pair.conversion_date_time = row[
                "conversion_time"
            ]
        if row["type"] == "RESTATEMENT":
            adjustment.restatement_value.adjusted_value = row["value"]
            adjustment.restatement_value.currency_code = currency
        request.conversion_adjustments.append(adjustment)
    service = utils.get_googleads_service_from_client(
        ga_client, "ConversionAdjustmentUploadService"
    )

    def _format(errors: dict[int, str]) -> str:
        return "\n- ".join(
            f"row {i}: {m}" if i >= 0 else m
            for i, m in sorted(errors.items())[:20]
        )

    def _validate() -> None:
        request.validate_only = True
        response = service.upload_conversion_adjustments(request=request)
        errors = _row_errors(ga_client, response.partial_failure_error)
        if errors:
            raise ToolError(
                "Error: Google rejected adjustments in the dry run:\n- "
                + _format(errors)
            )

    def _commit() -> str:
        request.validate_only = False
        response = service.upload_conversion_adjustments(request=request)
        errors = _row_errors(ga_client, response.partial_failure_error)
        accepted = len(rows) - len([i for i in errors if i >= 0])
        message = f"Uploaded {accepted} of {len(rows)} adjustments."
        if errors:
            message += "\nRejected:\n- " + _format(errors)
        return message

    def _describe(row: dict) -> str:
        key = row["order_id"] or f"gclid {row['gclid'][:6]}…"
        if row["type"] == "RESTATEMENT":
            return (
                f"{key}: new value {row['value']:.2f} {currency} "
                "(value-based bidding learns from it)"
            )
        return f"{key}: retracted"

    change_items = [
        pac.ChangeItem(
            "UPDATE",
            f"Conversion {r['type'].lower()}",
            f"{action_name} ({action_id})",
            r["time"],
            _describe(r),
        )
        for r in rows
    ]
    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "upload_conversion_adjustments",
            "conversion_action_id": action_id,
            "rows": rows,
        },
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to upload conversion adjustments",
    )


def _guard_value_rule(
    ga_client: Any, cid: str, act: str, amount: float
) -> None:
    """Caps value rules like a target ROAS change: raising conversion values
    makes value-based bidding bid more, so the increase per change is
    limited by the server's safety limit."""
    max_pct = mh.get_max_increase_pct()
    if max_pct is None:
        return
    limit = 1 + max_pct / 100
    if act == "MULTIPLY":
        if amount > limit:
            raise ToolError(
                f"Error: A factor of {amount:g} raises conversion values more "
                f"than the {max_pct:.0f}% safety limit; use at most "
                f"{limit:g}, or apply it in steps."
            )
        return
    rows = mh.search(
        ga_client,
        cid,
        "SELECT metrics.conversions, metrics.conversions_value FROM customer "
        "WHERE segments.date DURING LAST_30_DAYS",
    )
    conversions = sum(float(r.metrics.conversions) for r in rows)
    total = sum(float(r.metrics.conversions_value) for r in rows)
    if conversions <= 0 or total <= 0:
        raise ToolError(
            "Error: ADD and SET rules need an average conversion value as "
            "baseline, and this account has none in the last 30 days. Use a "
            "MULTIPLY rule instead."
        )
    average = total / conversions
    new_average = average + amount if act == "ADD" else amount
    if new_average > average * limit:
        raise ToolError(
            f"Error: This rule would raise the average conversion value from "
            f"{average:.2f} to {new_average:.2f}, more than the "
            f"{max_pct:.0f}% safety limit."
        )


class ValueRuleAction(str, Enum):
    ADD = "ADD"
    MULTIPLY = "MULTIPLY"
    SET = "SET"


class ValueRuleDevice(str, Enum):
    MOBILE = "MOBILE"
    DESKTOP = "DESKTOP"
    TABLET = "TABLET"


class ValueRuleOperation(str, Enum):
    CREATE = "CREATE"
    PAUSE = "PAUSE"
    ENABLE = "ENABLE"
    REMOVE = "REMOVE"


@conversions_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_conversion_value_rules(
    customer_id: str | int,
    operation: ValueRuleOperation = ValueRuleOperation.CREATE,
    action: ValueRuleAction | None = None,
    value: float | None = None,
    location_ids: list[str] | None = None,
    devices: list[ValueRuleDevice] | None = None,
    user_list_ids: list[str] | None = None,
    rule_ids: list[str] | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adjusts conversion values by location, device or audience.

    Value-based bidding then values conversions from, say, a high-margin
    region or existing customers higher. CREATE: `action` (MULTIPLY by
    `value`, e.g. 1.5; ADD `value` in the account currency; or SET the
    value) and exactly one condition: `location_ids`, `devices` or
    `user_list_ids`. The rule joins the account's value rule set (created
    when missing). PAUSE / ENABLE / REMOVE: `rule_ids` (search on
    conversion_value_rule).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        operation: CREATE (default), PAUSE, ENABLE or REMOVE.
        action: MULTIPLY, ADD or SET (CREATE).
        value: Factor (MULTIPLY, 0.1-10) or amount (ADD, SET). Increases
            are capped by the safety limit, like a target ROAS change.
        location_ids: Geo target constant IDs (condition).
        devices: MOBILE, DESKTOP and/or TABLET (condition).
        user_list_ids: User list IDs, e.g. Customer Match (condition).
        rule_ids: Existing rule IDs (PAUSE, ENABLE, REMOVE).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    op_value = mh.enum_value(operation, ValueRuleOperation, "operation")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    operations: list[Any] = []
    change_items: list[pac.ChangeItem] = []
    rules = {
        r.conversion_value_rule.resource_name: r.conversion_value_rule
        for r in mh.search(
            ga_client,
            cid,
            "SELECT conversion_value_rule.resource_name, "
            "conversion_value_rule.status, "
            "conversion_value_rule.action.operation, "
            "conversion_value_rule.action.value FROM conversion_value_rule "
            "WHERE conversion_value_rule.status != 'REMOVED'",
        )
    }
    sets = [
        r.conversion_value_rule_set
        for r in mh.search(
            ga_client,
            cid,
            "SELECT conversion_value_rule_set.resource_name, "
            "conversion_value_rule_set.conversion_value_rules, "
            "conversion_value_rule_set.dimensions, "
            "conversion_value_rule_set.status, "
            "conversion_value_rule_set.conversion_action_categories "
            "FROM conversion_value_rule_set WHERE "
            "conversion_value_rule_set.attachment_type = 'CUSTOMER' "
            "AND conversion_value_rule_set.status != 'REMOVED'",
        )
    ]

    def _set_op(rule_set: Any) -> Any:
        op = ga_client.get_type("MutateOperation")
        op.conversion_value_rule_set_operation.update.resource_name = (
            rule_set.resource_name
        )
        operations.append(op)
        return op.conversion_value_rule_set_operation

    def _set_status(rule_set: Any, status: str) -> None:
        set_op = _set_op(rule_set)
        set_op.update.status = getattr(
            enums.ConversionValueRuleSetStatusEnum, status
        )
        set_op.update_mask.paths.append("status")
        change_items.append(
            pac.ChangeItem(
                "UPDATE",
                "Value rule set",
                rule_set.resource_name,
                "Status",
                status,
            )
        )

    if op_value != "CREATE":
        ids = mh.require_ids(rule_ids, "rule_ids")
        rns = [f"customers/{cid}/conversionValueRules/{i}" for i in ids]
        missing = [rn for rn in rns if rn not in rules]
        if missing:
            raise ToolError(f"Error: Rules not found: {', '.join(missing)}")
        if op_value == "ENABLE":
            for rn in rns:
                _guard_value_rule(
                    ga_client,
                    cid,
                    mh.enum_name(rules[rn].action.operation),
                    float(rules[rn].action.value),
                )
        chosen = set(rns)
        rule_ops = []
        for rn in rns:
            op = ga_client.get_type("MutateOperation")
            rule_op = op.conversion_value_rule_operation
            if op_value == "REMOVE":
                rule_op.remove = rn
            else:
                rule_op.update.resource_name = rn
                rule_op.update.status = getattr(
                    enums.ConversionValueRuleStatusEnum,
                    "PAUSED" if op_value == "PAUSE" else "ENABLED",
                )
                rule_op.update_mask.paths.append("status")
            rule_ops.append(op)
            change_items.append(
                pac.ChangeItem(
                    op_value if op_value == "REMOVE" else "UPDATE",
                    "Value rule",
                    rn,
                    "Status",
                    {"PAUSE": "PAUSED", "ENABLE": "ENABLED"}.get(
                        op_value, "removed"
                    ),
                )
            )
        enabled_after = {
            rn
            for rn, rule in rules.items()
            if mh.enum_name(rule.status) == "ENABLED" and rn not in chosen
        } | (chosen if op_value == "ENABLE" else set())
        sets_after = []
        for rule_set in sets:
            members = list(rule_set.conversion_value_rules)
            if not chosen & set(members):
                continue
            status = mh.enum_name(rule_set.status)
            if op_value == "REMOVE":
                # A rule in a set cannot be removed; take it out first.
                remaining = [m for m in members if m not in chosen]
                if not remaining:
                    op = ga_client.get_type("MutateOperation")
                    op.conversion_value_rule_set_operation.remove = (
                        rule_set.resource_name
                    )
                    operations.append(op)
                    change_items.append(
                        pac.ChangeItem(
                            "REMOVE",
                            "Value rule set",
                            rule_set.resource_name,
                            "Rules",
                            "no rules left",
                        )
                    )
                    continue
                set_op = _set_op(rule_set)
                set_op.update.conversion_value_rules.extend(remaining)
                set_op.update_mask.paths.append("conversion_value_rules")
                if status == "ENABLED" and not set(remaining) & enabled_after:
                    set_op.update.status = (
                        enums.ConversionValueRuleSetStatusEnum.PAUSED
                    )
                    set_op.update_mask.paths.append("status")
            elif op_value == "PAUSE":
                # The set must be paused before its last enabled rule.
                if status == "ENABLED" and not set(members) & enabled_after:
                    _set_status(rule_set, "PAUSED")
            elif status == "PAUSED":
                sets_after.append(rule_set)
        operations.extend(rule_ops)
        for rule_set in sets_after:
            _set_status(rule_set, "ENABLED")
        payload: dict[str, Any] = {"rule_ids": ids}
    else:
        act = (
            mh.enum_value(action, ValueRuleAction, "action") if action else None
        )
        if not act or value is None:
            raise ToolError("Error: CREATE needs 'action' and 'value'.")
        amount = float(value)
        if act == "MULTIPLY" and not 0.1 <= amount <= 10:
            raise ToolError("Error: A MULTIPLY factor must be 0.1-10.")
        if act != "MULTIPLY" and amount < 0:
            raise ToolError("Error: 'value' must not be negative.")
        _guard_value_rule(ga_client, cid, act, amount)
        locations = (
            mh.require_ids(location_ids, "location_ids") if location_ids else []
        )
        device_values = [
            mh.enum_value(d, ValueRuleDevice, "devices") for d in devices or []
        ]
        lists = (
            mh.require_ids(user_list_ids, "user_list_ids")
            if user_list_ids
            else []
        )
        conditions = [c for c in (locations, device_values, lists) if c]
        if len(conditions) != 1:
            raise ToolError(
                "Error: Give exactly one condition: location_ids, devices or "
                "user_list_ids."
            )
        dimension = (
            "GEO_LOCATION"
            if locations
            else "DEVICE" if device_values else "AUDIENCE"
        )
        rule_rn = f"customers/{cid}/conversionValueRules/-1"
        op = ga_client.get_type("MutateOperation")
        rule = op.conversion_value_rule_operation.create
        rule.resource_name = rule_rn
        rule.action.operation = getattr(enums.ValueRuleOperationEnum, act)
        rule.action.value = amount
        if locations:
            rule.geo_location_condition.geo_target_constants.extend(
                f"geoTargetConstants/{i}" for i in locations
            )
            rule.geo_location_condition.geo_match_type = (
                enums.ValueRuleGeoLocationMatchTypeEnum.ANY
            )
        elif device_values:
            rule.device_condition.device_types.extend(
                getattr(enums.ValueRuleDeviceTypeEnum, d) for d in device_values
            )
        else:
            rule.audience_condition.user_lists.extend(
                f"customers/{cid}/userLists/{i}" for i in lists
            )
        operations.append(op)
        condition_text = (
            f"locations {', '.join(locations)}"
            if locations
            else (
                f"devices {', '.join(device_values)}"
                if device_values
                else f"user lists {', '.join(lists)}"
            )
        )
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Value rule",
                condition_text,
                "Action",
                f"{act} {amount:g}",
            )
        )

        # The general account set: enabled, not limited to store visits or
        # store sales (those are separate customer-level sets).
        general = [
            s
            for s in sets
            if mh.enum_name(s.status) == "ENABLED"
            and not list(s.conversion_action_categories)
        ]
        set_op = ga_client.get_type("MutateOperation")
        if general:
            current = general[0]
            update = set_op.conversion_value_rule_set_operation.update
            update.resource_name = current.resource_name
            update.conversion_value_rules.extend(
                list(current.conversion_value_rules) + [rule_rn]
            )
            dims = [mh.enum_name(d) for d in current.dimensions]
            paths = set_op.conversion_value_rule_set_operation.update_mask.paths
            paths.append("conversion_value_rules")
            if dimension not in dims:
                update.dimensions.extend(
                    getattr(enums.ValueRuleSetDimensionEnum, d)
                    for d in dims + [dimension]
                )
                paths.append("dimensions")
            set_label = current.resource_name
        else:
            create = set_op.conversion_value_rule_set_operation.create
            create.conversion_value_rules.append(rule_rn)
            create.dimensions.append(
                getattr(enums.ValueRuleSetDimensionEnum, dimension)
            )
            create.attachment_type = (
                enums.ValueRuleSetAttachmentTypeEnum.CUSTOMER
            )
            set_label = "new account value rule set"
        operations.append(set_op)
        change_items.append(
            pac.ChangeItem(
                "UPDATE" if general else "CREATE",
                "Value rule set",
                set_label,
                "Rules",
                "adds the new rule (account-wide)",
            )
        )
        payload = {
            "action": act,
            "value": amount,
            "location_ids": locations,
            "devices": device_values,
            "user_list_ids": lists,
        }

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_conversion_value_rules",
            "operation": op_value,
            **payload,
        },
        change_items=change_items,
        success_message=f"Applied {op_value} on conversion value rules.",
        error_prefix="Failed to manage conversion value rules",
    )
