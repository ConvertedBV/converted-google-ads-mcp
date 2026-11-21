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

"""Registry of GA4 Admin API resource types.

Each entry says where a resource lives (account, property, data stream, or a
singleton settings object under one of those) and which operations the Admin
API offers for it. Method names and request field names are read from the
installed client library, so the registry stays in step with the API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from fastmcp.exceptions import ToolError

from ads_mcp.analytics import client as ga

# parent kinds
ACCOUNT = "account"  # accounts/{account_id}/...
PROPERTY = "property"  # properties/{property_id}/...
STREAM = "data_stream"  # properties/{p}/dataStreams/{s}/...
ROOT = "root"  # top level (accounts, properties)


@dataclass(frozen=True)
class ResourceType:
    key: str  # snake_case, e.g. "custom_dimension"
    plural_method: str  # e.g. "custom_dimensions"
    parent: str  # ACCOUNT / PROPERTY / STREAM / ROOT
    collection: str | None  # URL segment, e.g. "customDimensions"
    singleton: str | None = None  # URL segment of a settings object
    description: str = ""
    # Operations allowed through the generic tools. Some exist in the API but
    # are deliberately left out (e.g. deleting accounts).
    allowed: frozenset[str] = field(default_factory=frozenset)


_DEFS: list[tuple[str, str, str | None, str | None, str]] = [
    # key, parent, collection, singleton, description
    ("account", ROOT, "accounts", None, "Analytics account"),
    ("property", ROOT, "properties", None, "GA4 property"),
    (
        "data_sharing_settings",
        ACCOUNT,
        None,
        "dataSharingSettings",
        "Account data sharing settings (read only)",
    ),
    (
        "access_binding",
        PROPERTY,
        "accessBindings",
        None,
        "User access (roles) on the property; also exists on accounts",
    ),
    ("data_stream", PROPERTY, "dataStreams", None, "Web/app data streams"),
    ("key_event", PROPERTY, "keyEvents", None, "Key events (conversions)"),
    (
        "conversion_event",
        PROPERTY,
        "conversionEvents",
        None,
        "Legacy name for key events; prefer key_event",
    ),
    (
        "custom_dimension",
        PROPERTY,
        "customDimensions",
        None,
        "Custom dimensions (event, user or item scope)",
    ),
    ("custom_metric", PROPERTY, "customMetrics", None, "Custom metrics"),
    (
        "calculated_metric",
        PROPERTY,
        "calculatedMetrics",
        None,
        "Calculated metrics (formula over other metrics)",
    ),
    ("audience", PROPERTY, "audiences", None, "Audiences (archive, no delete)"),
    (
        "channel_group",
        PROPERTY,
        "channelGroups",
        None,
        "Custom channel groups",
    ),
    (
        "reporting_data_annotation",
        PROPERTY,
        "reportingDataAnnotations",
        None,
        "Annotations shown on report charts",
    ),
    (
        "google_ads_link",
        PROPERTY,
        "googleAdsLinks",
        None,
        "Links to Google Ads accounts",
    ),
    ("big_query_link", PROPERTY, "bigQueryLinks", None, "BigQuery export"),
    ("firebase_link", PROPERTY, "firebaseLinks", None, "Firebase project"),
    ("ad_sense_link", PROPERTY, "adSenseLinks", None, "AdSense links"),
    (
        "search_ads360_link",
        PROPERTY,
        "searchAds360Links",
        None,
        "Search Ads 360 links",
    ),
    (
        "display_video360_advertiser_link",
        PROPERTY,
        "displayVideo360AdvertiserLinks",
        None,
        "Display & Video 360 advertiser links",
    ),
    (
        "display_video360_advertiser_link_proposal",
        PROPERTY,
        "displayVideo360AdvertiserLinkProposals",
        None,
        "Display & Video 360 link proposals",
    ),
    (
        "expanded_data_set",
        PROPERTY,
        "expandedDataSets",
        None,
        "Expanded data sets (360)",
    ),
    (
        "subproperty_event_filter",
        PROPERTY,
        "subpropertyEventFilters",
        None,
        "Subproperty event filters (360)",
    ),
    (
        "subproperty_sync_config",
        PROPERTY,
        "subpropertySyncConfigs",
        None,
        "Subproperty sync configs (360)",
    ),
    (
        "rollup_property_source_link",
        PROPERTY,
        "rollupPropertySourceLinks",
        None,
        "Roll-up property sources (360)",
    ),
    (
        "data_retention_settings",
        PROPERTY,
        None,
        "dataRetentionSettings",
        "How long event and user data are kept",
    ),
    (
        "attribution_settings",
        PROPERTY,
        None,
        "attributionSettings",
        "Attribution model and lookback windows",
    ),
    (
        "google_signals_settings",
        PROPERTY,
        None,
        "googleSignalsSettings",
        "Google signals",
    ),
    (
        "reporting_identity_settings",
        PROPERTY,
        None,
        "reportingIdentitySettings",
        "Reporting identity (blended, observed, device-based)",
    ),
    (
        "user_provided_data_settings",
        PROPERTY,
        None,
        "userProvidedDataSettings",
        "User-provided data collection (read only)",
    ),
    (
        "measurement_protocol_secret",
        STREAM,
        "measurementProtocolSecrets",
        None,
        "Measurement Protocol API secrets of a data stream",
    ),
    (
        "event_create_rule",
        STREAM,
        "eventCreateRules",
        None,
        "Rules that create new events from existing ones",
    ),
    (
        "event_edit_rule",
        STREAM,
        "eventEditRules",
        None,
        "Rules that modify incoming events",
    ),
    (
        "enhanced_measurement_settings",
        STREAM,
        None,
        "enhancedMeasurementSettings",
        "Enhanced measurement of a web stream",
    ),
    (
        "data_redaction_settings",
        STREAM,
        None,
        "dataRedactionSettings",
        "Email / query-parameter redaction of a web stream",
    ),
    (
        "global_site_tag",
        STREAM,
        None,
        "globalSiteTag",
        "The gtag.js snippet of a web stream (read only)",
    ),
]

# Operations never offered by the generic tools, even though the API has them.
_BLOCKED: set[tuple[str, str]] = {
    ("account", "delete"),
    ("property", "delete"),
    ("property", "create"),  # use the dedicated create in the UI/admin flow
}


def _client_class():
    from google.analytics.admin_v1alpha import AnalyticsAdminServiceClient

    return AnalyticsAdminServiceClient


def _types():
    from google.analytics.admin_v1alpha import types

    return types


def _camel(snake: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in snake.split("_"))


def _plural(key: str) -> str:
    if key.endswith("_settings"):
        return key
    if key == "property":
        return "properties"
    return key + "s"


@lru_cache(maxsize=1)
def registry() -> dict[str, ResourceType]:
    client_cls = _client_class()
    out: dict[str, ResourceType] = {}
    for key, parent, collection, singleton, description in _DEFS:
        ops = set()
        for verb in ("get", "create", "update", "delete", "archive"):
            if hasattr(client_cls, f"{verb}_{key}"):
                ops.add(verb)
        if hasattr(client_cls, f"list_{_plural(key)}"):
            ops.add("list")
        ops = {op for op in ops if (key, op) not in _BLOCKED}
        out[key] = ResourceType(
            key=key,
            plural_method=_plural(key),
            parent=parent,
            collection=collection,
            singleton=singleton,
            description=description,
            allowed=frozenset(ops),
        )
    return out


def get_type(resource_type: str) -> ResourceType:
    key = str(resource_type or "").strip().lower().replace("-", "_")
    reg = registry()
    if key not in reg:
        # Accept plurals and common aliases.
        aliases = {
            "conversion": "key_event",
            "conversions": "key_event",
            "key_events": "key_event",
            "annotation": "reporting_data_annotation",
            "annotations": "reporting_data_annotation",
            "stream": "data_stream",
            "streams": "data_stream",
            "user": "access_binding",
            "users": "access_binding",
            "access": "access_binding",
            "properties": "property",
            "accounts": "account",
        }
        key = aliases.get(key, key.rstrip("s"))
    if key not in reg:
        raise ToolError(
            f"Error: Unknown resource_type '{resource_type}'. Valid types: "
            + ", ".join(sorted(reg))
        )
    return reg[key]


def request_class(resource: ResourceType, verb: str):
    types = _types()
    if verb == "list":
        name = f"List{_camel(resource.plural_method)}Request"
    else:
        name = f"{verb.capitalize()}{_camel(resource.key)}Request"
    cls = getattr(types, name, None)
    if cls is None:
        raise ToolError(
            f"Error: '{verb}' is not available for {resource.key} in the "
            "installed Analytics Admin API client."
        )
    return cls


def body_field(resource: ResourceType, verb: str) -> str:
    """Name of the request field holding the resource (create/update)."""
    cls = request_class(resource, verb)
    for name in cls.meta.fields:
        if name not in ("parent", "name", "update_mask", "validate_only") and (
            not name.endswith("_id")
        ):
            return name
    raise ToolError(
        f"Error: Could not find the resource field of {cls.__name__}."
    )


def message_class(resource: ResourceType, verb: str = "update"):
    """The proto-plus message class of the resource."""
    for v in (verb, "create", "update"):
        try:
            cls = request_class(resource, v)
        except ToolError:
            continue
        field_name = body_field(resource, v)
        return cls.meta.fields[field_name].message
    # Read-only types: use the return type of get.
    types = _types()
    return getattr(types, _camel(resource.key), None)


# protobuf FieldDescriptor type numbers -> readable names
_PRIMITIVE_NAMES = {
    1: "double",
    2: "float",
    3: "int64",
    4: "uint64",
    5: "int32",
    8: "bool",
    9: "string",
    12: "bytes",
    13: "uint32",
    17: "sint32",
    18: "sint64",
}


def describe_fields(msg_cls, depth: int = 0, max_depth: int = 2) -> Any:
    """Field names, types and enum values of a message, for the agent."""
    if msg_cls is None or not hasattr(msg_cls, "meta"):
        return None
    out: dict[str, Any] = {}
    for name, fld in msg_cls.meta.fields.items():
        info: dict[str, Any] = {}
        if fld.message is not None:
            info["type"] = getattr(
                fld.message,
                "__name__",
                getattr(fld.message, "DESCRIPTOR", None)
                and fld.message.DESCRIPTOR.full_name,
            )
            if depth < max_depth and hasattr(fld.message, "meta"):
                info["fields"] = describe_fields(
                    fld.message, depth + 1, max_depth
                )
        elif fld.enum is not None:
            info["type"] = "enum"
            info["values"] = [
                e.name for e in fld.enum if not e.name.endswith("UNSPECIFIED")
            ]
        else:
            info["type"] = _PRIMITIVE_NAMES.get(int(fld.proto_type), "value")
        if fld.repeated:
            info["repeated"] = True
        out[name] = info
    return out


def parent_path(
    resource: ResourceType,
    *,
    account_id: Any = None,
    property_id: Any = None,
    data_stream_id: Any = None,
) -> str | None:
    """Parent resource name for list/create."""
    if resource.parent == ROOT:
        return None
    if resource.parent == ACCOUNT:
        if account_id is None:
            raise ToolError(
                f"Error: 'account_id' is required for {resource.key}."
            )
        return ga.account_name(account_id)
    if resource.parent == PROPERTY:
        if resource.key == "access_binding" and account_id and not property_id:
            return ga.account_name(account_id)
        if property_id is None:
            raise ToolError(
                f"Error: 'property_id' is required for {resource.key}."
            )
        return ga.property_name(property_id)
    if property_id is None or data_stream_id is None:
        raise ToolError(
            f"Error: 'property_id' and 'data_stream_id' are required for "
            f"{resource.key}."
        )
    return ga.data_stream_name(property_id, data_stream_id)


def resource_path(
    resource: ResourceType,
    *,
    account_id: Any = None,
    property_id: Any = None,
    data_stream_id: Any = None,
    resource_id: Any = None,
    name: str | None = None,
) -> str:
    """Full resource name for get/update/delete."""
    if name:
        return str(name).strip()
    if resource.key == "account":
        return ga.account_name(account_id or resource_id)
    if resource.key == "property":
        return ga.property_name(property_id or resource_id)
    parent = parent_path(
        resource,
        account_id=account_id,
        property_id=property_id,
        data_stream_id=data_stream_id,
    )
    if resource.singleton:
        return f"{parent}/{resource.singleton}"
    if resource.key == "data_stream" and data_stream_id and not resource_id:
        resource_id = data_stream_id
    if resource_id in (None, ""):
        raise ToolError(
            f"Error: 'resource_id' (or the full 'name') is required for "
            f"{resource.key}."
        )
    rid = str(resource_id).strip().rstrip("/").split("/")[-1]
    return f"{parent}/{resource.collection}/{rid}"


def scope_id_of(path: str) -> str:
    """The account or property a resource name belongs to (for checksums)."""
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] in ("properties", "accounts"):
        return f"{parts[0]}/{parts[1]}"
    return path
