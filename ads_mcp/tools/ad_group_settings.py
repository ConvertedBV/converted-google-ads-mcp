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

"""Ad group settings and ad customizers.

Registered on the ``campaign_management`` namespace.
"""

from enum import Enum
import re
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_management import campaign_management_mcp
from ads_mcp.tools.campaign_settings import clean_final_url_suffix
from ads_mcp.tools.campaign_settings import clean_tracking_template
import ads_mcp.utils as utils


class AdRotation(str, Enum):
    OPTIMIZE = "OPTIMIZE"
    ROTATE_FOREVER = "ROTATE_FOREVER"


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def update_ad_group_settings(
    customer_id: str | int,
    ad_group_id: str,
    name: str | None = None,
    target_cpa: float | None = None,
    target_roas_percent: float | None = None,
    ad_rotation: AdRotation | None = None,
    tracking_url_template: str | None = None,
    final_url_suffix: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Changes an ad group's name, own target, ad rotation or tracking.

    An ad group target CPA / ROAS overrides the campaign's target for that
    ad group (smart bidding campaigns only). A looser target than the one
    the ad group uses today is capped by the safety limit. Default bids are
    changed with update_cpc_bid, status with update_entity_status.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        ad_group_id: Ad group ID.
        name: New name.
        target_cpa: Ad group target CPA in the account currency.
        target_roas_percent: Ad group target ROAS, e.g. 400 for 400%.
        ad_rotation: OPTIMIZE (best performing ads) or ROTATE_FOREVER.
        tracking_url_template: Ad group tracking template, '' removes it.
        final_url_suffix: Ad group final URL suffix, '' removes it.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    ag_id = mh.require_simple_id(ad_group_id, "ad_group_id")
    clean_name = " ".join((name or "").split()) or None
    template = clean_tracking_template(tracking_url_template)
    suffix = clean_final_url_suffix(final_url_suffix)
    rotation = (
        mh.enum_value(ad_rotation, AdRotation, "ad_rotation")
        if ad_rotation
        else None
    )
    cpa = mh.to_micros(target_cpa, "target_cpa") if target_cpa else None
    roas = None
    if target_roas_percent is not None:
        if float(target_roas_percent) <= 0:
            raise ToolError("Error: 'target_roas_percent' must be > 0.")
        roas = round(float(target_roas_percent) / 100, 4)
    if all(
        v is None for v in (clean_name, template, suffix, rotation, cpa, roas)
    ):
        raise ToolError("Error: Pass at least one setting to change.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT ad_group.name, ad_group.target_cpa_micros, "
        "ad_group.effective_target_cpa_micros, ad_group.target_roas, "
        "ad_group.effective_target_roas, ad_group.ad_rotation_mode, "
        "ad_group.tracking_url_template, ad_group.final_url_suffix, "
        "customer.currency_code FROM ad_group "
        f"WHERE ad_group.id = {ag_id}",
    )
    if not rows:
        raise ToolError(f"Error: Ad group {ag_id} was not found.")
    ad_group = rows[0].ad_group
    currency = rows[0].customer.currency_code
    if cpa:
        baseline = (
            ad_group.target_cpa_micros or ad_group.effective_target_cpa_micros
        )
        if not baseline:
            raise ToolError(
                "Error: This ad group's campaign has no target CPA; set the "
                "bidding strategy first with update_campaign_bidding."
            )
        mh.enforce_increase_guardrail(baseline, cpa, "ad group target CPA")
    if roas:
        baseline = ad_group.target_roas or ad_group.effective_target_roas
        if not baseline:
            raise ToolError(
                "Error: This ad group's campaign has no target ROAS; set the "
                "bidding strategy first with update_campaign_bidding."
            )
        mh.enforce_decrease_guardrail(baseline, roas, "ad group target ROAS")

    op = ga_client.get_type("MutateOperation")
    update = op.ad_group_operation.update
    update.resource_name = f"customers/{cid}/adGroups/{ag_id}"
    paths = op.ad_group_operation.update_mask.paths
    fields: dict[str, Any] = {}
    original: dict[str, Any] = {}
    if clean_name:
        update.name = clean_name
        paths.append("name")
        fields["Name"], original["Name"] = clean_name, ad_group.name
    if cpa:
        update.target_cpa_micros = cpa
        paths.append("target_cpa_micros")
        fields["Target CPA"] = mh.format_money(cpa, currency)
        original["Target CPA"] = mh.format_money(
            int(
                ad_group.target_cpa_micros
                or ad_group.effective_target_cpa_micros
            ),
            currency,
        ) + ("" if ad_group.target_cpa_micros else " (from campaign)")
    if roas:
        update.target_roas = roas
        paths.append("target_roas")
        fields["Target ROAS"] = f"{roas * 100:.0f}%"
        current = ad_group.target_roas or ad_group.effective_target_roas
        original["Target ROAS"] = f"{current * 100:.0f}%" + (
            "" if ad_group.target_roas else " (from campaign)"
        )
    if rotation:
        update.ad_rotation_mode = getattr(
            ga_client.enums.AdGroupAdRotationModeEnum, rotation
        )
        paths.append("ad_rotation_mode")
        fields["Ad rotation"] = rotation
        original["Ad rotation"] = mh.enum_name(ad_group.ad_rotation_mode)
    if template is not None:
        update.tracking_url_template = template
        paths.append("tracking_url_template")
        fields["Tracking template"] = template or "(none)"
        original["Tracking template"] = (
            ad_group.tracking_url_template or "(none)"
        )
    if suffix is not None:
        update.final_url_suffix = suffix
        paths.append("final_url_suffix")
        fields["Final URL suffix"] = suffix or "(none)"
        original["Final URL suffix"] = ad_group.final_url_suffix or "(none)"

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_ad_group_settings",
            "ad_group_id": ag_id,
            "name": clean_name,
            "target_cpa_micros": cpa,
            "target_roas": roas,
            "ad_rotation": rotation,
            "tracking_url_template": template,
            "final_url_suffix": suffix,
        },
        change_items=pac.build_change_items(
            operation="UPDATE",
            entity_readable="Ad group",
            primary_key_readable=f"{ad_group.name} ({ag_id})",
            fields=fields,
            original_fields=original,
        ),
        success_message=f"Updated ad group {ag_id}.",
        error_prefix="Failed to update ad group",
    )


class CustomizerType(str, Enum):
    TEXT = "TEXT"
    NUMBER = "NUMBER"
    PRICE = "PRICE"
    PERCENT = "PERCENT"


class CustomizerLevel(str, Enum):
    CUSTOMER = "CUSTOMER"
    CAMPAIGN = "CAMPAIGN"
    AD_GROUP = "AD_GROUP"


_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_VALUE_RES = {
    "TEXT": re.compile(r"^.{1,80}$"),
    "NUMBER": re.compile(r"^-?\d+(?:[.,]\d+)?$"),
    "PERCENT": re.compile(r"^-?\d+(?:[.,]\d+)?%$"),
    "PRICE": re.compile(
        r"^(?:[^\d\s]{1,3}\s?\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?\s?[A-Z]{3})$"
    ),
}
_LEVEL_RESOURCE = {
    "CUSTOMER": ("customer_customizer", None, None),
    "CAMPAIGN": ("campaign_customizer", "campaign", "campaigns"),
    "AD_GROUP": ("ad_group_customizer", "ad_group", "adGroups"),
}


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_ad_customizer(
    customer_id: str | int,
    name: str,
    value: str,
    level: CustomizerLevel = CustomizerLevel.CUSTOMER,
    entity_ids: list[str] | None = None,
    customizer_type: CustomizerType = CustomizerType.TEXT,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Sets an ad customizer value (creating the customizer if needed).

    Responsive search ads insert it with {{CUSTOMIZER.name:default}}, e.g.
    headline "Airco vanaf {{CUSTOMIZER.price:€1.499}}". Set one value for the
    whole account (CUSTOMER) and override it per campaign or ad group;
    setting a value again replaces it. Values: TEXT (max 80 chars), NUMBER
    (e.g. 12), PRICE (e.g. €1.499 or 1499 EUR), PERCENT (e.g. 20%).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        name: Customizer name (letters, digits, underscores), e.g. price.
        value: The value to show.
        level: CUSTOMER (default), CAMPAIGN or AD_GROUP.
        entity_ids: Campaign or ad group IDs for CAMPAIGN / AD_GROUP.
        customizer_type: TEXT (default), NUMBER, PRICE or PERCENT; used when
            the customizer is new, must match an existing one.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    attr_name = (name or "").strip()
    if not _NAME_RE.match(attr_name):
        raise ToolError(
            "Error: 'name' may only contain letters, digits and underscores "
            "(max 40)."
        )
    level_value = mh.enum_value(level, CustomizerLevel, "level")
    type_value = mh.enum_value(
        customizer_type, CustomizerType, "customizer_type"
    )
    text = " ".join((value or "").split())
    ids = (
        mh.require_ids(entity_ids, "entity_ids")
        if level_value != "CUSTOMER"
        else []
    )
    if level_value != "CUSTOMER" and not ids:
        raise ToolError("Error: Pass 'entity_ids' for this level.")
    if level_value == "CUSTOMER" and entity_ids:
        raise ToolError("Error: CUSTOMER level takes no 'entity_ids'.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    attrs = mh.search(
        ga_client,
        cid,
        "SELECT customizer_attribute.resource_name, customizer_attribute.name, "
        "customizer_attribute.type FROM customizer_attribute "
        "WHERE customizer_attribute.status = 'ENABLED'",
    )
    existing = {
        r.customizer_attribute.name.lower(): r.customizer_attribute
        for r in attrs
    }
    operations: list[Any] = []
    change_items: list[pac.ChangeItem] = []
    attr = existing.get(attr_name.lower())
    if attr is not None:
        type_value = mh.enum_name(attr.type_)
        attr_rn = attr.resource_name
        attr_name = attr.name
    else:
        attr_rn = f"customers/{cid}/customizerAttributes/-1"
        op = ga_client.get_type("MutateOperation")
        create = op.customizer_attribute_operation.create
        create.resource_name = attr_rn
        create.name = attr_name
        create.type_ = getattr(enums.CustomizerAttributeTypeEnum, type_value)
        operations.append(op)
        change_items.append(
            pac.ChangeItem(
                "CREATE", "Customizer", attr_name, "Type", type_value
            )
        )
    if not _VALUE_RES[type_value].match(text):
        raise ToolError(
            f"Error: '{text}' is not a valid {type_value} value (see the "
            "examples in the description)."
        )

    resource, parent_field, parent_path = _LEVEL_RESOURCE[level_value]
    if attr is not None:
        where = f"{resource}.customizer_attribute = '{attr_rn}'"
        if parent_field:
            targets = [f"customers/{cid}/{parent_path}/{i}" for i in ids]
            where += (
                f" AND {resource}.{parent_field} IN ({mh.quoted_list(targets)})"
            )
        current = mh.search(
            ga_client,
            cid,
            f"SELECT {resource}.resource_name, {resource}.value.string_value"
            + (f", {resource}.{parent_field}" if parent_field else "")
            + f" FROM {resource} WHERE {where} "
            f"AND {resource}.status = 'ENABLED'",
        )
    else:
        current = []
    old_values = {}
    for row in current:
        entry = getattr(row, resource)
        key = getattr(entry, parent_field) if parent_field else "customer"
        old_values[key] = entry.value.string_value
        op = ga_client.get_type("MutateOperation")
        getattr(op, f"{resource}_operation").remove = entry.resource_name
        operations.append(op)

    targets = (
        [f"customers/{cid}/{parent_path}/{i}" for i in ids]
        if parent_field
        else ["customer"]
    )
    for target in targets:
        op = ga_client.get_type("MutateOperation")
        create = getattr(op, f"{resource}_operation").create
        if parent_field:
            setattr(create, parent_field, target)
        create.customizer_attribute = attr_rn
        create.value.type_ = getattr(
            enums.CustomizerAttributeTypeEnum, type_value
        )
        create.value.string_value = text
        operations.append(op)
        change_items.extend(
            pac.build_change_items(
                operation="UPDATE" if target in old_values else "CREATE",
                entity_readable=f"Customizer value ({level_value.lower()})",
                primary_key_readable=(
                    f"{attr_name} @ {target if parent_field else cid}"
                ),
                fields={"Value": text},
                original_fields=(
                    {"Value": old_values[target]}
                    if target in old_values
                    else None
                ),
            )
        )

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_ad_customizer",
            "name": attr_name,
            "type": type_value,
            "value": text,
            "level": level_value,
            "entity_ids": ids,
        },
        change_items=change_items,
        success_message=(
            f"Set customizer '{attr_name}' to '{text}' on {len(targets)} "
            f"{level_value.lower()}(s). Use {{CUSTOMIZER.{attr_name}:default}} "
            "in responsive search ads."
        ),
        error_prefix="Failed to set ad customizer",
    )
