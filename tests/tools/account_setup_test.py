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

"""Tests for account settings, portfolios, shared budgets, radius, lead forms."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import account_setup as acs
from ads_mcp.tools import campaign_settings as cs
from ads_mcp.tools import extensions as ex
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestAccountSettings(MutateToolTestCase):

    def _customer(self):
        return self.row(
            {
                "customer.descriptive_name": "FavorCool",
                "customer.auto_tagging_enabled": False,
                "customer.final_url_suffix": "",
            }
        )

    def test_auto_tagging_and_suffix(self):
        self.search_results([self._customer()])
        proposal = self.propose(
            acs.update_account_settings,
            customer_id=CID,
            auto_tagging_enabled=True,
            final_url_suffix="?utm_source=google&utm_medium=cpc",
        )
        items = self.items(proposal)
        self.assertEqual(
            items["Auto-tagging"]["original_value_readable"], "False"
        )
        self.assertEqual(
            items["Final URL suffix"]["original_value_readable"], "(none)"
        )
        op = self.request("GoogleAdsService", "mutate").mutate_operations[0]
        update = op.customer_operation.update
        self.assertEqual(update.resource_name, f"customers/{CID}")
        self.assertTrue(update.auto_tagging_enabled)
        self.assertEqual(
            update.final_url_suffix, "utm_source=google&utm_medium=cpc"
        )
        self.assertEqual(
            list(op.customer_operation.update_mask.paths),
            ["auto_tagging_enabled", "final_url_suffix"],
        )

    def test_clear_template(self):
        self.search_results([self._customer()])
        self.propose(
            acs.update_account_settings,
            customer_id=CID,
            tracking_url_template="",
        )
        op = self.request("GoogleAdsService", "mutate").mutate_operations[0]
        self.assertEqual(
            list(op.customer_operation.update_mask.paths),
            ["tracking_url_template"],
        )

    def test_validation(self):
        with self.assertRaisesRegex(ToolError, "at least one"):
            acs.update_account_settings(customer_id=CID)
        with self.assertRaisesRegex(ToolError, "lpurl"):
            acs.update_account_settings(
                customer_id=CID, tracking_url_template="track?x=1"
            )
        with self.assertRaisesRegex(ToolError, "key=value"):
            acs.update_account_settings(
                customer_id=CID, final_url_suffix="utm source"
            )

    def test_campaign_tracking(self):
        self.search_results(
            [
                self.row(
                    {
                        "campaign.name": "Brand",
                        "campaign.final_url_suffix": "a=b",
                    }
                )
            ]
        )
        proposal = self.propose(
            cs.update_campaign_settings,
            customer_id=CID,
            campaign_id="111",
            tracking_url_template="{lpurl}?src=ads",
            final_url_suffix="",
        )
        items = self.items(proposal)
        self.assertEqual(items["Final URL suffix"]["value_readable"], "(none)")
        self.assertEqual(
            items["Final URL suffix"]["original_value_readable"], "a=b"
        )
        op = self.request("CampaignService", "mutate_campaigns").operations[0]
        self.assertEqual(op.update.tracking_url_template, "{lpurl}?src=ads")
        self.assertEqual(
            list(op.update_mask.paths),
            ["tracking_url_template", "final_url_suffix"],
        )


class TestRadius(MutateToolTestCase):

    def test_radius_targets(self):
        proposal = self.propose(
            cs.add_campaign_targeting,
            customer_id=CID,
            campaign_id="111",
            radius_targets=[
                {"radius": 20, "city_name": "Gent", "postal_code": "9000"},
                {
                    "radius": 10,
                    "unit": "MILES",
                    "latitude": 51.05,
                    "longitude": 3.72,
                },
            ],
        )
        values = [i["value_readable"] for i in proposal["change_items"]]
        self.assertEqual(
            values,
            ["20 km around 9000, Gent, BE", "10 mi around 51.05, 3.72"],
        )
        ops = self.request(
            "CampaignCriterionService", "mutate_campaign_criteria"
        ).operations
        first, second = ops[0].create.proximity, ops[1].create.proximity
        self.assertEqual(first.address.city_name, "Gent")
        self.assertEqual(first.radius_units.name, "KILOMETERS")
        self.assertEqual(second.geo_point.latitude_in_micro_degrees, 51_050_000)
        self.assertEqual(second.radius_units.name, "MILES")

    def test_validation(self):
        cases = {
            "1-800 kilometers": {"radius": 900, "city_name": "Gent"},
            "either 'latitude'": {"radius": 5},
            "both 'latitude'": {"radius": 5, "latitude": 51.0},
        }
        for message, radius in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    cs.add_campaign_targeting(
                        customer_id=CID,
                        campaign_id="111",
                        radius_targets=[radius],
                    )


class TestPortfolio(MutateToolTestCase):

    def _currency(self):
        return [self.row({"customer.currency_code": "EUR"})]

    def _campaign(self, cid: int, strategy: str, **extra):
        values = {
            "campaign.id": cid,
            "campaign.name": f"Campaign {cid}",
            "campaign.bidding_strategy_type": getattr(
                self.enums.BiddingStrategyTypeEnum, strategy
            ),
            "customer.currency_code": "EUR",
        }
        values.update(extra)
        return self.row(values)

    def test_new_portfolio_moves_campaigns(self):
        self.search_results(
            self._currency(),
            [
                self._campaign(
                    111,
                    "MAXIMIZE_CONVERSIONS",
                    **{
                        "campaign.maximize_conversions.target_cpa_micros": 40_000_000
                    },
                ),
                self._campaign(222, "MANUAL_CPC"),
            ],
        )
        proposal = self.propose(
            acs.manage_portfolio_bidding_strategy,
            customer_id=CID,
            campaign_ids=["111", "222"],
            name="Leads BE",
            strategy="MAXIMIZE_CONVERSIONS",
            target_cpa=50,
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        new = ops[0].bidding_strategy_operation.create
        self.assertEqual(new.name, "Leads BE")
        self.assertEqual(new.maximize_conversions.target_cpa_micros, 50_000_000)
        for op in ops[1:]:
            self.assertEqual(
                op.campaign_operation.update.bidding_strategy,
                new.resource_name,
            )
            self.assertEqual(
                list(op.campaign_operation.update_mask.paths),
                ["bidding_strategy"],
            )
        self.assertIn("tCPA 50.00 EUR", str(proposal))

    def test_guardrail_against_current_target(self):
        self.search_results(
            self._currency(),
            [
                self._campaign(
                    111,
                    "MAXIMIZE_CONVERSIONS",
                    **{
                        "campaign.maximize_conversions.target_cpa_micros": 10_000_000
                    },
                )
            ],
        )
        with self.assertRaisesRegex(ToolError, "safety limit"):
            acs.manage_portfolio_bidding_strategy(
                customer_id=CID,
                campaign_ids=["111"],
                name="Leads",
                strategy="MAXIMIZE_CONVERSIONS",
                target_cpa=50,
            )

    def test_existing_portfolio(self):
        self.search_results(
            self._currency(),
            [self._campaign(111, "MANUAL_CPC")],
            [
                self.row(
                    {
                        "bidding_strategy.name": "Brand IS",
                        "bidding_strategy.type_": self.enums.BiddingStrategyTypeEnum.TARGET_IMPRESSION_SHARE,
                        "bidding_strategy.campaign_count": 2,
                    }
                )
            ],
        )
        self.propose(
            acs.manage_portfolio_bidding_strategy,
            customer_id=CID,
            campaign_ids=["111"],
            bidding_strategy_id="999",
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        self.assertEqual(len(ops), 1)
        self.assertEqual(
            ops[0].campaign_operation.update.bidding_strategy,
            f"customers/{CID}/biddingStrategies/999",
        )

    def test_impression_share(self):
        self.search_results(self._currency())
        self.propose(
            acs.manage_portfolio_bidding_strategy,
            customer_id=CID,
            name="Brand",
            strategy="TARGET_IMPRESSION_SHARE",
            impression_share_location="ABSOLUTE_TOP_OF_PAGE",
            impression_share_percent=90,
            max_cpc_limit=2,
        )
        new = (
            self.request("GoogleAdsService", "mutate")
            .mutate_operations[0]
            .bidding_strategy_operation.create
        )
        info = new.target_impression_share
        self.assertEqual(info.location.name, "ABSOLUTE_TOP_OF_PAGE")
        self.assertEqual(info.location_fraction_micros, 900_000)
        self.assertEqual(info.cpc_bid_ceiling_micros, 2_000_000)

    def test_validation(self):
        cases = {
            "either 'bidding_strategy_id'": {},
            "needs a 'strategy'": {"name": "X"},
            "Pass 'campaign_ids'": {"bidding_strategy_id": "9"},
            "only apply to a new": {
                "bidding_strategy_id": "9",
                "campaign_ids": ["1"],
                "target_cpa": 5,
            },
        }
        for message, kwargs in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    acs.manage_portfolio_bidding_strategy(
                        customer_id=CID, **kwargs
                    )
        self.search_results(self._currency())
        with self.assertRaisesRegex(ToolError, "does not apply"):
            acs.manage_portfolio_bidding_strategy(
                customer_id=CID,
                name="X",
                strategy="MAXIMIZE_CLICKS",
                target_cpa=5,
            )
        self.search_results(self._currency())
        with self.assertRaisesRegex(ToolError, "needs 'max_cpc_limit'"):
            acs.manage_portfolio_bidding_strategy(
                customer_id=CID,
                name="X",
                strategy="TARGET_IMPRESSION_SHARE",
                impression_share_percent=80,
            )


class TestSharedBudget(MutateToolTestCase):

    def _campaign(self, cid: int, budget: int, shared: bool = False):
        return self.row(
            {
                "campaign.id": cid,
                "campaign.name": f"Campaign {cid}",
                "campaign_budget.amount_micros": budget,
                "campaign_budget.explicitly_shared": shared,
                "customer.currency_code": "EUR",
            }
        )

    def test_new_shared_budget(self):
        self.search_results(
            [self._campaign(111, 20_000_000), self._campaign(222, 10_000_000)]
        )
        proposal = self.propose(
            acs.manage_shared_budget,
            customer_id=CID,
            campaign_ids=["111", "222"],
            name="Gedeeld BE",
            daily_budget=40,
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        budget = ops[0].campaign_budget_operation.create
        self.assertTrue(budget.explicitly_shared)
        self.assertEqual(budget.amount_micros, 40_000_000)
        self.assertEqual(
            ops[1].campaign_operation.update.campaign_budget,
            budget.resource_name,
        )
        self.assertIn("30.00 EUR", str(proposal))

    def test_guardrail(self):
        self.search_results([self._campaign(111, 10_000_000)])
        with self.assertRaisesRegex(ToolError, "safety limit"):
            acs.manage_shared_budget(
                customer_id=CID,
                campaign_ids=["111"],
                name="Te groot",
                daily_budget=50,
            )

    def test_existing_must_be_shared(self):
        self.search_results(
            [self._campaign(111, 10_000_000)],
            [self.row({"campaign_budget.explicitly_shared": False})],
        )
        with self.assertRaisesRegex(ToolError, "not a shared budget"):
            acs.manage_shared_budget(
                customer_id=CID, campaign_ids=["111"], budget_id="77"
            )

    def test_missing_campaign(self):
        self.search_results([self._campaign(111, 10_000_000)])
        with self.assertRaisesRegex(ToolError, "not found.*222"):
            acs.manage_shared_budget(
                customer_id=CID,
                campaign_ids=["111", "222"],
                name="X",
                daily_budget=10,
            )


def lead_form(**extra):
    form = {
        "business_name": "FavorCool",
        "headline": "Gratis airco-offerte",
        "description": "Laat je gegevens achter, we bellen binnen 24 uur.",
        "privacy_policy_url": "https://favorcool.be/privacy",
        "final_url": "https://favorcool.be",
        "call_to_action_description": "Vraag je offerte aan",
        "fields": ["full_name", "EMAIL", "PHONE_NUMBER", "POSTAL_CODE"],
    }
    form.update(extra)
    return form


class TestLeadForm(MutateToolTestCase):

    def setUp(self):
        super().setUp()
        self.service("GoogleAdsService").search.return_value = [
            self.row({"customer.currency_code": "EUR"})
        ]

    def test_lead_form(self):
        self.propose(
            ex.create_extension_asset,
            customer_id=CID,
            extension_type="LEAD_FORM",
            parent_id="111",
            lead_form=lead_form(post_submit_call_to_action_type="VISIT_SITE"),
        )
        asset_op, link_op = self.request(
            "GoogleAdsService", "mutate"
        ).mutate_operations
        asset = asset_op.asset_operation.create
        info = asset.lead_form_asset
        self.assertEqual(info.call_to_action_type.name, "GET_QUOTE")
        self.assertEqual(
            [f.input_type.name for f in info.fields],
            ["FULL_NAME", "EMAIL", "PHONE_NUMBER", "POSTAL_CODE"],
        )
        self.assertEqual(info.desired_intent.name, "HIGH_INTENT")
        self.assertEqual(
            info.post_submit_call_to_action_type.name, "VISIT_SITE"
        )
        self.assertEqual(list(asset.final_urls), ["https://favorcool.be"])
        link = link_op.campaign_asset_operation.create
        self.assertEqual(link.field_type.name, "LEAD_FORM")

    def test_validation(self):
        cases = {
            "link to a campaign": dict(
                link_level="CUSTOMER", lead_form=lead_form()
            ),
            "email address or phone": dict(
                parent_id="111", lead_form=lead_form(fields=["FULL_NAME"])
            ),
            "Invalid field": dict(
                parent_id="111", lead_form=lead_form(fields=["EMAIL", "SHOE"])
            ),
            "maximum is 30": dict(
                parent_id="111", lead_form=lead_form(headline="x" * 31)
            ),
        }
        for message, kwargs in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    ex.create_extension_asset(
                        customer_id=CID, extension_type="LEAD_FORM", **kwargs
                    )


if __name__ == "__main__":
    unittest.main()
