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

"""Google Analytics 4 configuration tools (Admin API, read only)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp.analytics import client as ga
from ads_mcp.analytics import registry as reg
from ads_mcp.analytics import reports as rep

analytics_admin_mcp = FastMCP("analytics_admin")

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _safe_list(method, **kwargs) -> list[dict] | dict:
    try:
        return [ga.message_to_dict(x) for x in method(**kwargs)]
    except Exception as exc:  # noqa: BLE001 - shown per section
        return {"error": ga.format_api_error(exc)}


def _safe_get(method, **kwargs) -> dict:
    try:
        return ga.message_to_dict(method(**kwargs))
    except Exception as exc:  # noqa: BLE001 - shown per section
        return {"error": ga.format_api_error(exc)}


@analytics_admin_mcp.tool(annotations=_READ)
def get_property_setup(property_id: str | int) -> str:
    """Complete configuration of a GA4 property in one call.

    Property settings, data streams (with measurement IDs, enhanced
    measurement, redaction), key events, custom dimensions and metrics,
    calculated metrics, channel groups, audiences (names), data retention,
    attribution, Google signals, Google Ads / BigQuery / other links,
    Measurement Protocol secrets (names only) and annotations.

    Args:
        property_id: GA4 property ID.
    """
    client = ga.admin_client()
    p = ga.property_name(property_id)
    out: dict[str, Any] = {
        "property": ga.message_to_dict(ga.call_api(client.get_property, name=p))
    }
    streams = _safe_list(client.list_data_streams, parent=p)
    if isinstance(streams, list):
        for s in streams:
            name = s.get("name", "")
            if s.get("type") == "WEB_DATA_STREAM" or s.get("web_stream_data"):
                s["enhanced_measurement_settings"] = _safe_get(
                    client.get_enhanced_measurement_settings,
                    name=f"{name}/enhancedMeasurementSettings",
                )
                s["data_redaction_settings"] = _safe_get(
                    client.get_data_redaction_settings,
                    name=f"{name}/dataRedactionSettings",
                )
            secrets = _safe_list(
                client.list_measurement_protocol_secrets, parent=name
            )
            if isinstance(secrets, list):
                s["measurement_protocol_secrets"] = [
                    {
                        "name": x.get("name"),
                        "display_name": x.get("display_name"),
                    }
                    for x in secrets
                ]
            s["event_create_rules"] = _safe_list(
                client.list_event_create_rules, parent=name
            )
            s["event_edit_rules"] = _safe_list(
                client.list_event_edit_rules, parent=name
            )
    out["data_streams"] = streams
    out["key_events"] = _safe_list(client.list_key_events, parent=p)
    out["custom_dimensions"] = _safe_list(
        client.list_custom_dimensions, parent=p
    )
    out["custom_metrics"] = _safe_list(client.list_custom_metrics, parent=p)
    out["calculated_metrics"] = _safe_list(
        client.list_calculated_metrics, parent=p
    )
    groups = _safe_list(client.list_channel_groups, parent=p)
    if isinstance(groups, list):
        groups = [
            {
                "name": g.get("name"),
                "display_name": g.get("display_name"),
                "system_defined": g.get("system_defined", False),
                "primary": g.get("primary", False),
            }
            for g in groups
        ]
    out["channel_groups"] = groups
    audiences = _safe_list(client.list_audiences, parent=p)
    if isinstance(audiences, list):
        audiences = [
            {
                "name": a.get("name"),
                "display_name": a.get("display_name"),
                "membership_duration_days": a.get("membership_duration_days"),
            }
            for a in audiences
        ]
    out["audiences"] = audiences
    out["data_retention_settings"] = _safe_get(
        client.get_data_retention_settings, name=f"{p}/dataRetentionSettings"
    )
    out["attribution_settings"] = _safe_get(
        client.get_attribution_settings, name=f"{p}/attributionSettings"
    )
    out["google_signals_settings"] = _safe_get(
        client.get_google_signals_settings, name=f"{p}/googleSignalsSettings"
    )
    out["reporting_identity_settings"] = _safe_get(
        client.get_reporting_identity_settings,
        name=f"{p}/reportingIdentitySettings",
    )
    out["google_ads_links"] = _safe_list(client.list_google_ads_links, parent=p)
    out["bigquery_links"] = _safe_list(client.list_big_query_links, parent=p)
    out["firebase_links"] = _safe_list(client.list_firebase_links, parent=p)
    out["search_ads_360_links"] = _safe_list(
        client.list_search_ads360_links, parent=p
    )
    out["display_video_360_links"] = _safe_list(
        client.list_display_video360_advertiser_links, parent=p
    )
    out["annotations"] = _safe_list(
        client.list_reporting_data_annotations, parent=p
    )
    return ga.to_json(out, limit=200_000)


@analytics_admin_mcp.tool(annotations=_READ)
def list_resource_types() -> str:
    """Lists every GA4 Admin resource type the generic tools support.

    Shows where each type lives (account, property, data stream) and which
    operations exist (list, get, create, update, delete, archive). Use the
    key as resource_type in list_resources, get_resource,
    get_resource_schema and analytics_management_manage_resource.
    """
    out = []
    for key, rt in sorted(reg.registry().items()):
        out.append(
            {
                "resource_type": key,
                "lives_under": rt.parent,
                "singleton_settings": bool(rt.singleton),
                "operations": sorted(rt.allowed),
                "description": rt.description,
            }
        )
    return ga.to_json({"resource_types": out}, limit=200_000)


@analytics_admin_mcp.tool(annotations=_READ)
def get_resource_schema(resource_type: str) -> str:
    """Field structure (names, types, enum values) of a GA4 resource type.

    Use before creating or updating with manage_resource, e.g. for audiences,
    channel groups, calculated metrics or event rules.

    Args:
        resource_type: e.g. 'audience', 'channel_group', 'key_event'.
    """
    rt = reg.get_type(resource_type)
    msg_cls = reg.message_class(rt)
    return ga.to_json(
        {
            "resource_type": rt.key,
            "operations": sorted(rt.allowed),
            "fields": reg.describe_fields(msg_cls, max_depth=4),
            "notes": [
                "Fields marked output-only by Google (name, create_time, ...) "
                "are ignored on create.",
                "Enum values are passed as strings, e.g. 'ONCE_PER_EVENT'.",
            ],
        },
        limit=200_000,
    )


@analytics_admin_mcp.tool(annotations=_READ)
def list_resources(
    resource_type: str,
    property_id: str | int | None = None,
    account_id: str | int | None = None,
    data_stream_id: str | int | None = None,
    include_deleted: bool = False,
) -> str:
    """Lists GA4 resources of one type (any type from list_resource_types).

    Examples: resource_type='key_event' with property_id; 'data_stream';
    'audience'; 'access_binding' with property_id or account_id (users and
    roles); 'property' with account_id; 'event_edit_rule' with property_id
    and data_stream_id.

    Args:
        resource_type: Key from list_resource_types.
        property_id: GA4 property ID (for property and stream resources).
        account_id: GA4 account ID (accounts, properties, account access).
        data_stream_id: Data stream ID (for stream resources).
        include_deleted: For accounts/properties: include ones in the trash.
    """
    rt = reg.get_type(resource_type)
    if "list" not in rt.allowed:
        if rt.singleton and "get" in rt.allowed:
            return get_resource(
                resource_type,
                property_id=property_id,
                account_id=account_id,
                data_stream_id=data_stream_id,
            )
        raise ToolError(f"Error: {rt.key} cannot be listed.")
    client = ga.admin_client()
    method = getattr(client, f"list_{rt.plural_method}")
    kwargs: dict[str, Any] = {}
    if rt.key == "property":
        if account_id is None:
            raise ToolError(
                "Error: 'account_id' is required to list properties."
            )
        kwargs["request"] = {
            "filter": f"parent:{ga.account_name(account_id)}",
            "show_deleted": include_deleted,
        }
    elif rt.key == "account":
        kwargs["request"] = {"show_deleted": include_deleted}
    else:
        kwargs["parent"] = reg.parent_path(
            rt,
            account_id=account_id,
            property_id=property_id,
            data_stream_id=data_stream_id,
        )
    items = [ga.message_to_dict(x) for x in ga.call_api(method, **kwargs)]
    return ga.to_json(
        {"resource_type": rt.key, "count": len(items), "items": items}
    )


@analytics_admin_mcp.tool(annotations=_READ)
def get_resource(
    resource_type: str,
    property_id: str | int | None = None,
    account_id: str | int | None = None,
    data_stream_id: str | int | None = None,
    resource_id: str | None = None,
    name: str | None = None,
) -> str:
    """Reads one GA4 resource or settings object.

    Settings objects (data_retention_settings, attribution_settings,
    google_signals_settings, enhanced_measurement_settings,
    data_redaction_settings, global_site_tag, data_sharing_settings, ...)
    need only their parent IDs.

    Args:
        resource_type: Key from list_resource_types.
        property_id / account_id / data_stream_id: Parent IDs.
        resource_id: Last part of the resource name (e.g. the key event ID).
        name: Full resource name instead of the IDs, e.g.
            'properties/123/customDimensions/456'.
    """
    from ads_mcp.analytics.writes import fetch_current

    rt = reg.get_type(resource_type)
    path = reg.resource_path(
        rt,
        account_id=account_id,
        property_id=property_id,
        data_stream_id=data_stream_id,
        resource_id=resource_id,
        name=name,
    )
    current = fetch_current(rt, path)
    if current is None:
        raise ToolError(f"Error: {rt.key} cannot be read.")
    return ga.to_json(
        {"resource_type": rt.key, "name": path, "resource": current}
    )


@analytics_admin_mcp.tool(annotations=_READ)
def search_change_history(
    account_id: str | int,
    property_id: str | int | None = None,
    days: int = 30,
    resource_types: list[str] | None = None,
    actions: list[str] | None = None,
    actor_email: str | None = None,
    limit: int = 200,
) -> str:
    """Who changed what in GA4 settings (change history, up to 2 years back).

    Args:
        account_id: GA4 account ID (required by the API).
        property_id: Optional property to limit the history to.
        days: How many days back (1-730).
        resource_types: Optional ChangeHistoryResourceType names, e.g.
            ['KEY_EVENT','CUSTOM_DIMENSION','DATA_STREAM','PROPERTY',
            'GOOGLE_ADS_LINK','DATA_RETENTION_SETTINGS','AUDIENCE'].
        actions: Optional ['CREATED','UPDATED','DELETED'].
        actor_email: Only changes by this user.
        limit: Max events.
    """
    days = max(1, min(int(days or 30), 730))
    now = dt.datetime.now(dt.timezone.utc)
    request: dict[str, Any] = {
        "account": ga.account_name(account_id),
        "earliest_change_time": now - dt.timedelta(days=days),
        "page_size": 200,
    }
    if property_id is not None:
        request["property"] = ga.property_name(property_id)
    if resource_types:
        request["resource_type"] = [str(r).upper() for r in resource_types]
    if actions:
        request["action"] = [str(a).upper() for a in actions]
    if actor_email:
        request["actor_email"] = [actor_email]
    client = ga.admin_client()
    events = []
    for ev in ga.call_api(client.search_change_history_events, request=request):
        item = ga.message_to_dict(ev)
        for change in item.get("changes", []):
            # Keep before/after but drop deeply nested noise.
            for side in ("resource_before_change", "resource_after_change"):
                if side in change and isinstance(change[side], dict):
                    change[side] = {k: v for k, v in change[side].items() if v}
        events.append(item)
        if len(events) >= limit:
            break
    return ga.to_json({"count": len(events), "events": events})


@analytics_admin_mcp.tool(annotations=_READ)
def run_access_report(
    property_id: str | int,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 200,
) -> str:
    """Data access report: who looked at the property's reports and when.

    Args:
        property_id: GA4 property ID.
        dimensions: Default ['userEmail','accessMechanism'];
            also 'epochTimeMicros', 'dataApiQuotaCategory', 'reportType'.
        metrics: Default ['accessCount'].
        period / start_date / end_date: As in run_report.
        limit: Max rows.
    """
    dims = dimensions or ["userEmail", "accessMechanism"]
    mets = metrics or ["accessCount"]
    rng = rep.build_date_ranges(
        start_date, end_date, period or "LAST_30_DAYS", None
    )
    request = {
        "entity": ga.property_name(property_id),
        "dimensions": [{"dimension_name": d} for d in dims],
        "metrics": [{"metric_name": m} for m in mets],
        "date_ranges": rng,
        "limit": max(1, min(int(limit or 200), 10_000)),
        "return_entity_quota": True,
    }
    resp = ga.call_api(ga.admin_client().run_access_report, request=request)
    dnames = [h.dimension_name for h in resp.dimension_headers]
    mnames = [h.metric_name for h in resp.metric_headers]
    rows = []
    for r in resp.rows:
        row = {n: v.value for n, v in zip(dnames, r.dimension_values)}
        row.update({n: v.value for n, v in zip(mnames, r.metric_values)})
        rows.append(row)
    return ga.to_json({"row_count": resp.row_count, "rows": rows})
