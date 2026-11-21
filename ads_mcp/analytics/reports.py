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

"""Building GA4 Data API report requests and flattening their responses."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from fastmcp.exceptions import ToolError

from ads_mcp.analytics import client as ga

DEFAULT_LIMIT = 1000
MAX_LIMIT = 100_000

_PRESETS = {
    "TODAY": ("today", "today"),
    "YESTERDAY": ("yesterday", "yesterday"),
    "LAST_7_DAYS": ("7daysAgo", "yesterday"),
    "LAST_14_DAYS": ("14daysAgo", "yesterday"),
    "LAST_28_DAYS": ("28daysAgo", "yesterday"),
    "LAST_30_DAYS": ("30daysAgo", "yesterday"),
    "LAST_90_DAYS": ("90daysAgo", "yesterday"),
    "LAST_365_DAYS": ("365daysAgo", "yesterday"),
}

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|today|yesterday|\d+daysAgo)$")


def _month_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    start = day.replace(day=1)
    nxt = (start + dt.timedelta(days=32)).replace(day=1)
    return start, nxt - dt.timedelta(days=1)


def resolve_preset(
    preset: str, today: dt.date | None = None
) -> tuple[str, str]:
    """Named periods -> (start_date, end_date) in GA4 syntax."""
    key = str(preset).strip().upper()
    if key in _PRESETS:
        return _PRESETS[key]
    today = today or dt.date.today()
    if key == "THIS_MONTH":
        return today.replace(day=1).isoformat(), "today"
    if key == "LAST_MONTH":
        start, end = _month_bounds(today.replace(day=1) - dt.timedelta(days=1))
        return start.isoformat(), end.isoformat()
    if key == "THIS_YEAR":
        return today.replace(month=1, day=1).isoformat(), "today"
    if key == "LAST_YEAR":
        return (
            dt.date(today.year - 1, 1, 1).isoformat(),
            dt.date(today.year - 1, 12, 31).isoformat(),
        )
    if key == "LAST_12_MONTHS":
        # 12 complete months before the current one.
        end = today.replace(day=1) - dt.timedelta(days=1)
        start = (end.replace(day=1) - dt.timedelta(days=330)).replace(day=1)
        while (end.year - start.year) * 12 + end.month - start.month > 11:
            start = (start + dt.timedelta(days=32)).replace(day=1)
        return start.isoformat(), end.isoformat()
    raise ToolError(
        f"Error: unknown period '{preset}'. Use TODAY, YESTERDAY, LAST_7_DAYS, "
        "LAST_14_DAYS, LAST_28_DAYS, LAST_30_DAYS, LAST_90_DAYS, "
        "LAST_365_DAYS, THIS_MONTH, LAST_MONTH, THIS_YEAR, LAST_YEAR, "
        "LAST_12_MONTHS, or explicit start_date/end_date."
    )


def _check_date(value: str, label: str) -> str:
    text = str(value).strip()
    if not _DATE_RE.match(text):
        raise ToolError(
            f"Error: {label} '{value}' must be YYYY-MM-DD, 'today', "
            "'yesterday' or 'NdaysAgo'."
        )
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        try:
            dt.date.fromisoformat(text)
        except ValueError as exc:
            raise ToolError(f"Error: {label} '{value}' is not a date.") from exc
    return text


def build_date_ranges(
    start_date: str | None,
    end_date: str | None,
    period: str | None,
    date_ranges: Any,
) -> list[dict[str, str]]:
    """One or more date ranges (max 4) from the tool arguments."""
    ranges = ga.parse_json_arg(date_ranges, "date_ranges")
    out: list[dict[str, str]] = []
    if ranges:
        if not isinstance(ranges, list):
            raise ToolError("Error: 'date_ranges' must be a list.")
        for i, item in enumerate(ranges):
            if not isinstance(item, dict):
                raise ToolError("Error: each date range must be an object.")
            if item.get("period"):
                s, e = resolve_preset(item["period"])
            else:
                s = _check_date(item.get("start_date", ""), "start_date")
                e = _check_date(item.get("end_date", ""), "end_date")
            rng = {"start_date": s, "end_date": e}
            if item.get("name"):
                rng["name"] = str(item["name"])
            out.append(rng)
    else:
        if start_date or end_date:
            if not (start_date and end_date):
                raise ToolError(
                    "Error: give both start_date and end_date, or a period."
                )
            out.append(
                {
                    "start_date": _check_date(start_date, "start_date"),
                    "end_date": _check_date(end_date, "end_date"),
                }
            )
        else:
            s, e = resolve_preset(period or "LAST_28_DAYS")
            out.append({"start_date": s, "end_date": e})
    if len(out) > 4:
        raise ToolError("Error: GA4 allows at most 4 date ranges per report.")
    return out


_STRING_OPS = {
    "EXACT": "EXACT",
    "EQUALS": "EXACT",
    "BEGINS_WITH": "BEGINS_WITH",
    "STARTS_WITH": "BEGINS_WITH",
    "ENDS_WITH": "ENDS_WITH",
    "CONTAINS": "CONTAINS",
    "FULL_REGEXP": "FULL_REGEXP",
    "REGEX": "FULL_REGEXP",
    "PARTIAL_REGEXP": "PARTIAL_REGEXP",
}
_NUMERIC_OPS = {
    "NUMERIC_EQUAL": "EQUAL",
    "=": "EQUAL",
    "EQUAL": "EQUAL",
    "GT": "GREATER_THAN",
    ">": "GREATER_THAN",
    "GREATER_THAN": "GREATER_THAN",
    "GTE": "GREATER_THAN_OR_EQUAL",
    ">=": "GREATER_THAN_OR_EQUAL",
    "GREATER_THAN_OR_EQUAL": "GREATER_THAN_OR_EQUAL",
    "LT": "LESS_THAN",
    "<": "LESS_THAN",
    "LESS_THAN": "LESS_THAN",
    "LTE": "LESS_THAN_OR_EQUAL",
    "<=": "LESS_THAN_OR_EQUAL",
    "LESS_THAN_OR_EQUAL": "LESS_THAN_OR_EQUAL",
}


def _numeric_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        raise ToolError("Error: numeric filter values cannot be booleans.")
    if isinstance(value, int) or (
        isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip())
    ):
        return {"int64_value": int(value)}
    try:
        return {"double_value": float(value)}
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Error: '{value}' is not a number.") from exc


def _single_filter(spec: dict[str, Any]) -> dict[str, Any]:
    field = spec.get("field") or spec.get("field_name")
    if not field:
        raise ToolError("Error: every filter needs a 'field'.")
    op = str(spec.get("op") or spec.get("operator") or "EXACT").upper()
    value = spec.get("value")
    values = spec.get("values")
    flt: dict[str, Any] = {"field_name": field}
    if op in ("IN_LIST", "IN"):
        vals = values if values is not None else value
        if not isinstance(vals, list) or not vals:
            raise ToolError("Error: IN_LIST needs a non-empty list 'values'.")
        flt["in_list_filter"] = {
            "values": [str(v) for v in vals],
            "case_sensitive": bool(spec.get("case_sensitive", False)),
        }
    elif op in _STRING_OPS:
        if value is None:
            raise ToolError(f"Error: filter on '{field}' needs a 'value'.")
        flt["string_filter"] = {
            "match_type": _STRING_OPS[op],
            "value": str(value),
            "case_sensitive": bool(spec.get("case_sensitive", False)),
        }
    elif op in _NUMERIC_OPS:
        flt["numeric_filter"] = {
            "operation": _NUMERIC_OPS[op],
            "value": _numeric_value(value),
        }
    elif op == "BETWEEN":
        low = spec.get("from", spec.get("low"))
        high = spec.get("to", spec.get("high"))
        if low is None or high is None:
            raise ToolError("Error: BETWEEN needs 'from' and 'to'.")
        flt["between_filter"] = {
            "from_value": _numeric_value(low),
            "to_value": _numeric_value(high),
        }
    elif op in ("EMPTY", "IS_EMPTY"):
        flt["empty_filter"] = {}
    else:
        raise ToolError(
            f"Error: unknown filter op '{op}'. Use EXACT, CONTAINS, "
            "BEGINS_WITH, ENDS_WITH, FULL_REGEXP, PARTIAL_REGEXP, IN_LIST, "
            "EQUAL, GT, GTE, LT, LTE, BETWEEN or EMPTY."
        )
    expr: dict[str, Any] = {"filter": flt}
    if spec.get("not"):
        expr = {"not_expression": expr}
    return expr


def build_filter(filters: Any, label: str) -> dict[str, Any] | None:
    """Simple list of conditions (AND) or a raw FilterExpression object.

    Simple form: [{"field": "sessionDefaultChannelGroup", "op": "EXACT",
    "value": "Organic Search"}, {"field": "sessions", "op": "GT",
    "value": 10}]. Add "not": true to negate a condition; use
    {"or": [...]} inside the list for alternatives.
    A dict with and_group / or_group / not_expression / filter is passed
    through as a raw FilterExpression.
    """
    parsed = ga.parse_json_arg(filters, label)
    if not parsed:
        return None
    if isinstance(parsed, dict):
        if set(parsed) & {"and_group", "or_group", "not_expression", "filter"}:
            return parsed
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ToolError(f"Error: '{label}' must be a list of conditions.")

    def _expr(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ToolError(f"Error: '{label}' items must be objects.")
        if "or" in item:
            return {"or_group": {"expressions": [_expr(x) for x in item["or"]]}}
        if "and" in item:
            return {
                "and_group": {"expressions": [_expr(x) for x in item["and"]]}
            }
        return _single_filter(item)

    exprs = [_expr(x) for x in parsed]
    if len(exprs) == 1:
        return exprs[0]
    return {"and_group": {"expressions": exprs}}


def build_order_bys(
    order_by: Any, metrics: list[str], dimensions: list[str]
) -> list[dict[str, Any]]:
    """'-sessions' = descending by metric sessions; 'date' = by dimension."""
    if not order_by:
        return []
    items = order_by if isinstance(order_by, list) else [order_by]
    out = []
    for raw in items:
        text = str(raw).strip()
        desc = text.startswith("-")
        name = text.lstrip("-+").strip()
        if name in metrics:
            out.append({"metric": {"metric_name": name}, "desc": desc})
        elif name in dimensions:
            out.append({"dimension": {"dimension_name": name}, "desc": desc})
        else:
            raise ToolError(
                f"Error: order_by '{name}' must be one of the requested "
                "dimensions or metrics."
            )
    return out


def _metric_value(raw: str, metric_type: str | None) -> Any:
    if raw is None or raw == "":
        return None
    try:
        if metric_type and "INTEGER" in metric_type:
            return int(raw)
        number = float(raw)
        return (
            int(number)
            if number.is_integer() and "." not in raw
            else round(number, 6)
        )
    except ValueError:
        return raw


def flatten_report(response: Any) -> dict[str, Any]:
    """Turns a RunReport/RunRealtimeReport response into plain rows."""
    dim_names = [h.name for h in response.dimension_headers]
    met_headers = [
        (h.name, getattr(h.type_, "name", str(h.type_)))
        for h in response.metric_headers
    ]

    def _row(r) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for name, val in zip(dim_names, r.dimension_values):
            row[name] = val.value
        for (name, mtype), val in zip(met_headers, r.metric_values):
            row[name] = _metric_value(val.value, mtype)
        return row

    out: dict[str, Any] = {
        "row_count": getattr(response, "row_count", len(response.rows)),
        "rows": [_row(r) for r in response.rows],
    }
    for attr in ("totals", "maximums", "minimums"):
        values = getattr(response, attr, None)
        if values:
            out[attr] = [_row(r) for r in values]
    out["metric_types"] = {n: t for n, t in met_headers}
    meta = getattr(response, "metadata", None)
    if meta:
        md: dict[str, Any] = {}
        for key in (
            "currency_code",
            "time_zone",
            "empty_reason",
            "subject_to_thresholding",
            "data_loss_from_other_row",
        ):
            val = getattr(meta, key, None)
            if val not in (None, "", False):
                md[key] = val
        sampling = getattr(meta, "sampling_metadatas", None)
        if sampling:
            md["sampled"] = [ga.message_to_dict(s) for s in sampling]
        if md:
            out["metadata"] = md
    quota = getattr(response, "property_quota", None)
    if quota:
        out["property_quota"] = ga.message_to_dict(quota)
    return out
