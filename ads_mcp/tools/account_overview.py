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

"""Account overview: the starting point before analysing or changing an account."""

from collections import Counter
from enum import Enum
import json

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
import ads_mcp.utils as utils

account_mcp = FastMCP("account")

BUDGET_LOST_IS_FLAG = 0.10
MIN_SPEND_WITHOUT_CONVERSIONS = 100.0


class Period(str, Enum):
    LAST_7_DAYS = "LAST_7_DAYS"
    LAST_14_DAYS = "LAST_14_DAYS"
    LAST_30_DAYS = "LAST_30_DAYS"


def _money(micros: int) -> float:
    return round((micros or 0) / 1_000_000, 2)


@account_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_account_overview(
    customer_id: str | int,
    period: Period = Period.LAST_30_DAYS,
    login_customer_id: str | int | None = None,
) -> str:
    """Summarizes an account and flags what needs attention.

    Use this first when working on an account. Returns account settings and,
    per non-removed campaign: status and primary status (with reasons),
    channel, bidding strategy, daily budget, spend, clicks, conversions,
    value, CPA, ROAS and (for Search) impression share lost to budget.
    Flags: auto-tagging off (breaks offline conversion imports),
    campaigns limited by budget, campaigns spending without conversions,
    campaigns that are not eligible or limited, and disapproved enabled ads.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        period: LAST_7_DAYS, LAST_14_DAYS or LAST_30_DAYS (default).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    period_value = mh.enum_value(period, Period, "period")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)

    customer_rows = mh.search(
        ga_client,
        cid,
        "SELECT customer.descriptive_name, customer.currency_code, "
        "customer.time_zone, customer.auto_tagging_enabled FROM customer",
    )
    customer = customer_rows[0].customer if customer_rows else None
    currency = customer.currency_code if customer else ""

    # No status filter on metrics: campaigns paused during the period still
    # count; removed campaigns without activity are dropped below.
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.primary_status, campaign.primary_status_reasons, "
        "campaign.advertising_channel_type, campaign.bidding_strategy_type, "
        "campaign_budget.amount_micros, metrics.cost_micros, metrics.clicks, "
        "metrics.impressions, metrics.conversions, metrics.conversions_value, "
        "metrics.search_impression_share, "
        "metrics.search_budget_lost_impression_share FROM campaign "
        f"WHERE segments.date DURING {period_value}",
    )
    disapproved = Counter(
        row.campaign.id
        for row in mh.search(
            ga_client,
            cid,
            "SELECT campaign.id FROM ad_group_ad WHERE "
            "ad_group_ad.policy_summary.approval_status = 'DISAPPROVED' "
            "AND ad_group_ad.status = 'ENABLED'",
        )
    )

    campaigns = []
    flags: list[str] = []
    totals = Counter()
    if customer is not None and not customer.auto_tagging_enabled:
        flags.append(
            "Auto-tagging is off: GCLIDs are missing, so offline conversion "
            "imports and GA4 attribution break."
        )
    for row in rows:
        campaign = row.campaign
        metrics = row.metrics
        status = campaign.status.name
        if status == "REMOVED" and not metrics.impressions:
            continue
        cost = _money(metrics.cost_micros)
        conversions = round(metrics.conversions or 0, 1)
        value = round(metrics.conversions_value or 0, 2)
        lost_budget = metrics.search_budget_lost_impression_share or 0
        entry = {
            "id": campaign.id,
            "name": campaign.name,
            "status": status,
            "primary_status": campaign.primary_status.name,
            "primary_status_reasons": [
                r.name for r in campaign.primary_status_reasons
            ],
            "channel": campaign.advertising_channel_type.name,
            "bidding": campaign.bidding_strategy_type.name,
            "daily_budget": _money(row.campaign_budget.amount_micros),
            "cost": cost,
            "clicks": metrics.clicks,
            "conversions": conversions,
            "conversion_value": value,
            "cpa": round(cost / conversions, 2) if conversions else None,
            "roas": round(value / cost, 2) if cost and value else None,
            "search_impression_share": (
                round(metrics.search_impression_share, 3)
                if metrics.search_impression_share
                else None
            ),
            "search_lost_is_budget": (
                round(lost_budget, 3) if lost_budget else None
            ),
            "disapproved_ads": disapproved.get(campaign.id, 0),
        }
        campaigns.append(entry)
        totals["cost"] += cost
        totals["conversions"] += conversions
        totals["value"] += value

        if status == "ENABLED":
            if lost_budget > BUDGET_LOST_IS_FLAG:
                flags.append(
                    f"'{campaign.name}' loses {lost_budget:.0%} of search "
                    "impressions to budget."
                )
            if cost >= MIN_SPEND_WITHOUT_CONVERSIONS and not conversions:
                flags.append(
                    f"'{campaign.name}' spent {cost:.2f} {currency} without "
                    "conversions."
                )
            if campaign.primary_status.name in (
                "LIMITED",
                "NOT_ELIGIBLE",
                "MISCONFIGURED",
            ):
                reasons = ", ".join(entry["primary_status_reasons"]) or "-"
                flags.append(
                    f"'{campaign.name}' is {campaign.primary_status.name} "
                    f"({reasons})."
                )
        if entry["disapproved_ads"]:
            flags.append(
                f"'{campaign.name}' has {entry['disapproved_ads']} disapproved "
                "enabled ads."
            )

    campaigns.sort(key=lambda c: c["cost"], reverse=True)
    total_cost = round(totals["cost"], 2)
    total_conversions = round(totals["conversions"], 1)
    return json.dumps(
        {
            "account": {
                "name": customer.descriptive_name if customer else None,
                "currency": currency,
                "time_zone": customer.time_zone if customer else None,
                "auto_tagging": (
                    customer.auto_tagging_enabled if customer else None
                ),
            },
            "period": period_value,
            "totals": {
                "cost": total_cost,
                "conversions": total_conversions,
                "conversion_value": round(totals["value"], 2),
                "cpa": (
                    round(total_cost / total_conversions, 2)
                    if total_conversions
                    else None
                ),
            },
            "campaigns": campaigns,
            "flags": flags,
        },
        indent=2,
        ensure_ascii=False,
    )
