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

"""Campaign automation: AI Max, final URL expansion, text automation,
URL exclusions and frequency caps.

Registered on the ``campaign_management`` namespace.
"""

from enum import Enum
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_management import campaign_management_mcp
import ads_mcp.utils as utils

MAX_URL_EXCLUSIONS = 50
_AUTOMATION_CHANNELS = ("SEARCH", "PERFORMANCE_MAX")
_FREQUENCY_CHANNELS = ("DISPLAY", "DEMAND_GEN", "VIDEO")


def _on_off(value: bool) -> str:
    return "OPTED_IN" if value else "OPTED_OUT"


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def update_campaign_automation(
    customer_id: str | int,
    campaign_id: str,
    ai_max: bool | None = None,
    final_url_expansion: bool | None = None,
    text_asset_automation: bool | None = None,
    add_url_exclusions: list[str] | None = None,
    remove_url_exclusion_ids: list[str] | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Turns Google's automation on or off for a Search or PMax campaign.

    - `ai_max` (Search): AI Max for Search, which matches more searches and
      adapts ads; use brand lists (manage_brand_list) to keep it in check.
    - `final_url_expansion`: let Google send people to other pages of the
      site than the final URL (PMax, and Search with AI Max).
    - `text_asset_automation`: let Google write extra headlines and
      descriptions from the landing page (PMax, and Search with AI Max).
    - `add_url_exclusions`: pages that final URL expansion must never use,
      as URL parts like '/vacatures' or '/blog' (excluded when the URL
      contains them). Remove with `remove_url_exclusion_ids`
      (campaign_criterion.criterion_id of type WEBPAGE).
    Pass only what to change.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_id: Search or Performance Max campaign ID.
        ai_max: Turn AI Max on (True) or off (False). Search only.
        final_url_expansion: Allow (True) or block (False) other pages.
        text_asset_automation: Allow (True) or block (False) generated text.
        add_url_exclusions: URL parts to exclude from final URL expansion.
        remove_url_exclusion_ids: Criterion IDs of URL exclusions to remove.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    camp_id = mh.require_simple_id(campaign_id, "campaign_id")
    exclusions = []
    for raw in add_url_exclusions or []:
        text = str(raw).strip()
        if not text:
            continue
        if " " in text or "'" in text or len(text) > 200:
            raise ToolError(f"Error: Invalid URL part '{text}'.")
        if text not in exclusions:
            exclusions.append(text)
    if len(exclusions) > MAX_URL_EXCLUSIONS:
        raise ToolError(f"Error: At most {MAX_URL_EXCLUSIONS} URL exclusions.")
    remove_ids = (
        mh.require_ids(remove_url_exclusion_ids, "remove_url_exclusion_ids")
        if remove_url_exclusion_ids
        else []
    )
    if (
        ai_max is None
        and final_url_expansion is None
        and text_asset_automation is None
        and not exclusions
        and not remove_ids
    ):
        raise ToolError("Error: Pass at least one setting to change.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign.name, campaign.advertising_channel_type, "
        "campaign.ai_max_setting.enable_ai_max, "
        "campaign.asset_automation_settings FROM campaign "
        f"WHERE campaign.id = {camp_id}",
    )
    if not rows:
        raise ToolError(f"Error: Campaign {camp_id} was not found.")
    campaign = rows[0].campaign
    channel = mh.enum_name(campaign.advertising_channel_type)
    if channel not in _AUTOMATION_CHANNELS:
        raise ToolError(
            f"Error: Campaign {camp_id} is {channel}; these settings apply "
            "to Search and Performance Max."
        )
    if ai_max is not None and channel != "SEARCH":
        raise ToolError("Error: AI Max is a Search campaign setting.")
    ai_max_after = (
        ai_max
        if ai_max is not None
        else bool(campaign.ai_max_setting.enable_ai_max)
    )
    if (
        channel == "SEARCH"
        and not ai_max_after
        and (final_url_expansion or text_asset_automation)
    ):
        raise ToolError(
            "Error: On Search, final URL expansion and text automation are "
            "part of AI Max; turn on ai_max as well."
        )

    enums = ga_client.enums
    label = f"{campaign.name} ({camp_id})"
    rn = f"customers/{cid}/campaigns/{camp_id}"
    operations: list[Any] = []
    fields: dict[str, Any] = {}
    original: dict[str, Any] = {}

    current = {
        mh.enum_name(s.asset_automation_type): mh.enum_name(
            s.asset_automation_status
        )
        for s in campaign.asset_automation_settings
    }
    wanted = dict(current)
    for automation_type, value, readable in (
        (
            "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
            final_url_expansion,
            "Final URL expansion",
        ),
        ("TEXT_ASSET_AUTOMATION", text_asset_automation, "Text automation"),
    ):
        if value is None:
            continue
        wanted[automation_type] = _on_off(value)
        fields[readable] = "on" if value else "off"
        original[readable] = {
            "OPTED_IN": "on",
            "OPTED_OUT": "off",
        }.get(current.get(automation_type, ""), "(default)")

    if ai_max is not None or wanted != current:
        op = ga_client.get_type("MutateOperation")
        update = op.campaign_operation.update
        update.resource_name = rn
        if ai_max is not None:
            update.ai_max_setting.enable_ai_max = ai_max
            op.campaign_operation.update_mask.paths.append(
                "ai_max_setting.enable_ai_max"
            )
            fields["AI Max"] = "on" if ai_max else "off"
            original["AI Max"] = (
                "on" if campaign.ai_max_setting.enable_ai_max else "off"
            )
        if wanted != current:
            for automation_type, status in wanted.items():
                setting = type(
                    ga_client.get_type("Campaign")
                ).AssetAutomationSetting()
                setting.asset_automation_type = getattr(
                    enums.AssetAutomationTypeEnum, automation_type
                )
                setting.asset_automation_status = getattr(
                    enums.AssetAutomationStatusEnum, status
                )
                update.asset_automation_settings.append(setting)
            op.campaign_operation.update_mask.paths.append(
                "asset_automation_settings"
            )
        operations.append(op)

    change_items = (
        pac.build_change_items(
            operation="UPDATE",
            entity_readable="Campaign automation",
            primary_key_readable=label,
            fields=fields,
            original_fields=original,
        )
        if fields
        else []
    )

    for text in exclusions:
        op = ga_client.get_type("MutateOperation")
        criterion = op.campaign_criterion_operation.create
        criterion.campaign = rn
        criterion.negative = True
        criterion.webpage.criterion_name = f"Exclude {text}"[:100]
        condition = ga_client.get_type("WebpageConditionInfo")
        condition.operand = enums.WebpageConditionOperandEnum.URL
        condition.operator = enums.WebpageConditionOperatorEnum.CONTAINS
        condition.argument = text
        criterion.webpage.conditions.append(condition)
        operations.append(op)
        change_items.append(
            pac.ChangeItem(
                "CREATE", "URL exclusion", label, "URL contains", text
            )
        )
    if remove_ids:
        rns = [
            f"customers/{cid}/campaignCriteria/{camp_id}~{c}"
            for c in remove_ids
        ]
        found = mh.search(
            ga_client,
            cid,
            "SELECT campaign_criterion.resource_name, "
            "campaign_criterion.webpage.criterion_name FROM campaign_criterion "
            "WHERE campaign_criterion.type = 'WEBPAGE' "
            "AND campaign_criterion.negative = TRUE "
            f"AND campaign_criterion.resource_name IN ({mh.quoted_list(rns)})",
        )
        names = {
            r.campaign_criterion.resource_name: r.campaign_criterion.webpage.criterion_name
            for r in found
        }
        missing = [r for r in rns if r not in names]
        if missing:
            raise ToolError(
                "Error: Not URL exclusions of this campaign: "
                + ", ".join(missing)
            )
        for crit_rn in rns:
            op = ga_client.get_type("MutateOperation")
            op.campaign_criterion_operation.remove = crit_rn
            operations.append(op)
            change_items.append(
                pac.ChangeItem(
                    "REMOVE",
                    "URL exclusion",
                    label,
                    "Exclusion",
                    names[crit_rn],
                )
            )

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_campaign_automation",
            "campaign_id": camp_id,
            "ai_max": ai_max,
            "final_url_expansion": final_url_expansion,
            "text_asset_automation": text_asset_automation,
            "add_url_exclusions": exclusions,
            "remove_url_exclusion_ids": remove_ids,
        },
        change_items=change_items,
        success_message=f"Updated automation of campaign {camp_id}.",
        error_prefix="Failed to update campaign automation",
    )


class FrequencyEvent(str, Enum):
    IMPRESSION = "IMPRESSION"
    VIDEO_VIEW = "VIDEO_VIEW"


class FrequencyTimeUnit(str, Enum):
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


class FrequencyCapInput(BaseModel):
    """At most `max_events` per person per time unit."""

    max_events: int = Field(description="Maximum per person, e.g. 3.")
    time_unit: FrequencyTimeUnit = Field(
        default=FrequencyTimeUnit.WEEK, description="DAY, WEEK or MONTH."
    )
    event: FrequencyEvent = Field(
        default=FrequencyEvent.IMPRESSION,
        description="IMPRESSION (default) or VIDEO_VIEW.",
    )


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def set_frequency_caps(
    customer_id: str | int,
    campaign_id: str,
    caps: list[FrequencyCapInput],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Limits how often one person sees a Display or Demand Gen campaign.

    Replaces the campaign's frequency caps; pass an empty list to remove
    them. Example: [{{"max_events": 3, "time_unit": "WEEK"}}] for at most
    three impressions per person per week.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_id: Display or Demand Gen campaign ID.
        caps: Up to 3 caps (one per event and time unit).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    camp_id = mh.require_simple_id(campaign_id, "campaign_id")
    if len(caps or []) > 3:
        raise ToolError("Error: At most 3 frequency caps.")
    clean = []
    seen = set()
    for raw in caps or []:
        cap = FrequencyCapInput(**raw) if isinstance(raw, dict) else raw
        if not 1 <= int(cap.max_events) <= 255:
            raise ToolError("Error: 'max_events' must be 1-255.")
        unit = mh.enum_value(cap.time_unit, FrequencyTimeUnit, "time_unit")
        event = mh.enum_value(cap.event, FrequencyEvent, "event")
        if (unit, event) in seen:
            raise ToolError("Error: One cap per event and time unit.")
        seen.add((unit, event))
        clean.append({"max": int(cap.max_events), "unit": unit, "event": event})

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign.name, campaign.advertising_channel_type, "
        f"campaign.frequency_caps FROM campaign WHERE campaign.id = {camp_id}",
    )
    if not rows:
        raise ToolError(f"Error: Campaign {camp_id} was not found.")
    campaign = rows[0].campaign
    channel = mh.enum_name(campaign.advertising_channel_type)
    if channel not in _FREQUENCY_CHANNELS:
        raise ToolError(
            f"Error: Frequency caps apply to Display and Demand Gen, not "
            f"{channel}."
        )

    def _describe(entries) -> str:
        return (
            "; ".join(
                f"{e['max']} {e['event'].lower()}s per {e['unit'].lower()}"
                for e in entries
            )
            or "(none)"
        )

    current = [
        {
            "max": int(c.cap),
            "unit": mh.enum_name(c.key.time_unit),
            "event": mh.enum_name(c.key.event_type),
        }
        for c in campaign.frequency_caps
    ]
    enums = ga_client.enums
    op = ga_client.get_type("MutateOperation")
    update = op.campaign_operation.update
    update.resource_name = f"customers/{cid}/campaigns/{camp_id}"
    for entry in clean:
        cap = ga_client.get_type("FrequencyCapEntry")
        cap.cap = entry["max"]
        cap.key.level = enums.FrequencyCapLevelEnum.CAMPAIGN
        cap.key.event_type = getattr(
            enums.FrequencyCapEventTypeEnum, entry["event"]
        )
        cap.key.time_unit = getattr(
            enums.FrequencyCapTimeUnitEnum, entry["unit"]
        )
        cap.key.time_length = 1
        update.frequency_caps.append(cap)
    op.campaign_operation.update_mask.paths.append("frequency_caps")

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "set_frequency_caps",
            "campaign_id": camp_id,
            "caps": clean,
        },
        change_items=pac.build_change_items(
            operation="UPDATE",
            entity_readable="Frequency caps",
            primary_key_readable=f"{campaign.name} ({camp_id})",
            fields={"Caps": _describe(clean)},
            original_fields={"Caps": _describe(current)},
        ),
        success_message=f"Set frequency caps on campaign {camp_id}.",
        error_prefix="Failed to set frequency caps",
    )
