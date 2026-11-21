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

"""Custom segments (custom audiences) from keywords, websites and apps.

Registered on the ``audiences`` namespace. A custom segment describes people
by what they search for, which websites they visit or which apps they use.
Use the returned ID in manage_audience_targeting (custom_audience_ids), as a
Demand Gen ad group audience, or as a Performance Max audience signal.
"""

from enum import Enum
import re
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.audiences import audiences_mcp
import ads_mcp.utils as utils

MAX_MEMBERS = 500
_APP_RE = re.compile(r"^[A-Za-z0-9_.-]{3,200}$")


class CustomSegmentType(str, Enum):
    """AUTO lets Google interpret the members (recommended); SEARCH means
    people who searched for these keywords on Google. INTEREST and
    PURCHASE_INTENT are legacy types that new segments cannot use."""

    AUTO = "AUTO"
    SEARCH = "SEARCH"
    INTEREST = "INTEREST"
    PURCHASE_INTENT = "PURCHASE_INTENT"


class CustomSegmentOperation(str, Enum):
    CREATE = "CREATE"
    REPLACE_MEMBERS = "REPLACE_MEMBERS"
    RENAME = "RENAME"


def _members(
    keywords: list[str] | None,
    urls: list[str] | None,
    apps: list[str] | None,
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for text in (
        mh.clean_texts(keywords, "keywords", MAX_MEMBERS) if keywords else []
    ):
        if len(text) > 80 or len(text.split()) > 10:
            raise ToolError(
                f"Error: Keyword too long (max 80 characters, 10 words): "
                f"'{text}'."
            )
        result.append(("KEYWORD", text.lower()))
    for raw in urls or []:
        url = str(raw).strip()
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        result.append(("URL", mh.require_url(url, "urls")))
    for raw in apps or []:
        app = str(raw).strip()
        if not app:
            continue
        if not _APP_RE.match(app):
            raise ToolError(
                f"Error: '{app}' is not an app ID (e.g. com.example.app or "
                "an App Store ID)."
            )
        result.append(("APP", app))
    seen = set()
    unique = []
    for member in result:
        if member not in seen:
            seen.add(member)
            unique.append(member)
    if not unique:
        raise ToolError("Error: Pass 'keywords', 'urls' and/or 'apps'.")
    if len(unique) > MAX_MEMBERS:
        raise ToolError(f"Error: At most {MAX_MEMBERS} members per segment.")
    return unique


def _fill_members(ga_client: Any, audience: Any, members) -> None:
    enums = ga_client.enums.CustomAudienceMemberTypeEnum
    for kind, value in members:
        member = ga_client.get_type("CustomAudienceMember")
        member.member_type = getattr(enums, kind)
        if kind == "KEYWORD":
            member.keyword = value
        elif kind == "URL":
            member.url = value
        else:
            member.app = value
        audience.members.append(member)


@audiences_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_custom_segment(
    customer_id: str | int,
    operation: CustomSegmentOperation = CustomSegmentOperation.CREATE,
    custom_audience_id: str | None = None,
    name: str | None = None,
    segment_type: CustomSegmentType = CustomSegmentType.AUTO,
    keywords: list[str] | None = None,
    urls: list[str] | None = None,
    apps: list[str] | None = None,
    description: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates or edits a custom segment from keywords, websites and apps.

    Examples: people searching for "airco installateur" and "warmtepomp
    prijs" (segment_type SEARCH), or people interested in what competitors'
    websites offer (urls of those sites, AUTO). Operations:
    - CREATE: `name`, `segment_type` and members.
    - REPLACE_MEMBERS: `custom_audience_id` and the complete new member list.
    - RENAME: `custom_audience_id` and `name`.
    Use the ID in manage_audience_targeting, Demand Gen or as a PMax signal.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        operation: CREATE (default), REPLACE_MEMBERS or RENAME.
        custom_audience_id: Existing custom segment ID.
        name: Segment name (CREATE, RENAME).
        segment_type: AUTO (default) or SEARCH.
        keywords: Interests or search terms, max 80 characters and 10
            words each.
        urls: Websites people visit, e.g. competitor.be.
        apps: App package names or App Store IDs.
        description: Optional description (CREATE).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    op_value = mh.enum_value(operation, CustomSegmentOperation, "operation")
    type_value = mh.enum_value(segment_type, CustomSegmentType, "segment_type")
    clean_name = " ".join((name or "").split())
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    op = ga_client.get_type("CustomAudienceOperation")
    payload: dict[str, Any] = {
        "tool": "manage_custom_segment",
        "operation": op_value,
    }

    if op_value == "CREATE":
        if type_value not in ("AUTO", "SEARCH"):
            raise ToolError(
                "Error: New custom segments are AUTO or SEARCH; INTEREST and "
                "PURCHASE_INTENT only exist on older segments."
            )
        if not clean_name:
            raise ToolError("Error: 'name' is required.")
        members = _members(keywords, urls, apps)
        audience = op.create
        audience.name = clean_name
        audience.type_ = getattr(
            ga_client.enums.CustomAudienceTypeEnum, type_value
        )
        audience.status = ga_client.enums.CustomAudienceStatusEnum.ENABLED
        if description:
            audience.description = " ".join(description.split())[:200]
        _fill_members(ga_client, audience, members)
        change_items = pac.build_change_items(
            operation="CREATE",
            entity_readable="Custom segment",
            primary_key_readable=clean_name,
            fields={
                "Type": type_value,
                "Members": "; ".join(f"{k.lower()}: {v}" for k, v in members),
            },
        )
        payload.update(
            name=clean_name,
            type=type_value,
            members=members,
            description=description,
        )
        success = f"Created custom segment '{clean_name}'."
    else:
        segment_id = mh.require_simple_id(
            custom_audience_id, "custom_audience_id"
        )
        rn = f"customers/{cid}/customAudiences/{segment_id}"
        rows = mh.search(
            ga_client,
            cid,
            "SELECT custom_audience.name, custom_audience.type, "
            "custom_audience.members FROM custom_audience "
            f"WHERE custom_audience.id = {segment_id}",
        )
        if not rows:
            raise ToolError(f"Error: Custom segment {segment_id} not found.")
        current = rows[0].custom_audience
        audience = op.update
        audience.resource_name = rn
        if op_value == "RENAME":
            if not clean_name:
                raise ToolError("Error: 'name' is required for RENAME.")
            audience.name = clean_name
            op.update_mask.paths.append("name")
            fields = {"Name": clean_name}
            original = {"Name": current.name}
            payload["name"] = clean_name
        else:
            members = _members(keywords, urls, apps)
            _fill_members(ga_client, audience, members)
            op.update_mask.paths.append("members")
            fields = {
                "Members": "; ".join(f"{k.lower()}: {v}" for k, v in members)
            }
            original = {
                "Members": "; ".join(
                    f"{mh.enum_name(m.member_type).lower()}: "
                    f"{m.keyword or m.url or m.app or m.place_category}"
                    for m in current.members
                )
            }
            payload["members"] = members
        change_items = pac.build_change_items(
            operation="UPDATE",
            entity_readable="Custom segment",
            primary_key_readable=f"{current.name} ({rn})",
            fields=fields,
            original_fields=original,
        )
        payload["custom_audience_id"] = segment_id
        success = f"Updated custom segment {rn}."

    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="CustomAudienceService",
        request_type="MutateCustomAudiencesRequest",
        mutate_method="mutate_custom_audiences",
        operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=payload,
        change_items=change_items,
        success_message=success,
        error_prefix="Failed to manage custom segment",
    )
