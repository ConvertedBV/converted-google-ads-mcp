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

"""Builders shared by the campaign management and campaign builder tools.

Input models, validation against Google Ads limits, and functions that fill
proto messages (bidding, responsive search ads, keywords, ad schedules).
"""

from enum import Enum
import re
from typing import Any

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from ads_mcp import mutate_helpers as mh

# Google Ads limits for responsive search ads.
RSA_MIN_HEADLINES = 3
RSA_MAX_HEADLINES = 15
RSA_MIN_DESCRIPTIONS = 2
RSA_MAX_DESCRIPTIONS = 4
RSA_HEADLINE_MAX_CHARS = 30
RSA_DESCRIPTION_MAX_CHARS = 90
RSA_PATH_MAX_CHARS = 15

_CRITERION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_~=\-]+$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
# Dynamic insertions such as {KeyWord:Airco}, {LOCATION(City):Gent} or
# {CUSTOMIZER.price:99}. Google counts only the default text after ':'.
_DYNAMIC_RE = re.compile(r"\{([^{}:]+)(?::([^{}]*))?\}")


def display_length(text: str) -> int:
    """Length as Google Ads counts it for ad text limits.

    Dynamic insertions count as their default text; insertions without a
    default (e.g. countdowns) are left for the API dry run to judge.
    """
    return len(_DYNAMIC_RE.sub(lambda m: m.group(2) or "", text))


class KeywordMatchType(str, Enum):
    """Keyword match types."""

    EXACT = "EXACT"
    PHRASE = "PHRASE"
    BROAD = "BROAD"


class BiddingStrategy(str, Enum):
    """Standard (campaign-level) bidding strategies for Search campaigns."""

    MAXIMIZE_CONVERSIONS = "MAXIMIZE_CONVERSIONS"
    MAXIMIZE_CONVERSION_VALUE = "MAXIMIZE_CONVERSION_VALUE"
    MAXIMIZE_CLICKS = "MAXIMIZE_CLICKS"
    MANUAL_CPC = "MANUAL_CPC"


class PinnedText(BaseModel):
    """An ad text that is pinned to a fixed position."""

    text: str = Field(description="The ad text.")
    pin: int | None = Field(
        default=None,
        description=(
            "Optional position to pin to: 1-3 for headlines, 1-2 for "
            "descriptions. Pin sparingly; it lowers ad strength."
        ),
    )


class ResponsiveSearchAdInput(BaseModel):
    """Content of a responsive search ad."""

    headlines: list[str | PinnedText] = Field(
        description="3-15 headlines of at most 30 characters each."
    )
    descriptions: list[str | PinnedText] = Field(
        description="2-4 descriptions of at most 90 characters each."
    )
    final_url: str = Field(description="Landing page URL (http or https).")
    path1: str | None = Field(
        default=None, description="Optional display path 1 (max 15 chars)."
    )
    path2: str | None = Field(
        default=None,
        description="Optional display path 2 (max 15 chars, needs path1).",
    )


class AdScheduleInput(BaseModel):
    """A time window in which ads may serve."""

    day: str = Field(
        description=(
            "MONDAY ... SUNDAY, or MONDAY_TO_FRIDAY, WEEKEND or EVERY_DAY."
        )
    )
    start_time: str = Field(
        default="00:00",
        description="HH:MM in the account time zone; minutes 00/15/30/45.",
    )
    end_time: str = Field(
        default="24:00",
        description="HH:MM, later than start_time; 24:00 is end of day.",
    )
    bid_modifier: float | None = Field(
        default=None,
        description="Optional bid modifier, e.g. 1.2 for +20% (0.1-10).",
    )


class AdGroupInput(BaseModel):
    """A Search ad group with its keywords and ads."""

    name: str = Field(description="Ad group name.")
    keywords: list[str] = Field(
        description="Keyword texts without match-type symbols (1-200)."
    )
    match_type: KeywordMatchType = Field(
        default=KeywordMatchType.PHRASE,
        description="Match type for all keywords of this ad group.",
    )
    cpc_bid: float | None = Field(
        default=None,
        description=(
            "Default max CPC in the account currency. Required for "
            "MANUAL_CPC campaigns, ignored by automated bidding."
        ),
    )
    negative_keywords: list[str] = Field(
        default_factory=list,
        description="Optional ad-group-level negative keywords (EXACT).",
    )
    ads: list[ResponsiveSearchAdInput] = Field(
        description="1-3 responsive search ads."
    )


# ---------------------------------------------------------------------------
# Responsive search ads
# ---------------------------------------------------------------------------


def normalize_plain_texts(
    values: list[str] | None,
    kind: str,
    min_items: int,
    max_items: int,
    max_chars: int,
) -> list[str]:
    """Validates unpinned ad texts (PMax, Demand Gen) like RSA texts."""
    return [
        item["text"]
        for item in _normalize_texts(
            values or [], kind, min_items, max_items, max_chars, 0
        )
    ]


def _normalize_texts(
    values: list[Any],
    kind: str,
    min_items: int,
    max_items: int,
    max_chars: int,
    max_pin: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values or []:
        if isinstance(value, PinnedText):
            text, pin = value.text, value.pin
        elif isinstance(value, dict):
            text, pin = str(value.get("text", "")), value.get("pin")
        else:
            text, pin = str(value), None
        text = " ".join(text.split())
        if not text:
            continue
        if text.lower() in seen:
            raise ToolError(f"Error: Duplicate {kind} '{text}'.")
        seen.add(text.lower())
        length = display_length(text)
        if length > max_chars:
            raise ToolError(
                f"Error: {kind.capitalize()} '{text}' has {length} "
                f"characters; the maximum is {max_chars}."
            )
        if pin is not None and not 1 <= int(pin) <= max_pin:
            raise ToolError(
                f"Error: {kind.capitalize()} pin must be between 1 and "
                f"{max_pin}, got {pin}."
            )
        items.append({"text": text, "pin": int(pin) if pin else None})
    if not min_items <= len(items) <= max_items:
        raise ToolError(
            f"Error: This ad needs {min_items}-{max_items} "
            f"{kind}s; got {len(items)}."
        )
    return items


def normalize_rsa(ad: ResponsiveSearchAdInput | dict) -> dict[str, Any]:
    """Validates RSA content and returns a plain, checksum-friendly dict."""
    if isinstance(ad, dict):
        ad = ResponsiveSearchAdInput(**ad)
    path1 = (ad.path1 or "").strip() or None
    path2 = (ad.path2 or "").strip() or None
    for label, path in (("path1", path1), ("path2", path2)):
        if path and len(path) > RSA_PATH_MAX_CHARS:
            raise ToolError(
                f"Error: '{label}' may have at most {RSA_PATH_MAX_CHARS} "
                "characters."
            )
    if path2 and not path1:
        raise ToolError("Error: 'path2' requires 'path1'.")
    return {
        "headlines": _normalize_texts(
            ad.headlines,
            "headline",
            RSA_MIN_HEADLINES,
            RSA_MAX_HEADLINES,
            RSA_HEADLINE_MAX_CHARS,
            3,
        ),
        "descriptions": _normalize_texts(
            ad.descriptions,
            "description",
            RSA_MIN_DESCRIPTIONS,
            RSA_MAX_DESCRIPTIONS,
            RSA_DESCRIPTION_MAX_CHARS,
            2,
        ),
        "final_url": mh.require_url(ad.final_url, "final_url"),
        "path1": path1,
        "path2": path2,
    }


def _text_asset(ga_client: Any, item: dict[str, Any], prefix: str) -> Any:
    asset = ga_client.get_type("AdTextAsset")
    asset.text = item["text"]
    if item["pin"]:
        asset.pinned_field = getattr(
            ga_client.enums.ServedAssetFieldTypeEnum, f"{prefix}_{item['pin']}"
        )
    return asset


def fill_rsa(ga_client: Any, ad: Any, rsa: dict[str, Any]) -> None:
    """Fills an Ad message with normalized responsive search ad content."""
    ad.final_urls.append(rsa["final_url"])
    for item in rsa["headlines"]:
        ad.responsive_search_ad.headlines.append(
            _text_asset(ga_client, item, "HEADLINE")
        )
    for item in rsa["descriptions"]:
        ad.responsive_search_ad.descriptions.append(
            _text_asset(ga_client, item, "DESCRIPTION")
        )
    if rsa["path1"]:
        ad.responsive_search_ad.path1 = rsa["path1"]
    if rsa["path2"]:
        ad.responsive_search_ad.path2 = rsa["path2"]


def describe_texts(items: list[dict[str, Any]]) -> str:
    return " | ".join(
        f"{i['text']} [pin {i['pin']}]" if i["pin"] else i["text"]
        for i in items
    )


def rsa_change_fields(rsa: dict[str, Any]) -> dict[str, Any]:
    return {
        "Headlines": describe_texts(rsa["headlines"]),
        "Descriptions": describe_texts(rsa["descriptions"]),
        "Final URL": rsa["final_url"],
        "Display path": "/".join(p for p in (rsa["path1"], rsa["path2"]) if p)
        or None,
    }


# ---------------------------------------------------------------------------
# Bidding
# ---------------------------------------------------------------------------


def resolve_bidding(
    strategy: Any,
    target_cpa: float | None,
    target_roas_percent: float | None,
    max_cpc_limit: float | None,
) -> dict[str, Any]:
    """Validates a bidding choice and returns a normalized dict."""
    value = mh.enum_value(strategy, BiddingStrategy, "bidding_strategy")
    cpa = mh.to_micros(target_cpa, "target_cpa") if target_cpa else None
    roas = None
    if target_roas_percent:
        if float(target_roas_percent) <= 0:
            raise ToolError("Error: 'target_roas_percent' must be > 0.")
        roas = round(float(target_roas_percent) / 100, 4)
    ceiling = (
        mh.to_micros(max_cpc_limit, "max_cpc_limit") if max_cpc_limit else None
    )
    if cpa and value != BiddingStrategy.MAXIMIZE_CONVERSIONS.value:
        raise ToolError(
            "Error: 'target_cpa' only applies to MAXIMIZE_CONVERSIONS."
        )
    if roas and value != BiddingStrategy.MAXIMIZE_CONVERSION_VALUE.value:
        raise ToolError(
            "Error: 'target_roas_percent' only applies to "
            "MAXIMIZE_CONVERSION_VALUE."
        )
    if ceiling and value != BiddingStrategy.MAXIMIZE_CLICKS.value:
        raise ToolError(
            "Error: 'max_cpc_limit' only applies to MAXIMIZE_CLICKS."
        )
    return {
        "strategy": value,
        "target_cpa_micros": cpa,
        "target_roas": roas,
        "cpc_ceiling_micros": ceiling,
    }


def apply_bidding(
    ga_client: Any, campaign: Any, bidding: dict[str, Any]
) -> list[str]:
    """Sets a standard bidding strategy on a Campaign; returns mask paths."""
    strategy = bidding["strategy"]
    if strategy == BiddingStrategy.MAXIMIZE_CONVERSIONS.value:
        campaign.maximize_conversions = ga_client.get_type(
            "MaximizeConversions"
        )
        if bidding["target_cpa_micros"]:
            campaign.maximize_conversions.target_cpa_micros = bidding[
                "target_cpa_micros"
            ]
            return ["maximize_conversions.target_cpa_micros"]
        return ["maximize_conversions"]
    if strategy == BiddingStrategy.MAXIMIZE_CONVERSION_VALUE.value:
        campaign.maximize_conversion_value = ga_client.get_type(
            "MaximizeConversionValue"
        )
        if bidding["target_roas"]:
            campaign.maximize_conversion_value.target_roas = bidding[
                "target_roas"
            ]
            return ["maximize_conversion_value.target_roas"]
        return ["maximize_conversion_value"]
    if strategy == BiddingStrategy.MAXIMIZE_CLICKS.value:
        campaign.target_spend = ga_client.get_type("TargetSpend")
        if bidding["cpc_ceiling_micros"]:
            campaign.target_spend.cpc_bid_ceiling_micros = bidding[
                "cpc_ceiling_micros"
            ]
            return ["target_spend.cpc_bid_ceiling_micros"]
        return ["target_spend"]
    campaign.manual_cpc = ga_client.get_type("ManualCpc")
    return ["manual_cpc"]


def describe_bidding(bidding: dict[str, Any], currency: str = "") -> str:
    parts = [bidding["strategy"]]
    if bidding.get("target_cpa_micros"):
        parts.append(
            f"target CPA {mh.format_money(bidding['target_cpa_micros'], currency)}"
        )
    if bidding.get("target_roas"):
        parts.append(f"target ROAS {bidding['target_roas'] * 100:.0f}%")
    if bidding.get("cpc_ceiling_micros"):
        parts.append(
            "max CPC limit "
            f"{mh.format_money(bidding['cpc_ceiling_micros'], currency)}"
        )
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Targeting
# ---------------------------------------------------------------------------

_DAY_GROUPS = {
    "MONDAY_TO_FRIDAY": [
        "MONDAY",
        "TUESDAY",
        "WEDNESDAY",
        "THURSDAY",
        "FRIDAY",
    ],
    "WEEKEND": ["SATURDAY", "SUNDAY"],
    "EVERY_DAY": [
        "MONDAY",
        "TUESDAY",
        "WEDNESDAY",
        "THURSDAY",
        "FRIDAY",
        "SATURDAY",
        "SUNDAY",
    ],
}
_MINUTES = {0: "ZERO", 15: "FIFTEEN", 30: "THIRTY", 45: "FORTY_FIVE"}


def _parse_time(value: str, arg_name: str) -> tuple[int, int]:
    match = _TIME_RE.match((value or "").strip())
    if not match:
        raise ToolError(f"Error: '{arg_name}' must be HH:MM, got '{value}'.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if minute not in _MINUTES or not 0 <= hour <= 24 or (hour == 24 and minute):
        raise ToolError(
            f"Error: '{arg_name}' must be between 00:00 and 24:00 with "
            "minutes 00, 15, 30 or 45."
        )
    return hour, minute


def validate_bid_modifier(value: float | None, arg_name: str) -> float | None:
    if value is None:
        return None
    modifier = float(value)
    if modifier != 0 and not 0.1 <= modifier <= 10:
        raise ToolError(
            f"Error: '{arg_name}' must be 0 (exclude) or between 0.1 and 10 "
            f"(e.g. 1.2 = +20%), got {value}."
        )
    return round(modifier, 4)


def normalize_ad_schedules(
    schedules: list[AdScheduleInput | dict] | None,
) -> list[dict[str, Any]]:
    """Expands day groups and validates time windows."""
    result: list[dict[str, Any]] = []
    for raw in schedules or []:
        schedule = AdScheduleInput(**raw) if isinstance(raw, dict) else raw
        day = schedule.day.strip().upper()
        days = _DAY_GROUPS.get(day, [day])
        if any(d not in _DAY_GROUPS["EVERY_DAY"] for d in days):
            raise ToolError(f"Error: Invalid schedule day '{schedule.day}'.")
        start = _parse_time(schedule.start_time, "start_time")
        end = _parse_time(schedule.end_time, "end_time")
        if end <= start:
            raise ToolError(
                "Error: Schedule 'end_time' must be later than 'start_time'."
            )
        modifier = validate_bid_modifier(schedule.bid_modifier, "bid_modifier")
        for d in days:
            result.append(
                {"day": d, "start": start, "end": end, "bid_modifier": modifier}
            )
    return result


def fill_ad_schedule(
    ga_client: Any, criterion: Any, schedule: dict[str, Any]
) -> None:
    info = criterion.ad_schedule
    info.day_of_week = getattr(ga_client.enums.DayOfWeekEnum, schedule["day"])
    info.start_hour, start_minute = schedule["start"]
    info.end_hour, end_minute = schedule["end"]
    info.start_minute = getattr(
        ga_client.enums.MinuteOfHourEnum, _MINUTES[start_minute]
    )
    info.end_minute = getattr(
        ga_client.enums.MinuteOfHourEnum, _MINUTES[end_minute]
    )
    if schedule["bid_modifier"] is not None:
        criterion.bid_modifier = schedule["bid_modifier"]


def describe_schedule(schedule: dict[str, Any]) -> str:
    (sh, sm), (eh, em) = schedule["start"], schedule["end"]
    text = f"{schedule['day']} {sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
    if schedule["bid_modifier"] is not None:
        text += f" (bid x{schedule['bid_modifier']})"
    return text


def lookup_constants(
    ga_client: Any,
    customer_id: str,
    ids: list[str],
    kind: str,
) -> dict[str, str]:
    """Resolves geo target or language constant IDs to readable names.

    Raises when an ID does not exist, so typos fail before any mutation.
    """
    if not ids:
        return {}
    if kind == "geo":
        resource, prefix, name_field = (
            "geo_target_constant",
            "geoTargetConstants",
            "canonical_name",
        )
    else:
        resource, prefix, name_field = (
            "language_constant",
            "languageConstants",
            "name",
        )
    names = [f"{prefix}/{i}" for i in ids]
    rows = mh.search(
        ga_client,
        customer_id,
        f"SELECT {resource}.resource_name, {resource}.{name_field} "
        f"FROM {resource} WHERE {resource}.resource_name IN "
        f"({mh.quoted_list(names)})",
    )
    found = {}
    for row in rows:
        message = getattr(row, resource)
        found[message.resource_name.rsplit("/", 1)[-1]] = getattr(
            message, name_field
        )
    missing = [i for i in ids if i not in found]
    if missing:
        label = "location" if kind == "geo" else "language"
        raise ToolError(
            f"Error: Unknown {label} IDs: {', '.join(missing)}. Look them up "
            f"with the search tool on the {resource} resource."
        )
    return found


def require_token(value: str, arg_name: str) -> str:
    """Validates an opaque resource ID token (e.g. a recommendation ID)."""
    cleaned = str(value).strip().rsplit("/", 1)[-1]
    if not _CRITERION_TOKEN_RE.match(cleaned):
        raise ToolError(f"Error: Invalid {arg_name} '{value}'.")
    return cleaned


def fill_keyword(
    ga_client: Any, criterion: Any, text: str, match_type: str
) -> None:
    criterion.keyword.text = text
    criterion.keyword.match_type = getattr(
        ga_client.enums.KeywordMatchTypeEnum, match_type
    )
