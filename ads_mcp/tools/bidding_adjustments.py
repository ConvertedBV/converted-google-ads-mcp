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

"""Smart bidding adjustments: data exclusions and seasonality adjustments.

Registered on the ``campaign_management`` namespace.
"""

import datetime
from enum import Enum
import re

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_management import campaign_management_mcp
import ads_mcp.utils as utils

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")
MAX_SEASONALITY_DAYS = 14

_KINDS = {
    "DATA_EXCLUSION": (
        "BiddingDataExclusion",
        "biddingDataExclusions",
        "bidding_data_exclusion",
        "data_exclusion_id",
    ),
    "SEASONALITY_ADJUSTMENT": (
        "BiddingSeasonalityAdjustment",
        "biddingSeasonalityAdjustments",
        "bidding_seasonality_adjustment",
        "seasonality_adjustment_id",
    ),
}


_REQUESTS = {
    "DATA_EXCLUSION": (
        "MutateBiddingDataExclusionsRequest",
        "mutate_bidding_data_exclusions",
    ),
    "SEASONALITY_ADJUSTMENT": (
        "MutateBiddingSeasonalityAdjustmentsRequest",
        "mutate_bidding_seasonality_adjustments",
    ),
}


class AdjustmentKind(str, Enum):
    """DATA_EXCLUSION: tell smart bidding to ignore a period with broken
    conversion data. SEASONALITY_ADJUSTMENT: tell it to expect a temporary
    change in conversion rate (e.g. a promotion)."""

    DATA_EXCLUSION = "DATA_EXCLUSION"
    SEASONALITY_ADJUSTMENT = "SEASONALITY_ADJUSTMENT"


class AdjustmentOperation(str, Enum):
    CREATE = "CREATE"
    REMOVE = "REMOVE"


class AdjustmentChannel(str, Enum):
    SEARCH = "SEARCH"
    DISPLAY = "DISPLAY"
    SHOPPING = "SHOPPING"
    PERFORMANCE_MAX = "PERFORMANCE_MAX"


def _datetime(value: str, arg_name: str, end: bool) -> str:
    cleaned = (value or "").strip()
    if not _DATETIME_RE.match(cleaned):
        raise ToolError(
            f"Error: '{arg_name}' must be 'YYYY-MM-DD' or "
            "'YYYY-MM-DD HH:MM:SS' (account time zone)."
        )
    if len(cleaned) == 10:
        cleaned += " 23:59:59" if end else " 00:00:00"
    return cleaned


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_bidding_adjustments(
    customer_id: str | int,
    kind: AdjustmentKind,
    operation: AdjustmentOperation,
    name: str | None = None,
    start: str | None = None,
    end: str | None = None,
    campaign_ids: list[str] | None = None,
    channels: list[AdjustmentChannel] | None = None,
    conversion_rate_modifier: float | None = None,
    description: str | None = None,
    adjustment_id: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates or removes smart bidding data exclusions and seasonality
    adjustments.

    - DATA_EXCLUSION: for a period where conversion tracking was broken
      (e.g. a missing tag), so smart bidding ignores that data. Can be in the
      past or future.
    - SEASONALITY_ADJUSTMENT: for a short future event (max 14 days, ideally
      1-7) with an expected conversion rate change, e.g. 1.5 for +50% during
      a sale. Not for regular seasonality, which bidding learns itself.
    Scope: `campaign_ids` (those campaigns), else `channels` (all campaigns
    of those types), else the whole account. REMOVE needs `adjustment_id`
    (from bidding_data_exclusion / bidding_seasonality_adjustment).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        kind: DATA_EXCLUSION or SEASONALITY_ADJUSTMENT.
        operation: CREATE or REMOVE.
        name: Name (CREATE).
        start: Start 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS' (CREATE).
        end: End, same format; a date means end of that day (CREATE).
        campaign_ids: Optional campaigns to limit the scope to.
        channels: Optional channel types (SEARCH, DISPLAY, SHOPPING,
            PERFORMANCE_MAX) when no campaign_ids are given.
        conversion_rate_modifier: Expected conversion rate factor, 0.1-10
            (SEASONALITY_ADJUSTMENT only).
        description: Optional description.
        adjustment_id: ID to remove (REMOVE).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    kind_value = mh.enum_value(kind, AdjustmentKind, "kind")
    op_value = mh.enum_value(operation, AdjustmentOperation, "operation")
    type_name, prefix, label_resource, id_field = _KINDS[kind_value]
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    op = ga_client.get_type(f"{type_name}Operation")

    if op_value == "REMOVE":
        item_id = mh.require_simple_id(adjustment_id, "adjustment_id")
        rows = mh.search(
            ga_client,
            cid,
            f"SELECT {label_resource}.name, {label_resource}.start_date_time, "
            f"{label_resource}.end_date_time FROM {label_resource} "
            f"WHERE {label_resource}.{id_field} = {item_id}",
        )
        if not rows:
            raise ToolError(f"Error: {kind_value} {item_id} was not found.")
        current = getattr(rows[0], label_resource)
        op.remove = f"customers/{cid}/{prefix}/{item_id}"
        change_items = [
            pac.ChangeItem(
                "REMOVE",
                kind_value.replace("_", " ").title(),
                current.name,
                "Period",
                f"{current.start_date_time} - {current.end_date_time}",
            )
        ]
        payload = {"adjustment_id": item_id}
    else:
        clean_name = " ".join((name or "").split())
        if not clean_name:
            raise ToolError("Error: 'name' is required for CREATE.")
        start_value = _datetime(start, "start", end=False)
        end_value = _datetime(end, "end", end=True)
        start_dt = datetime.datetime.fromisoformat(start_value)
        end_dt = datetime.datetime.fromisoformat(end_value)
        if end_dt <= start_dt:
            raise ToolError("Error: 'end' must be after 'start'.")
        campaigns = (
            mh.require_ids(campaign_ids, "campaign_ids") if campaign_ids else []
        )
        channel_values = mh.dedupe(
            [
                mh.enum_value(c, AdjustmentChannel, "channels")
                for c in channels or []
            ]
        )
        if campaigns and channel_values:
            raise ToolError("Error: Pass either 'campaign_ids' or 'channels'.")
        modifier = None
        if kind_value == "SEASONALITY_ADJUSTMENT":
            if conversion_rate_modifier is None:
                raise ToolError(
                    "Error: 'conversion_rate_modifier' is required for a "
                    "seasonality adjustment."
                )
            modifier = round(float(conversion_rate_modifier), 2)
            if not 0.1 <= modifier <= 10:
                raise ToolError(
                    "Error: 'conversion_rate_modifier' must be 0.1-10."
                )
            if (end_dt - start_dt).days >= MAX_SEASONALITY_DAYS:
                raise ToolError(
                    f"Error: Seasonality adjustments may last at most "
                    f"{MAX_SEASONALITY_DAYS} days."
                )
        elif conversion_rate_modifier is not None:
            raise ToolError(
                "Error: 'conversion_rate_modifier' only applies to "
                "SEASONALITY_ADJUSTMENT."
            )

        item = op.create
        item.name = clean_name
        if description:
            item.description = description.strip()
        item.start_date_time = start_value
        item.end_date_time = end_value
        if campaigns:
            item.scope = enums.SeasonalityEventScopeEnum.CAMPAIGN
            item.campaigns.extend(
                f"customers/{cid}/campaigns/{c}" for c in campaigns
            )
            scope_text = f"campaigns {', '.join(campaigns)}"
        elif channel_values:
            item.scope = enums.SeasonalityEventScopeEnum.CHANNEL
            item.advertising_channel_types.extend(
                getattr(enums.AdvertisingChannelTypeEnum, c)
                for c in channel_values
            )
            scope_text = f"all {', '.join(channel_values)} campaigns"
        else:
            item.scope = enums.SeasonalityEventScopeEnum.CUSTOMER
            scope_text = "whole account"
        if modifier is not None:
            item.conversion_rate_modifier = modifier
        change_items = pac.build_change_items(
            operation="CREATE",
            entity_readable=kind_value.replace("_", " ").title(),
            primary_key_readable=clean_name,
            fields={
                "Period": f"{start_value} - {end_value}",
                "Scope": scope_text,
                "Conversion rate modifier": modifier,
                "Description": description,
            },
        )
        payload = {
            "name": clean_name,
            "start": start_value,
            "end": end_value,
            "campaigns": campaigns,
            "channels": channel_values,
            "modifier": modifier,
            "description": description,
        }

    plural = "Exclusions" if kind_value == "DATA_EXCLUSION" else "Adjustments"
    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name=f"{type_name}Service",
        request_type=_REQUESTS[kind_value][0],
        mutate_method=_REQUESTS[kind_value][1],
        operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_bidding_adjustments",
            "kind": kind_value,
            "operation": op_value,
            **payload,
        },
        change_items=change_items,
        success_message=f"Bidding {plural.lower()} updated ({op_value}).",
        error_prefix=f"Failed to {op_value.lower()} {kind_value.lower()}",
    )
