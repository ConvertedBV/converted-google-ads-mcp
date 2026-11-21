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

"""Google Analytics 4 reporting tools (Data API, read only)."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp.analytics import client as ga
from ads_mcp.analytics import reports as rep

analytics_reporting_mcp = FastMCP("analytics_reporting")

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


@analytics_reporting_mcp.tool(annotations=_READ)
def list_properties(name_contains: str | None = None) -> str:
    """Lists every GA4 account and property the signed-in user can access.

    Start here to find a property_id (the number in 'properties/123456789').
    Includes property type (standard / 360 / subproperty / roll-up) and the
    parent account.

    Args:
        name_contains: Optional case-insensitive filter on account or
            property display name (e.g. 'crealingua').
    """
    client = ga.admin_client()
    pager = ga.call_api(client.list_account_summaries, page_size=200)
    needle = (name_contains or "").strip().lower()
    accounts = []
    total = 0
    for summary in pager:
        props = []
        for p in summary.property_summaries:
            if (
                needle
                and needle not in p.display_name.lower()
                and needle not in summary.display_name.lower()
            ):
                continue
            props.append(
                {
                    "property_id": p.property.split("/")[-1],
                    "display_name": p.display_name,
                    "property_type": getattr(
                        p.property_type, "name", str(p.property_type)
                    ),
                    "parent": p.parent,
                }
            )
        if props or (needle and needle in summary.display_name.lower()):
            accounts.append(
                {
                    "account_id": summary.account.split("/")[-1],
                    "display_name": summary.display_name,
                    "properties": props,
                }
            )
            total += len(props)
        elif not needle:
            accounts.append(
                {
                    "account_id": summary.account.split("/")[-1],
                    "display_name": summary.display_name,
                    "properties": [],
                }
            )
    return ga.to_json(
        {"accounts": accounts, "property_count": total}, limit=200_000
    )


@analytics_reporting_mcp.tool(annotations=_READ)
def get_metadata(
    property_id: str | int,
    search: str | None = None,
    kind: str | None = None,
    custom_only: bool = False,
) -> str:
    """Lists the dimensions and metrics available for a property.

    Includes the property's custom dimensions/metrics
    ('customEvent:param', 'customUser:param') and key-event metrics such as
    'keyEvents:purchase'. Use it before run_report to get exact API names.

    Args:
        property_id: GA4 property ID (e.g. '123456789').
        search: Optional text to match in api_name, UI name or description
            (e.g. 'channel', 'revenue', 'landing').
        kind: 'dimensions', 'metrics' or None for both.
        custom_only: Only custom definitions.
    """
    client = ga.data_client()
    name = f"{ga.property_name(property_id)}/metadata"
    meta = ga.call_api(client.get_metadata, name=name)
    needle = (search or "").strip().lower()

    def _match(item) -> bool:
        if custom_only and not item.custom_definition:
            return False
        if not needle:
            return True
        hay = f"{item.api_name} {item.ui_name} {item.description}".lower()
        return needle in hay

    out: dict[str, Any] = {}
    if kind in (None, "", "dimensions"):
        out["dimensions"] = [
            {
                "api_name": d.api_name,
                "ui_name": d.ui_name,
                "category": d.category,
                "custom": d.custom_definition,
                "description": d.description[:160],
            }
            for d in meta.dimensions
            if _match(d)
        ]
    if kind in (None, "", "metrics"):
        out["metrics"] = [
            {
                "api_name": m.api_name,
                "ui_name": m.ui_name,
                "category": m.category,
                "type": getattr(m.type_, "name", str(m.type_)),
                "custom": m.custom_definition,
                "description": m.description[:160],
            }
            for m in meta.metrics
            if _match(m)
        ]
    if not needle and not custom_only:
        out["note"] = (
            "Full list; pass 'search' to narrow it. Key events are "
            "available per event as metric 'keyEvents:<event_name>' and "
            "'sessionKeyEventRate:<event_name>'."
        )
    return ga.to_json(out, limit=200_000)


def _names(values: list[str] | None, label: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [v.strip() for v in values.split(",")]
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    if len(cleaned) != len(set(cleaned)):
        raise ToolError(f"Error: duplicate names in '{label}'.")
    return cleaned


@analytics_reporting_mcp.tool(annotations=_READ)
def check_compatibility(
    property_id: str | int,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    dimension_filters: Any = None,
    metric_filters: Any = None,
) -> str:
    """Checks which dimensions and metrics can be combined in one report.

    Use when run_report fails with an incompatibility error.

    Args:
        property_id: GA4 property ID.
        dimensions: Dimension API names.
        metrics: Metric API names.
        dimension_filters: Same format as in run_report.
        metric_filters: Same format as in run_report.
    """
    from google.analytics.data_v1beta import types

    dims = _names(dimensions, "dimensions")
    mets = _names(metrics, "metrics")
    request = types.CheckCompatibilityRequest(
        property=ga.property_name(property_id),
        dimensions=[{"name": d} for d in dims],
        metrics=[{"name": m} for m in mets],
        dimension_filter=rep.build_filter(
            dimension_filters, "dimension_filters"
        ),
        metric_filter=rep.build_filter(metric_filters, "metric_filters"),
    )
    resp = ga.call_api(ga.data_client().check_compatibility, request=request)
    return ga.to_json(
        {
            "dimensions": [
                {
                    "api_name": d.dimension_metadata.api_name,
                    "compatibility": d.compatibility.name,
                }
                for d in resp.dimension_compatibilities
            ],
            "metrics": [
                {
                    "api_name": m.metric_metadata.api_name,
                    "compatibility": m.compatibility.name,
                }
                for m in resp.metric_compatibilities
            ],
        }
    )


@analytics_reporting_mcp.tool(annotations=_READ)
def run_report(
    property_id: str | int,
    metrics: list[str],
    dimensions: list[str] | None = None,
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    date_ranges: Any = None,
    dimension_filters: Any = None,
    metric_filters: Any = None,
    order_by: list[str] | None = None,
    limit: int = rep.DEFAULT_LIMIT,
    offset: int = 0,
    keep_empty_rows: bool = False,
    currency_code: str | None = None,
    include_totals: bool = True,
) -> str:
    """Runs a GA4 report: any dimensions x metrics over one or more periods.

    The workhorse for all questions about website traffic and results, e.g.
    sessions and revenue per channel per month:
    dimensions=['yearMonth','sessionDefaultChannelGroup'],
    metrics=['sessions','keyEvents','ecommercePurchases','purchaseRevenue'].
    Key events per event: metric 'keyEvents:<event_name>'. Use get_metadata
    for exact names (custom dimensions: 'customEvent:<param>'). Session-scoped
    traffic dimensions start with 'session' (sessionSource,
    sessionDefaultChannelGroup, sessionCampaignName); user-scoped first-touch
    ones with 'firstUser'.

    Args:
        property_id: GA4 property ID.
        metrics: 1-10 metric API names.
        dimensions: 0-9 dimension API names.
        period: Named period when no dates are given: TODAY, YESTERDAY,
            LAST_7_DAYS, LAST_14_DAYS, LAST_28_DAYS (default), LAST_30_DAYS,
            LAST_90_DAYS, LAST_365_DAYS, THIS_MONTH, LAST_MONTH, THIS_YEAR,
            LAST_YEAR, LAST_12_MONTHS (12 complete months).
        start_date: YYYY-MM-DD, 'today', 'yesterday' or 'NdaysAgo'.
        end_date: Same formats; both dates are inclusive.
        date_ranges: Up to 4 ranges to compare, e.g. [{"start_date":
            "2026-10-01","end_date":"2026-10-31","name":"oct"},
            {"period":"LAST_MONTH","name":"prev"}]. Adds a 'dateRange'
            column. Overrides period/start/end.
        dimension_filters: Conditions (AND) on dimensions, e.g.
            [{"field":"sessionDefaultChannelGroup","op":"IN_LIST",
            "values":["Organic Search","Paid Search"]},
            {"field":"pagePath","op":"CONTAINS","value":"/fr/",
            "not":true}]. Ops: EXACT, CONTAINS, BEGINS_WITH, ENDS_WITH,
            FULL_REGEXP, PARTIAL_REGEXP, IN_LIST, EMPTY. Use {"or":[...]}
            for alternatives. A raw GA4 FilterExpression object also works.
        metric_filters: Conditions on metrics (applied after aggregation),
            ops EQUAL, GT, GTE, LT, LTE, BETWEEN ({"from":..,"to":..}).
        order_by: e.g. ['-sessions'] (descending) or ['yearMonth'].
        limit: Rows to return (default 1000, max 100000). Large results are
            trimmed in the output; filter or aggregate instead.
        offset: Row offset for paging.
        keep_empty_rows: Include rows where all metrics are 0.
        currency_code: ISO currency for revenue metrics (default: the
            property's currency).
        include_totals: Add totals for the metrics.
    """
    from google.analytics.data_v1beta import types

    dims = _names(dimensions, "dimensions")
    mets = _names(metrics, "metrics")
    if not mets:
        raise ToolError("Error: give at least one metric.")
    if len(mets) > 10 or len(dims) > 9:
        raise ToolError(
            "Error: GA4 allows at most 10 metrics and 9 dimensions."
        )
    limit = max(1, min(int(limit or rep.DEFAULT_LIMIT), rep.MAX_LIMIT))
    request = types.RunReportRequest(
        property=ga.property_name(property_id),
        dimensions=[{"name": d} for d in dims],
        metrics=[{"name": m} for m in mets],
        date_ranges=rep.build_date_ranges(
            start_date, end_date, period, date_ranges
        ),
        dimension_filter=rep.build_filter(
            dimension_filters, "dimension_filters"
        ),
        metric_filter=rep.build_filter(metric_filters, "metric_filters"),
        order_bys=rep.build_order_bys(order_by, mets, dims),
        limit=limit,
        offset=max(0, int(offset or 0)),
        keep_empty_rows=keep_empty_rows,
        currency_code=currency_code or "",
        metric_aggregations=["TOTAL"] if include_totals else [],
        return_property_quota=True,
    )
    resp = ga.call_api(ga.data_client().run_report, request=request)
    out = rep.flatten_report(resp)
    out["date_ranges"] = [ga.message_to_dict(r) for r in request.date_ranges]
    if out["row_count"] > len(out["rows"]) + offset:
        out["paging"] = (
            f"{out['row_count']} rows in total; use offset="
            f"{offset + len(out['rows'])} for the next page."
        )
    return ga.to_json(out)


@analytics_reporting_mcp.tool(annotations=_READ)
def run_pivot_report(
    property_id: str | int,
    dimensions: list[str],
    metrics: list[str],
    pivots: Any,
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    dimension_filters: Any = None,
    metric_filters: Any = None,
) -> str:
    """Runs a pivot report (cross-tab), e.g. channels x months.

    Args:
        property_id: GA4 property ID.
        dimensions: All dimensions used in any pivot.
        metrics: Metric API names.
        pivots: List of GA4 Pivot objects, e.g. [{"field_names":
            ["sessionDefaultChannelGroup"], "limit": 10}, {"field_names":
            ["yearMonth"], "limit": 12}].
        period / start_date / end_date: As in run_report.
        dimension_filters / metric_filters: As in run_report.
    """
    from google.analytics.data_v1beta import types

    pivots_parsed = ga.parse_json_arg(pivots, "pivots")
    if not isinstance(pivots_parsed, list) or not pivots_parsed:
        raise ToolError("Error: 'pivots' must be a non-empty list.")
    try:
        request = types.RunPivotReportRequest(
            property=ga.property_name(property_id),
            dimensions=[{"name": d} for d in _names(dimensions, "dimensions")],
            metrics=[{"name": m} for m in _names(metrics, "metrics")],
            date_ranges=rep.build_date_ranges(
                start_date, end_date, period, None
            ),
            pivots=pivots_parsed,
            dimension_filter=rep.build_filter(
                dimension_filters, "dimension_filters"
            ),
            metric_filter=rep.build_filter(metric_filters, "metric_filters"),
            return_property_quota=True,
        )
    except (ValueError, TypeError) as exc:
        raise ToolError(f"Error: invalid pivot definition: {exc}") from exc
    resp = ga.call_api(ga.data_client().run_pivot_report, request=request)
    return ga.to_json(ga.message_to_dict(resp))


@analytics_reporting_mcp.tool(annotations=_READ)
def run_realtime_report(
    property_id: str | int,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
    minutes_ago: int = 30,
    dimension_filters: Any = None,
    limit: int = 100,
) -> str:
    """Realtime data for the last 30 minutes (60 for GA4 360).

    Useful to check that tracking works right now, e.g. dimensions
    ['eventName'] with metrics ['eventCount'] after a tag change.

    Args:
        property_id: GA4 property ID.
        metrics: Default ['activeUsers']; also eventCount, keyEvents,
            screenPageViews.
        dimensions: e.g. ['eventName'], ['unifiedScreenName'], ['country'],
            ['deviceCategory'].
        minutes_ago: How far back (1-30, or up to 60 on 360).
        dimension_filters: As in run_report.
        limit: Max rows.
    """
    from google.analytics.data_v1beta import types

    mets = _names(metrics, "metrics") or ["activeUsers"]
    minutes = max(1, min(int(minutes_ago or 30), 60))
    request = types.RunRealtimeReportRequest(
        property=ga.property_name(property_id),
        dimensions=[{"name": d} for d in _names(dimensions, "dimensions")],
        metrics=[{"name": m} for m in mets],
        dimension_filter=rep.build_filter(
            dimension_filters, "dimension_filters"
        ),
        minute_ranges=[
            {"start_minutes_ago": minutes - 1, "end_minutes_ago": 0}
        ],
        limit=max(1, min(int(limit or 100), 10_000)),
        metric_aggregations=["TOTAL"],
        return_property_quota=True,
    )
    resp = ga.call_api(ga.data_client().run_realtime_report, request=request)
    return ga.to_json(rep.flatten_report(resp))


def _funnel_step(step: Any, index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ToolError("Error: every funnel step must be an object.")
    name = step.get("name") or f"Step {index + 1}"
    out: dict[str, Any] = {"name": name}
    if step.get("is_directly_followed_by"):
        out["is_directly_followed_by"] = True
    if "filter_expression" in step:
        out["filter_expression"] = step["filter_expression"]
    elif step.get("event_name"):
        out["filter_expression"] = {
            "funnel_event_filter": {"event_name": step["event_name"]}
        }
    elif step.get("page_path_contains"):
        out["filter_expression"] = {
            "and_group": {
                "expressions": [
                    {"funnel_event_filter": {"event_name": "page_view"}},
                    {
                        "funnel_field_filter": {
                            "field_name": "pagePath",
                            "string_filter": {
                                "match_type": "CONTAINS",
                                "value": step["page_path_contains"],
                            },
                        }
                    },
                ]
            }
        }
    else:
        raise ToolError(
            f"Error: step '{name}' needs 'event_name', 'page_path_contains' "
            "or a raw 'filter_expression'."
        )
    return out


@analytics_reporting_mcp.tool(annotations=_READ)
def run_funnel_report(
    property_id: str | int,
    steps: Any,
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    is_open_funnel: bool = False,
    breakdown_dimension: str | None = None,
) -> str:
    """Runs a funnel report (Data API v1alpha), e.g. quote form -> purchase.

    Args:
        property_id: GA4 property ID.
        steps: Ordered list, each {"name": "...", "event_name": "..."} or
            {"name": "...", "page_path_contains": "/offerte"}; optional
            "is_directly_followed_by": true. Raw GA4 FunnelStep
            'filter_expression' objects are accepted too.
        period / start_date / end_date: As in run_report.
        is_open_funnel: Users may enter at any step.
        breakdown_dimension: Optional dimension to split each step, e.g.
            'sessionDefaultChannelGroup' or 'deviceCategory'.
    """
    from google.analytics.data_v1alpha import types

    parsed = ga.parse_json_arg(steps, "steps")
    if not isinstance(parsed, list) or len(parsed) < 2:
        raise ToolError("Error: a funnel needs at least 2 steps.")
    kwargs: dict[str, Any] = {
        "property": ga.property_name(property_id),
        "date_ranges": rep.build_date_ranges(
            start_date, end_date, period, None
        ),
        "funnel": {
            "is_open_funnel": is_open_funnel,
            "steps": [_funnel_step(s, i) for i, s in enumerate(parsed)],
        },
        "return_property_quota": True,
    }
    if breakdown_dimension:
        kwargs["funnel_breakdown"] = {
            "breakdown_dimension": {"name": breakdown_dimension}
        }
    try:
        request = types.RunFunnelReportRequest(**kwargs)
    except (ValueError, TypeError) as exc:
        raise ToolError(f"Error: invalid funnel definition: {exc}") from exc
    resp = ga.call_api(
        ga.data_alpha_client().run_funnel_report, request=request
    )
    out: dict[str, Any] = {}
    for key in ("funnel_table", "funnel_visualization"):
        sub = getattr(resp, key, None)
        if sub is not None and sub.rows:
            out[key] = _flatten_funnel(sub)
    if resp.property_quota:
        out["property_quota"] = ga.message_to_dict(resp.property_quota)
    return ga.to_json(out)


def _flatten_funnel(sub) -> dict[str, Any]:
    dims = [h.name for h in sub.dimension_headers]
    mets = [h.name for h in sub.metric_headers]
    rows = []
    for r in sub.rows:
        row = {n: v.value for n, v in zip(dims, r.dimension_values)}
        for n, v in zip(mets, r.metric_values):
            row[n] = rep._metric_value(v.value, None)
        rows.append(row)
    return {"rows": rows}


@analytics_reporting_mcp.tool(annotations=_READ)
def get_property_quotas(property_id: str | int) -> str:
    """Shows how much of the property's Data API quota is used.

    Args:
        property_id: GA4 property ID.
    """
    name = f"{ga.property_name(property_id)}/propertyQuotasSnapshot"
    resp = ga.call_api(
        ga.data_alpha_client().get_property_quotas_snapshot, name=name
    )
    return ga.to_json(ga.message_to_dict(resp))
