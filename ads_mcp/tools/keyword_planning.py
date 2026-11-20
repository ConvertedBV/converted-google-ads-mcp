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

"""Keyword Planner tools: keyword ideas, historical metrics and forecasts.

Read-only: nothing in the account changes, so these tools stay available in
read-only mode. Keyword Planner needs a developer token with at least Basic
access.
"""

import datetime
from enum import Enum
import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

from ads_mcp import ad_builders as ab
from ads_mcp import mutate_helpers as mh
import ads_mcp.utils as utils

keyword_planning_mcp = FastMCP("keyword_planning")

MAX_SEED_KEYWORDS = 20
MAX_METRIC_KEYWORDS = 500
MAX_FORECAST_KEYWORDS = 500
MAX_IDEAS = 500


class KeywordNetwork(str, Enum):
    GOOGLE_SEARCH = "GOOGLE_SEARCH"
    GOOGLE_SEARCH_AND_PARTNERS = "GOOGLE_SEARCH_AND_PARTNERS"


class ForecastBidding(str, Enum):
    """Bidding strategies supported by the keyword forecast."""

    MANUAL_CPC = "MANUAL_CPC"
    MAXIMIZE_CLICKS = "MAXIMIZE_CLICKS"
    MAXIMIZE_CONVERSIONS = "MAXIMIZE_CONVERSIONS"


def _money(micros: int | None) -> float | None:
    return round(micros / 1_000_000, 2) if micros else None


def _currency(ga_client: Any, cid: str) -> str:
    rows = mh.search(
        ga_client, cid, "SELECT customer.currency_code FROM customer"
    )
    return rows[0].customer.currency_code if rows else ""


def _targeting(
    location_ids: list[str],
    language_id: str | None,
) -> tuple[list[str], str | None]:
    locations = mh.require_ids(location_ids, "location_ids", max_items=50)
    language = (
        mh.require_simple_id(language_id, "language_id")
        if language_id
        else None
    )
    return (
        [f"geoTargetConstants/{loc}" for loc in locations],
        f"languageConstants/{language}" if language else None,
    )


def _metrics(metrics: Any, include_monthly: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "avg_monthly_searches": metrics.avg_monthly_searches or 0,
        "competition": metrics.competition.name,
        "competition_index": metrics.competition_index or 0,
        "top_of_page_bid_low": _money(metrics.low_top_of_page_bid_micros),
        "top_of_page_bid_high": _money(metrics.high_top_of_page_bid_micros),
    }
    if include_monthly:
        result["monthly_searches"] = [
            {
                "month": f"{m.year}-{m.month.value - 1:02d}",
                "searches": m.monthly_searches,
            }
            for m in metrics.monthly_search_volumes
        ]
    return result


def _call(fn, label: str) -> Any:
    try:
        return fn()
    except GoogleAdsException as exc:
        raise ToolError(
            f"{label} failed:\n{utils.format_google_ads_error(exc)}"
        ) from exc


@keyword_planning_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def generate_keyword_ideas(
    customer_id: str | int,
    location_ids: list[str],
    seed_keywords: list[str] | None = None,
    url: str | None = None,
    site: str | None = None,
    language_id: str | None = None,
    network: KeywordNetwork = KeywordNetwork.GOOGLE_SEARCH,
    max_results: int = 50,
    include_monthly_searches: bool = False,
    login_customer_id: str | int | None = None,
) -> str:
    """Generates keyword ideas with search volume, competition and bids.

    Use this to research keywords before building or expanding a campaign.
    Seed with keywords, a landing page URL, both, or a whole site (domain);
    results are sorted by average monthly searches. Location and language IDs
    are geo target / language constants (e.g. 2056 = Belgium, 1010 = Dutch,
    1002 = French). Top-of-page bids are in the account currency.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        location_ids: Geo target constant IDs (1-50).
        seed_keywords: Up to 20 seed keywords.
        url: A landing page URL to seed from.
        site: A domain to seed from (e.g. 'example.be'); not combinable with
            seed_keywords or url.
        language_id: Optional language constant ID; empty = all languages.
        network: GOOGLE_SEARCH (default) or GOOGLE_SEARCH_AND_PARTNERS.
        max_results: Number of ideas to return (1-500, default 50).
        include_monthly_searches: Add the last 12 months of volumes per idea.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    geos, language = _targeting(location_ids, language_id)
    seeds = (
        mh.clean_texts(seed_keywords, "seed_keywords", MAX_SEED_KEYWORDS)
        if seed_keywords
        else []
    )
    page_url = mh.require_url(url, "url") if url else None
    domain = (site or "").strip() or None
    if domain and (seeds or page_url):
        raise ToolError("Error: 'site' cannot be combined with other seeds.")
    if not (seeds or page_url or domain):
        raise ToolError(
            "Error: Pass 'seed_keywords', 'url' and/or 'site' as a seed."
        )
    if not 1 <= int(max_results) <= MAX_IDEAS:
        raise ToolError(f"Error: 'max_results' must be 1-{MAX_IDEAS}.")
    network_value = mh.enum_value(network, KeywordNetwork, "network")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    request = ga_client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = cid
    request.geo_target_constants.extend(geos)
    if language:
        request.language = language
    request.keyword_plan_network = getattr(
        ga_client.enums.KeywordPlanNetworkEnum, network_value
    )
    request.include_adult_keywords = False
    request.page_size = int(max_results)
    if seeds and page_url:
        request.keyword_and_url_seed.keywords.extend(seeds)
        request.keyword_and_url_seed.url = page_url
    elif seeds:
        request.keyword_seed.keywords.extend(seeds)
    elif page_url:
        request.url_seed.url = page_url
    else:
        request.site_seed.site = domain

    service = utils.get_googleads_service_from_client(
        ga_client, "KeywordPlanIdeaService"
    )

    def _fetch() -> list[Any]:
        ideas = []
        for idea in service.generate_keyword_ideas(request=request):
            ideas.append(idea)
            if len(ideas) >= int(max_results):
                break
        return ideas

    ideas = _call(_fetch, "Keyword idea generation")
    results = [
        {
            "keyword": idea.text,
            **_metrics(idea.keyword_idea_metrics, include_monthly_searches),
        }
        for idea in ideas
    ]
    results.sort(key=lambda r: r["avg_monthly_searches"], reverse=True)
    return json.dumps(
        {
            "currency": _currency(ga_client, cid),
            "locations": location_ids,
            "language": language_id,
            "network": network_value,
            "ideas": results,
        },
        indent=2,
        ensure_ascii=False,
    )


@keyword_planning_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_keyword_metrics(
    customer_id: str | int,
    keywords: list[str],
    location_ids: list[str],
    language_id: str | None = None,
    network: KeywordNetwork = KeywordNetwork.GOOGLE_SEARCH,
    include_monthly_searches: bool = False,
    login_customer_id: str | int | None = None,
) -> str:
    """Returns historical search volume, competition and bids for keywords.

    Use this to validate a keyword list (e.g. a proposed ad group) before
    building. Google groups close variants; `close_variants` lists the
    variants whose volume is included.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        keywords: Up to 500 keywords.
        location_ids: Geo target constant IDs (1-50).
        language_id: Optional language constant ID; empty = all languages.
        network: GOOGLE_SEARCH (default) or GOOGLE_SEARCH_AND_PARTNERS.
        include_monthly_searches: Add the last 12 months of volumes.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    geos, language = _targeting(location_ids, language_id)
    texts = mh.clean_texts(keywords, "keywords", MAX_METRIC_KEYWORDS)
    network_value = mh.enum_value(network, KeywordNetwork, "network")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    request = ga_client.get_type("GenerateKeywordHistoricalMetricsRequest")
    request.customer_id = cid
    request.keywords.extend(texts)
    request.geo_target_constants.extend(geos)
    if language:
        request.language = language
    request.keyword_plan_network = getattr(
        ga_client.enums.KeywordPlanNetworkEnum, network_value
    )
    service = utils.get_googleads_service_from_client(
        ga_client, "KeywordPlanIdeaService"
    )
    response = _call(
        lambda: service.generate_keyword_historical_metrics(request=request),
        "Keyword metrics",
    )
    results = [
        {
            "keyword": result.text,
            "close_variants": list(result.close_variants),
            **_metrics(result.keyword_metrics, include_monthly_searches),
        }
        for result in response.results
    ]
    returned = {r["keyword"].casefold() for r in results}
    returned |= {v.casefold() for r in results for v in r["close_variants"]}
    return json.dumps(
        {
            "currency": _currency(ga_client, cid),
            "keywords": results,
            "no_data": [t for t in texts if t.casefold() not in returned],
        },
        indent=2,
        ensure_ascii=False,
    )


@keyword_planning_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def forecast_keywords(
    customer_id: str | int,
    keywords: list[str],
    location_ids: list[str],
    daily_budget: float,
    bidding_strategy: ForecastBidding = ForecastBidding.MAXIMIZE_CLICKS,
    match_type: ab.KeywordMatchType = ab.KeywordMatchType.PHRASE,
    max_cpc: float | None = None,
    language_ids: list[str] | None = None,
    days: int = 30,
    login_customer_id: str | int | None = None,
) -> str:
    """Forecasts clicks, cost, conversions and CPA for a set of keywords.

    Use this to size a budget before building a campaign: compare forecasts
    for different budgets or bids. The forecast starts tomorrow and covers
    `days` days.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        keywords: Up to 500 keywords (one match type for all).
        location_ids: Geo target constant IDs (1-50).
        daily_budget: Daily budget in the account currency.
        bidding_strategy: MAXIMIZE_CLICKS (default), MAXIMIZE_CONVERSIONS or
            MANUAL_CPC (then `max_cpc` is required).
        match_type: EXACT, PHRASE (default) or BROAD.
        max_cpc: Max CPC (MANUAL_CPC) or CPC ceiling (MAXIMIZE_CLICKS).
        language_ids: Optional language constant IDs.
        days: Forecast length in days (7-365, default 30).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    locations = mh.require_ids(location_ids, "location_ids", max_items=50)
    languages = (
        mh.require_ids(language_ids, "language_ids") if language_ids else []
    )
    texts = mh.clean_texts(keywords, "keywords", MAX_FORECAST_KEYWORDS)
    budget_micros = mh.to_micros(daily_budget, "daily_budget")
    strategy = mh.enum_value(
        bidding_strategy, ForecastBidding, "bidding_strategy"
    )
    match = mh.enum_value(match_type, ab.KeywordMatchType, "match_type")
    cpc_micros = mh.to_micros(max_cpc, "max_cpc") if max_cpc else None
    if strategy == "MANUAL_CPC" and not cpc_micros:
        raise ToolError("Error: MANUAL_CPC forecasts need 'max_cpc'.")
    if strategy == "MAXIMIZE_CONVERSIONS" and cpc_micros:
        raise ToolError(
            "Error: 'max_cpc' does not apply to MAXIMIZE_CONVERSIONS."
        )
    if not 7 <= int(days) <= 365:
        raise ToolError("Error: 'days' must be between 7 and 365.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency = _currency(ga_client, cid)
    request = ga_client.get_type("GenerateKeywordForecastMetricsRequest")
    request.customer_id = cid
    if currency:
        request.currency_code = currency
    start = datetime.date.today() + datetime.timedelta(days=1)
    end = start + datetime.timedelta(days=int(days) - 1)
    request.forecast_period.start_date = start.isoformat()
    request.forecast_period.end_date = end.isoformat()

    campaign = request.campaign
    campaign.geo_target_constants.extend(
        f"geoTargetConstants/{loc}" for loc in locations
    )
    campaign.language_constants.extend(
        f"languageConstants/{lang}" for lang in languages
    )
    bidding = campaign.bidding_strategy
    if strategy == "MANUAL_CPC":
        bidding.manual_cpc_bidding_strategy.max_cpc_bid_micros = cpc_micros
        bidding.manual_cpc_bidding_strategy.daily_budget_micros = budget_micros
    elif strategy == "MAXIMIZE_CLICKS":
        clicks = bidding.maximize_clicks_bidding_strategy
        clicks.daily_target_spend_micros = budget_micros
        if cpc_micros:
            clicks.max_cpc_bid_ceiling_micros = cpc_micros
    else:
        bidding.maximize_conversions_bidding_strategy.daily_target_spend_micros = (
            budget_micros
        )
    ad_group = ga_client.get_type("ForecastAdGroup")
    match_enum = getattr(ga_client.enums.KeywordMatchTypeEnum, match)
    for text in texts:
        keyword = ga_client.get_type("KeywordInfo")
        keyword.text = text
        keyword.match_type = match_enum
        ad_group.keywords.append(keyword)
    campaign.ad_groups.append(ad_group)

    service = utils.get_googleads_service_from_client(
        ga_client, "KeywordPlanIdeaService"
    )
    response = _call(
        lambda: service.generate_keyword_forecast_metrics(request=request),
        "Keyword forecast",
    )
    metrics = response.campaign_forecast_metrics
    total_days = int(days)
    clicks = round(metrics.clicks or 0, 1)
    conversions = round(metrics.conversions or 0, 1)
    cost = _money(metrics.cost_micros) or 0
    return json.dumps(
        {
            "currency": currency,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "input": {
                "keywords": len(texts),
                "match_type": match,
                "bidding_strategy": strategy,
                "daily_budget": _money(budget_micros),
                "max_cpc": _money(cpc_micros),
            },
            "total": {
                "clicks": clicks,
                "cost": cost,
                "average_cpc": _money(metrics.average_cpc_micros),
                "conversions": conversions,
                "average_cpa": _money(metrics.average_cpa_micros),
            },
            "per_day": {
                "clicks": round(clicks / total_days, 1),
                "cost": round(cost / total_days, 2),
                "conversions": round(conversions / total_days, 2),
            },
            "budget_used_pct": (
                round(cost / (_money(budget_micros) * total_days) * 100)
                if budget_micros
                else None
            ),
        },
        indent=2,
    )
