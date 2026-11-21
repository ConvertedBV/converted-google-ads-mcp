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

"""Tests for the Audience Insights research tools."""

import json
import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import audience_research as ar
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestAudienceResearch(MutateToolTestCase):

    def _metadata(self, name, interest_id, dimension="IN_MARKET_USER_INTEREST"):
        metadata = self.real_ga_client.get_type(
            "AudienceInsightsAttributeMetadata"
        )
        metadata.display_name = name
        metadata.dimension = getattr(
            self.enums.AudienceInsightsDimensionEnum, dimension
        )
        metadata.attribute.user_interest.user_interest_category = (
            f"customers/{CID}/userInterests/{interest_id}"
        )
        metadata.potential_youtube_reach = 120000
        return metadata

    def test_suggest_audiences(self):
        response = self.real_ga_client.get_type(
            "GenerateSuggestedTargetingInsightsResponse"
        )
        suggestion = self.real_ga_client.get_type("TargetingSuggestionMetrics")
        suggestion.coverage = 0.42
        suggestion.index = 1.8
        age = self.real_ga_client.get_type("AgeRangeInfo")
        age.type_ = self.enums.AgeRangeTypeEnum.AGE_RANGE_35_44
        suggestion.age_ranges.append(age)
        group = self.real_ga_client.get_type(
            "AudienceInsightsAttributeMetadataGroup"
        )
        group.attributes.append(self._metadata("Home improvement", "80432"))
        suggestion.user_interests.append(group)
        response.suggestions.append(suggestion)
        service = self.service("AudienceInsightsService")
        service.generate_suggested_targeting_insights.return_value = response

        result = json.loads(
            ar.suggest_audiences(
                customer_id=CID,
                description="Gezinnen in Vlaanderen die een airco overwegen",
                client_name="FavorCool",
            )
        )
        first = result["suggestions"][0]
        self.assertEqual(first["coverage"], 0.42)
        self.assertEqual(first["age_ranges"], ["AGE_RANGE_35_44"])
        self.assertEqual(first["interests"][0]["user_interest_id"], "80432")
        request = self.request(
            "AudienceInsightsService", "generate_suggested_targeting_insights"
        )
        self.assertEqual(request.customer_insights_group, "FavorCool")
        description = request.audience_description
        self.assertEqual(
            description.country_locations[0].geo_target_constant,
            "geoTargetConstants/2056",
        )
        self.assertEqual(description.marketing_objective.name, "CONSIDERATION")
        with self.assertRaisesRegex(ToolError, "full sentence"):
            ar.suggest_audiences(customer_id=CID, description="airco")

    def test_find_segments(self):
        response = self.real_ga_client.get_type(
            "ListAudienceInsightsAttributesResponse"
        )
        response.attributes.append(self._metadata("Air Conditioners", "80141"))
        service = self.service("AudienceInsightsService")
        service.list_audience_insights_attributes.return_value = response
        result = json.loads(
            ar.find_audience_segments(
                customer_id=CID,
                query="air conditioning",
                segment_types=["IN_MARKET_USER_INTEREST"],
            )
        )
        self.assertEqual(result["segments"][0]["user_interest_id"], "80141")
        request = self.request(
            "AudienceInsightsService", "list_audience_insights_attributes"
        )
        self.assertEqual(
            [d.name for d in request.dimensions], ["IN_MARKET_USER_INTEREST"]
        )
        self.assertEqual(request.query_text, "air conditioning")

    def test_composition(self):
        response = self.real_ga_client.get_type(
            "GenerateAudienceCompositionInsightsResponse"
        )
        section = self.real_ga_client.get_type("AudienceCompositionSection")
        section.dimension = (
            self.enums.AudienceInsightsDimensionEnum.AFFINITY_USER_INTEREST
        )
        item = self.real_ga_client.get_type("AudienceCompositionAttribute")
        item.attribute_metadata = self._metadata(
            "Green living", "92931", "AFFINITY_USER_INTEREST"
        )
        item.metrics.audience_share = 0.31
        item.metrics.index = 2.4
        section.top_attributes.append(item)
        response.sections.append(section)
        service = self.service("AudienceInsightsService")
        service.generate_audience_composition_insights.return_value = response
        result = json.loads(
            ar.analyze_audience_composition(
                customer_id=CID, user_interest_ids=["80141"]
            )
        )
        top = result["sections"]["AFFINITY_USER_INTEREST"][0]
        self.assertEqual((top["name"], top["index"]), ("Green living", 2.4))
        request = self.request(
            "AudienceInsightsService", "generate_audience_composition_insights"
        )
        attribute = request.audience.topic_audience_combinations[0].attributes[
            0
        ]
        self.assertEqual(
            attribute.user_interest.user_interest_category,
            f"customers/{CID}/userInterests/80141",
        )
        self.assertIn("YOUTUBE_CHANNEL", [d.name for d in request.dimensions])


if __name__ == "__main__":
    unittest.main()
