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

"""Tests for product groups, listing group filters and campaign automation."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import campaign_automation as ca
from ads_mcp.tools import product_groups as pg
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestProductGroups(MutateToolTestCase):

    def _ad_group(self, strategy="MANUAL_CPC", kind="SHOPPING_PRODUCT_ADS"):
        return self.row(
            {
                "ad_group.name": "Alle producten",
                "ad_group.type_": getattr(self.enums.AdGroupTypeEnum, kind),
                "ad_group.cpc_bid_micros": 500_000,
                "campaign.bidding_strategy_type": getattr(
                    self.enums.BiddingStrategyTypeEnum, strategy
                ),
                "customer.currency_code": "EUR",
            }
        )

    def _root(self):
        return self.row(
            {
                "ad_group_criterion.resource_name": f"customers/{CID}/adGroupCriteria/7~1",
                "ad_group_criterion.cpc_bid_micros": 600_000,
            }
        )

    def test_replace_tree_by_brand(self):
        self.search_results([self._ad_group()], [self._root()])
        self.propose(
            pg.set_product_groups,
            customer_id=CID,
            ad_group_id="7",
            dimension="BRAND",
            values=[
                {"value": "Daikin", "cpc_bid": 0.9},
                {"value": "Noname", "excluded": True},
            ],
            other_cpc_bid=0.4,
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        self.assertEqual(
            ops[0].ad_group_criterion_operation.remove,
            f"customers/{CID}/adGroupCriteria/7~1",
        )
        root = ops[1].ad_group_criterion_operation.create
        self.assertEqual(root.listing_group.type_.name, "SUBDIVISION")
        daikin = ops[2].ad_group_criterion_operation.create
        self.assertEqual(
            daikin.listing_group.case_value.product_brand.value, "Daikin"
        )
        self.assertEqual(daikin.cpc_bid_micros, 900_000)
        self.assertEqual(
            daikin.listing_group.parent_ad_group_criterion, root.resource_name
        )
        self.assertTrue(ops[3].ad_group_criterion_operation.create.negative)
        other = ops[4].ad_group_criterion_operation.create
        self.assertIn("product_brand", other.listing_group.case_value)
        self.assertEqual(other.listing_group.case_value.product_brand.value, "")
        self.assertEqual(other.cpc_bid_micros, 400_000)

    def test_bid_capped(self):
        self.search_results([self._ad_group()], [self._root()])
        with self.assertRaisesRegex(ToolError, "safety limit"):
            pg.set_product_groups(
                customer_id=CID,
                ad_group_id="7",
                dimension="BRAND",
                values=[{"value": "Daikin", "cpc_bid": 5}],
            )

    def test_bids_need_manual_cpc(self):
        self.search_results([self._ad_group("TARGET_SPEND")], [])
        with self.assertRaisesRegex(ToolError, "manual CPC"):
            pg.set_product_groups(
                customer_id=CID,
                ad_group_id="7",
                dimension="BRAND",
                values=[{"value": "Daikin", "cpc_bid": 0.5}],
            )

    def test_validation(self):
        with self.assertRaisesRegex(ToolError, "numeric category"):
            pg.set_product_groups(
                customer_id=CID,
                ad_group_id="7",
                dimension="CATEGORY",
                values=[{"value": "Airco"}],
            )
        with self.assertRaisesRegex(ToolError, "every product"):
            pg.set_product_groups(
                customer_id=CID,
                ad_group_id="7",
                dimension="CONDITION",
                values=[{"value": "used", "excluded": True}],
                other_excluded=True,
            )
        self.search_results([self._ad_group(kind="SEARCH_STANDARD")])
        with self.assertRaisesRegex(ToolError, "not a Shopping"):
            pg.set_product_groups(
                customer_id=CID,
                ad_group_id="7",
                dimension="BRAND",
                values=[{"value": "x"}],
            )

    def test_pmax_filters_custom_label(self):
        asset_group = self.row(
            {
                "asset_group.name": "Retail",
                "campaign.advertising_channel_type": self.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX,
                "campaign.shopping_setting.merchant_id": 123,
            }
        )
        root_rn = f"customers/{CID}/assetGroupListingGroupFilters/9~1"
        child_rn = f"customers/{CID}/assetGroupListingGroupFilters/9~2"
        existing = [
            self.row(
                {"asset_group_listing_group_filter.resource_name": root_rn}
            ),
            self.row(
                {
                    "asset_group_listing_group_filter.resource_name": child_rn,
                    "asset_group_listing_group_filter.parent_listing_group_filter": root_rn,
                }
            ),
        ]
        self.search_results([asset_group], existing)
        self.propose(
            pg.set_listing_group_filters,
            customer_id=CID,
            asset_group_id="9",
            dimension="CUSTOM_LABEL",
            custom_label_index=2,
            values=[{"value": "outlet", "excluded": True}],
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        removes = [
            o.asset_group_listing_group_filter_operation.remove for o in ops[:2]
        ]
        self.assertEqual(removes, [child_rn, root_rn])
        unit = ops[3].asset_group_listing_group_filter_operation.create
        self.assertEqual(unit.type_.name, "UNIT_EXCLUDED")
        attr = unit.case_value.product_custom_attribute
        self.assertEqual(attr.value, "outlet")
        self.assertEqual(attr.index.name, "INDEX2")
        other = ops[4].asset_group_listing_group_filter_operation.create
        self.assertEqual(other.type_.name, "UNIT_INCLUDED")

    def test_pmax_needs_merchant_center(self):
        self.search_results(
            [
                self.row(
                    {
                        "asset_group.name": "Leads",
                        "campaign.advertising_channel_type": self.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX,
                    }
                )
            ]
        )
        with self.assertRaisesRegex(ToolError, "Merchant Center"):
            pg.set_listing_group_filters(
                customer_id=CID,
                asset_group_id="9",
                dimension="BRAND",
                values=[{"value": "x"}],
            )


class TestCampaignAutomation(MutateToolTestCase):

    def _campaign(self, channel="SEARCH", ai_max=False):
        return self.row(
            {
                "campaign.name": "Brand",
                "campaign.advertising_channel_type": getattr(
                    self.enums.AdvertisingChannelTypeEnum, channel
                ),
                "campaign.ai_max_setting.enable_ai_max": ai_max,
            }
        )

    def test_ai_max_with_url_expansion(self):
        self.search_results([self._campaign()])
        self.propose(
            ca.update_campaign_automation,
            customer_id=CID,
            campaign_id="111",
            ai_max=True,
            final_url_expansion=False,
            add_url_exclusions=["/vacatures"],
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        update = ops[0].campaign_operation
        self.assertTrue(update.update.ai_max_setting.enable_ai_max)
        self.assertIn("asset_automation_settings", update.update_mask.paths)
        setting = update.update.asset_automation_settings[0]
        self.assertEqual(
            setting.asset_automation_type.name,
            "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
        )
        self.assertEqual(setting.asset_automation_status.name, "OPTED_OUT")
        webpage = ops[1].campaign_criterion_operation.create
        self.assertTrue(webpage.negative)
        self.assertEqual(webpage.webpage.conditions[0].argument, "/vacatures")

    def test_ai_max_only_search(self):
        self.search_results([self._campaign("PERFORMANCE_MAX")])
        with self.assertRaisesRegex(ToolError, "Search campaign setting"):
            ca.update_campaign_automation(
                customer_id=CID, campaign_id="111", ai_max=True
            )

    def test_search_automation_needs_ai_max(self):
        self.search_results([self._campaign()])
        with self.assertRaisesRegex(ToolError, "part of AI Max"):
            ca.update_campaign_automation(
                customer_id=CID, campaign_id="111", text_asset_automation=True
            )

    def test_frequency_caps(self):
        self.search_results([self._campaign("DISPLAY")])
        self.propose(
            ca.set_frequency_caps,
            customer_id=CID,
            campaign_id="111",
            caps=[{"max_events": 3, "time_unit": "WEEK"}],
        )
        op = self.request("GoogleAdsService", "mutate").mutate_operations[0]
        cap = op.campaign_operation.update.frequency_caps[0]
        self.assertEqual(cap.cap, 3)
        self.assertEqual(cap.key.time_unit.name, "WEEK")
        self.assertEqual(cap.key.level.name, "CAMPAIGN")

    def test_frequency_caps_not_on_search(self):
        self.search_results([self._campaign("SEARCH")])
        with self.assertRaisesRegex(ToolError, "Display and Demand Gen"):
            ca.set_frequency_caps(
                customer_id=CID,
                campaign_id="111",
                caps=[{"max_events": 3}],
            )


if __name__ == "__main__":
    unittest.main()
