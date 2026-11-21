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

"""Brand lists: brand exclusions and brand restrictions.

Registered on the ``campaign_management`` namespace. A brand list is a
shared set of type BRANDS. Attached to a Search or Performance Max campaign
as an exclusion it keeps ads off searches for those brands (for example
competitors, or your own brand in a non-brand PMax campaign). Attached to a
Search campaign as an inclusion it restricts broad match and AI Max to
searches about those brands.
"""

from enum import Enum
import json
from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.account_maintenance import _campaign_names
from ads_mcp.tools.campaign_management import campaign_management_mcp
import ads_mcp.utils as utils

MAX_BRANDS_PER_LIST = 1000
_BRAND_CHANNELS = ("SEARCH", "PERFORMANCE_MAX")


@campaign_management_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def suggest_brands(
    customer_id: str | int,
    brand_prefix: str,
    login_customer_id: str | int | None = None,
) -> str:
    """Finds brands by name for brand lists (exclusions or restrictions).

    Returns each brand's `brand_id` (use it in manage_brand_list), name,
    website URLs and state. Only brands Google recognizes can be added.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        brand_prefix: Start of the brand name, e.g. 'daik' for Daikin.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    prefix = " ".join((brand_prefix or "").split())
    if len(prefix) < 2:
        raise ToolError("Error: 'brand_prefix' needs at least 2 characters.")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    service = utils.get_googleads_service_from_client(
        ga_client, "BrandSuggestionService"
    )
    request = ga_client.get_type("SuggestBrandsRequest")
    request.customer_id = cid
    request.brand_prefix = prefix
    try:
        response = service.suggest_brands(request=request)
    except GoogleAdsException as exc:
        raise ToolError(
            "Brand suggestions failed:\n" + utils.format_google_ads_error(exc)
        ) from exc
    brands = [
        {
            "brand_id": brand.id,
            "name": brand.name,
            "urls": list(brand.urls),
            "state": mh.enum_name(brand.state),
        }
        for brand in response.brands
    ]
    return json.dumps(
        {"prefix": prefix, "brands": brands}, indent=2, ensure_ascii=False
    )


class BrandListOperation(str, Enum):
    CREATE_LIST = "CREATE_LIST"
    ADD_BRANDS = "ADD_BRANDS"
    REMOVE_BRANDS = "REMOVE_BRANDS"
    ATTACH_CAMPAIGNS = "ATTACH_CAMPAIGNS"
    DETACH_CAMPAIGNS = "DETACH_CAMPAIGNS"


class BrandListUse(str, Enum):
    """EXCLUDE keeps ads off these brands; RESTRICT limits Search broad
    match and AI Max to these brands."""

    EXCLUDE = "EXCLUDE"
    RESTRICT = "RESTRICT"


def _clean_brand_ids(values: list[str] | None) -> list[str]:
    ids = mh.dedupe([str(v).strip() for v in values or [] if str(v).strip()])
    if not ids:
        raise ToolError("Error: Pass 'brand_ids' from suggest_brands.")
    if len(ids) > MAX_BRANDS_PER_LIST:
        raise ToolError(
            f"Error: At most {MAX_BRANDS_PER_LIST} brands per call."
        )
    if any("'" in i or len(i) > 100 for i in ids):
        raise ToolError("Error: Invalid brand ID.")
    return ids


def _fetch_brand_list(ga_client: Any, cid: str, set_id: str) -> Any:
    rows = mh.search(
        ga_client,
        cid,
        "SELECT shared_set.name, shared_set.type, shared_set.member_count, "
        "shared_set.reference_count FROM shared_set "
        f"WHERE shared_set.id = {set_id}",
    )
    if not rows or mh.enum_name(rows[0].shared_set.type_) != "BRANDS":
        raise ToolError(
            f"Error: Shared set {set_id} is not a brand list of this account."
        )
    return rows[0].shared_set


def _check_channels(
    ga_client: Any, cid: str, campaigns: list[str], use: str
) -> None:
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign.id, campaign.advertising_channel_type FROM campaign "
        f"WHERE campaign.id IN ({', '.join(campaigns)})",
    )
    wrong = []
    for row in rows:
        channel = mh.enum_name(row.campaign.advertising_channel_type)
        allowed = _BRAND_CHANNELS if use == "EXCLUDE" else ("SEARCH",)
        if channel not in allowed:
            wrong.append(f"{row.campaign.id} ({channel})")
    if wrong:
        raise ToolError(
            "Error: Brand exclusions work on Search and Performance Max, "
            "brand restrictions only on Search. Not possible for: "
            + ", ".join(wrong)
        )


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_brand_list(
    customer_id: str | int,
    operation: BrandListOperation,
    shared_set_id: str | None = None,
    name: str | None = None,
    brand_ids: list[str] | None = None,
    criterion_ids: list[str] | None = None,
    campaign_ids: list[str] | None = None,
    use: BrandListUse = BrandListUse.EXCLUDE,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Manages brand lists and the campaigns that use them.

    Typical uses: exclude competitors' brands, exclude your own brand from a
    non-brand Performance Max or Search campaign, or restrict a brand
    campaign's broad match / AI Max to your own brand (`use=RESTRICT`).
    Operations:
    - CREATE_LIST: `name`, `brand_ids` (from suggest_brands) and optionally
      `campaign_ids` + `use` to attach right away; created atomically.
    - ADD_BRANDS / REMOVE_BRANDS: `shared_set_id` plus `brand_ids` or
      `criterion_ids` (shared_criterion.criterion_id).
    - ATTACH_CAMPAIGNS / DETACH_CAMPAIGNS: `shared_set_id`, `campaign_ids`
      (+ `use` when attaching).
    Find brand lists with the search tool on shared_set (type BRANDS).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        operation: One of the operations above.
        shared_set_id: Numeric ID of an existing brand list.
        name: Name for a new brand list (CREATE_LIST).
        brand_ids: Brand IDs from suggest_brands.
        criterion_ids: Brand criterion IDs to remove from the list.
        campaign_ids: Campaigns to attach or detach.
        use: EXCLUDE (default; Search and PMax) or RESTRICT (Search only).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    op_value = mh.enum_value(operation, BrandListOperation, "operation")
    use_value = mh.enum_value(use, BrandListUse, "use")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    payload: dict[str, Any] = {
        "tool": "manage_brand_list",
        "operation": op_value,
    }
    operations: list[Any] = []
    change_items: list[pac.ChangeItem] = []

    def _brand_op(set_rn: str, brand_id: str, label: str) -> None:
        op = ga_client.get_type("MutateOperation")
        criterion = op.shared_criterion_operation.create
        criterion.shared_set = set_rn
        criterion.brand.entity_id = brand_id
        operations.append(op)
        change_items.append(
            pac.ChangeItem("CREATE", "Brand", label, "Brand ID", brand_id)
        )

    def _attach_op(set_rn: str, campaign: str, label: str, names) -> None:
        op = ga_client.get_type("MutateOperation")
        criterion = op.campaign_criterion_operation.create
        criterion.campaign = f"customers/{cid}/campaigns/{campaign}"
        criterion.brand_list.shared_set = set_rn
        criterion.negative = use_value == "EXCLUDE"
        operations.append(op)
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Brand list "
                + ("exclusion" if criterion.negative else "restriction"),
                label,
                "Campaign",
                f"{names[campaign]} ({campaign})",
            )
        )

    if op_value == "CREATE_LIST":
        list_name = " ".join((name or "").split())
        if not list_name:
            raise ToolError("Error: 'name' is required for CREATE_LIST.")
        brands = _clean_brand_ids(brand_ids)
        campaigns = (
            mh.require_ids(campaign_ids, "campaign_ids") if campaign_ids else []
        )
        names = _campaign_names(ga_client, cid, campaigns) if campaigns else {}
        if campaigns:
            _check_channels(ga_client, cid, campaigns, use_value)
        set_rn = f"customers/{cid}/sharedSets/-1"
        set_op = ga_client.get_type("MutateOperation")
        shared_set = set_op.shared_set_operation.create
        shared_set.resource_name = set_rn
        shared_set.name = list_name
        shared_set.type_ = ga_client.enums.SharedSetTypeEnum.BRANDS
        operations.append(set_op)
        change_items.append(
            pac.ChangeItem("CREATE", "Brand list", list_name, "Name", list_name)
        )
        for brand in brands:
            _brand_op(set_rn, brand, list_name)
        for campaign in campaigns:
            _attach_op(set_rn, campaign, list_name, names)
        payload.update(name=list_name, brand_ids=brands, campaign_ids=campaigns)
        if campaigns:
            payload["use"] = use_value
        success = f"Created brand list '{list_name}' ({len(brands)} brands)."
    else:
        set_id = mh.require_simple_id(shared_set_id, "shared_set_id")
        shared_set = _fetch_brand_list(ga_client, cid, set_id)
        set_rn = f"customers/{cid}/sharedSets/{set_id}"
        label = f"{shared_set.name} ({set_rn})"
        payload["shared_set_id"] = set_id
        if op_value == "ADD_BRANDS":
            brands = _clean_brand_ids(brand_ids)
            for brand in brands:
                _brand_op(set_rn, brand, label)
            payload["brand_ids"] = brands
        elif op_value == "REMOVE_BRANDS":
            ids = mh.require_ids(
                criterion_ids, "criterion_ids", max_items=MAX_BRANDS_PER_LIST
            )
            rns = [f"customers/{cid}/sharedCriteria/{set_id}~{c}" for c in ids]
            rows = mh.search(
                ga_client,
                cid,
                "SELECT shared_criterion.resource_name, "
                "shared_criterion.brand.display_name FROM shared_criterion "
                "WHERE shared_criterion.resource_name IN "
                f"({mh.quoted_list(rns)})",
            )
            found = {r.shared_criterion.resource_name: r for r in rows}
            missing = [rn for rn in rns if rn not in found]
            if missing:
                raise ToolError(
                    f"Error: Not in brand list {set_id}: {', '.join(missing)}"
                )
            for rn in rns:
                op = ga_client.get_type("MutateOperation")
                op.shared_criterion_operation.remove = rn
                operations.append(op)
                change_items.append(
                    pac.ChangeItem(
                        "REMOVE",
                        "Brand",
                        label,
                        "Brand",
                        found[rn].shared_criterion.brand.display_name or rn,
                    )
                )
            payload["criterion_ids"] = ids
        else:
            campaigns = mh.require_ids(campaign_ids, "campaign_ids")
            names = _campaign_names(ga_client, cid, campaigns)
            if op_value == "ATTACH_CAMPAIGNS":
                _check_channels(ga_client, cid, campaigns, use_value)
                for campaign in campaigns:
                    _attach_op(set_rn, campaign, label, names)
                payload["use"] = use_value
            else:
                rows = mh.search(
                    ga_client,
                    cid,
                    "SELECT campaign_criterion.resource_name, campaign.id, "
                    "campaign_criterion.negative FROM campaign_criterion "
                    "WHERE campaign_criterion.type = 'BRAND_LIST' "
                    f"AND campaign_criterion.brand_list.shared_set = '{set_rn}' "
                    f"AND campaign.id IN ({', '.join(campaigns)})",
                )
                attached = {str(r.campaign.id): r for r in rows}
                missing = [c for c in campaigns if c not in attached]
                if missing:
                    raise ToolError(
                        "Error: The brand list is not attached to campaigns: "
                        + ", ".join(missing)
                    )
                for campaign in campaigns:
                    row = attached[campaign]
                    op = ga_client.get_type("MutateOperation")
                    op.campaign_criterion_operation.remove = (
                        row.campaign_criterion.resource_name
                    )
                    operations.append(op)
                    change_items.append(
                        pac.ChangeItem(
                            "REMOVE",
                            "Brand list "
                            + (
                                "exclusion"
                                if row.campaign_criterion.negative
                                else "restriction"
                            ),
                            label,
                            "Campaign",
                            f"{names[campaign]} ({campaign})",
                        )
                    )
            payload["campaign_ids"] = campaigns
        success = f"Applied {op_value} on {set_rn}."

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=payload,
        change_items=change_items,
        success_message=success,
        error_prefix=f"Failed to {op_value.lower()} brand list",
    )
