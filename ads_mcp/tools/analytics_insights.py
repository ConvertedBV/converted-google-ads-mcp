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

"""Opinionated GA4 analyses built on the Data and Admin APIs (read only)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp.analytics import client as ga
from ads_mcp.analytics import reports as rep

analytics_insights_mcp = FastMCP("analytics_insights")

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

_PAYMENT_REFERRERS = (
    "mollie",
    "stripe",
    "paypal",
    "adyen",
    "ogone",
    "ingenico",
    "multisafepay",
    "buckaroo",
    "worldline",
    "payconiq",
    "bancontact",
    "klarna",
    "sisow",
    "pay.nl",
    "checkout.com",
    "viva",
)


def _run(
    property_id: Any,
    dimensions: list[str],
    metrics: list[str],
    date_ranges: list[dict[str, str]],
    *,
    dimension_filter: dict | None = None,
    order_bys: list[dict] | None = None,
    limit: int = 10_000,
) -> dict[str, Any]:
    from google.analytics.data_v1beta import types

    request = types.RunReportRequest(
        property=ga.property_name(property_id),
        dimensions=[{"name": d} for d in dimensions],
        metrics=[{"name": m} for m in metrics],
        date_ranges=date_ranges,
        dimension_filter=dimension_filter,
        order_bys=order_bys or [],
        limit=limit,
        metric_aggregations=["TOTAL"],
    )
    resp = ga.call_api(ga.data_client().run_report, request=request)
    return rep.flatten_report(resp)


def _key_event_names(property_id: Any) -> list[str]:
    client = ga.admin_client()
    try:
        events = client.list_key_events(parent=ga.property_name(property_id))
        return [e.event_name for e in events]
    except Exception:  # noqa: BLE001 - optional enrichment
        return []


def _ratio(num: Any, den: Any, digits: int = 4) -> float | None:
    try:
        if not den:
            return None
        return round(float(num) / float(den), digits)
    except (TypeError, ValueError):
        return None


def _previous_range(start: str, end: str) -> dict[str, str] | None:
    try:
        s = dt.date.fromisoformat(start)
        e = dt.date.fromisoformat(end)
    except ValueError:
        return None
    days = (e - s).days + 1
    pe = s - dt.timedelta(days=1)
    ps = pe - dt.timedelta(days=days - 1)
    return {"start_date": ps.isoformat(), "end_date": pe.isoformat()}


def _concrete_range(period: str | None, start: str | None, end: str | None):
    ranges = rep.build_date_ranges(start, end, period or "LAST_28_DAYS", None)
    rng = ranges[0]
    today = dt.date.today()

    def _abs(value: str) -> str:
        if value == "today":
            return today.isoformat()
        if value == "yesterday":
            return (today - dt.timedelta(days=1)).isoformat()
        if value.endswith("daysAgo"):
            return (today - dt.timedelta(days=int(value[:-7]))).isoformat()
        return value

    return {
        "start_date": _abs(rng["start_date"]),
        "end_date": _abs(rng["end_date"]),
    }


@analytics_insights_mcp.tool(annotations=_READ)
def channel_performance(
    property_id: str | int,
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    by_month: bool = True,
    channel_dimension: str = "sessionDefaultChannelGroup",
    key_events: list[str] | None = None,
    channels: list[str] | None = None,
) -> str:
    """Per channel (and month): sessions, key events per event, purchases, revenue.

    The standard table to compare e.g. SEO (Organic Search) with Google Ads
    (Paid Search + Cross-network/Performance Max), including the share of
    total revenue and conversion rates. Attribution is GA4's session-scoped
    last non-direct click; Google Ads itself counts differently.

    Args:
        property_id: GA4 property ID.
        period / start_date / end_date: As in run_report (default
            LAST_28_DAYS).
        by_month: Split per month (dimension yearMonth).
        channel_dimension: sessionDefaultChannelGroup (default),
            sessionPrimaryChannelGroup, sessionSourceMedium, sessionSource,
            or a custom channel group 'sessionCustomChannelGroup:<id>'.
        key_events: Key event names to count separately (default: all key
            events of the property, max 15).
        channels: Optional list of channel values to keep (e.g.
            ['Organic Search','Paid Search','Cross-network']).
    """
    rng = rep.build_date_ranges(start_date, end_date, period, None)
    events = list(key_events or _key_event_names(property_id))[:15]
    dims = (["yearMonth"] if by_month else []) + [channel_dimension]
    dim_filter = None
    if channels:
        dim_filter = rep.build_filter(
            [{"field": channel_dimension, "op": "IN_LIST", "values": channels}],
            "channels",
        )
    base = [
        "sessions",
        "engagedSessions",
        "totalUsers",
        "keyEvents",
        "ecommercePurchases",
        "purchaseRevenue",
        "totalRevenue",
    ]
    metric_sets = [base]
    ev_metrics = [f"keyEvents:{e}" for e in events]
    for i in range(0, len(ev_metrics), 10):
        metric_sets.append(ev_metrics[i : i + 10])

    merged: dict[tuple, dict[str, Any]] = {}
    totals: dict[str, Any] = {}
    for mets in metric_sets:
        data = _run(property_id, dims, mets, rng, dimension_filter=dim_filter)
        for row in data["rows"]:
            key = tuple(row.get(d) for d in dims)
            merged.setdefault(key, {d: row.get(d) for d in dims}).update(
                {m: row.get(m) for m in mets}
            )
        if data.get("totals"):
            totals.update({m: data["totals"][0].get(m) for m in mets})

    rows = list(merged.values())
    total_rev = totals.get("totalRevenue") or 0
    for row in rows:
        row["session_key_event_rate"] = _ratio(
            row.get("keyEvents"), row.get("sessions")
        )
        row["session_purchase_rate"] = _ratio(
            row.get("ecommercePurchases"), row.get("sessions")
        )
        row["revenue_per_session"] = _ratio(
            row.get("totalRevenue"), row.get("sessions"), 2
        )
        row["share_of_total_revenue"] = _ratio(
            row.get("totalRevenue"), total_rev
        )
    rows.sort(
        key=lambda r: (
            r.get("yearMonth", ""),
            -(r.get("totalRevenue") or 0),
            -(r.get("sessions") or 0),
        )
    )
    return ga.to_json(
        {
            "date_range": rng[0],
            "channel_dimension": channel_dimension,
            "key_events_counted": events,
            "rows": rows,
            "totals": totals,
            "notes": [
                "keyEvents counts all key events; keyEvents:<name> per event.",
                "purchaseRevenue = ecommerce purchases; totalRevenue also "
                "includes other revenue (e.g. ad revenue, subscriptions).",
                "Revenue values are as sent by the site (check VAT in/excl).",
            ],
        }
    )


@analytics_insights_mcp.tool(annotations=_READ)
def property_overview(
    property_id: str | int,
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """One-call summary of a property, compared with the previous period.

    Totals (users, sessions, engagement, key events, purchases, revenue),
    top channels, top landing pages, key events by name, devices, and flags
    for data problems (unassigned traffic, '(not set)' landing pages,
    payment providers showing up as referral, missing key events).

    Args:
        property_id: GA4 property ID.
        period / start_date / end_date: As in run_report (default
            LAST_28_DAYS); the previous period has the same length.
    """
    cur = _concrete_range(period, start_date, end_date)
    prev = _previous_range(cur["start_date"], cur["end_date"])
    ranges = [dict(cur, name="current")] + (
        [dict(prev, name="previous")] if prev else []
    )
    tot_metrics = [
        "totalUsers",
        "newUsers",
        "sessions",
        "engagedSessions",
        "engagementRate",
        "averageSessionDuration",
        "keyEvents",
        "ecommercePurchases",
        "purchaseRevenue",
        "totalRevenue",
    ]
    totals_data = _run(property_id, [], tot_metrics, ranges)
    by_range = {r.get("dateRange", "current"): r for r in totals_data["rows"]}
    current = by_range.get("current", {})
    previous = by_range.get("previous", {})
    totals = {}
    for m in tot_metrics:
        c, p = current.get(m), previous.get(m)
        change = None
        if isinstance(c, (int, float)) and isinstance(p, (int, float)) and p:
            change = round((c - p) / p, 4)
        totals[m] = {"current": c, "previous": p, "change": change}

    one = [cur]
    channels = _run(
        property_id,
        ["sessionDefaultChannelGroup"],
        [
            "sessions",
            "engagedSessions",
            "keyEvents",
            "ecommercePurchases",
            "totalRevenue",
        ],
        one,
        order_bys=[{"metric": {"metric_name": "sessions"}, "desc": True}],
        limit=15,
    )["rows"]
    landing = _run(
        property_id,
        ["landingPagePlusQueryString"],
        ["sessions", "keyEvents", "totalRevenue"],
        one,
        order_bys=[{"metric": {"metric_name": "sessions"}, "desc": True}],
        limit=15,
    )["rows"]
    events = _run(
        property_id,
        ["eventName"],
        ["eventCount", "keyEvents"],
        one,
        order_bys=[{"metric": {"metric_name": "eventCount"}, "desc": True}],
        limit=40,
    )["rows"]
    devices = _run(
        property_id,
        ["deviceCategory"],
        ["sessions", "keyEvents", "totalRevenue"],
        one,
    )["rows"]
    referrals = _run(
        property_id,
        ["sessionSource", "sessionMedium"],
        ["sessions", "totalRevenue", "ecommercePurchases"],
        one,
        dimension_filter=rep.build_filter(
            [{"field": "sessionMedium", "op": "EXACT", "value": "referral"}],
            "referrals",
        ),
        order_bys=[{"metric": {"metric_name": "totalRevenue"}, "desc": True}],
        limit=50,
    )["rows"]

    flags = []
    total_sessions = current.get("sessions") or 0
    for row in channels:
        if row.get("sessionDefaultChannelGroup") == "Unassigned":
            share = _ratio(row.get("sessions"), total_sessions)
            if share and share > 0.03:
                flags.append(
                    f"{share:.0%} of sessions are 'Unassigned': check UTM "
                    "tags (missing utm_source / utm_medium) and Measurement "
                    "Protocol hits without session data."
                )
    for row in landing:
        if row.get("landingPagePlusQueryString") in ("(not set)", ""):
            share = _ratio(row.get("sessions"), total_sessions)
            if share and share > 0.05:
                flags.append(
                    f"{share:.0%} of sessions have landing page '(not set)': "
                    "often sessions that start without a page_view (e.g. "
                    "returning from a payment page after the session "
                    "expired, or server-side events)."
                )
    for row in referrals:
        src = str(row.get("sessionSource", "")).lower()
        if any(p in src for p in _PAYMENT_REFERRERS) and (
            row.get("totalRevenue") or row.get("ecommercePurchases")
        ):
            flags.append(
                f"Payment provider '{row.get('sessionSource')}' appears as "
                "a referral with revenue: add it to 'List unwanted "
                "referrals' in the web stream's tag settings so purchases "
                "keep their original channel."
            )
    key_event_rows = [e for e in events if (e.get("keyEvents") or 0) > 0]
    if not key_event_rows:
        flags.append("No key events were recorded in this period.")

    return ga.to_json(
        {
            "date_range": cur,
            "previous_range": prev,
            "totals": totals,
            "channels": channels,
            "top_landing_pages": landing,
            "events": events,
            "devices": devices,
            "referrals_with_revenue": [
                r for r in referrals if r.get("totalRevenue")
            ][:15],
            "flags": flags,
        }
    )


@analytics_insights_mcp.tool(annotations=_READ)
def tracking_health(property_id: str | int) -> str:
    """Audits a property's setup and recent data for tracking problems.

    Checks data retention, key events (and which recorded nothing in the
    last 30 days), Google Ads links, web streams and enhanced measurement,
    Google signals, currency and time zone, unassigned traffic, payment
    providers counted as referral, and whether data arrived yesterday.

    Args:
        property_id: GA4 property ID.
    """
    client = ga.admin_client()
    pname = ga.property_name(property_id)
    findings: list[dict[str, str]] = []

    def _add(level: str, topic: str, text: str) -> None:
        findings.append({"level": level, "topic": topic, "finding": text})

    prop = ga.call_api(client.get_property, name=pname)
    setup: dict[str, Any] = {
        "display_name": prop.display_name,
        "time_zone": prop.time_zone,
        "currency_code": prop.currency_code,
        "industry_category": getattr(prop.industry_category, "name", ""),
        "service_level": getattr(prop.service_level, "name", ""),
    }

    try:
        retention = client.get_data_retention_settings(
            name=f"{pname}/dataRetentionSettings"
        )
        setup["event_data_retention"] = retention.event_data_retention.name
        if retention.event_data_retention.name == "TWO_MONTHS":
            _add(
                "warning",
                "data_retention",
                "Event data retention is 2 months: Explorations cannot go "
                "further back. Set it to 14 months (standard) or longer (360).",
            )
    except Exception as exc:  # noqa: BLE001
        _add(
            "info",
            "data_retention",
            f"Could not read: {ga.format_api_error(exc)}",
        )

    streams = list(ga.call_api(client.list_data_streams, parent=pname))
    setup["data_streams"] = []
    for s in streams:
        info: dict[str, Any] = {
            "id": s.name.split("/")[-1],
            "type": s.type_.name,
            "display_name": s.display_name,
        }
        if s.web_stream_data:
            info["measurement_id"] = s.web_stream_data.measurement_id
            info["default_uri"] = s.web_stream_data.default_uri
            try:
                em = client.get_enhanced_measurement_settings(
                    name=f"{s.name}/enhancedMeasurementSettings"
                )
                info["enhanced_measurement"] = em.stream_enabled
                if not em.stream_enabled:
                    _add(
                        "info",
                        "enhanced_measurement",
                        f"Enhanced measurement is off on stream {s.display_name}.",
                    )
            except Exception:  # noqa: BLE001
                pass
        setup["data_streams"].append(info)
    if not streams:
        _add("error", "data_streams", "The property has no data streams.")

    key_events = list(ga.call_api(client.list_key_events, parent=pname))
    setup["key_events"] = [k.event_name for k in key_events]
    if not key_events:
        _add("warning", "key_events", "No key events are defined.")

    try:
        links = list(client.list_google_ads_links(parent=pname))
        setup["google_ads_links"] = [l.customer_id for l in links]
        if not links:
            _add(
                "warning",
                "google_ads_link",
                "No Google Ads account is linked: no GA4 key event import, "
                "audiences or cost data in GA4.",
            )
    except Exception as exc:  # noqa: BLE001
        _add(
            "info",
            "google_ads_link",
            f"Could not read: {ga.format_api_error(exc)}",
        )

    try:
        signals = client.get_google_signals_settings(
            name=f"{pname}/googleSignalsSettings"
        )
        setup["google_signals"] = signals.state.name
    except Exception:  # noqa: BLE001
        pass

    last30 = [{"start_date": "30daysAgo", "end_date": "yesterday"}]
    if key_events:
        rows = _run(pname, ["eventName"], ["keyEvents"], last30)["rows"]
        counts = {r["eventName"]: r.get("keyEvents") or 0 for r in rows}
        silent = [
            k.event_name for k in key_events if not counts.get(k.event_name)
        ]
        setup["key_event_counts_last_30_days"] = {
            k.event_name: counts.get(k.event_name, 0) for k in key_events
        }
        if silent:
            _add(
                "warning",
                "key_events",
                "Key events without any count in the last 30 days: "
                + ", ".join(silent)
                + ". The tag may be broken or the event renamed.",
            )

    yday = _run(
        pname,
        [],
        ["sessions", "eventCount"],
        [{"start_date": "yesterday", "end_date": "yesterday"}],
    )["rows"]
    if not yday or not (yday[0].get("eventCount") or 0):
        _add("error", "data_collection", "No events were collected yesterday.")

    channels = _run(
        pname,
        ["sessionDefaultChannelGroup"],
        ["sessions", "totalRevenue"],
        last30,
    )
    total = (channels.get("totals") or [{}])[0].get("sessions") or 0
    for row in channels["rows"]:
        if row["sessionDefaultChannelGroup"] == "Unassigned":
            share = _ratio(row.get("sessions"), total)
            if share and share > 0.03:
                _add(
                    "warning",
                    "attribution",
                    f"{share:.0%} of sessions are Unassigned (check UTM tagging).",
                )
    refs = _run(
        pname,
        ["sessionSource"],
        ["sessions", "totalRevenue"],
        last30,
        dimension_filter=rep.build_filter(
            [{"field": "sessionMedium", "op": "EXACT", "value": "referral"}],
            "referrals",
        ),
        limit=200,
    )["rows"]
    for row in refs:
        src = str(row.get("sessionSource", "")).lower()
        if any(p in src for p in _PAYMENT_REFERRERS):
            _add(
                "warning",
                "unwanted_referrals",
                f"Payment provider '{row['sessionSource']}' shows up as "
                f"referral ({row.get('sessions')} sessions, revenue "
                f"{row.get('totalRevenue')}). Add it to unwanted referrals.",
            )
        own = setup["data_streams"][0].get("default_uri", "") if streams else ""
        host = own.replace("https://", "").replace("http://", "").strip("/")
        if host and host.replace("www.", "") in src:
            _add(
                "warning",
                "self_referral",
                f"The site itself ('{row['sessionSource']}') appears as "
                "referral: check cross-domain settings or tag gaps.",
            )

    return ga.to_json({"setup": setup, "findings": findings})
