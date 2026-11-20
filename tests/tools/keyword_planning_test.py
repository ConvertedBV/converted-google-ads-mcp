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

"""Tests for the Keyword Planner tools."""

import datetime
import json
import unittest

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from unittest.mock import MagicMock

from ads_mcp.tools import keyword_planning as kp
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestKeywordPlanning(MutateToolTestCase):

    def setUp(self):
        super().setUp()
        self.service_mock = self.service("KeywordPlanIdeaService")

    def _currency(self):
        return [self.row({"customer.currency_code": "EUR"})]

    def _idea(self, text, searches, competition="MEDIUM", low=None, high=None):
        idea = self.real_ga_client.get_type("GenerateKeywordIdeaResult")
        idea.text = text
        metrics = idea.keyword_idea_metrics
        metrics.avg_monthly_searches = searches
        metrics.competition = getattr(
            self.enums.KeywordPlanCompetitionLevelEnum, competition
        )
        if low:
            metrics.low_top_of_page_bid_micros = low
        if high:
            metrics.high_top_of_page_bid_micros = high
        volume = self.real_ga_client.get_type("MonthlySearchVolume")
        volume.year = 2026
        volume.month = self.enums.MonthOfYearEnum.OCTOBER
        volume.monthly_searches = searches
        metrics.monthly_search_volumes.append(volume)
        return idea

    def test_ideas_from_keywords_and_url(self):
        self.search_results(self._currency())
        self.service_mock.generate_keyword_ideas.return_value = [
            self._idea("airco plaatsen", 1300, "HIGH", 1_200_000, 4_500_000),
            self._idea("airco kopen", 2900),
            self._idea("airco prijs", 880),
        ]
        result = json.loads(
            kp.generate_keyword_ideas(
                customer_id=CID,
                location_ids=["2056"],
                language_id="1010",
                seed_keywords=["airco", "airco"],
                url="https://favorcool.be/airco",
                max_results=2,
                include_monthly_searches=True,
            )
        )
        self.assertEqual(result["currency"], "EUR")
        self.assertEqual(
            [i["keyword"] for i in result["ideas"]],
            ["airco kopen", "airco plaatsen"],
        )
        plaatsen = result["ideas"][1]
        self.assertEqual(plaatsen["top_of_page_bid_low"], 1.2)
        self.assertEqual(plaatsen["top_of_page_bid_high"], 4.5)
        self.assertEqual(plaatsen["competition"], "HIGH")
        self.assertEqual(
            plaatsen["monthly_searches"],
            [{"month": "2026-10", "searches": 1300}],
        )
        request = self.request(
            "KeywordPlanIdeaService", "generate_keyword_ideas"
        )
        self.assertEqual(list(request.keyword_and_url_seed.keywords), ["airco"])
        self.assertEqual(
            request.keyword_and_url_seed.url, "https://favorcool.be/airco"
        )
        self.assertEqual(
            list(request.geo_target_constants), ["geoTargetConstants/2056"]
        )
        self.assertEqual(request.language, "languageConstants/1010")
        self.assertEqual(request.keyword_plan_network.name, "GOOGLE_SEARCH")
        self.assertFalse(request.include_adult_keywords)

    def test_site_seed_alone(self):
        self.search_results(self._currency())
        self.service_mock.generate_keyword_ideas.return_value = []
        kp.generate_keyword_ideas(
            customer_id=CID, location_ids=["2056"], site="favorcool.be"
        )
        request = self.request(
            "KeywordPlanIdeaService", "generate_keyword_ideas"
        )
        self.assertEqual(request.site_seed.site, "favorcool.be")
        with self.assertRaisesRegex(ToolError, "cannot be combined"):
            kp.generate_keyword_ideas(
                customer_id=CID,
                location_ids=["2056"],
                site="favorcool.be",
                seed_keywords=["airco"],
            )
        with self.assertRaisesRegex(ToolError, "as a seed"):
            kp.generate_keyword_ideas(customer_id=CID, location_ids=["2056"])

    def test_metrics_report_keywords_without_data(self):
        self.search_results(self._currency())
        result_row = self.real_ga_client.get_type(
            "GenerateKeywordHistoricalMetricsResult"
        )
        result_row.text = "airco plaatsen"
        result_row.close_variants.append("airco plaatsing")
        result_row.keyword_metrics.avg_monthly_searches = 1300
        response = MagicMock()
        response.results = [result_row]
        self.service_mock.generate_keyword_historical_metrics.return_value = (
            response
        )

        result = json.loads(
            kp.get_keyword_metrics(
                customer_id=CID,
                keywords=["airco plaatsen", "airco plaatsing", "zeldzame term"],
                location_ids=["2056"],
            )
        )
        self.assertEqual(result["keywords"][0]["avg_monthly_searches"], 1300)
        self.assertEqual(result["no_data"], ["zeldzame term"])

    def test_forecast_manual_cpc(self):
        self.search_results(self._currency())
        response = self.real_ga_client.get_type(
            "GenerateKeywordForecastMetricsResponse"
        )
        metrics = response.campaign_forecast_metrics
        metrics.clicks = 300
        metrics.cost_micros = 600_000_000
        metrics.average_cpc_micros = 2_000_000
        metrics.conversions = 15
        metrics.average_cpa_micros = 40_000_000
        self.service_mock.generate_keyword_forecast_metrics.return_value = (
            response
        )

        result = json.loads(
            kp.forecast_keywords(
                customer_id=CID,
                keywords=["airco plaatsen", "airco installatie"],
                location_ids=["2056"],
                language_ids=["1010"],
                daily_budget=25,
                bidding_strategy="MANUAL_CPC",
                max_cpc=2.5,
                match_type="EXACT",
                days=30,
            )
        )
        self.assertEqual(result["total"]["cost"], 600.0)
        self.assertEqual(result["total"]["average_cpa"], 40.0)
        self.assertEqual(result["per_day"]["clicks"], 10.0)
        self.assertEqual(result["budget_used_pct"], 80)

        request = self.request(
            "KeywordPlanIdeaService", "generate_keyword_forecast_metrics"
        )
        self.assertEqual(request.currency_code, "EUR")
        manual = request.campaign.bidding_strategy.manual_cpc_bidding_strategy
        self.assertEqual(manual.max_cpc_bid_micros, 2_500_000)
        self.assertEqual(manual.daily_budget_micros, 25_000_000)
        keywords = request.campaign.ad_groups[0].keywords
        self.assertEqual(
            [(k.text, k.match_type.name) for k in keywords],
            [("airco plaatsen", "EXACT"), ("airco installatie", "EXACT")],
        )
        start = datetime.date.fromisoformat(request.forecast_period.start_date)
        end = datetime.date.fromisoformat(request.forecast_period.end_date)
        self.assertEqual((end - start).days, 29)
        self.assertEqual(
            start, datetime.date.today() + datetime.timedelta(days=1)
        )

    def test_forecast_validation(self):
        with self.assertRaisesRegex(ToolError, "need 'max_cpc'"):
            kp.forecast_keywords(
                customer_id=CID,
                keywords=["airco"],
                location_ids=["2056"],
                daily_budget=10,
                bidding_strategy="MANUAL_CPC",
            )
        with self.assertRaisesRegex(ToolError, "between 7 and 365"):
            kp.forecast_keywords(
                customer_id=CID,
                keywords=["airco"],
                location_ids=["2056"],
                daily_budget=10,
                days=3,
            )

    def test_api_error_becomes_tool_error(self):
        error = MagicMock()
        error.error_code = "DEVELOPER_TOKEN_NOT_APPROVED"
        error.message = "Basic access required"
        failure = MagicMock()
        failure.errors = [error]
        self.service_mock.generate_keyword_historical_metrics.side_effect = (
            GoogleAdsException(None, None, failure, "req-1")
        )
        with self.assertRaisesRegex(ToolError, "Keyword metrics failed"):
            kp.get_keyword_metrics(
                customer_id=CID, keywords=["airco"], location_ids=["2056"]
            )


if __name__ == "__main__":
    unittest.main()
