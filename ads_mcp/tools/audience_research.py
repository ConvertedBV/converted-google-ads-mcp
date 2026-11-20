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

"""Audience research with Google's Audience Insights (read-only).

Registered on the ``audiences`` namespace. Results feed the targeting tools:
user interest IDs work in manage_audience_targeting and as PMax signals;
age and gender in manage_demographic_exclusions.
"""

from enum import Enum
import json
from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp.tools.audiences import audiences_mcp
import ads_mcp.utils as utils


class MarketingObjective(str, Enum):
    AWARENESS = "AWARENESS"
    CONSIDERATION = "CONSIDERATION"
    RESEARCH = "RESEARCH"


class SegmentType(str, Enum):
    AFFINITY = "AFFINITY_USER_INTEREST"
    IN_MARKET = "IN_MARKET_USER_INTEREST"
    LIFE_EVENT = "LIFE_EVENT_USER_INTEREST"


_COMPOSITION_DIMENSIONS = (
    "AFFINITY_USER_INTEREST",
    "IN_MARKET_USER_INTEREST",
    "AGE_RANGE",
    "GENDER",
    "PARENTAL_STATUS",
    "INCOME_RANGE",
    "YOUTUBE_CHANNEL",
)


def _call(fn, label: str) -> Any:
    try:
        return fn()
    except GoogleAdsException as exc:
        raise ToolError(
            f"{label} failed:\n{utils.format_google_ads_error(exc)}"
        ) from exc


def _attribute(metadata: Any, enums: Any) -> dict[str, Any]:
    """Flattens AudienceInsightsAttributeMetadata into usable fields."""
    attribute = metadata.attribute
    result: dict[str, Any] = {
        "name": metadata.display_name,
        "type": mh.enum_name(
            metadata.dimension, enums.AudienceInsightsDimensionEnum
        ),
    }
    if metadata.display_info:
        result["info"] = metadata.display_info
    interest = attribute.user_interest.user_interest_category
    if interest:
        result["user_interest_id"] = interest.rsplit("/", 1)[-1]
    if attribute.youtube_channel.channel_id:
        result["youtube_channel_id"] = attribute.youtube_channel.channel_id
    if attribute.age_range.type_:
        result["age_range"] = mh.enum_name(
            attribute.age_range.type_, enums.AgeRangeTypeEnum
        )
    if attribute.gender.type_:
        result["gender"] = mh.enum_name(
            attribute.gender.type_, enums.GenderTypeEnum
        )
    if metadata.potential_youtube_reach:
        result["youtube_reach"] = metadata.potential_youtube_reach
    return result


def _countries(country_ids: list[str] | None) -> list[str]:
    return [
        f"geoTargetConstants/{c}"
        for c in mh.require_ids(
            country_ids or ["2056"], "country_ids", max_items=10
        )
    ]


def _insights_group(client_name: str | None, cid: str) -> str:
    return " ".join((client_name or "").split()) or f"Customer {cid}"


@audiences_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def suggest_audiences(
    customer_id: str | int,
    description: str,
    country_ids: list[str] | None = None,
    objective: MarketingObjective = MarketingObjective.CONSIDERATION,
    client_name: str | None = None,
    login_customer_id: str | int | None = None,
) -> str:
    """Suggests target audiences from a plain-language description.

    Describe the ideal customer, e.g. "families in Flanders considering an
    air conditioner for their home". Returns suggested audiences with their
    interests (with user_interest_id for targeting or PMax signals), age
    ranges, gender, parental status, coverage, index and potential YouTube
    reach. Uses Google's Audience Insights.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        description: Plain-language description of the audience.
        country_ids: Country geo target IDs (default ["2056"], Belgium).
        objective: AWARENESS, CONSIDERATION (default) or RESEARCH.
        client_name: Name of the advertiser being planned for.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    text = " ".join((description or "").split())
    if len(text) < 10:
        raise ToolError("Error: Describe the audience in a full sentence.")
    objective_value = mh.enum_value(objective, MarketingObjective, "objective")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    request = ga_client.get_type("GenerateSuggestedTargetingInsightsRequest")
    request.customer_id = cid
    request.customer_insights_group = _insights_group(client_name, cid)
    audience = request.audience_description
    audience.audience_description = text
    audience.marketing_objective = getattr(
        ga_client.enums.AudienceInsightsMarketingObjectiveEnum, objective_value
    )
    for country in _countries(country_ids):
        location = ga_client.get_type("LocationInfo")
        location.geo_target_constant = country
        audience.country_locations.append(location)
    service = utils.get_googleads_service_from_client(
        ga_client, "AudienceInsightsService"
    )
    response = _call(
        lambda: service.generate_suggested_targeting_insights(request=request),
        "Audience suggestions",
    )
    suggestions = []
    for suggestion in response.suggestions:
        suggestions.append(
            {
                "coverage": round(suggestion.coverage or 0, 3),
                "index": round(suggestion.index or 0, 2),
                "youtube_reach": suggestion.potential_youtube_reach or None,
                "age_ranges": [
                    mh.enum_name(a.type_, enums.AgeRangeTypeEnum)
                    for a in suggestion.age_ranges
                ],
                "gender": (
                    mh.enum_name(suggestion.gender.type_, enums.GenderTypeEnum)
                    if suggestion.gender.type_
                    else None
                ),
                "parental_status": (
                    mh.enum_name(
                        suggestion.parental_status.type_,
                        enums.ParentalStatusTypeEnum,
                    )
                    if suggestion.parental_status.type_
                    else None
                ),
                "interests": [
                    _attribute(attribute, enums)
                    for group in suggestion.user_interests
                    for attribute in group.attributes
                ],
                "locations": [
                    _attribute(loc, enums) for loc in suggestion.locations
                ],
            }
        )
    return json.dumps(
        {"description": text, "suggestions": suggestions},
        indent=2,
        ensure_ascii=False,
    )


@audiences_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_audience_segments(
    customer_id: str | int,
    query: str,
    segment_types: list[SegmentType] | None = None,
    country_id: str = "2056",
    client_name: str | None = None,
    login_customer_id: str | int | None = None,
) -> str:
    """Finds Google audience segments by keyword.

    Searches affinity, in-market and life-event segments (e.g. "air
    conditioning", "home improvement", "moving") and returns their
    user_interest_id, usable in manage_audience_targeting
    (user_interest_ids) and as Performance Max audience signals.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        query: Search text in English or the target language.
        segment_types: AFFINITY, IN_MARKET and/or LIFE_EVENT (default all).
        country_id: Country geo target ID (default 2056, Belgium).
        client_name: Name of the advertiser being planned for.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    text = " ".join((query or "").split())
    if not text:
        raise ToolError("Error: 'query' is required.")
    types = (
        [mh.enum_value(t, SegmentType, "segment_types") for t in segment_types]
        if segment_types
        else [t.value for t in SegmentType]
    )
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    request = ga_client.get_type("ListAudienceInsightsAttributesRequest")
    request.customer_id = cid
    request.customer_insights_group = _insights_group(client_name, cid)
    request.query_text = text
    request.dimensions.extend(
        getattr(ga_client.enums.AudienceInsightsDimensionEnum, t) for t in types
    )
    location = ga_client.get_type("LocationInfo")
    location.geo_target_constant = _countries([country_id])[0]
    request.location_country_filters.append(location)
    service = utils.get_googleads_service_from_client(
        ga_client, "AudienceInsightsService"
    )
    response = _call(
        lambda: service.list_audience_insights_attributes(request=request),
        "Audience segment search",
    )
    return json.dumps(
        {
            "query": text,
            "segments": [_attribute(a, enums) for a in response.attributes],
        },
        indent=2,
        ensure_ascii=False,
    )


@audiences_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def analyze_audience_composition(
    customer_id: str | int,
    user_interest_ids: list[str],
    country_ids: list[str] | None = None,
    client_name: str | None = None,
    login_customer_id: str | int | None = None,
) -> str:
    """Describes who is in an audience: other interests, demographics, and
    the YouTube channels they watch.

    Pass the user_interest_ids that define the audience (from
    find_audience_segments or suggest_audiences). Each attribute comes with
    its share of the audience and an index versus the country baseline
    (index > 1 means over-represented), which helps choose extra signals,
    exclusions, ad texts and visuals.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        user_interest_ids: 1-10 user interest IDs defining the audience.
        country_ids: Country geo target IDs (default ["2056"], Belgium).
        client_name: Name of the advertiser being planned for.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    interests = mh.require_ids(
        user_interest_ids, "user_interest_ids", max_items=10
    )
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    request = ga_client.get_type("GenerateAudienceCompositionInsightsRequest")
    request.customer_id = cid
    request.customer_insights_group = _insights_group(client_name, cid)
    for country in _countries(country_ids):
        location = ga_client.get_type("LocationInfo")
        location.geo_target_constant = country
        request.audience.country_locations.append(location)
    group = ga_client.get_type("InsightsAudienceAttributeGroup")
    for interest in interests:
        attribute = ga_client.get_type("AudienceInsightsAttribute")
        attribute.user_interest.user_interest_category = (
            f"customers/{cid}/userInterests/{interest}"
        )
        group.attributes.append(attribute)
    request.audience.topic_audience_combinations.append(group)
    request.dimensions.extend(
        getattr(ga_client.enums.AudienceInsightsDimensionEnum, d)
        for d in _COMPOSITION_DIMENSIONS
    )
    service = utils.get_googleads_service_from_client(
        ga_client, "AudienceInsightsService"
    )
    response = _call(
        lambda: service.generate_audience_composition_insights(request=request),
        "Audience composition",
    )
    sections = {}
    for section in response.sections:
        sections[
            mh.enum_name(section.dimension, enums.AudienceInsightsDimensionEnum)
        ] = [
            {
                **_attribute(item.attribute_metadata, enums),
                "audience_share": round(item.metrics.audience_share or 0, 3),
                "index": round(item.metrics.index or 0, 2),
            }
            for item in section.top_attributes[:15]
        ]
    return json.dumps(
        {"user_interest_ids": interests, "sections": sections},
        indent=2,
        ensure_ascii=False,
    )
