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
import datetime
from enum import Enum
import json

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
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


MAX_CHANGE_ROWS = 500


@account_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_change_history(
    customer_id: str | int,
    days: int = 7,
    campaign_id: str | None = None,
    limit: int = 100,
    login_customer_id: str | int | None = None,
) -> str:
    """Lists recent changes in the account: what changed, when and by whom.

    Covers changes made in the Google Ads UI, through the API (including this
    server) and by Google (e.g. auto-applied recommendations). Google keeps
    the change history for 30 days. Use it to answer "what changed before the
    drop?" or to check that a change went through. Pass a change_id
    to revert_change to undo an update.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        days: How many days back, 1-30 (default 7).
        campaign_id: Optional campaign ID to limit the history to.
        limit: Maximum number of changes, 1-500 (default 100, newest first).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    if not 1 <= int(days) <= 30:
        raise ToolError("Error: 'days' must be 1-30.")
    if not 1 <= int(limit) <= MAX_CHANGE_ROWS:
        raise ToolError(f"Error: 'limit' must be 1-{MAX_CHANGE_ROWS}.")
    campaign_filter = ""
    if campaign_id:
        campaign_filter = (
            " AND campaign.id = "
            f"{mh.require_simple_id(campaign_id, 'campaign_id')}"
        )
    today = datetime.date.today()
    start = today - datetime.timedelta(days=int(days) - 1)
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT change_event.resource_name, "
        "change_event.change_date_time, "
        "change_event.change_resource_type, "
        "change_event.change_resource_name, change_event.client_type, "
        "change_event.user_email, change_event.resource_change_operation, "
        "change_event.changed_fields, campaign.name FROM change_event "
        f"WHERE change_event.change_date_time >= '{start.isoformat()}' "
        f"AND change_event.change_date_time <= '{today.isoformat()} 23:59:59'"
        f"{campaign_filter} "
        f"ORDER BY change_event.change_date_time DESC LIMIT {int(limit)}",
    )
    changes = []
    for row in rows:
        event = row.change_event
        changes.append(
            {
                "change_id": event.resource_name,
                "time": event.change_date_time,
                "operation": event.resource_change_operation.name,
                "resource_type": event.change_resource_type.name,
                "resource": event.change_resource_name,
                "campaign": row.campaign.name or None,
                "changed_fields": list(event.changed_fields.paths),
                "by": event.user_email or None,
                "via": event.client_type.name,
            }
        )
    return json.dumps(
        {
            "from": start.isoformat(),
            "to": today.isoformat(),
            "count": len(changes),
            "changes": changes,
        },
        indent=2,
        ensure_ascii=False,
    )


@account_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_locations(
    customer_id: str | int,
    names: list[str],
    country_code: str | None = "BE",
    locale: str = "nl",
    login_customer_id: str | int | None = None,
) -> str:
    """Finds location IDs for targeting by name (cities, regions, countries).

    Returns geo target constant IDs with their full name, type and reach, to
    use as location_ids in the campaign tools. Searches in the given country
    (default BE; pass null for worldwide) and language.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        names: Up to 25 place names, e.g. ["Gent", "Oost-Vlaanderen"].
        country_code: Optional ISO country code to search in (default BE).
        locale: Language of the names, e.g. nl, fr or en (default nl).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    mh.require_customer_id(customer_id)
    places = mh.clean_texts(names, "names", 25)
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    request = ga_client.get_type("SuggestGeoTargetConstantsRequest")
    request.locale = (locale or "nl").strip().lower()
    if country_code:
        request.country_code = country_code.strip().upper()
    request.location_names.names.extend(places)
    service = utils.get_googleads_service_from_client(
        ga_client, "GeoTargetConstantService"
    )
    try:
        response = service.suggest_geo_target_constants(request=request)
    except GoogleAdsException as exc:
        raise ToolError(
            f"Location search failed:\n{utils.format_google_ads_error(exc)}"
        ) from exc
    results = []
    for suggestion in response.geo_target_constant_suggestions:
        constant = suggestion.geo_target_constant
        results.append(
            {
                "search_term": suggestion.search_term,
                "id": constant.id,
                "name": constant.canonical_name or constant.name,
                "type": constant.target_type,
                "country": constant.country_code,
                "status": constant.status.name,
                "reach": suggestion.reach or None,
            }
        )
    return json.dumps({"locations": results}, indent=2, ensure_ascii=False)
