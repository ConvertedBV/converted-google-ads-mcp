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

"""Tests for the Performance Max and Demand Gen builders."""

import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp import image_assets as ia
from ads_mcp.tools import asset_campaigns as ac
from tests.tools.mutate_tool_base import CID, MutateToolTestCase

# url -> (width, height)
SIZES = {
    "https://favorcool.be/hero.png": (1200, 628),
    "https://favorcool.be/square.png": (1200, 1200),
    "https://favorcool.be/portrait.png": (960, 1200),
    "https://favorcool.be/logo.png": (512, 512),
}


def fake_fetch(url):
    width, height = SIZES[url]
    return ia.FetchedImage(
        url, f"{url}-bytes".encode(), "IMAGE_PNG", width, height
    )


def group(name="Airco", **extra):
    data = {
        "name": name,
        "final_url": "https://favorcool.be/airco",
        "headlines": ["Airco laten plaatsen", "Klaar vóór de zomer", "Erkend"],
        "long_headlines": ["Airco door eigen installateurs, vaste prijs"],
        "descriptions": [
            "Vaste prijs.",
            "Plan vandaag je gratis adviesgesprek in.",
        ],
        "marketing_images": ["https://favorcool.be/hero.png"],
        "square_marketing_images": ["https://favorcool.be/square.png", "555"],
        "portrait_marketing_images": ["https://favorcool.be/portrait.png"],
        "youtube_video_ids": ["dQw4w9WgXcQ"],
        "search_themes": ["airco plaatsen", "warmtepomp airco"],
        "audience_ids": ["77"],
    }
    data.update(extra)
    return data


class AssetCampaignTestCase(MutateToolTestCase):

    def setUp(self):
        super().setUp()
        patcher = patch(
            "ads_mcp.image_assets.fetch_image", side_effect=fake_fetch
        )
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)

    def lookups(self, with_language=True):
        results = [
            [self.row({"customer.currency_code": "EUR"})],
            [
                self.row(
                    {
                        "geo_target_constant.resource_name": "geoTargetConstants/2056",
                        "geo_target_constant.canonical_name": "Belgium",
                    }
                )
            ],
        ]
        if with_language:
            results.append(
                [
                    self.row(
                        {
                            "language_constant.resource_name": "languageConstants/1010",
                            "language_constant.name": "Dutch",
                        }
                    )
                ]
            )
        return results

    @staticmethod
    def kinds(ops):
        return [type(op).pb(op).WhichOneof("operation") for op in ops]


class TestPerformanceMax(AssetCampaignTestCase):

    def _kwargs(self, **extra):
        kwargs = dict(
            customer_id=CID,
            name="P.MAX - Airco",
            daily_budget=40,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
            target_cpa=35,
            location_ids=["2056"],
            language_ids=["1010"],
            business_name="FavorCool",
            logo_image="https://favorcool.be/logo.png",
            asset_groups=[group()],
        )
        kwargs.update(extra)
        return kwargs

    def test_full_build(self):
        self.search_results(*self.lookups(), *self.lookups())
        proposal = self.propose(
            ac.create_performance_max_campaign, **self._kwargs()
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        kinds = self.kinds(ops)

        self.assertEqual(
            kinds[:4],
            [
                "campaign_budget_operation",
                "campaign_operation",
                "campaign_criterion_operation",
                "campaign_criterion_operation",
            ],
        )
        campaign = ops[1].campaign_operation.create
        self.assertEqual(
            campaign.advertising_channel_type.name, "PERFORMANCE_MAX"
        )
        self.assertEqual(campaign.status.name, "PAUSED")
        self.assertTrue(campaign.brand_guidelines_enabled)
        self.assertEqual(
            campaign.maximize_conversions.target_cpa_micros, 35_000_000
        )
        self.assertFalse(
            ops[0].campaign_budget_operation.create.explicitly_shared
        )

        # All assets are created before anything links to them.
        first_link = min(
            kinds.index("campaign_asset_operation"),
            kinds.index("asset_group_operation"),
        )
        self.assertTrue(
            all(k == "asset_operation" for k in kinds[4:first_link])
        )
        assets = [op.asset_operation.create for op in ops[4:first_link]]
        texts = [a.text_asset.text for a in assets if a.text_asset.text]
        self.assertIn("FavorCool", texts)
        self.assertEqual(len(texts), len(set(texts)))  # deduplicated
        images = [a for a in assets if a.image_asset.data]
        self.assertEqual(len(images), 4)  # hero, square, portrait, logo
        self.assertEqual(
            images[0].image_asset.mime_type, self.enums.MimeTypeEnum.IMAGE_PNG
        )
        self.assertEqual(
            sum(1 for a in assets if a.youtube_video_asset.youtube_video_id), 1
        )

        brand = [
            op.campaign_asset_operation.create
            for op in ops
            if self.kinds([op])[0] == "campaign_asset_operation"
        ]
        self.assertEqual(
            sorted(b.field_type.name for b in brand), ["BUSINESS_NAME", "LOGO"]
        )
        asset_group = next(
            op.asset_group_operation.create
            for op in ops
            if self.kinds([op])[0] == "asset_group_operation"
        )
        self.assertEqual(
            list(asset_group.final_urls), ["https://favorcool.be/airco"]
        )
        links = [
            op.asset_group_asset_operation.create
            for op in ops
            if self.kinds([op])[0] == "asset_group_asset_operation"
        ]
        fields = [link.field_type.name for link in links]
        self.assertEqual(fields.count("HEADLINE"), 3)
        self.assertEqual(fields.count("SQUARE_MARKETING_IMAGE"), 2)
        self.assertIn(f"customers/{CID}/assets/555", [l.asset for l in links])
        self.assertEqual(fields.count("YOUTUBE_VIDEO"), 1)
        signals = [
            op.asset_group_signal_operation.create
            for op in ops
            if self.kinds([op])[0] == "asset_group_signal_operation"
        ]
        self.assertEqual(
            [s.search_theme.text for s in signals if s.search_theme.text],
            ["airco plaatsen", "warmtepomp airco"],
        )
        self.assertEqual(
            signals[-1].audience.audience, f"customers/{CID}/audiences/77"
        )

        values = [i["value_readable"] for i in proposal["change_items"]]
        self.assertIn("40.00 EUR", values)
        self.assertIn("Belgium (2056)", values)

        result = self.commit(
            ac.create_performance_max_campaign, proposal, **self._kwargs()
        )
        self.assertIn("Created Performance Max campaign", result)

    def test_changed_image_invalidates_checksum(self):
        self.search_results(*self.lookups(), *self.lookups())
        proposal = self.propose(
            ac.create_performance_max_campaign, **self._kwargs()
        )

        def changed(url):
            image = fake_fetch(url)
            return ia.FetchedImage(
                url, b"other-bytes", image.mime, image.width, image.height
            )

        self.fetch.side_effect = changed
        with self.assertRaisesRegex(ToolError, "mismatched checksum"):
            self.commit(
                ac.create_performance_max_campaign, proposal, **self._kwargs()
            )

    def test_validation(self):
        cases = {
            "supports MAXIMIZE_CONVERSIONS, MAXIMIZE_CONVERSION_VALUE": {
                "bidding_strategy": "MAXIMIZE_CLICKS",
                "target_cpa": None,
            },
            "at least one description of max 60": {
                "asset_groups": [group(descriptions=["x" * 61, "y" * 70])]
            },
            "needs 3-15 headlines": {
                "asset_groups": [group(headlines=["Een"])]
            },
            "'marketing_images' needs 1-20": {
                "asset_groups": [group(marketing_images=[])]
            },
            "Asset group names must be unique": {
                "asset_groups": [group(), group()]
            },
        }
        for message, override in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    ac.create_performance_max_campaign(
                        **self._kwargs(**override)
                    )

    def test_wrong_aspect_ratio_is_caught_before_the_api(self):
        self.search_results(*self.lookups())
        with self.assertRaisesRegex(ToolError, "landscape 1.91:1"):
            ac.create_performance_max_campaign(
                **self._kwargs(
                    asset_groups=[
                        group(
                            marketing_images=["https://favorcool.be/square.png"]
                        )
                    ]
                )
            )
        self.service("GoogleAdsService").mutate.assert_not_called()


class TestAssetGroup(AssetCampaignTestCase):

    def test_add_to_pmax_with_existing_assets(self):
        self.search_results(
            [
                self.row(
                    {
                        "campaign.name": "P.MAX - Airco",
                        "campaign.advertising_channel_type": self.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX,
                    }
                )
            ]
        )
        self.propose(
            ac.create_asset_group,
            customer_id=CID,
            campaign_id="111",
            asset_group=group(
                marketing_images=["101"],
                square_marketing_images=["102"],
                portrait_marketing_images=[],
                youtube_video_ids=[],
            ),
        )
        self.fetch.assert_not_called()
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        group_op = next(
            op for op in ops if self.kinds([op])[0] == "asset_group_operation"
        )
        self.assertEqual(
            group_op.asset_group_operation.create.campaign,
            f"customers/{CID}/campaigns/111",
        )

    def test_non_pmax_refused(self):
        self.search_results(
            [
                self.row(
                    {
                        "campaign.advertising_channel_type": self.enums.AdvertisingChannelTypeEnum.SEARCH
                    }
                )
            ]
        )
        with self.assertRaisesRegex(ToolError, "not a Performance Max"):
            ac.create_asset_group(
                customer_id=CID, campaign_id="111", asset_group=group()
            )


class TestDemandGen(AssetCampaignTestCase):

    def _ad(self, **extra):
        ad = {
            "name": "Zomer",
            "final_url": "https://favorcool.be/airco",
            "headlines": ["Nu bestellen, klaar vóór de zomer"],
            "descriptions": ["Eigen installateurs, vaste prijs."],
            "business_name": "FavorCool",
            "logo_images": ["https://favorcool.be/logo.png"],
            "marketing_images": ["https://favorcool.be/hero.png"],
            "square_marketing_images": ["https://favorcool.be/square.png"],
            "call_to_action": "Learn more",
        }
        ad.update(extra)
        return ad

    def _kwargs(self, **extra):
        kwargs = dict(
            customer_id=CID,
            name="Demand_gen - airco",
            daily_budget=25,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
            ad_groups=[
                {
                    "name": "General_airco",
                    "location_ids": ["2056"],
                    "language_ids": ["1010"],
                    "ads": [self._ad()],
                }
            ],
        )
        kwargs.update(extra)
        return kwargs

    def test_full_build(self):
        self.search_results(*self.lookups())
        self.propose(ac.create_demand_gen_campaign, **self._kwargs())
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        kinds = self.kinds(ops)
        campaign = ops[1].campaign_operation.create
        self.assertEqual(campaign.advertising_channel_type.name, "DEMAND_GEN")
        self.assertTrue(
            campaign.demand_gen_campaign_settings.upgraded_targeting
        )
        self.assertNotIn("campaign_criterion_operation", kinds)
        criteria = [
            op.ad_group_criterion_operation.create
            for op in ops
            if self.kinds([op])[0] == "ad_group_criterion_operation"
        ]
        self.assertEqual(
            criteria[0].location.geo_target_constant, "geoTargetConstants/2056"
        )
        self.assertEqual(
            criteria[1].language.language_constant, "languageConstants/1010"
        )
        ad = next(
            op.ad_group_ad_operation.create.ad
            for op in ops
            if self.kinds([op])[0] == "ad_group_ad_operation"
        )
        info = ad.demand_gen_multi_asset_ad
        self.assertEqual(
            info.headlines[0].text, "Nu bestellen, klaar vóór de zomer"
        )
        self.assertEqual(info.business_name, "FavorCool")
        self.assertEqual(info.call_to_action_text, "Learn more")
        self.assertEqual(len(info.logo_images), 1)
        self.assertEqual(len(info.marketing_images), 1)
        self.assertTrue(
            info.logo_images[0].asset.startswith(f"customers/{CID}/assets/-")
        )

    def test_needs_a_landscape_or_square_image(self):
        with self.assertRaisesRegex(ToolError, "landscape or square image"):
            ac.create_demand_gen_campaign(
                **self._kwargs(
                    ad_groups=[
                        {
                            "name": "A",
                            "location_ids": ["2056"],
                            "ads": [
                                self._ad(
                                    marketing_images=[],
                                    square_marketing_images=[],
                                )
                            ],
                        }
                    ]
                )
            )

    def test_headline_limit_is_40(self):
        with self.assertRaisesRegex(ToolError, "maximum is 40"):
            ac.create_demand_gen_campaign(
                **self._kwargs(
                    ad_groups=[
                        {
                            "name": "A",
                            "location_ids": ["2056"],
                            "ads": [self._ad(headlines=["x" * 41])],
                        }
                    ]
                )
            )


class TestCreateImageAssets(AssetCampaignTestCase):

    def test_upload_with_role_check(self):
        proposal = self.propose(
            ac.create_image_assets,
            customer_id=CID,
            images=[
                {
                    "url": "https://favorcool.be/logo.png",
                    "name": "Logo",
                    "role": "LOGO",
                }
            ],
        )
        self.assertEqual(
            proposal["change_items"][0]["value_readable"], "512x512 PNG (LOGO)"
        )
        op = self.request("AssetService", "mutate_assets").operations[0]
        self.assertEqual(op.create.name, "Logo")
        self.assertEqual(
            op.create.image_asset.mime_type, self.enums.MimeTypeEnum.IMAGE_PNG
        )
        with self.assertRaisesRegex(ToolError, "portrait 4:5"):
            ac.create_image_assets(
                customer_id=CID,
                images=[
                    {
                        "url": "https://favorcool.be/logo.png",
                        "name": "X",
                        "role": "PORTRAIT",
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
