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

"""Propose & confirm engine for GA4 Admin API changes.

The Admin API has no validate_only mode. A proposal therefore:
  1. builds the request message locally (rejects unknown fields and enum
     values before anything is sent),
  2. reads the current resource (for updates, deletes and archives) so the
     change items show the current value next to the new one,
  3. returns the change items and a checksum bound to the property/account
     and the exact parameters.
Only a second call with validate_only=False and that checksum commits.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp.exceptions import ToolError

from ads_mcp import propose_and_confirm as pac
from ads_mcp.analytics import client as ga
from ads_mcp.analytics import registry as reg

VERBS = ("create", "update", "delete", "archive")


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, key))
        return out
    return {prefix: value}


def _pick(data: Any, dotted: str) -> Any:
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def build_message(msg_cls, fields: dict[str, Any] | None, label: str):
    """Builds a proto-plus message, turning field errors into ToolErrors."""
    try:
        return msg_cls(fields or {})
    except (ValueError, TypeError, KeyError) as exc:
        valid = ", ".join(sorted(msg_cls.meta.fields))
        raise ToolError(
            f"Error: invalid fields for {label}: {exc}. Valid top-level "
            f"fields: {valid}. Use analytics_admin_get_resource_schema for "
            "the full structure."
        ) from exc


def fetch_current(resource: reg.ResourceType, path: str) -> dict | None:
    """Reads the current resource; None when it cannot be found."""
    client = ga.admin_client()
    if "get" in resource.allowed:
        method = getattr(client, f"get_{resource.key}")
        return ga.message_to_dict(
            ga.call_api(method, name=path),
            include_defaults=bool(resource.singleton),
        )
    if "list" in resource.allowed:
        parent = path.rsplit("/", 2)[0]
        method = getattr(client, f"list_{resource.plural_method}")
        for item in ga.call_api(method, parent=parent):
            if item.name == path:
                return ga.message_to_dict(item)
        raise ToolError(f"Error: {resource.key} '{path}' was not found.")
    return None


def propose_change(
    *,
    tool_name: str,
    resource_type: str,
    verb: str,
    fields: dict[str, Any] | None = None,
    update_mask: list[str] | None = None,
    account_id: Any = None,
    property_id: Any = None,
    data_stream_id: Any = None,
    resource_id: Any = None,
    name: str | None = None,
    extra_request_fields: dict[str, Any] | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
    entity_label: str | None = None,
) -> str:
    """Generic propose & confirm for create/update/delete/archive."""
    verb = str(verb or "").strip().lower()
    if verb not in VERBS:
        raise ToolError(
            f"Error: operation must be one of {', '.join(v.upper() for v in VERBS)}."
        )
    resource = reg.get_type(resource_type)
    if verb not in resource.allowed:
        raise ToolError(
            f"Error: {verb.upper()} is not available for {resource.key}. "
            f"Allowed: {', '.join(sorted(o.upper() for o in resource.allowed))}."
        )
    label = entity_label or resource.key.replace("_", " ")
    fields = dict(fields or {})
    extra = dict(extra_request_fields or {})

    request_cls = reg.request_class(resource, verb)
    request_kwargs: dict[str, Any] = {}
    original: dict | None = None
    path: str

    if verb == "create":
        parent = reg.parent_path(
            resource,
            account_id=account_id,
            property_id=property_id,
            data_stream_id=data_stream_id,
        )
        body_name = reg.body_field(resource, "create")
        msg_cls = request_cls.meta.fields[body_name].message
        body = build_message(msg_cls, fields, label)
        if parent is not None:
            request_kwargs["parent"] = parent
        request_kwargs[body_name] = body
        path = parent or ""
        primary_key = f"{parent}/{resource.collection}" if parent else label
        change_fields = {k: v for k, v in _flatten(fields).items()}
        original_fields: dict[str, Any] = {}
        op_label = "CREATE"
    else:
        path = reg.resource_path(
            resource,
            account_id=account_id,
            property_id=property_id,
            data_stream_id=data_stream_id,
            resource_id=resource_id,
            name=name,
        )
        primary_key = path
        op_label = verb.upper()
        if verb == "update":
            if not fields:
                raise ToolError("Error: 'fields' with new values is required.")
            body_name = reg.body_field(resource, "update")
            msg_cls = request_cls.meta.fields[body_name].message
            mask = list(update_mask or [])
            if not mask:
                mask = sorted(fields.keys())
            body = build_message(msg_cls, {**fields, "name": path}, label)
            request_kwargs[body_name] = body
            if "update_mask" in request_cls.meta.fields:
                request_kwargs["update_mask"] = {"paths": mask}
            change_fields = {
                k: v
                for k, v in _flatten(fields).items()
                if any(k == m or k.startswith(m + ".") for m in mask)
            }
        else:  # delete / archive
            request_kwargs["name"] = path
            change_fields = {"name": path}
        original = fetch_current(resource, path)
        original_fields = (
            {k: _pick(original, k) for k in change_fields} if original else {}
        )
        if verb in ("delete", "archive") and original:
            # Show what is being removed.
            summary = {
                k: original.get(k)
                for k in (
                    "display_name",
                    "event_name",
                    "parameter_name",
                    "title",
                    "customer_id",
                    "email_address",
                    "user",
                )
                if original.get(k) not in (None, "")
            }
            change_fields = {**change_fields, **summary}

    for key, value in extra.items():
        if key not in request_cls.meta.fields:
            raise ToolError(f"Error: unknown request field '{key}'.")
        request_kwargs[key] = value

    try:
        request = request_cls(**request_kwargs)
    except (ValueError, TypeError, KeyError) as exc:
        raise ToolError(f"Error: invalid request for {label}: {exc}") from exc

    scope = reg.scope_id_of(path or ga.account_name(account_id or 0))
    operation_payload = {
        "tool": tool_name,
        "resource_type": resource.key,
        "verb": verb,
        "path": path,
        "fields": fields,
        "update_mask": request_kwargs.get("update_mask"),
        "extra": extra,
    }

    change_items = pac.build_change_items(
        operation=op_label,
        entity_readable=f"GA4 {label}",
        primary_key_readable=primary_key,
        fields={
            k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
            for k, v in change_fields.items()
        },
        original_fields={
            k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
            for k, v in original_fields.items()
            if v is not None
        },
    )
    if not change_items:
        change_items = [
            pac.ChangeItem(
                operation=op_label,
                entity_readable=f"GA4 {label}",
                primary_key_readable=primary_key,
            )
        ]

    def _commit() -> str:
        client = ga.admin_client()
        method_name = (
            f"{verb}_{resource.key}"
            if verb != "list"
            else f"list_{resource.plural_method}"
        )
        method = getattr(client, method_name)
        result = ga.call_api(method, request=request)
        payload: dict[str, Any] = {
            "status": "DONE",
            "operation": op_label,
            "resource_type": resource.key,
        }
        result_dict = ga.message_to_dict(result)
        if result_dict:
            payload["result"] = result_dict
        else:
            payload["name"] = path
        return ga.to_json(payload)

    return pac.propose_and_confirm(
        customer_id=scope,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=None,
        commit_fn=_commit,
        error_prefix=f"Failed to {verb} {label}",
    )
