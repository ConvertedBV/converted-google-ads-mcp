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

"""Product groups: which products a Shopping ad group or a retail
Performance Max asset group advertises, split by one product dimension.

Registered on the ``campaign_management`` namespace. Both tools replace the
whole tree in one atomic request: the existing tree is removed and a new one
is built with a node per value plus an "everything else" node, so the tree is
always complete.
"""

from enum import Enum
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_management import campaign_management_mcp
import ads_mcp.utils as utils

MAX_VALUES = 100


class ProductDimension(str, Enum):
    BRAND = "BRAND"
    CATEGORY = "CATEGORY"
    PRODUCT_TYPE = "PRODUCT_TYPE"
    CUSTOM_LABEL = "CUSTOM_LABEL"
    ITEM_ID = "ITEM_ID"
    CONDITION = "CONDITION"
    CHANNEL = "CHANNEL"


# dimension -> (case_value field, value field)
_FIELDS = {
    "BRAND": ("product_brand", "value"),
    "CATEGORY": ("product_category", "category_id"),
    "PRODUCT_TYPE": ("product_type", "value"),
    "CUSTOM_LABEL": ("product_custom_attribute", "value"),
    "ITEM_ID": ("product_item_id", "value"),
    "CONDITION": ("product_condition", "condition"),
    "CHANNEL": ("product_channel", "channel"),
}
_ENUM_VALUES = {
    "CONDITION": ("ProductConditionEnum", ("NEW", "USED", "REFURBISHED")),
    "CHANNEL": ("ProductChannelEnum", ("ONLINE", "LOCAL")),
}


class ProductGroupValue(BaseModel):
    """One product group: a dimension value with its bid or exclusion."""

    value: str = Field(
        description="Brand, product type, custom label or item ID text; a "
        "numeric category ID; NEW/USED/REFURBISHED; or ONLINE/LOCAL."
    )
    cpc_bid: float | None = Field(
        default=None,
        description="Max CPC for this group (Shopping manual CPC only).",
    )
    excluded: bool = Field(default=False, description="Do not advertise.")


def _normalize(
    dimension: Any,
    level: int,
    custom_label_index: int,
    values: list[ProductGroupValue | dict],
) -> tuple[str, list[dict[str, Any]]]:
    dim = mh.enum_value(dimension, ProductDimension, "dimension")
    if dim in ("CATEGORY", "PRODUCT_TYPE") and int(level) != 1:
        # Google needs every deeper level under its parent level.
        raise ToolError("Error: Only the top level (1) is supported.")
    if dim == "CUSTOM_LABEL" and not 0 <= int(custom_label_index) <= 4:
        raise ToolError("Error: 'custom_label_index' must be 0-4.")
    if not values or len(values) > MAX_VALUES:
        raise ToolError(f"Error: Pass 1-{MAX_VALUES} values.")
    result = []
    seen = set()
    for raw in values:
        item = ProductGroupValue(**raw) if isinstance(raw, dict) else raw
        text = " ".join(str(item.value).split())
        if not text:
            raise ToolError("Error: Empty product group value.")
        if dim == "CATEGORY" and not text.isdigit():
            raise ToolError(
                f"Error: Category values are numeric category IDs; got '{text}'."
            )
        if dim in _ENUM_VALUES:
            text = text.upper()
            if text not in _ENUM_VALUES[dim][1]:
                raise ToolError(
                    f"Error: {dim} values are "
                    f"{', '.join(_ENUM_VALUES[dim][1])}; got '{text}'."
                )
        key = text.lower()
        if key in seen:
            raise ToolError(f"Error: Duplicate value '{text}'.")
        seen.add(key)
        if item.excluded and item.cpc_bid is not None:
            raise ToolError(
                f"Error: '{text}' is excluded and cannot have a bid."
            )
        result.append(
            {
                "value": text,
                "cpc_bid_micros": (
                    mh.to_micros(item.cpc_bid, "cpc_bid")
                    if item.cpc_bid is not None
                    else None
                ),
                "excluded": bool(item.excluded),
            }
        )
    return dim, result


def _fill_case_value(
    ga_client: Any,
    case_value: Any,
    dim: str,
    value: str | None,
    level: int,
    custom_label_index: int,
    pmax: bool = False,
) -> None:
    """Sets the dimension on a case value; value None = 'everything else'."""
    field, value_field = _FIELDS[dim]
    enums = ga_client.enums
    # PMax listing group filters use their own enums with the same values.
    names = {
        "CATEGORY": "ProductCategoryLevelEnum",
        "PRODUCT_TYPE": "ProductTypeLevelEnum",
        "CUSTOM_LABEL": "ProductCustomAttributeIndexEnum",
        "CONDITION": "ProductConditionEnum",
        "CHANNEL": "ProductChannelEnum",
    }
    if pmax:
        names = {
            "CATEGORY": "ListingGroupFilterProductCategoryLevelEnum",
            "PRODUCT_TYPE": "ListingGroupFilterProductTypeLevelEnum",
            "CUSTOM_LABEL": "ListingGroupFilterCustomAttributeIndexEnum",
            "CONDITION": "ListingGroupFilterProductConditionEnum",
            "CHANNEL": "ListingGroupFilterProductChannelEnum",
        }
    target = getattr(case_value, field)
    target._pb.SetInParent()
    if dim == "CATEGORY":
        target.level = getattr(
            getattr(enums, names["CATEGORY"]), f"LEVEL{int(level)}"
        )
    elif dim == "PRODUCT_TYPE":
        target.level = getattr(
            getattr(enums, names["PRODUCT_TYPE"]), f"LEVEL{int(level)}"
        )
    elif dim == "CUSTOM_LABEL":
        target.index = getattr(
            getattr(enums, names["CUSTOM_LABEL"]),
            f"INDEX{int(custom_label_index)}",
        )
    if value is None:
        return
    if dim in _ENUM_VALUES:
        setattr(target, value_field, getattr(getattr(enums, names[dim]), value))
    elif dim == "CATEGORY":
        setattr(target, value_field, int(value))
    else:
        setattr(target, value_field, value)


def _existing_key(case_value: Any) -> tuple[str, str | None] | None:
    """(dimension, lowercased value or None for 'everything else')."""
    for dim, (field, value_field) in _FIELDS.items():
        if field in case_value:
            raw = getattr(getattr(case_value, field), value_field)
            if dim in _ENUM_VALUES:
                name = mh.enum_name(raw)
                value = (
                    None if name in ("UNSPECIFIED", "UNKNOWN", "0") else name
                )
            else:
                value = str(raw) if raw not in ("", 0, None) else None
            return dim, value.lower() if value else None
    return None


def _describe_node(key, bid: int, excluded: bool, currency: str) -> tuple:
    if key is None:
        return "all products", "default bid"
    dim, value = key
    state = (
        "excluded"
        if excluded
        else mh.format_money(bid, currency) if bid else "default bid"
    )
    return f"{dim.lower()}: {value or 'everything else'}", state


def _label(dim: str, level: int, index: int) -> str:
    if dim in ("CATEGORY", "PRODUCT_TYPE"):
        return f"{dim.lower()} level {level}"
    if dim == "CUSTOM_LABEL":
        return f"custom label {index}"
    return dim.lower()


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def set_product_groups(
    customer_id: str | int,
    ad_group_id: str,
    dimension: ProductDimension,
    values: list[ProductGroupValue],
    other_cpc_bid: float | None = None,
    other_excluded: bool = False,
    custom_label_index: int = 0,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Splits a Shopping ad group's products by one dimension, with bids.

    Replaces the ad group's product group tree with: one group per value
    (own max CPC, or excluded) and "everything else" (`other_cpc_bid`, or
    excluded with `other_excluded`). Example: bid more on brand 'daikin',
    exclude 'used' products, or exclude custom label 0 'low margin'.
    Product dimension values come from the Merchant Center feed (find them
    with the search tool on shopping_performance_view). Bids above the
    current highest bid in the ad group are capped by the safety limit.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        ad_group_id: Shopping product ad group ID.
        dimension: BRAND, CATEGORY, PRODUCT_TYPE, CUSTOM_LABEL, ITEM_ID,
            CONDITION or CHANNEL.
        values: Groups: {{"value": "daikin", "cpc_bid": 0.8}} or
            {{"value": "used", "excluded": true}}.
        other_cpc_bid: Max CPC for everything else (default: the ad group's
            default bid).
        other_excluded: Exclude everything else instead.
        custom_label_index: 0-4 for CUSTOM_LABEL.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    ag_id = mh.require_simple_id(ad_group_id, "ad_group_id")
    level = 1  # top-level categories and product types
    dim, groups = _normalize(dimension, level, custom_label_index, values)
    if other_excluded and other_cpc_bid is not None:
        raise ToolError("Error: 'everything else' cannot be excluded and bid.")
    if all(g["excluded"] for g in groups) and other_excluded:
        raise ToolError("Error: That would exclude every product.")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT ad_group.name, ad_group.type, ad_group.cpc_bid_micros, "
        "campaign.bidding_strategy_type, customer.currency_code "
        f"FROM ad_group WHERE ad_group.id = {ag_id}",
    )
    if (
        not rows
        or mh.enum_name(rows[0].ad_group.type_) != "SHOPPING_PRODUCT_ADS"
    ):
        raise ToolError(
            f"Error: Ad group {ag_id} is not a Shopping product ad group."
        )
    ad_group = rows[0].ad_group
    currency = rows[0].customer.currency_code
    manual = mh.enum_name(rows[0].campaign.bidding_strategy_type) in (
        "MANUAL_CPC",
        "ENHANCED_CPC",
    )
    tree = mh.search(
        ga_client,
        cid,
        "SELECT ad_group_criterion.resource_name, "
        "ad_group_criterion.cpc_bid_micros, ad_group_criterion.negative, "
        "ad_group_criterion.listing_group.type, "
        "ad_group_criterion.listing_group.parent_ad_group_criterion, "
        "ad_group_criterion.listing_group.case_value.product_brand.value, "
        "ad_group_criterion.listing_group.case_value.product_category.category_id, "
        "ad_group_criterion.listing_group.case_value.product_type.value, "
        "ad_group_criterion.listing_group.case_value.product_custom_attribute.value, "
        "ad_group_criterion.listing_group.case_value.product_item_id.value, "
        "ad_group_criterion.listing_group.case_value.product_condition.condition, "
        "ad_group_criterion.listing_group.case_value.product_channel.channel "
        "FROM ad_group_criterion WHERE ad_group_criterion.type = "
        f"'LISTING_GROUP' AND ad_group.id = {ag_id} "
        "AND ad_group_criterion.status != 'REMOVED'",
    )
    roots = [
        r.ad_group_criterion.resource_name
        for r in tree
        if not r.ad_group_criterion.listing_group.parent_ad_group_criterion
    ]
    default_bid = int(ad_group.cpc_bid_micros or 0)
    # Current state per product group, so each new bid is compared with
    # what that group bids today and the proposal shows what is replaced.
    current: dict[Any, tuple[int, bool]] = {}
    for r in tree:
        criterion = r.ad_group_criterion
        if mh.enum_name(criterion.listing_group.type_) != "UNIT":
            continue
        key = (
            _existing_key(criterion.listing_group.case_value)
            if criterion.listing_group.parent_ad_group_criterion
            else None
        )
        current[key] = (int(criterion.cpc_bid_micros or 0), criterion.negative)
    other_micros = (
        mh.to_micros(other_cpc_bid, "other_cpc_bid")
        if other_cpc_bid is not None
        else None
    )
    checks = [((dim, g["value"].lower()), g["cpc_bid_micros"]) for g in groups]
    checks.append(((dim, None), other_micros))
    for key, bid in checks:
        if not bid:
            continue
        if not manual:
            raise ToolError(
                "Error: Product group bids only apply with manual CPC; "
                "leave cpc_bid empty for this campaign's bidding strategy."
            )
        previous = current.get(key)
        baseline = previous[0] if previous and previous[0] else default_bid
        if baseline <= 0:
            raise ToolError(
                "Error: There is no current bid to compare with; set the ad "
                "group's default bid first with update_cpc_bid."
            )
        mh.enforce_increase_guardrail(
            baseline,
            bid,
            f"bid of product group '{key[1] or 'everything else'}'",
        )

    enums = ga_client.enums
    operations: list[Any] = []
    change_items: list[pac.ChangeItem] = []
    for rn in roots:
        op = ga_client.get_type("MutateOperation")
        op.ad_group_criterion_operation.remove = rn
        operations.append(op)
    for key, (bid, excluded) in current.items():
        field, state = _describe_node(key, bid, excluded, currency)
        change_items.append(
            pac.ChangeItem(
                "REMOVE", "Product group", ad_group.name, field, state
            )
        )
    group_rn = f"customers/{cid}/adGroups/{ag_id}"
    root_rn = f"customers/{cid}/adGroupCriteria/{ag_id}~-1"
    root_op = ga_client.get_type("MutateOperation")
    root = root_op.ad_group_criterion_operation.create
    root.resource_name = root_rn
    root.ad_group = group_rn
    root.status = enums.AdGroupCriterionStatusEnum.ENABLED
    root.listing_group.type_ = enums.ListingGroupTypeEnum.SUBDIVISION
    operations.append(root_op)

    def _unit(value: str | None, bid: int | None, excluded: bool, n: int):
        op = ga_client.get_type("MutateOperation")
        unit = op.ad_group_criterion_operation.create
        unit.resource_name = f"customers/{cid}/adGroupCriteria/{ag_id}~-{n}"
        unit.ad_group = group_rn
        unit.status = enums.AdGroupCriterionStatusEnum.ENABLED
        unit.listing_group.type_ = enums.ListingGroupTypeEnum.UNIT
        unit.listing_group.parent_ad_group_criterion = root_rn
        _fill_case_value(
            ga_client,
            unit.listing_group.case_value,
            dim,
            value,
            level,
            custom_label_index,
        )
        if excluded:
            unit.negative = True
        elif bid:
            unit.cpc_bid_micros = bid
        operations.append(op)
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Product group",
                f"{ad_group.name}: {_label(dim, level, custom_label_index)}",
                value or "everything else",
                (
                    "excluded"
                    if excluded
                    else (
                        mh.format_money(bid, currency)
                        if bid
                        else "ad group default bid"
                    )
                ),
            )
        )

    for n, group in enumerate(groups, start=2):
        _unit(group["value"], group["cpc_bid_micros"], group["excluded"], n)
    _unit(None, other_micros, other_excluded, len(groups) + 2)

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "set_product_groups",
            "ad_group_id": ag_id,
            "dimension": dim,
            "level": level,
            "custom_label_index": custom_label_index,
            "groups": groups,
            "other_cpc_bid_micros": other_micros,
            "other_excluded": other_excluded,
            "replaced_roots": roots,
        },
        change_items=change_items,
        success_message=(
            f"Set {len(groups) + 1} product groups on ad group {ag_id}."
        ),
        error_prefix="Failed to set product groups",
    )


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def set_listing_group_filters(
    customer_id: str | int,
    asset_group_id: str,
    dimension: ProductDimension,
    values: list[ProductGroupValue],
    other_excluded: bool = False,
    custom_label_index: int = 0,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Chooses which products a retail Performance Max asset group shows.

    Replaces the asset group's listing group filters with one node per value
    (included, or excluded with `excluded`) and "everything else" (included,
    or excluded with `other_excluded`). Example: only advertise brand
    'daikin' (values=[{{"value": "daikin"}}], other_excluded=True) or
    exclude custom label 0 'outlet'. Bids do not apply in PMax.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        asset_group_id: Asset group in a PMax campaign with Merchant Center.
        dimension: BRAND, CATEGORY, PRODUCT_TYPE, CUSTOM_LABEL, ITEM_ID,
            CONDITION or CHANNEL.
        values: {{"value": "daikin"}} or {{"value": "outlet", "excluded": true}}.
        other_excluded: Exclude every product not listed.
        custom_label_index: 0-4 for CUSTOM_LABEL.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    group_id = mh.require_simple_id(asset_group_id, "asset_group_id")
    level = 1  # top-level categories and product types
    dim, groups = _normalize(dimension, level, custom_label_index, values)
    if any(g["cpc_bid_micros"] for g in groups):
        raise ToolError("Error: Bids do not apply to Performance Max.")
    if all(g["excluded"] for g in groups) and other_excluded:
        raise ToolError("Error: That would exclude every product.")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT asset_group.name, campaign.advertising_channel_type, "
        "campaign.shopping_setting.merchant_id FROM asset_group "
        f"WHERE asset_group.id = {group_id}",
    )
    if not rows:
        raise ToolError(f"Error: Asset group {group_id} was not found.")
    if (
        mh.enum_name(rows[0].campaign.advertising_channel_type)
        != "PERFORMANCE_MAX"
        or not rows[0].campaign.shopping_setting.merchant_id
    ):
        raise ToolError(
            "Error: Listing group filters need a Performance Max campaign "
            "with a Merchant Center account."
        )
    name = rows[0].asset_group.name
    existing = mh.search(
        ga_client,
        cid,
        "SELECT asset_group_listing_group_filter.resource_name, "
        "asset_group_listing_group_filter.parent_listing_group_filter, "
        "asset_group_listing_group_filter.type, "
        "asset_group_listing_group_filter.case_value.product_brand.value, "
        "asset_group_listing_group_filter.case_value.product_category.category_id, "
        "asset_group_listing_group_filter.case_value.product_type.value, "
        "asset_group_listing_group_filter.case_value.product_custom_attribute.value, "
        "asset_group_listing_group_filter.case_value.product_item_id.value, "
        "asset_group_listing_group_filter.case_value.product_condition.condition, "
        "asset_group_listing_group_filter.case_value.product_channel.channel "
        "FROM asset_group_listing_group_filter "
        f"WHERE asset_group.id = {group_id}",
    )
    parents = {
        r.asset_group_listing_group_filter.resource_name: (
            r.asset_group_listing_group_filter.parent_listing_group_filter
        )
        for r in existing
    }

    def _depth(rn: str) -> int:
        depth = 0
        while parents.get(rn):
            rn = parents[rn]
            depth += 1
            if depth > 10:
                break
        return depth

    enums = ga_client.enums
    operations: list[Any] = []
    change_items: list[pac.ChangeItem] = []
    # Children before parents: a subdivision with children cannot be removed.
    for rn in sorted(parents, key=_depth, reverse=True):
        op = ga_client.get_type("MutateOperation")
        op.asset_group_listing_group_filter_operation.remove = rn
        operations.append(op)
    for r in existing:
        node = r.asset_group_listing_group_filter
        kind = mh.enum_name(node.type_)
        if kind not in ("UNIT_INCLUDED", "UNIT_EXCLUDED"):
            continue
        key = (
            _existing_key(node.case_value)
            if node.parent_listing_group_filter
            else None
        )
        field = (
            "all products"
            if key is None
            else f"{key[0].lower()}: {key[1] or 'everything else'}"
        )
        change_items.append(
            pac.ChangeItem(
                "REMOVE",
                "Listing group filter",
                name,
                field,
                "excluded" if kind == "UNIT_EXCLUDED" else "included",
            )
        )
    group_rn = f"customers/{cid}/assetGroups/{group_id}"
    root_rn = f"customers/{cid}/assetGroupListingGroupFilters/{group_id}~-1"
    root_op = ga_client.get_type("MutateOperation")
    root = root_op.asset_group_listing_group_filter_operation.create
    root.resource_name = root_rn
    root.asset_group = group_rn
    root.type_ = enums.ListingGroupFilterTypeEnum.SUBDIVISION
    root.listing_source = enums.ListingGroupFilterListingSourceEnum.SHOPPING
    operations.append(root_op)

    def _unit(value: str | None, excluded: bool, n: int) -> None:
        op = ga_client.get_type("MutateOperation")
        unit = op.asset_group_listing_group_filter_operation.create
        unit.resource_name = (
            f"customers/{cid}/assetGroupListingGroupFilters/{group_id}~-{n}"
        )
        unit.asset_group = group_rn
        unit.parent_listing_group_filter = root_rn
        unit.listing_source = enums.ListingGroupFilterListingSourceEnum.SHOPPING
        unit.type_ = (
            enums.ListingGroupFilterTypeEnum.UNIT_EXCLUDED
            if excluded
            else enums.ListingGroupFilterTypeEnum.UNIT_INCLUDED
        )
        _fill_case_value(
            ga_client,
            unit.case_value,
            dim,
            value,
            level,
            custom_label_index,
            pmax=True,
        )
        operations.append(op)
        change_items.append(
            pac.ChangeItem(
                "CREATE",
                "Listing group filter",
                f"{name}: {_label(dim, level, custom_label_index)}",
                value or "everything else",
                "excluded" if excluded else "included",
            )
        )

    for n, group in enumerate(groups, start=2):
        _unit(group["value"], group["excluded"], n)
    _unit(None, other_excluded, len(groups) + 2)

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "set_listing_group_filters",
            "asset_group_id": group_id,
            "dimension": dim,
            "level": level,
            "custom_label_index": custom_label_index,
            "groups": groups,
            "other_excluded": other_excluded,
            "replaced": sorted(parents),
        },
        change_items=change_items,
        success_message=(
            f"Set {len(groups) + 1} listing group filters on asset group "
            f"{group_id}."
        ),
        error_prefix="Failed to set listing group filters",
    )
