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

"""Tests for the campaign builder tools."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import campaign_builder as cb
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


def _ad(url="https://example.be/onderhoud"):
    return {
        "headlines": ["Boiler onderhoud", "Snel ter plaatse", "Erkend vakman"],
        "descriptions": ["Plan vandaag in.", "Vaste prijs, geen verrassingen."],
        "final_url": url,
    }


def _group(name="Onderhoud", **extra):
    group = {
        "name": name,
        "keywords": ["boiler onderhoud", "ketel onderhoud"],
        "negative_keywords": ["vacature"],
        "ads": [_ad()],
    }
    group.update(extra)
    return group


class TestCreateSearchCampaign(MutateToolTestCase):

    def _lookups(self):
        currency = self.row({"customer.currency_code": "EUR"})
        geo = self.row(
            {
                "geo_target_constant.resource_name": "geoTargetConstants/2056",
                "geo_target_constant.canonical_name": "Belgium",
            }
        )
        language = self.row(
            {
                "language_constant.resource_name": "languageConstants/1010",
                "language_constant.name": "Dutch",
            }
        )
        return [currency], [geo], [language]

    def _kwargs(self, **extra):
        kwargs = dict(
            customer_id=CID,
            name="Boilers BE",
            daily_budget=50,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
            target_cpa=30,
            location_ids=["2056"],
            language_ids=["1010"],
            negative_keywords=["gratis"],
            ad_groups=[_group(), _group("Herstelling", match_type="EXACT")],
            start_date="2026-12-01",
        )
        kwargs.update(extra)
        return kwargs

    def test_full_build_is_atomic_and_paused(self):
        self.search_results(*self._lookups(), *self._lookups())
        service = self.service("GoogleAdsService")
        service.mutate.return_value = self.mutate_response(
            ("campaign_budget_result", f"customers/{CID}/campaignBudgets/1"),
            ("campaign_result", f"customers/{CID}/campaigns/2"),
            ("ad_group_result", f"customers/{CID}/adGroups/3"),
        )

        proposal = self.propose(cb.create_search_campaign, **self._kwargs())
        request = self.request("GoogleAdsService", "mutate")
        self.assertTrue(request.validate_only)
        ops = request.mutate_operations

        budget = ops[0].campaign_budget_operation.create
        campaign = ops[1].campaign_operation.create
        self.assertEqual(budget.amount_micros, 50_000_000)
        self.assertEqual(campaign.campaign_budget, budget.resource_name)
        self.assertEqual(campaign.status.name, "PAUSED")
        self.assertEqual(campaign.advertising_channel_type.name, "SEARCH")
        self.assertFalse(campaign.network_settings.target_content_network)
        self.assertFalse(campaign.network_settings.target_search_network)
        self.assertEqual(
            campaign.contains_eu_political_advertising.name,
            "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        )
        self.assertEqual(
            campaign.maximize_conversions.target_cpa_micros, 30_000_000
        )
        self.assertEqual(campaign.start_date_time, "2026-12-01 00:00:00")
        self.assertEqual(
            campaign.geo_target_type_setting.positive_geo_target_type.name,
            "PRESENCE",
        )

        kinds = [type(op).pb(op).WhichOneof("operation") for op in ops]
        # budget, campaign, location, language, campaign negative, then per
        # ad group: group, 2 keywords, 1 negative, 1 ad.
        self.assertEqual(
            kinds,
            ["campaign_budget_operation", "campaign_operation"]
            + ["campaign_criterion_operation"] * 3
            + (
                ["ad_group_operation"]
                + ["ad_group_criterion_operation"] * 3
                + ["ad_group_ad_operation"]
            )
            * 2,
        )
        first_group = ops[5].ad_group_operation.create
        self.assertEqual(first_group.campaign, campaign.resource_name)
        self.assertEqual(
            ops[6].ad_group_criterion_operation.create.ad_group,
            first_group.resource_name,
        )
        self.assertTrue(ops[8].ad_group_criterion_operation.create.negative)
        second_keyword = ops[11].ad_group_criterion_operation.create.keyword
        self.assertEqual(second_keyword.match_type.name, "EXACT")
        self.assertNotEqual(
            ops[10].ad_group_operation.create.resource_name,
            first_group.resource_name,
        )

        values = [i["value_readable"] for i in proposal["change_items"]]
        self.assertIn("50.00 EUR", values)
        self.assertIn("Belgium (2056)", values)

        result = self.commit(
            cb.create_search_campaign, proposal, **self._kwargs()
        )
        self.assertFalse(
            self.request("GoogleAdsService", "mutate").validate_only
        )
        self.assertIn("Created Search campaign 'Boilers BE' (PAUSED)", result)
        self.assertIn("- campaign: 2", result)

    def test_changed_parameters_invalidate_checksum(self):
        self.search_results(*self._lookups(), *self._lookups())
        proposal = self.propose(cb.create_search_campaign, **self._kwargs())
        with self.assertRaisesRegex(ToolError, "mismatched checksum"):
            self.commit(
                cb.create_search_campaign,
                proposal,
                **self._kwargs(daily_budget=500),
            )

    def test_manual_cpc_requires_bids(self):
        with self.assertRaisesRegex(
            ToolError, "needs 'cpc_bid' for MANUAL_CPC"
        ):
            cb.create_search_campaign(
                **self._kwargs(bidding_strategy="MANUAL_CPC", target_cpa=None)
            )

    def test_validation(self):
        cases = {
            "Ad group names must be unique": {
                "ad_groups": [_group(), _group()]
            },
            "does not run worldwide": {"location_ids": []},
            "1-3 responsive search ads": {
                "ad_groups": [_group(ads=[_ad(), _ad(), _ad(), _ad()])]
            },
            "only applies to MAXIMIZE_CONVERSIONS": {
                "bidding_strategy": "MAXIMIZE_CLICKS"
            },
            "must not be before": {"end_date": "2026-11-01"},
        }
        for message, override in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    cb.create_search_campaign(**self._kwargs(**override))
        self.service("GoogleAdsService").mutate.assert_not_called()

    def test_radius_targets(self):
        self.search_results(*self._lookups())
        proposal = self.propose(
            cb.create_search_campaign,
            **self._kwargs(
                radius_targets=[
                    {"radius": 25, "city_name": "Gent", "postal_code": "9000"}
                ]
            ),
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        radius = [
            o.campaign_criterion_operation.create.proximity
            for o in ops
            if o.campaign_criterion_operation.create.proximity.radius
        ]
        self.assertEqual(len(radius), 1)
        self.assertEqual(radius[0].address.postal_code, "9000")
        self.assertIn("25 km around 9000, Gent, BE", str(proposal))


class TestCreateAdGroupAndAd(MutateToolTestCase):

    def _campaign_row(self, channel="SEARCH", strategy="MAXIMIZE_CONVERSIONS"):
        return self.row(
            {
                "campaign.name": "Generic",
                "campaign.advertising_channel_type": getattr(
                    self.enums.AdvertisingChannelTypeEnum, channel
                ),
                "campaign.bidding_strategy_type": getattr(
                    self.enums.BiddingStrategyTypeEnum, strategy
                ),
                "customer.currency_code": "EUR",
            }
        )

    def test_create_ad_group(self):
        self.search_results([self._campaign_row()])
        proposal = self.propose(
            cb.create_ad_group,
            customer_id=CID,
            campaign_id="111",
            ad_group=_group(cpc_bid=1.5),
            status="PAUSED",
        )
        self.assertEqual(
            proposal["change_items"][0]["value_readable"], "Generic"
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        group = ops[0].ad_group_operation.create
        self.assertEqual(group.campaign, f"customers/{CID}/campaigns/111")
        self.assertEqual(group.status.name, "PAUSED")
        self.assertEqual(group.cpc_bid_micros, 1_500_000)
        self.assertEqual(group.type_.name, "SEARCH_STANDARD")
        self.assertEqual(len(ops), 5)

    def test_non_search_campaign_refused(self):
        self.search_results([self._campaign_row(channel="DISPLAY")])
        with self.assertRaisesRegex(ToolError, "not a Search campaign"):
            cb.create_ad_group(
                customer_id=CID, campaign_id="111", ad_group=_group()
            )

    def test_manual_cpc_campaign_requires_bid(self):
        self.search_results([self._campaign_row(strategy="MANUAL_CPC")])
        with self.assertRaisesRegex(ToolError, "cpc_bid"):
            cb.create_ad_group(
                customer_id=CID, campaign_id="111", ad_group=_group()
            )

    def test_create_rsa(self):
        self.search_results([])
        self.propose(
            cb.create_responsive_search_ad,
            customer_id=CID,
            ad_group_id="5",
            ad=_ad(),
        )
        op = self.request("AdGroupAdService", "mutate_ad_group_ads").operations[
            0
        ]
        self.assertEqual(op.create.ad_group, f"customers/{CID}/adGroups/5")
        self.assertEqual(op.create.status.name, "ENABLED")
        self.assertEqual(len(op.create.ad.responsive_search_ad.headlines), 3)

    def test_rsa_limit_per_ad_group(self):
        existing = self.row({"ad_group.name": "Onderhoud"})
        self.search_results([existing, existing, existing])
        with self.assertRaisesRegex(ToolError, "already has 3"):
            cb.create_responsive_search_ad(
                customer_id=CID, ad_group_id="5", ad=_ad()
            )


if __name__ == "__main__":
    unittest.main()
