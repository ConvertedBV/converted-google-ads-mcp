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

"""Call, promotion and price assets (extensions), created and linked at once.

Registered on the ``campaign_management`` namespace. Sitelinks, callouts and
structured snippets are created with the assets tools and linked with
link_assets.
"""

from enum import Enum
import re
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ads_mcp import ad_builders as ab
from ads_mcp import image_assets as ia
from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_management import campaign_management_mcp
import ads_mcp.utils as utils

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2}$")


class ExtensionType(str, Enum):
    CALL = "CALL"
    PROMOTION = "PROMOTION"
    PRICE = "PRICE"


class LinkLevel(str, Enum):
    CUSTOMER = "CUSTOMER"
    CAMPAIGN = "CAMPAIGN"
    AD_GROUP = "AD_GROUP"
    NONE = "NONE"


class CallInput(BaseModel):
    phone_number: str = Field(
        description="Phone number as dialled, e.g. 09 123 45 67."
    )
    country_code: str = Field(default="BE", description="ISO country code.")


class PromotionInput(BaseModel):
    promotion_target: str = Field(
        description="What is on promotion, max 20 chars."
    )
    final_url: str = Field(description="Landing page of the promotion.")
    percent_off: float | None = Field(
        default=None, description="Discount percentage, e.g. 10 for 10%."
    )
    money_amount_off: float | None = Field(
        default=None, description="Discount amount in the account currency."
    )
    up_to: bool = Field(default=False, description="Show as 'up to'.")
    promotion_code: str | None = Field(
        default=None, description="Optional code."
    )
    orders_over_amount: float | None = Field(
        default=None, description="Optional minimum order amount."
    )
    start_date: str | None = Field(default=None, description="YYYY-MM-DD.")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD.")
    language_code: str = Field(default="nl", description="e.g. nl or fr.")


class PriceOfferingInput(BaseModel):
    header: str = Field(description="Service or product, max 25 chars.")
    description: str = Field(description="Short description, max 25 chars.")
    price: float = Field(description="Price in the account currency.")
    final_url: str = Field(description="Landing page for this offering.")
    unit: str | None = Field(
        default=None,
        description="Optional PER_HOUR, PER_DAY, PER_WEEK, PER_MONTH, PER_YEAR.",
    )


class PriceInput(BaseModel):
    type: str = Field(
        default="SERVICES",
        description="SERVICES, SERVICE_CATEGORIES, SERVICE_TIERS, BRANDS, "
        "PRODUCT_CATEGORIES, PRODUCT_TIERS, EVENTS, LOCATIONS or NEIGHBORHOODS.",
    )
    price_qualifier: str | None = Field(
        default=None, description="Optional FROM, UP_TO or AVERAGE."
    )
    language_code: str = Field(default="nl", description="e.g. nl or fr.")
    offerings: list[PriceOfferingInput] = Field(description="3-8 offerings.")


def _short(text: str, label: str, max_chars: int) -> str:
    return ab.normalize_plain_texts([text], label, 1, 1, max_chars)[0]


def _enum_name(enum: Any, value: str | None, label: str) -> str | None:
    if value is None:
        return None
    name = str(value).strip().upper()
    if name not in {e.name for e in enum} or name in ("UNSPECIFIED", "UNKNOWN"):
        raise ToolError(f"Error: Invalid {label} '{value}'.")
    return name


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_extension_asset(
    customer_id: str | int,
    extension_type: ExtensionType,
    link_level: LinkLevel = LinkLevel.CAMPAIGN,
    parent_id: str | None = None,
    call: CallInput | None = None,
    promotion: PromotionInput | None = None,
    price: PriceInput | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a call, promotion or price asset and links it in one step.

    Pass the matching input (`call`, `promotion` or `price`) and where to
    link it: CUSTOMER (whole account), CAMPAIGN or AD_GROUP with
    `parent_id`, or NONE to only create it (link later with link_assets).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        extension_type: CALL, PROMOTION or PRICE.
        link_level: CAMPAIGN (default), AD_GROUP, CUSTOMER or NONE.
        parent_id: Campaign or ad group ID for CAMPAIGN / AD_GROUP.
        call: {{"phone_number", "country_code"}} for CALL.
        promotion: Promotion details for PROMOTION (percent_off or
            money_amount_off).
        price: Price details with 3-8 offerings for PRICE.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    kind = mh.enum_value(extension_type, ExtensionType, "extension_type")
    level = mh.enum_value(link_level, LinkLevel, "link_level")
    parent = (
        mh.require_simple_id(parent_id, "parent_id")
        if level in ("CAMPAIGN", "AD_GROUP")
        else None
    )
    inputs = {"CALL": call, "PROMOTION": promotion, "PRICE": price}
    if inputs[kind] is None or any(
        v is not None for k, v in inputs.items() if k != kind
    ):
        raise ToolError(
            f"Error: Pass only '{kind.lower()}' for a {kind} extension."
        )

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    rows = mh.search(
        ga_client, cid, "SELECT customer.currency_code FROM customer"
    )
    currency = rows[0].customer.currency_code if rows else ""
    ids = ia.TempIds(cid)
    asset_rn = ids.next("assets")
    asset_op = ga_client.get_type("MutateOperation")
    asset = asset_op.asset_operation.create
    asset.resource_name = asset_rn
    fields: dict[str, Any] = {}

    if kind == "CALL":
        data = CallInput(**call) if isinstance(call, dict) else call
        country = data.country_code.strip().upper()
        if not _COUNTRY_RE.match(country):
            raise ToolError("Error: 'country_code' must be like BE or NL.")
        number = " ".join(data.phone_number.split())
        if not re.match(r"^\+?[\d ()\-./]{6,25}$", number):
            raise ToolError("Error: Invalid phone number.")
        asset.call_asset.phone_number = number
        asset.call_asset.country_code = country
        normalized = {"phone_number": number, "country_code": country}
        fields = {"Phone number": number, "Country": country}
    elif kind == "PROMOTION":
        data = (
            PromotionInput(**promotion)
            if isinstance(promotion, dict)
            else promotion
        )
        if (data.percent_off is None) == (data.money_amount_off is None):
            raise ToolError(
                "Error: Pass exactly one of 'percent_off' or 'money_amount_off'."
            )
        target = _short(data.promotion_target, "promotion target", 20)
        language = data.language_code.strip().lower()
        if not _LANGUAGE_RE.match(language):
            raise ToolError("Error: 'language_code' must be like nl or fr.")
        start = mh.require_date(data.start_date, "start_date")
        end = mh.require_date(data.end_date, "end_date")
        if start and end and end < start:
            raise ToolError(
                "Error: 'end_date' must not be before 'start_date'."
            )
        url = mh.require_url(data.final_url, "final_url")
        info = asset.promotion_asset
        info.promotion_target = target
        info.language_code = language
        asset.final_urls.append(url)
        if data.up_to:
            info.discount_modifier = (
                enums.PromotionExtensionDiscountModifierEnum.UP_TO
            )
        if data.percent_off is not None:
            if not 0 < float(data.percent_off) <= 100:
                raise ToolError("Error: 'percent_off' must be 0-100.")
            # The API expects micros: 1,000,000 = 1%.
            info.percent_off = int(round(float(data.percent_off) * 1_000_000))
            discount = f"{float(data.percent_off):g}%"
        else:
            info.money_amount_off.amount_micros = mh.to_micros(
                data.money_amount_off, "money_amount_off"
            )
            info.money_amount_off.currency_code = currency
            discount = mh.format_money(
                info.money_amount_off.amount_micros, currency
            )
        if data.promotion_code:
            info.promotion_code = _short(
                data.promotion_code, "promotion code", 15
            )
        if data.orders_over_amount is not None:
            if data.promotion_code:
                raise ToolError(
                    "Error: Use either 'promotion_code' or 'orders_over_amount'."
                )
            info.orders_over_amount.amount_micros = mh.to_micros(
                data.orders_over_amount, "orders_over_amount"
            )
            info.orders_over_amount.currency_code = currency
        if start:
            info.start_date = start
        if end:
            info.end_date = end
        normalized = {
            "target": target,
            "url": url,
            "percent_off": data.percent_off,
            "money_off": data.money_amount_off,
            "up_to": data.up_to,
            "code": data.promotion_code,
            "orders_over": data.orders_over_amount,
            "start": start,
            "end": end,
            "language": language,
        }
        fields = {
            "Promotion": target,
            "Discount": ("up to " if data.up_to else "") + discount,
            "Code": data.promotion_code,
            "Minimum order": data.orders_over_amount,
            "Period": (
                f"{start or '…'} - {end or '…'}" if (start or end) else None
            ),
            "Final URL": url,
        }
    else:
        data = PriceInput(**price) if isinstance(price, dict) else price
        price_type = _enum_name(enums.PriceExtensionTypeEnum, data.type, "type")
        qualifier = _enum_name(
            enums.PriceExtensionPriceQualifierEnum,
            data.price_qualifier,
            "price_qualifier",
        )
        language = data.language_code.strip().lower()
        if not _LANGUAGE_RE.match(language):
            raise ToolError("Error: 'language_code' must be like nl or fr.")
        offerings = [
            PriceOfferingInput(**o) if isinstance(o, dict) else o
            for o in data.offerings
        ]
        if not 3 <= len(offerings) <= 8:
            raise ToolError("Error: A price extension needs 3-8 offerings.")
        info = asset.price_asset
        info.type_ = getattr(enums.PriceExtensionTypeEnum, price_type)
        info.language_code = language
        if qualifier:
            info.price_qualifier = getattr(
                enums.PriceExtensionPriceQualifierEnum, qualifier
            )
        normalized_offerings = []
        for offering in offerings:
            header = _short(offering.header, "price header", 25)
            text = _short(offering.description, "price description", 25)
            unit = _enum_name(
                enums.PriceExtensionPriceUnitEnum, offering.unit, "unit"
            )
            url = mh.require_url(offering.final_url, "final_url")
            item = ga_client.get_type("PriceOffering")
            item.header = header
            item.description = text
            item.price.amount_micros = mh.to_micros(offering.price, "price")
            item.price.currency_code = currency
            item.final_url = url
            if unit:
                item.unit = getattr(enums.PriceExtensionPriceUnitEnum, unit)
            info.price_offerings.append(item)
            normalized_offerings.append(
                {
                    "header": header,
                    "description": text,
                    "price_micros": item.price.amount_micros,
                    "unit": unit,
                    "url": url,
                }
            )
        normalized = {
            "type": price_type,
            "qualifier": qualifier,
            "language": language,
            "offerings": normalized_offerings,
        }
        fields = {
            "Type": price_type,
            "Qualifier": qualifier,
            "Offerings": " | ".join(
                f"{o['header']}: {mh.format_money(o['price_micros'], currency)}"
                for o in normalized_offerings
            ),
        }

    operations = [asset_op]
    if level != "NONE":
        link_op = ga_client.get_type("MutateOperation")
        if level == "CUSTOMER":
            link = link_op.customer_asset_operation.create
            target_rn = f"customers/{cid}"
        elif level == "CAMPAIGN":
            link = link_op.campaign_asset_operation.create
            target_rn = f"customers/{cid}/campaigns/{parent}"
            link.campaign = target_rn
        else:
            link = link_op.ad_group_asset_operation.create
            target_rn = f"customers/{cid}/adGroups/{parent}"
            link.ad_group = target_rn
        link.asset = asset_rn
        link.field_type = getattr(enums.AssetFieldTypeEnum, kind)
        operations.append(link_op)
        fields["Linked to"] = target_rn
    else:
        fields["Linked to"] = "not linked"

    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable=f"{kind.title()} extension",
        primary_key_readable=kind.title(),
        fields=fields,
    )
    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_extension_asset",
            "type": kind,
            "level": level,
            "parent_id": parent,
            "data": normalized,
        },
        change_items=change_items,
        success_message=f"Created {kind.lower()} extension.",
        error_prefix=f"Failed to create {kind.lower()} extension",
    )
