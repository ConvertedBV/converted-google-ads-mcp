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

"""Account-level exclusions: placements, YouTube channels, content labels.

Registered on the ``campaign_management`` namespace. These apply to every
campaign in the account that serves on the Display Network, YouTube,
Demand Gen or Performance Max inventory.
"""

from enum import Enum
import re

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_management import campaign_management_mcp
import ads_mcp.utils as utils

_PLACEMENT_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}(/\S*)?$", re.I)
_CHANNEL_RE = re.compile(r"^UC[\w-]{22}$")


class ExclusionOperation(str, Enum):
    ADD = "ADD"
    REMOVE = "REMOVE"


class ContentLabel(str, Enum):
    """Content categories that can be excluded account-wide."""

    SEXUALLY_SUGGESTIVE = "SEXUALLY_SUGGESTIVE"
    BELOW_THE_FOLD = "BELOW_THE_FOLD"
    PARKED_DOMAIN = "PARKED_DOMAIN"
    JUVENILE = "JUVENILE"
    PROFANITY = "PROFANITY"
    TRAGEDY = "TRAGEDY"
    VIDEO = "VIDEO"
    EMBEDDED_VIDEO = "EMBEDDED_VIDEO"
    LIVE_STREAMING_VIDEO = "LIVE_STREAMING_VIDEO"
    SOCIAL_ISSUES = "SOCIAL_ISSUES"


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_account_exclusions(
    customer_id: str | int,
    operation: ExclusionOperation,
    placements: list[str] | None = None,
    youtube_channel_ids: list[str] | None = None,
    content_labels: list[ContentLabel] | None = None,
    criterion_ids: list[str] | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds or removes account-wide exclusions for brand safety.

    ADD excludes websites/apps placements (domains such as 'example.com' or
    URLs without scheme), YouTube channels (IDs starting with 'UC') and
    sensitive content labels for every campaign in the account. REMOVE
    takes `criterion_ids` from the customer_negative_criterion resource.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        operation: 'ADD' or 'REMOVE'.
        placements: Domains or URLs to exclude (max 200).
        youtube_channel_ids: YouTube channel IDs to exclude.
        content_labels: Content categories to exclude.
        criterion_ids: Exclusion IDs to remove (REMOVE only).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    op_value = mh.enum_value(operation, ExclusionOperation, "operation")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    operations = []
    change_items: list[pac.ChangeItem] = []

    if op_value == "ADD":
        sites = []
        for raw in placements or []:
            site = re.sub(r"^https?://", "", str(raw).strip(), flags=re.I)
            if not _PLACEMENT_RE.match(site):
                raise ToolError(f"Error: Invalid placement '{raw}'.")
            sites.append(site.lower())
        sites = mh.dedupe(sites)
        channels = mh.dedupe(
            [str(c).strip() for c in youtube_channel_ids or []]
        )
        bad = [c for c in channels if not _CHANNEL_RE.match(c)]
        if bad:
            raise ToolError(f"Error: Invalid YouTube channel IDs: {bad}.")
        labels = mh.dedupe(
            [
                mh.enum_value(l, ContentLabel, "content_labels")
                for l in content_labels or []
            ]
        )
        total = len(sites) + len(channels) + len(labels)
        if not total:
            raise ToolError("Error: Pass something to exclude.")
        if total > mh.MAX_ENTITIES_PER_CALL:
            raise ToolError("Error: Too many exclusions in one call.")
        for site in sites:
            op = ga_client.get_type("CustomerNegativeCriterionOperation")
            op.create.placement.url = site
            operations.append(op)
            change_items.append(
                pac.ChangeItem(
                    "CREATE", "Account exclusion", cid, "Placement", site
                )
            )
        for channel in channels:
            op = ga_client.get_type("CustomerNegativeCriterionOperation")
            op.create.youtube_channel.channel_id = channel
            operations.append(op)
            change_items.append(
                pac.ChangeItem(
                    "CREATE",
                    "Account exclusion",
                    cid,
                    "YouTube channel",
                    channel,
                )
            )
        for label in labels:
            op = ga_client.get_type("CustomerNegativeCriterionOperation")
            op.create.content_label.type_ = getattr(
                ga_client.enums.ContentLabelTypeEnum, label
            )
            operations.append(op)
            change_items.append(
                pac.ChangeItem(
                    "CREATE", "Account exclusion", cid, "Content label", label
                )
            )
        payload = {"placements": sites, "channels": channels, "labels": labels}
    else:
        ids = mh.require_ids(criterion_ids, "criterion_ids")
        rows = mh.search(
            ga_client,
            cid,
            "SELECT customer_negative_criterion.id, "
            "customer_negative_criterion.type, "
            "customer_negative_criterion.placement.url, "
            "customer_negative_criterion.youtube_channel.channel_id, "
            "customer_negative_criterion.content_label.type "
            "FROM customer_negative_criterion WHERE "
            f"customer_negative_criterion.id IN ({', '.join(ids)})",
        )
        found = {str(r.customer_negative_criterion.id): r for r in rows}
        missing = [i for i in ids if i not in found]
        if missing:
            raise ToolError(
                f"Error: Exclusions not found: {', '.join(missing)}."
            )
        for criterion_id in ids:
            criterion = found[criterion_id].customer_negative_criterion
            op = ga_client.get_type("CustomerNegativeCriterionOperation")
            op.remove = (
                f"customers/{cid}/customerNegativeCriteria/{criterion_id}"
            )
            operations.append(op)
            readable = (
                criterion.placement.url
                or criterion.youtube_channel.channel_id
                or criterion.content_label.type_.name
            )
            change_items.append(
                pac.ChangeItem(
                    "REMOVE",
                    "Account exclusion",
                    cid,
                    criterion.type_.name.replace("_", " ").title(),
                    readable,
                )
            )
        payload = {"criterion_ids": ids}

    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="CustomerNegativeCriterionService",
        request_type="MutateCustomerNegativeCriteriaRequest",
        mutate_method="mutate_customer_negative_criteria",
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_account_exclusions",
            "operation": op_value,
            **payload,
        },
        change_items=change_items,
        success_message=f"Updated {len(operations)} account exclusions.",
        error_prefix="Failed to update account exclusions",
    )
