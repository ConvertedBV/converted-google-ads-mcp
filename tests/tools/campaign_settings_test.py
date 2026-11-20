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

"""Tests for bidding, settings, targeting and bid modifier tools."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import campaign_settings as cs
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestCampaignBidding(MutateToolTestCase):

    def _campaign(self, strategy: str, **extra):
        values = {
            "campaign.name": "Generic",
            "campaign.bidding_strategy_type": getattr(
                self.enums.BiddingStrategyTypeEnum, strategy
            ),
            "customer.currency_code": "EUR",
        }
        values.update(extra)
        return self.row(values)

    def test_target_cpa_only(self):
        self.search_results(
            [
                self._campaign(
                    "MAXIMIZE_CONVERSIONS",
                    **{
                        "campaign.maximize_conversions.target_cpa_micros": 20_000_000
                    },
                )
            ]
        )
        proposal = self.propose(
            cs.update_campaign_bidding,
            customer_id=CID,
            campaign_id="111",
            target_cpa=25,
        )
        item = self.items(proposal)["Target CPA"]
        self.assertEqual(item["value_readable"], "25.00 EUR")
        self.assertEqual(item["original_value_readable"], "20.00 EUR")
        op = self.request("CampaignService", "mutate_campaigns").operations[0]
        self.assertEqual(
            op.update.resource_name, f"customers/{CID}/campaigns/111"
        )
        self.assertEqual(
            op.update.maximize_conversions.target_cpa_micros, 25_000_000
        )
        self.assertEqual(
            list(op.update_mask.paths),
            ["maximize_conversions.target_cpa_micros"],
        )

    def test_target_cpa_guardrail(self):
        self.search_results(
            [
                self._campaign(
                    "TARGET_CPA",
                    **{"campaign.target_cpa.target_cpa_micros": 10_000_000},
                )
            ]
        )
        with self.assertRaisesRegex(ToolError, "safety limit"):
            cs.update_campaign_bidding(
                customer_id=CID, campaign_id="111", target_cpa=30
            )

    def test_switch_to_target_roas(self):
        self.search_results([self._campaign("TARGET_SPEND")])
        proposal = self.propose(
            cs.update_campaign_bidding,
            customer_id=CID,
            campaign_id="111",
            bidding_strategy="MAXIMIZE_CONVERSION_VALUE",
            target_roas_percent=400,
        )
        item = self.items(proposal)["Bidding"]
        self.assertIn("target ROAS 400%", item["value_readable"])
        self.assertEqual(
            item["original_value_readable"], "MAXIMIZE_CLICKS (TARGET_SPEND)"
        )
        op = self.request("CampaignService", "mutate_campaigns").operations[0]
        self.assertEqual(op.update.maximize_conversion_value.target_roas, 4.0)

    def test_wrong_target_kind(self):
        self.search_results([self._campaign("MAXIMIZE_CONVERSIONS")])
        with self.assertRaisesRegex(ToolError, "has no target ROAS"):
            cs.update_campaign_bidding(
                customer_id=CID, campaign_id="111", target_roas_percent=300
            )

    def test_portfolio_target_updates_portfolio(self):
        portfolio_rn = f"customers/{CID}/biddingStrategies/77"
        portfolio = self.row(
            {
                "bidding_strategy.name": "Shared tCPA",
                "bidding_strategy.type_": self.enums.BiddingStrategyTypeEnum.TARGET_CPA,
                "bidding_strategy.campaign_count": 4,
                "bidding_strategy.target_cpa.target_cpa_micros": 15_000_000,
            }
        )
        self.search_results(
            [
                self._campaign(
                    "TARGET_CPA", **{"campaign.bidding_strategy": portfolio_rn}
                )
            ],
            [portfolio],
        )
        proposal = self.propose(
            cs.update_campaign_bidding,
            customer_id=CID,
            campaign_id="111",
            target_cpa=18,
        )
        self.assertIn(
            "used by 4 campaigns",
            self.items(proposal)["Portfolio strategy"]["value_readable"],
        )
        op = self.request(
            "BiddingStrategyService", "mutate_bidding_strategies"
        ).operations[0]
        self.assertEqual(op.update.resource_name, portfolio_rn)
        self.assertEqual(op.update.target_cpa.target_cpa_micros, 18_000_000)

    def test_portfolio_switch_refused(self):
        self.search_results(
            [
                self._campaign(
                    "TARGET_CPA",
                    **{
                        "campaign.bidding_strategy": f"customers/{CID}/biddingStrategies/77"
                    },
                )
            ]
        )
        with self.assertRaisesRegex(ToolError, "portfolio strategy"):
            cs.update_campaign_bidding(
                customer_id=CID,
                campaign_id="111",
                bidding_strategy="MANUAL_CPC",
            )


class TestCampaignSettings(MutateToolTestCase):

    def test_update_name_end_date_and_partners(self):
        self.search_results(
            [
                self.row(
                    {
                        "campaign.name": "Old",
                        "campaign.end_date_time": "",
                        "campaign.network_settings.target_partner_search_network": False,
                    }
                )
            ]
        )
        proposal = self.propose(
            cs.update_campaign_settings,
            customer_id=CID,
            campaign_id="111",
            name="  New   name ",
            end_date="2026-12-31",
            search_partners=True,
        )
        items = self.items(proposal)
        self.assertEqual(items["Name"]["value_readable"], "New name")
        self.assertEqual(items["Name"]["original_value_readable"], "Old")
        op = self.request("CampaignService", "mutate_campaigns").operations[0]
        self.assertEqual(op.update.end_date_time, "2026-12-31 23:59:59")
        self.assertTrue(
            op.update.network_settings.target_partner_search_network
        )
        self.assertEqual(
            list(op.update_mask.paths),
            [
                "name",
                "end_date_time",
                "network_settings.target_partner_search_network",
            ],
        )

    def test_bad_date(self):
        with self.assertRaisesRegex(ToolError, "YYYY-MM-DD"):
            cs.update_campaign_settings(
                customer_id=CID, campaign_id="111", end_date="31/12/2026"
            )


class TestTargeting(MutateToolTestCase):

    def _geo(self, geo_id: str, name: str):
        return self.row(
            {
                "geo_target_constant.resource_name": f"geoTargetConstants/{geo_id}",
                "geo_target_constant.canonical_name": name,
            }
        )

    def test_add_targeting(self):
        self.search_results(
            [
                self._geo("2056", "Belgium"),
                self._geo("1001021", "Ghent,Belgium"),
            ],
            [
                self.row(
                    {
                        "language_constant.resource_name": "languageConstants/1010",
                        "language_constant.name": "Dutch",
                    }
                )
            ],
        )
        proposal = self.propose(
            cs.add_campaign_targeting,
            customer_id=CID,
            campaign_id="111",
            location_ids=["2056"],
            excluded_location_ids=["1001021"],
            language_ids=["1010"],
            ad_schedules=[
                {"day": "WEEKEND", "start_time": "09:00", "end_time": "17:00"}
            ],
        )
        values = [i["value_readable"] for i in proposal["change_items"]]
        self.assertIn("Belgium (2056)", values)
        self.assertIn("Ghent,Belgium (1001021)", values)
        self.assertIn("Dutch (1010)", values)
        self.assertIn("SUNDAY 09:00-17:00", values)
        ops = self.request(
            "CampaignCriterionService", "mutate_campaign_criteria"
        ).operations
        self.assertEqual(len(ops), 5)
        self.assertEqual(
            ops[0].create.location.geo_target_constant,
            "geoTargetConstants/2056",
        )
        self.assertTrue(ops[1].create.negative)
        self.assertEqual(ops[3].create.ad_schedule.day_of_week.name, "SATURDAY")
        self.assertEqual(ops[3].create.ad_schedule.start_hour, 9)

    def test_unknown_location(self):
        self.search_results([self._geo("2056", "Belgium")])
        with self.assertRaisesRegex(ToolError, "Unknown location IDs: 999"):
            cs.add_campaign_targeting(
                customer_id=CID, campaign_id="111", location_ids=["2056", "999"]
            )

    def test_remove_only_targeting_criteria(self):
        self.search_results([])
        with self.assertRaisesRegex(ToolError, "not removable targeting"):
            cs.remove_campaign_targeting(
                customer_id=CID, campaign_id="111", criterion_ids=["5"]
            )
        self.assertIn(
            "campaign_criterion.type IN ('LOCATION', 'LANGUAGE', 'AD_SCHEDULE', 'PROXIMITY')",
            self.queries()[0],
        )

    def test_remove_targeting(self):
        rn = f"customers/{CID}/campaignCriteria/111~2056"
        self.search_results(
            [
                self.row(
                    {
                        "campaign_criterion.resource_name": rn,
                        "campaign_criterion.type_": self.enums.CriterionTypeEnum.LOCATION,
                        "campaign_criterion.display_name": "Belgium",
                    }
                )
            ]
        )
        proposal = self.propose(
            cs.remove_campaign_targeting,
            customer_id=CID,
            campaign_id="111",
            criterion_ids=["2056"],
        )
        self.assertEqual(
            proposal["change_items"][0]["value_readable"], "Belgium"
        )
        op = self.request(
            "CampaignCriterionService", "mutate_campaign_criteria"
        ).operations[0]
        self.assertEqual(op.remove, rn)


class TestBidModifiers(MutateToolTestCase):

    def test_device_and_criterion_modifiers(self):
        mobile_rn = f"customers/{CID}/campaignCriteria/111~30001"
        self.search_results(
            [
                self.row(
                    {
                        "campaign_criterion.resource_name": mobile_rn,
                        "campaign_criterion.criterion_id": 30001,
                        "campaign_criterion.type_": self.enums.CriterionTypeEnum.DEVICE,
                        "campaign_criterion.device.type_": self.enums.DeviceEnum.MOBILE,
                        "campaign_criterion.display_name": "Mobile",
                    }
                ),
                self.row(
                    {
                        "campaign_criterion.resource_name": f"customers/{CID}/campaignCriteria/111~2056",
                        "campaign_criterion.criterion_id": 2056,
                        "campaign_criterion.type_": self.enums.CriterionTypeEnum.LOCATION,
                        "campaign_criterion.display_name": "Belgium",
                    }
                ),
            ]
        )
        proposal = self.propose(
            cs.update_bid_modifiers,
            customer_id=CID,
            campaign_id="111",
            device_bid_modifiers={"mobile": 1.2},
            criterion_bid_modifiers={"2056": 0.9},
        )
        self.assertEqual(len(proposal["change_items"]), 2)
        ops = self.request(
            "CampaignCriterionService", "mutate_campaign_criteria"
        ).operations
        self.assertEqual(ops[0].update.resource_name, mobile_rn)
        self.assertAlmostEqual(ops[0].update.bid_modifier, 1.2, places=5)
        self.assertEqual(list(ops[0].update_mask.paths), ["bid_modifier"])
        self.assertAlmostEqual(ops[1].update.bid_modifier, 0.9, places=5)

    def test_invalid_modifier(self):
        with self.assertRaisesRegex(ToolError, "0.1 and 10"):
            cs.update_bid_modifiers(
                customer_id=CID,
                campaign_id="111",
                device_bid_modifiers={"MOBILE": 25},
            )


if __name__ == "__main__":
    unittest.main()
