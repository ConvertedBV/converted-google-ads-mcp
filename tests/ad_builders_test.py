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

"""Tests for the shared ad builders and validation."""

import unittest

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from ads_mcp import ad_builders as ab

VALID_AD = {
    "headlines": ["Boiler onderhoud", "Snel ter plaatse", "Erkend vakman"],
    "descriptions": [
        "Plan vandaag je onderhoud in.",
        "Vaste prijs, geen verrassingen.",
    ],
    "final_url": "https://example.be/onderhoud",
    "path1": "boiler",
}


class TestResponsiveSearchAds(unittest.TestCase):

    def test_valid_ad_with_pin(self):
        ad = dict(VALID_AD)
        ad["headlines"] = [
            {"text": "Boiler onderhoud", "pin": 1},
            " Snel   ter plaatse ",
            "Erkend vakman",
        ]
        rsa = ab.normalize_rsa(ad)
        self.assertEqual(
            rsa["headlines"][0], {"text": "Boiler onderhoud", "pin": 1}
        )
        self.assertEqual(rsa["headlines"][1]["text"], "Snel ter plaatse")
        self.assertEqual(rsa["path1"], "boiler")
        self.assertIsNone(rsa["path2"])

    def test_limits(self):
        cases = {
            "needs 3-15 headlines": {"headlines": ["a", "b"]},
            "maximum is 30": {
                "headlines": ["x" * 31, "Snel ter plaatse", "Erkend vakman"]
            },
            "Duplicate headline": {
                "headlines": ["Erkend", "erkend", "Snel ter plaatse"]
            },
            "needs 2-4 descriptions": {"descriptions": ["Een"]},
            "requires 'path1'": {"path1": None, "path2": "x"},
            "full http": {"final_url": "example.be"},
            "pin must be between 1 and 3": {
                "headlines": [{"text": "a", "pin": 4}, "b", "c"]
            },
        }
        for message, override in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    ab.normalize_rsa({**VALID_AD, **override})

    def test_fill_rsa_sets_pins(self):
        client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="x",
            use_proto_plus=True,
            version="v26",
        )
        ad = client.get_type("Ad")
        rsa = ab.normalize_rsa(
            {**VALID_AD, "descriptions": [{"text": "Een", "pin": 2}, "Twee"]}
        )
        ab.fill_rsa(client, ad, rsa)
        self.assertEqual(list(ad.final_urls), ["https://example.be/onderhoud"])
        self.assertEqual(len(ad.responsive_search_ad.headlines), 3)
        self.assertEqual(
            ad.responsive_search_ad.descriptions[0].pinned_field.name,
            "DESCRIPTION_2",
        )


class TestBidding(unittest.TestCase):

    def setUp(self):
        self.client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="x",
            use_proto_plus=True,
            version="v26",
        )

    def test_resolve_and_apply(self):
        bidding = ab.resolve_bidding(
            "MAXIMIZE_CONVERSION_VALUE", None, 400, None
        )
        self.assertEqual(bidding["target_roas"], 4.0)
        campaign = self.client.get_type("Campaign")
        paths = ab.apply_bidding(self.client, campaign, bidding)
        self.assertEqual(paths, ["maximize_conversion_value.target_roas"])
        self.assertEqual(campaign.maximize_conversion_value.target_roas, 4.0)

    def test_strategy_without_target_sets_oneof(self):
        campaign = self.client.get_type("Campaign")
        bidding = ab.resolve_bidding("MAXIMIZE_CONVERSIONS", None, None, None)
        self.assertEqual(
            ab.apply_bidding(self.client, campaign, bidding),
            ["maximize_conversions"],
        )
        pb = type(campaign).pb(campaign)
        self.assertEqual(
            pb.WhichOneof("campaign_bidding_strategy"), "maximize_conversions"
        )

    def test_mismatched_targets(self):
        with self.assertRaisesRegex(
            ToolError, "only applies to MAXIMIZE_CONVERSIONS"
        ):
            ab.resolve_bidding("MAXIMIZE_CLICKS", 10, None, None)
        with self.assertRaisesRegex(
            ToolError, "only applies to MAXIMIZE_CONVERSION_VALUE"
        ):
            ab.resolve_bidding("MAXIMIZE_CONVERSIONS", None, 300, None)
        with self.assertRaisesRegex(
            ToolError, "only applies to MAXIMIZE_CLICKS"
        ):
            ab.resolve_bidding("MANUAL_CPC", None, None, 2)


class TestAdSchedules(unittest.TestCase):

    def test_day_groups_expand(self):
        schedules = ab.normalize_ad_schedules(
            [
                {
                    "day": "monday_to_friday",
                    "start_time": "08:00",
                    "end_time": "18:30",
                }
            ]
        )
        self.assertEqual(len(schedules), 5)
        self.assertEqual(schedules[0]["start"], (8, 0))
        self.assertEqual(schedules[4]["day"], "FRIDAY")
        self.assertEqual(
            ab.describe_schedule(schedules[0]), "MONDAY 08:00-18:30"
        )

    def test_invalid_windows(self):
        cases = {
            "minutes 00, 15, 30 or 45": {
                "day": "MONDAY",
                "start_time": "08:10",
            },
            "later than": {
                "day": "MONDAY",
                "start_time": "18:00",
                "end_time": "08:00",
            },
            "Invalid schedule day": {"day": "FUNDAY"},
            "0.1 and 10": {"day": "MONDAY", "bid_modifier": 20},
        }
        for message, schedule in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    ab.normalize_ad_schedules([schedule])


if __name__ == "__main__":
    unittest.main()
