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

"""Tests for Display, Shopping and Demand Gen video builders."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import asset_campaigns as ac
from ads_mcp.tools import more_campaigns as mc
from tests.tools.asset_campaigns_test import AssetCampaignTestCase
from tests.tools.mutate_tool_base import CID


def display_ad(**extra):
    ad = {
        "final_url": "https://favorcool.be/airco",
        "headlines": ["Airco laten plaatsen"],
        "long_headline": "Airco door eigen installateurs, klaar vóór de zomer",
        "descriptions": ["Vaste prijs, snelle plaatsing."],
        "business_name": "FavorCool",
        "marketing_images": ["https://favorcool.be/hero.png"],
        "square_marketing_images": ["https://favorcool.be/square.png"],
        "logo_images": ["https://favorcool.be/logo.png"],
    }
    ad.update(extra)
    return ad


class TestDisplay(AssetCampaignTestCase):

    def _kwargs(self, **extra):
        kwargs = dict(
            customer_id=CID,
            name="D | BE - Airco",
            daily_budget=20,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
            location_ids=["2056"],
            language_ids=["1010"],
            ad_groups=[
                {
                    "name": "In-market airco",
                    "user_interest_ids": ["80141"],
                    "user_list_ids": ["55"],
                    "placements": ["https://www.bouwenwonen.be"],
                    "ads": [display_ad()],
                }
            ],
        )
        kwargs.update(extra)
        return kwargs

    def test_build(self):
        self.search_results(*self.lookups())
        self.propose(mc.create_display_campaign, **self._kwargs())
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        kinds = self.kinds(ops)
        campaign = ops[1].campaign_operation.create
        self.assertEqual(campaign.advertising_channel_type.name, "DISPLAY")
        self.assertEqual(campaign.status.name, "PAUSED")
        group = next(
            o.ad_group_operation.create
            for o in ops
            if self.kinds([o])[0] == "ad_group_operation"
        )
        self.assertEqual(group.type_.name, "DISPLAY_STANDARD")
        criteria = [
            o.ad_group_criterion_operation.create
            for o in ops
            if self.kinds([o])[0] == "ad_group_criterion_operation"
        ]
        self.assertEqual(
            criteria[0].user_interest.user_interest_category,
            f"customers/{CID}/userInterests/80141",
        )
        self.assertEqual(
            criteria[1].user_list.user_list, f"customers/{CID}/userLists/55"
        )
        self.assertEqual(criteria[2].placement.url, "www.bouwenwonen.be")
        ad = next(
            o.ad_group_ad_operation.create.ad
            for o in ops
            if self.kinds([o])[0] == "ad_group_ad_operation"
        )
        info = ad.responsive_display_ad
        self.assertEqual(info.long_headline.text, display_ad()["long_headline"])
        self.assertEqual(len(info.marketing_images), 1)
        self.assertEqual(len(info.square_logo_images), 1)
        self.assertIn("asset_operation", kinds)

    def test_validation(self):
        cases = {
            "maximum is 90": {
                "ad_groups": [
                    {"name": "A", "ads": [display_ad(long_headline="x" * 91)]}
                ]
            },
            "'marketing_images' needs 1-20": {
                "ad_groups": [
                    {"name": "A", "ads": [display_ad(marketing_images=[])]}
                ]
            },
            "needs 'cpc_bid' for MANUAL_CPC": {
                "bidding_strategy": "MANUAL_CPC",
                "ad_groups": [{"name": "A", "ads": [display_ad()]}],
            },
            "Invalid placement": {
                "ad_groups": [
                    {
                        "name": "A",
                        "placements": ["geen site"],
                        "ads": [display_ad()],
                    }
                ]
            },
        }
        for message, override in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    mc.create_display_campaign(**self._kwargs(**override))


class TestShopping(AssetCampaignTestCase):

    def test_build(self):
        self.search_results(*self.lookups(with_language=False))
        proposal = self.propose(
            mc.create_shopping_campaign,
            customer_id=CID,
            name="Shopping - alle producten",
            daily_budget=30,
            merchant_center_id="123456789",
            feed_label="BE",
            location_ids=["2056"],
            default_cpc=0.6,
            campaign_priority=1,
        )
        self.assertIn("All products at 0.60 EUR", str(proposal))
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        campaign = ops[1].campaign_operation.create
        self.assertEqual(campaign.advertising_channel_type.name, "SHOPPING")
        self.assertEqual(campaign.shopping_setting.merchant_id, 123456789)
        self.assertEqual(campaign.shopping_setting.campaign_priority, 1)
        self.assertEqual(campaign.shopping_setting.feed_label, "BE")
        self.assertEqual(
            self.kinds(ops)[-3:],
            [
                "ad_group_operation",
                "ad_group_ad_operation",
                "ad_group_criterion_operation",
            ],
        )
        self.assertEqual(
            ops[-3].ad_group_operation.create.type_.name, "SHOPPING_PRODUCT_ADS"
        )
        listing = ops[-1].ad_group_criterion_operation.create
        self.assertEqual(listing.listing_group.type_.name, "UNIT")
        self.assertEqual(listing.cpc_bid_micros, 600_000)

    def test_only_manual_or_clicks(self):
        with self.assertRaisesRegex(ToolError, "MANUAL_CPC, MAXIMIZE_CLICKS"):
            mc.create_shopping_campaign(
                customer_id=CID,
                name="X",
                daily_budget=30,
                merchant_center_id="1",
                location_ids=["2056"],
                default_cpc=1,
                bidding_strategy="MAXIMIZE_CONVERSIONS",
            )


class TestDemandGenVideo(AssetCampaignTestCase):

    def test_video_ad(self):
        self.search_results(
            *self.lookups(with_language=False)[:1],
            *self.lookups(with_language=False)[1:],
        )
        self.propose(
            ac.create_demand_gen_campaign,
            customer_id=CID,
            name="DG - video",
            daily_budget=25,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
            ad_groups=[
                {
                    "name": "Video",
                    "location_ids": ["2056"],
                    "video_ads": [
                        {
                            "name": "Zomer video",
                            "final_url": "https://favorcool.be/airco",
                            "youtube_video_ids": ["dQw4w9WgXcQ"],
                            "headlines": ["Klaar vóór de zomer"],
                            "long_headlines": [
                                "Airco door eigen installateurs"
                            ],
                            "descriptions": ["Vaste prijs."],
                            "business_name": "FavorCool",
                            "logo_images": ["https://favorcool.be/logo.png"],
                        }
                    ],
                }
            ],
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        ad = next(
            o.ad_group_ad_operation.create.ad
            for o in ops
            if self.kinds([o])[0] == "ad_group_ad_operation"
        )
        info = ad.demand_gen_video_responsive_ad
        self.assertEqual(info.business_name.text, "FavorCool")
        self.assertEqual(
            info.long_headlines[0].text, "Airco door eigen installateurs"
        )
        self.assertEqual(len(info.videos), 1)
        video_asset = next(
            o.asset_operation.create
            for o in ops
            if self.kinds([o])[0] == "asset_operation"
            and o.asset_operation.create.youtube_video_asset.youtube_video_id
        )
        self.assertEqual(info.videos[0].asset, video_asset.resource_name)

    def test_video_id_format(self):
        with self.assertRaisesRegex(ToolError, "11 characters"):
            ac.create_demand_gen_campaign(
                customer_id=CID,
                name="DG",
                daily_budget=25,
                bidding_strategy="MAXIMIZE_CONVERSIONS",
                ad_groups=[
                    {
                        "name": "Video",
                        "location_ids": ["2056"],
                        "video_ads": [
                            {
                                "name": "V",
                                "final_url": "https://a.be",
                                "youtube_video_ids": ["kort"],
                                "headlines": ["H"],
                                "long_headlines": ["L"],
                                "descriptions": ["D"],
                                "business_name": "B",
                                "logo_images": ["1"],
                            }
                        ],
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
