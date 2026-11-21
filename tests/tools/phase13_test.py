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

"""Tests for ad group settings, customizers, conversion adjustments,
value rules and App campaigns."""

import json
import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import ad_group_settings as ags
from ads_mcp.tools import app_campaigns as apps
from ads_mcp.tools import conversion_extras as cx
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestAdGroupSettings(MutateToolTestCase):

    def _ad_group(self, **extra):
        values = {"ad_group.name": "Airco", "customer.currency_code": "EUR"}
        values.update(extra)
        return self.row(values)

    def test_target_cpa_from_campaign_baseline(self):
        self.search_results(
            [
                self._ad_group(
                    **{"ad_group.effective_target_cpa_micros": 40_000_000}
                )
            ]
        )
        proposal = self.propose(
            ags.update_ad_group_settings,
            customer_id=CID,
            ad_group_id="5",
            target_cpa=45,
            ad_rotation="ROTATE_FOREVER",
        )
        op = self.request("GoogleAdsService", "mutate").mutate_operations[0]
        self.assertEqual(
            op.ad_group_operation.update.target_cpa_micros, 45_000_000
        )
        self.assertEqual(
            sorted(op.ad_group_operation.update_mask.paths),
            ["ad_rotation_mode", "target_cpa_micros"],
        )
        self.assertIn("(from campaign)", json.dumps(proposal))

    def test_looser_target_capped(self):
        self.search_results(
            [self._ad_group(**{"ad_group.effective_target_roas": 4.0})]
        )
        with self.assertRaisesRegex(ToolError, "safety limit"):
            ags.update_ad_group_settings(
                customer_id=CID, ad_group_id="5", target_roas_percent=100
            )

    def test_no_target_without_smart_bidding(self):
        self.search_results([self._ad_group()])
        with self.assertRaisesRegex(ToolError, "no target CPA"):
            ags.update_ad_group_settings(
                customer_id=CID, ad_group_id="5", target_cpa=10
            )


class TestAdCustomizers(MutateToolTestCase):

    def test_new_customizer_on_campaigns(self):
        self.search_results([])
        self.propose(
            ags.manage_ad_customizer,
            customer_id=CID,
            name="price",
            value="€1.499",
            customizer_type="PRICE",
            level="CAMPAIGN",
            entity_ids=["111"],
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        attr = ops[0].customizer_attribute_operation.create
        self.assertEqual(attr.type_.name, "PRICE")
        value = ops[1].campaign_customizer_operation.create
        self.assertEqual(value.campaign, f"customers/{CID}/campaigns/111")
        self.assertEqual(value.customizer_attribute, attr.resource_name)
        self.assertEqual(value.value.string_value, "€1.499")

    def test_replaces_existing_value(self):
        attr_rn = f"customers/{CID}/customizerAttributes/3"
        self.search_results(
            [
                self.row(
                    {
                        "customizer_attribute.resource_name": attr_rn,
                        "customizer_attribute.name": "Discount",
                        "customizer_attribute.type_": self.enums.CustomizerAttributeTypeEnum.PERCENT,
                    }
                )
            ],
            [
                self.row(
                    {
                        "customer_customizer.resource_name": f"customers/{CID}/customerCustomizers/3",
                        "customer_customizer.value.string_value": "10%",
                    }
                )
            ],
        )
        proposal = self.propose(
            ags.manage_ad_customizer,
            customer_id=CID,
            name="discount",
            value="20%",
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        self.assertEqual(
            ops[0].customer_customizer_operation.remove,
            f"customers/{CID}/customerCustomizers/3",
        )
        self.assertEqual(
            ops[1].customer_customizer_operation.create.customizer_attribute,
            attr_rn,
        )
        self.assertIn("10%", json.dumps(proposal))

    def test_value_must_match_type(self):
        self.search_results([])
        with self.assertRaisesRegex(ToolError, "valid PERCENT"):
            ags.manage_ad_customizer(
                customer_id=CID,
                name="discount",
                value="twenty",
                customizer_type="PERCENT",
            )


class TestConversionAdjustments(MutateToolTestCase):

    def test_retract_and_restate(self):
        self.search_results(
            [
                self.row(
                    {
                        "conversion_action.name": "Lead",
                        "customer.currency_code": "EUR",
                    }
                )
            ]
        )
        service = self.service("ConversionAdjustmentUploadService")
        service.upload_conversion_adjustments.return_value = (
            self.real_ga_client.get_type("UploadConversionAdjustmentsResponse")
        )
        self.propose(
            cx.upload_conversion_adjustments,
            customer_id=CID,
            conversion_action_id="8",
            adjustments=[
                {
                    "adjustment_type": "RETRACTION",
                    "adjustment_time": "2026-11-21 10:00:00+01:00",
                    "order_id": "A-1",
                },
                {
                    "adjustment_type": "RESTATEMENT",
                    "adjustment_time": "2026-11-21T10:00:00+01:00",
                    "gclid": "Cj0KCQabcdefghij",
                    "conversion_time": "2026-11-01 09:00:00+01:00",
                    "restated_value": 250,
                },
            ],
        )
        request = self.request(
            "ConversionAdjustmentUploadService", "upload_conversion_adjustments"
        )
        self.assertTrue(request.validate_only)
        first, second = request.conversion_adjustments
        self.assertEqual(first.adjustment_type.name, "RETRACTION")
        self.assertEqual(first.order_id, "A-1")
        self.assertEqual(second.gclid_date_time_pair.gclid, "Cj0KCQabcdefghij")
        self.assertEqual(second.restatement_value.adjusted_value, 250)
        self.assertEqual(second.restatement_value.currency_code, "EUR")

    def test_validation(self):
        with self.assertRaisesRegex(ToolError, "either order_id"):
            cx.upload_conversion_adjustments(
                customer_id=CID,
                conversion_action_id="8",
                adjustments=[
                    {
                        "adjustment_type": "RETRACTION",
                        "adjustment_time": "2026-11-21 10:00:00+01:00",
                    }
                ],
            )
        with self.assertRaisesRegex(ToolError, "restated_value"):
            cx.upload_conversion_adjustments(
                customer_id=CID,
                conversion_action_id="8",
                adjustments=[
                    {
                        "adjustment_type": "RESTATEMENT",
                        "adjustment_time": "2026-11-21 10:00:00+01:00",
                        "order_id": "A-1",
                    }
                ],
            )


class TestValueRules(MutateToolTestCase):

    def test_create_rule_and_rule_set(self):
        self.search_results([])
        self.propose(
            cx.manage_conversion_value_rules,
            customer_id=CID,
            action="MULTIPLY",
            value=1.5,
            location_ids=["1001021"],
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        rule = ops[0].conversion_value_rule_operation.create
        self.assertEqual(rule.action.operation.name, "MULTIPLY")
        self.assertEqual(
            list(rule.geo_location_condition.geo_target_constants),
            ["geoTargetConstants/1001021"],
        )
        rule_set = ops[1].conversion_value_rule_set_operation.create
        self.assertEqual(
            list(rule_set.conversion_value_rules), [rule.resource_name]
        )
        self.assertEqual(rule_set.dimensions[0].name, "GEO_LOCATION")
        self.assertEqual(rule_set.attachment_type.name, "CUSTOMER")

    def test_add_to_existing_set(self):
        set_rn = f"customers/{CID}/conversionValueRuleSets/4"
        self.search_results(
            [
                self.row(
                    {
                        "conversion_value_rule_set.resource_name": set_rn,
                        "conversion_value_rule_set.conversion_value_rules": [
                            f"customers/{CID}/conversionValueRules/1"
                        ],
                        "conversion_value_rule_set.dimensions": [
                            self.enums.ValueRuleSetDimensionEnum.GEO_LOCATION
                        ],
                    }
                )
            ]
        )
        self.propose(
            cx.manage_conversion_value_rules,
            customer_id=CID,
            action="ADD",
            value=20,
            devices=["MOBILE"],
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        update = ops[1].conversion_value_rule_set_operation
        self.assertEqual(len(update.update.conversion_value_rules), 2)
        self.assertEqual(
            [d.name for d in update.update.dimensions],
            ["GEO_LOCATION", "DEVICE"],
        )
        self.assertIn("dimensions", update.update_mask.paths)

    def test_one_condition(self):
        with self.assertRaisesRegex(ToolError, "exactly one condition"):
            cx.manage_conversion_value_rules(
                customer_id=CID,
                action="MULTIPLY",
                value=2,
                devices=["MOBILE"],
                location_ids=["1"],
            )


class TestAppCampaign(MutateToolTestCase):

    def _kwargs(self, **extra):
        kwargs = dict(
            customer_id=CID,
            name="App installs BE",
            daily_budget=30,
            app_id="com.example.app",
            app_store="GOOGLE_APP_STORE",
            location_ids=["2056"],
            headlines=["Plan je onderhoud", "Altijd een technieker"],
            descriptions=["Boek je airco-onderhoud in een paar tikken."],
            youtube_video_ids=["abcdefghijk"],
        )
        kwargs.update(extra)
        return kwargs

    def _constants(self):
        return [
            self.row({"customer.currency_code": "EUR"}),
        ]

    def test_installs_campaign(self):
        self.search_results(
            self._constants(),
            [
                self.row(
                    {
                        "geo_target_constant.id": 2056,
                        "geo_target_constant.name": "Belgium",
                    }
                )
            ],
            [],
        )
        self.propose(apps.create_app_campaign, **self._kwargs(target_cost=2))
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        campaign = ops[1].campaign_operation.create
        self.assertEqual(
            campaign.advertising_channel_type.name, "MULTI_CHANNEL"
        )
        self.assertEqual(
            campaign.advertising_channel_sub_type.name, "APP_CAMPAIGN"
        )
        self.assertEqual(
            campaign.app_campaign_setting.app_id, "com.example.app"
        )
        self.assertEqual(
            campaign.app_campaign_setting.bidding_strategy_goal_type.name,
            "OPTIMIZE_INSTALLS_TARGET_INSTALL_COST",
        )
        self.assertEqual(campaign.target_cpa.target_cpa_micros, 2_000_000)
        self.assertEqual(campaign.status.name, "PAUSED")
        ad = ops[-1].ad_group_ad_operation.create.ad.app_ad
        self.assertEqual(len(ad.headlines), 2)
        self.assertEqual(len(ad.youtube_videos), 1)

    def test_in_app_actions_need_target_and_actions(self):
        with self.assertRaisesRegex(ToolError, "IN_APP_ACTIONS needs"):
            apps.create_app_campaign(**self._kwargs(goal="IN_APP_ACTIONS"))

    def test_app_id_format(self):
        with self.assertRaisesRegex(ToolError, "numeric App Store ID"):
            apps.create_app_campaign(
                **self._kwargs(app_store="APPLE_APP_STORE")
            )


if __name__ == "__main__":
    unittest.main()
