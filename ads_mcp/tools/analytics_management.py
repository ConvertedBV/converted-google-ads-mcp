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

"""Google Analytics 4 configuration changes (Admin API, propose & confirm).

Every tool here follows the same workflow as the Google Ads write tools:
1. Call with validate_only=True (default): nothing changes; the tool returns
   change_items (new value next to the current one) and a checksum.
2. Show the change_items to the user and wait for explicit approval.
3. Call again with validate_only=False, the exact same parameters and the
   checksum to commit.
"""

from __future__ import annotations

import datetime as dt
import re
from enum import Enum
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import propose_and_confirm as pac
from ads_mcp.analytics import client as ga
from ads_mcp.analytics import writes

analytics_management_mcp = FastMCP("analytics_management")

_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, openWorldHint=True
)


class Operation(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class ArchiveOperation(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    ARCHIVE = "ARCHIVE"


class GenericOperation(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    ARCHIVE = "ARCHIVE"


def _op(value: Any) -> str:
    return (value.value if isinstance(value, Enum) else str(value)).lower()


def _compact(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


def _date_obj(value: str, label: str) -> dict[str, int]:
    try:
        d = dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ToolError(f"Error: '{label}' must be YYYY-MM-DD.") from exc
    return {"year": d.year, "month": d.month, "day": d.day}


@analytics_management_mcp.tool(annotations=_WRITE)
def manage_key_event(
    property_id: str | int,
    operation: Operation,
    event_name: str | None = None,
    counting_method: str | None = None,
    default_value: float | None = None,
    currency_code: str | None = None,
    key_event_id: str | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Marks an event as key event (conversion), changes or removes it.

    The event itself must be collected on the site (or created with an
    event_create_rule via manage_resource); this only flags it.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        property_id: GA4 property ID.
        operation: CREATE, UPDATE or DELETE.
        event_name: Event to mark (CREATE), e.g. 'generate_lead'.
        counting_method: ONCE_PER_EVENT (default in GA4) or ONCE_PER_SESSION.
        default_value: Value used when the event has no 'value' parameter.
        currency_code: Currency of default_value (e.g. 'EUR').
        key_event_id: ID of the existing key event (UPDATE/DELETE); see
            list_resources resource_type='key_event'.
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    op = _op(operation)
    fields: dict[str, Any] = {}
    if op == "create":
        if not event_name:
            raise ToolError("Error: 'event_name' is required for CREATE.")
        fields["event_name"] = event_name
    if counting_method:
        fields["counting_method"] = counting_method.upper()
    if default_value is not None:
        fields["default_value"] = _compact(
            {
                "numeric_value": float(default_value),
                "currency_code": (currency_code or "").upper() or None,
            }
        )
    return writes.propose_change(
        tool_name="manage_key_event",
        resource_type="key_event",
        verb=op,
        fields=fields if op != "delete" else None,
        property_id=property_id,
        resource_id=key_event_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@analytics_management_mcp.tool(annotations=_WRITE)
def manage_custom_dimension(
    property_id: str | int,
    operation: ArchiveOperation,
    parameter_name: str | None = None,
    display_name: str | None = None,
    scope: str | None = None,
    description: str | None = None,
    disallow_ads_personalization: bool | None = None,
    custom_dimension_id: str | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates, updates or archives a custom dimension.

    Needed to report on your own event parameters, e.g. a 'form_type' or
    'lead_type' parameter sent with a quote request.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        property_id: GA4 property ID.
        operation: CREATE, UPDATE or ARCHIVE.
        parameter_name: Event/user parameter name (CREATE only; cannot change).
        display_name: Name shown in reports.
        scope: EVENT (default), USER or ITEM (CREATE only).
        description: Optional description.
        disallow_ads_personalization: For USER scope.
        custom_dimension_id: ID of the existing dimension (UPDATE/ARCHIVE).
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    op = _op(operation)
    fields = _compact(
        {
            "display_name": display_name,
            "description": description,
            "disallow_ads_personalization": disallow_ads_personalization,
        }
    )
    if op == "create":
        if not (parameter_name and display_name):
            raise ToolError(
                "Error: 'parameter_name' and 'display_name' are required."
            )
        fields["parameter_name"] = parameter_name
        fields["scope"] = (scope or "EVENT").upper()
    return writes.propose_change(
        tool_name="manage_custom_dimension",
        resource_type="custom_dimension",
        verb=op,
        fields=fields if op != "archive" else None,
        property_id=property_id,
        resource_id=custom_dimension_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@analytics_management_mcp.tool(annotations=_WRITE)
def manage_custom_metric(
    property_id: str | int,
    operation: ArchiveOperation,
    parameter_name: str | None = None,
    display_name: str | None = None,
    measurement_unit: str | None = None,
    description: str | None = None,
    restricted_metric_types: list[str] | None = None,
    custom_metric_id: str | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates, updates or archives a custom metric (event scope).

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        property_id: GA4 property ID.
        operation: CREATE, UPDATE or ARCHIVE.
        parameter_name: Numeric event parameter (CREATE only).
        display_name: Name shown in reports.
        measurement_unit: STANDARD, CURRENCY, FEET, METERS, KILOMETERS,
            MILES, MILLISECONDS, SECONDS, MINUTES or HOURS.
        description: Optional description.
        restricted_metric_types: Optional ['COST_DATA'] / ['REVENUE_DATA'].
        custom_metric_id: ID of the existing metric (UPDATE/ARCHIVE).
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    op = _op(operation)
    fields = _compact(
        {
            "display_name": display_name,
            "description": description,
            "measurement_unit": (measurement_unit or "").upper() or None,
            "restricted_metric_type": (
                [r.upper() for r in restricted_metric_types]
                if restricted_metric_types
                else None
            ),
        }
    )
    if op == "create":
        if not (parameter_name and display_name):
            raise ToolError(
                "Error: 'parameter_name' and 'display_name' are required."
            )
        fields["parameter_name"] = parameter_name
        fields["scope"] = "EVENT"
        fields.setdefault("measurement_unit", "STANDARD")
    return writes.propose_change(
        tool_name="manage_custom_metric",
        resource_type="custom_metric",
        verb=op,
        fields=fields if op != "archive" else None,
        property_id=property_id,
        resource_id=custom_metric_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@analytics_management_mcp.tool(annotations=_WRITE)
def manage_annotation(
    property_id: str | int,
    operation: Operation,
    title: str | None = None,
    description: str | None = None,
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    color: str | None = None,
    annotation_id: str | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds, edits or removes an annotation on the property's report charts.

    Use it to log campaign launches, budget changes, tracking fixes or site
    releases, so later analyses can explain jumps in the data.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        property_id: GA4 property ID.
        operation: CREATE, UPDATE or DELETE.
        title: Short title (required for CREATE).
        description: Optional longer text.
        date: Single day YYYY-MM-DD (or use start_date + end_date).
        start_date: Start of a period, YYYY-MM-DD.
        end_date: End of a period, YYYY-MM-DD.
        color: PURPLE, BROWN, BLUE, GREEN, RED, CYAN or ORANGE.
        annotation_id: ID of the existing annotation (UPDATE/DELETE).
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    op = _op(operation)
    fields = _compact(
        {
            "title": title,
            "description": description,
            "color": (color or "").upper() or None,
        }
    )
    if date:
        fields["annotation_date"] = _date_obj(date, "date")
    elif start_date or end_date:
        if not (start_date and end_date):
            raise ToolError("Error: give both start_date and end_date.")
        fields["annotation_date_range"] = {
            "start_date": _date_obj(start_date, "start_date"),
            "end_date": _date_obj(end_date, "end_date"),
        }
    if op == "create" and not (
        title
        and ("annotation_date" in fields or "annotation_date_range" in fields)
    ):
        raise ToolError("Error: CREATE needs 'title' and a date or period.")
    return writes.propose_change(
        tool_name="manage_annotation",
        resource_type="reporting_data_annotation",
        verb=op,
        fields=fields if op != "delete" else None,
        property_id=property_id,
        resource_id=annotation_id,
        validate_only=validate_only,
        checksum=checksum,
        entity_label="annotation",
    )


@analytics_management_mcp.tool(annotations=_WRITE)
def update_property_settings(
    property_id: str | int,
    display_name: str | None = None,
    time_zone: str | None = None,
    currency_code: str | None = None,
    industry_category: str | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Changes property name, time zone, currency or industry category.

    Time zone and currency changes only affect data from the change onward.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        property_id: GA4 property ID.
        display_name: New property name.
        time_zone: IANA name, e.g. 'Europe/Brussels'.
        currency_code: ISO code, e.g. 'EUR'.
        industry_category: e.g. BUSINESS_AND_INDUSTRIAL_MARKETS,
            SHOPPING, TRAVEL, FINANCE, HEALTHCARE, OTHER ...
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    fields = _compact(
        {
            "display_name": display_name,
            "time_zone": time_zone,
            "currency_code": (currency_code or "").upper() or None,
            "industry_category": (industry_category or "").upper() or None,
        }
    )
    if not fields:
        raise ToolError("Error: give at least one setting to change.")
    return writes.propose_change(
        tool_name="update_property_settings",
        resource_type="property",
        verb="update",
        fields=fields,
        property_id=property_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@analytics_management_mcp.tool(annotations=_WRITE)
def update_data_retention(
    property_id: str | int,
    event_data_retention: str | None = None,
    user_data_retention: str | None = None,
    reset_user_data_on_new_activity: bool | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Changes how long GA4 keeps event and user-level data.

    Standard properties: TWO_MONTHS or FOURTEEN_MONTHS. GA4 360 also
    TWENTY_SIX_MONTHS, THIRTY_EIGHT_MONTHS, FIFTY_MONTHS. Standard reports
    are not affected; Explorations are.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        property_id: GA4 property ID.
        event_data_retention: Retention for event data.
        user_data_retention: Retention for user data.
        reset_user_data_on_new_activity: Restart the period on new activity.
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    fields = _compact(
        {
            "event_data_retention": (event_data_retention or "").upper()
            or None,
            "user_data_retention": (user_data_retention or "").upper() or None,
            "reset_user_data_on_new_activity": reset_user_data_on_new_activity,
        }
    )
    if not fields:
        raise ToolError("Error: give at least one setting to change.")
    return writes.propose_change(
        tool_name="update_data_retention",
        resource_type="data_retention_settings",
        verb="update",
        fields=fields,
        property_id=property_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@analytics_management_mcp.tool(annotations=_WRITE)
def manage_google_ads_link(
    property_id: str | int,
    operation: Operation,
    customer_id: str | int | None = None,
    ads_personalization_enabled: bool | None = None,
    link_id: str | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Links a Google Ads account to the property, changes or removes a link.

    A link lets Google Ads import GA4 key events and audiences and shows
    Ads cost in GA4. The signed-in user needs Editor on the property and
    admin access to the Google Ads account.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        property_id: GA4 property ID.
        operation: CREATE, UPDATE or DELETE.
        customer_id: Google Ads customer ID (CREATE).
        ads_personalization_enabled: Allow personalized advertising
            (remarketing audiences) with GA4 data.
        link_id: ID of the existing link (UPDATE/DELETE); see list_resources
            resource_type='google_ads_link'.
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    op = _op(operation)
    fields: dict[str, Any] = {}
    if op == "create":
        if not customer_id:
            raise ToolError("Error: 'customer_id' is required for CREATE.")
        fields["customer_id"] = re.sub(r"\D", "", str(customer_id))
    if ads_personalization_enabled is not None:
        fields["ads_personalization_enabled"] = ads_personalization_enabled
    return writes.propose_change(
        tool_name="manage_google_ads_link",
        resource_type="google_ads_link",
        verb=op,
        fields=fields if op != "delete" else None,
        property_id=property_id,
        resource_id=link_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@analytics_management_mcp.tool(annotations=_WRITE)
def manage_data_stream(
    property_id: str | int,
    operation: Operation,
    display_name: str | None = None,
    default_uri: str | None = None,
    data_stream_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a web data stream, renames one, or deletes one.

    Deleting a stream stops data collection for its measurement ID.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        property_id: GA4 property ID.
        operation: CREATE (web stream), UPDATE (display name) or DELETE.
        display_name: Stream name.
        default_uri: Website URL for a new web stream, e.g.
            'https://www.example.be'.
        data_stream_id: ID of the existing stream (UPDATE/DELETE).
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    op = _op(operation)
    fields: dict[str, Any] = {}
    if op == "create":
        if not (display_name and default_uri):
            raise ToolError(
                "Error: 'display_name' and 'default_uri' are required."
            )
        fields = {
            "type_": "WEB_DATA_STREAM",
            "display_name": display_name,
            "web_stream_data": {"default_uri": default_uri},
        }
    elif op == "update":
        if not display_name:
            raise ToolError("Error: 'display_name' is required for UPDATE.")
        fields = {"display_name": display_name}
    return writes.propose_change(
        tool_name="manage_data_stream",
        resource_type="data_stream",
        verb=op,
        fields=fields or None,
        property_id=property_id,
        resource_id=data_stream_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@analytics_management_mcp.tool(annotations=_WRITE)
def update_enhanced_measurement(
    property_id: str | int,
    data_stream_id: str | int,
    stream_enabled: bool | None = None,
    scrolls_enabled: bool | None = None,
    outbound_clicks_enabled: bool | None = None,
    site_search_enabled: bool | None = None,
    video_engagement_enabled: bool | None = None,
    file_downloads_enabled: bool | None = None,
    page_changes_enabled: bool | None = None,
    form_interactions_enabled: bool | None = None,
    search_query_parameter: str | None = None,
    uri_query_parameter: str | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Turns enhanced measurement (automatic events) on or off per type.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        property_id: GA4 property ID.
        data_stream_id: Web data stream ID.
        stream_enabled: Master switch.
        scrolls_enabled / outbound_clicks_enabled / site_search_enabled /
        video_engagement_enabled / file_downloads_enabled /
        page_changes_enabled / form_interactions_enabled: Per event type.
        search_query_parameter: Comma-separated URL params for site search.
        uri_query_parameter: Extra params for site search.
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    fields = _compact(
        {
            "stream_enabled": stream_enabled,
            "scrolls_enabled": scrolls_enabled,
            "outbound_clicks_enabled": outbound_clicks_enabled,
            "site_search_enabled": site_search_enabled,
            "video_engagement_enabled": video_engagement_enabled,
            "file_downloads_enabled": file_downloads_enabled,
            "page_changes_enabled": page_changes_enabled,
            "form_interactions_enabled": form_interactions_enabled,
            "search_query_parameter": search_query_parameter,
            "uri_query_parameter": uri_query_parameter,
        }
    )
    if not fields:
        raise ToolError("Error: give at least one setting to change.")
    return writes.propose_change(
        tool_name="update_enhanced_measurement",
        resource_type="enhanced_measurement_settings",
        verb="update",
        fields=fields,
        property_id=property_id,
        data_stream_id=data_stream_id,
        validate_only=validate_only,
        checksum=checksum,
    )


_ROLES = {
    "viewer": "predefinedRoles/viewer",
    "analyst": "predefinedRoles/analyst",
    "marketer": "predefinedRoles/marketer",
    "editor": "predefinedRoles/editor",
    "admin": "predefinedRoles/admin",
    "administrator": "predefinedRoles/admin",
    "no-cost-data": "predefinedRoles/no-cost-data",
    "no-revenue-data": "predefinedRoles/no-revenue-data",
}


@analytics_management_mcp.tool(annotations=_WRITE)
def manage_user_access(
    operation: Operation,
    property_id: str | int | None = None,
    account_id: str | int | None = None,
    user_email: str | None = None,
    roles: list[str] | None = None,
    access_binding_id: str | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Gives a user access to a GA4 property or account, changes or removes it.

    Requires Administrator rights. Prefer the property level; account-level
    access applies to every property under the account.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        operation: CREATE, UPDATE or DELETE.
        property_id: GA4 property ID (property-level access).
        account_id: GA4 account ID (account-level access, when no
            property_id is given).
        user_email: Google account e-mail (CREATE).
        roles: e.g. ['viewer'], ['analyst'], ['marketer'], ['editor'],
            ['admin']; data restrictions 'no-cost-data', 'no-revenue-data'.
        access_binding_id: ID of the existing binding (UPDATE/DELETE); see
            list_resources resource_type='access_binding'.
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    if property_id is None and account_id is None:
        raise ToolError("Error: give property_id or account_id.")
    op = _op(operation)
    fields: dict[str, Any] = {}
    if roles:
        mapped = []
        for r in roles:
            key = str(r).strip().lower().replace("predefinedroles/", "")
            if key not in _ROLES:
                raise ToolError(
                    f"Error: unknown role '{r}'. Use: {', '.join(sorted(_ROLES))}."
                )
            mapped.append(_ROLES[key])
        fields["roles"] = mapped
    if op == "create":
        if not (user_email and roles):
            raise ToolError("Error: 'user_email' and 'roles' are required.")
        fields["user"] = user_email.strip()
    if op == "update" and not roles:
        raise ToolError("Error: 'roles' is required for UPDATE.")
    return writes.propose_change(
        tool_name="manage_user_access",
        resource_type="access_binding",
        verb=op,
        fields=fields if op != "delete" else None,
        update_mask=["roles"] if op == "update" else None,
        property_id=property_id,
        account_id=account_id,
        resource_id=access_binding_id,
        validate_only=validate_only,
        checksum=checksum,
        entity_label="user access",
    )


@analytics_management_mcp.tool(annotations=_WRITE)
def manage_resource(
    resource_type: str,
    operation: GenericOperation,
    fields: Any = None,
    update_mask: list[str] | None = None,
    property_id: str | int | None = None,
    account_id: str | int | None = None,
    data_stream_id: str | int | None = None,
    resource_id: str | None = None,
    name: str | None = None,
    calculated_metric_id: str | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Generic create / update / delete / archive for any GA4 Admin resource.

    Covers everything without a dedicated tool: audiences, custom channel
    groups, calculated metrics, event create/edit rules, attribution
    settings, Google signals, data redaction, Measurement Protocol secrets,
    BigQuery / Search Ads 360 / Display & Video 360 / Firebase / AdSense
    links, expanded data sets, subproperty filters, accounts (update only).
    Get the structure with analytics_admin_get_resource_schema and the
    current object with analytics_admin_get_resource first.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        resource_type: Key from analytics_admin_list_resource_types.
        operation: CREATE, UPDATE, DELETE or ARCHIVE (as the type allows).
        fields: The resource fields as JSON object (snake_case names, enums
            as strings). For UPDATE only the fields to change.
        update_mask: Fields to update (default: the top-level keys of
            fields). Nested paths like 'default_value.numeric_value' work.
        property_id / account_id / data_stream_id: Parent IDs.
        resource_id: ID of the existing resource (UPDATE/DELETE/ARCHIVE).
        name: Full resource name instead of the IDs.
        calculated_metric_id: Required ID when creating a calculated metric
            (letters, digits, underscores).
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    op = _op(operation)
    parsed = ga.parse_json_arg(fields, "fields")
    if parsed is not None and not isinstance(parsed, dict):
        raise ToolError("Error: 'fields' must be a JSON object.")
    if op in ("create", "update") and not parsed:
        raise ToolError(f"Error: 'fields' is required for {op.upper()}.")
    extra = {}
    if calculated_metric_id:
        extra["calculated_metric_id"] = calculated_metric_id
    return writes.propose_change(
        tool_name="manage_resource",
        resource_type=resource_type,
        verb=op,
        fields=parsed if op in ("create", "update") else None,
        update_mask=update_mask,
        property_id=property_id,
        account_id=account_id,
        data_stream_id=data_stream_id,
        resource_id=resource_id,
        name=name,
        extra_request_fields=extra,
        validate_only=validate_only,
        checksum=checksum,
    )


@analytics_management_mcp.tool(annotations=_WRITE)
def acknowledge_user_data_collection(
    property_id: str | int,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Accepts the user data collection terms for a property.

    Google requires this once before Measurement Protocol secrets can be
    created. Only do this when the user confirms that the site has the
    necessary privacy disclosures and rights to collect this data.

    Propose & Confirm: call with validate_only=True (default) to get
    change_items (new next to current values) and a checksum without
    changing anything; only after the user explicitly approves, call again
    with validate_only=False, the exact same parameters and the checksum.

    Args:
        property_id: GA4 property ID.
        validate_only: True proposes, False commits (needs checksum).
        checksum: Checksum from the proposal.
    """
    pname = ga.property_name(property_id)
    acknowledgement = (
        "I acknowledge that I have the necessary privacy disclosures and "
        "rights from my end users for the collection and processing of their "
        "data, including the association of such data with the visitation "
        "information Google Analytics collects from my site and/or app "
        "property."
    )
    change_items = [
        pac.ChangeItem(
            operation="UPDATE",
            entity_readable="GA4 user data collection terms",
            primary_key_readable=pname,
            field_readable="acknowledgement",
            value_readable=acknowledgement,
        )
    ]

    def _commit() -> str:
        ga.call_api(
            ga.admin_client().acknowledge_user_data_collection,
            request={"property": pname, "acknowledgement": acknowledgement},
        )
        return ga.to_json({"status": "DONE", "property": pname})

    return pac.propose_and_confirm(
        customer_id=pname,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={"tool": "acknowledge_user_data_collection"},
        change_items=change_items,
        commit_fn=_commit,
        error_prefix="Failed to acknowledge user data collection",
    )
