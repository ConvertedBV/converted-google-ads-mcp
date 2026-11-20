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

"""Tests for remove_entities and revert_change."""

import unittest

from fastmcp.exceptions import ToolError
from google.protobuf import field_mask_pb2

from ads_mcp.tools import removal as rm
from tests.tools.mutate_tool_base import CID, MutateToolTestCase

CAMPAIGN_RN = f"customers/{CID}/campaigns/111"
BUDGET_RN = f"customers/{CID}/campaignBudgets/5"
CHANGE_ID = f"customers/{CID}/changeEvents/1700000000000000~0~0"


class TestRemoveEntities(MutateToolTestCase):

    def _campaign(self, status: str):
        return self.row(
            {
                "campaign.resource_name": CAMPAIGN_RN,
                "campaign.name": "Oud",
                "campaign.status": getattr(
                    self.enums.CampaignStatusEnum, status
                ),
                "customer.currency_code": "EUR",
            }
        )

    def test_remove_paused_campaign_shows_spend(self):
        self.search_results(
            [self._campaign("PAUSED")],
            [
                self.row(
                    {
                        "campaign.resource_name": CAMPAIGN_RN,
                        "metrics.cost_micros": 5_000_000,
                        "metrics.conversions": 2,
                    }
                )
            ],
        )
        proposal = self.propose(
            rm.remove_entities,
            customer_id=CID,
            entity_type="CAMPAIGN",
            entity_ids=["111"],
        )
        item = proposal["change_items"][0]
        self.assertEqual(item["operation"], "REMOVE")
        self.assertIn("5.00 EUR spend, 2 conversions", item["value_readable"])
        op = self.request("GoogleAdsService", "mutate").mutate_operations[0]
        self.assertEqual(op.campaign_operation.remove, CAMPAIGN_RN)

    def test_enabled_campaign_refused(self):
        self.search_results([self._campaign("ENABLED")])
        with self.assertRaisesRegex(ToolError, "Pause these first"):
            rm.remove_entities(
                customer_id=CID, entity_type="CAMPAIGN", entity_ids=["111"]
            )
        self.service("GoogleAdsService").mutate.assert_not_called()

    def test_keyword_any_status(self):
        rn = f"customers/{CID}/adGroupCriteria/456~789"
        self.search_results(
            [
                self.row(
                    {
                        "ad_group_criterion.resource_name": rn,
                        "ad_group_criterion.keyword.text": "airco",
                        "customer.currency_code": "EUR",
                    }
                )
            ],
            [],
        )
        self.propose(
            rm.remove_entities,
            customer_id=CID,
            entity_type="KEYWORD",
            entity_ids=["456~789"],
        )
        self.assertIn("ad_group_criterion.type = 'KEYWORD'", self.queries()[0])
        op = self.request("GoogleAdsService", "mutate").mutate_operations[0]
        self.assertEqual(op.ad_group_criterion_operation.remove, rn)

    def test_missing_refused(self):
        self.search_results([])
        with self.assertRaisesRegex(ToolError, "Not found as removable"):
            rm.remove_entities(
                customer_id=CID, entity_type="AD", entity_ids=["1~2"]
            )


class TestRevertChange(MutateToolTestCase):

    def _event(self, operation="UPDATE", old=30_000_000, new=20_000_000):
        row = self.row(
            {
                "change_event.resource_name": CHANGE_ID,
                "change_event.change_date_time": "2026-11-18 10:00:00",
                "change_event.change_resource_type": (
                    self.enums.ChangeEventResourceTypeEnum.CAMPAIGN_BUDGET
                ),
                "change_event.change_resource_name": BUDGET_RN,
                "change_event.resource_change_operation": getattr(
                    self.enums.ResourceChangeOperationEnum, operation
                ),
                "change_event.old_resource.campaign_budget.amount_micros": old,
                "change_event.new_resource.campaign_budget.amount_micros": new,
            }
        )
        row.change_event.changed_fields = field_mask_pb2.FieldMask(
            paths=["amount_micros"]
        )
        return row

    def _current(self, amount):
        return self.row({"campaign_budget.amount_micros": amount})

    def test_restores_previous_budget(self):
        self.search_results([self._event()], [self._current(20_000_000)])
        proposal = self.propose(
            rm.revert_change, customer_id=CID, change_id=CHANGE_ID
        )
        item = self.items(proposal)["amount_micros"]
        self.assertEqual(item["value_readable"], "30000000")
        self.assertEqual(item["original_value_readable"], "20000000")
        op = self.request("GoogleAdsService", "mutate").mutate_operations[0]
        update = op.campaign_budget_operation.update
        self.assertEqual(update.resource_name, BUDGET_RN)
        self.assertEqual(update.amount_micros, 30_000_000)
        self.assertEqual(
            list(op.campaign_budget_operation.update_mask.paths),
            ["amount_micros"],
        )
        self.assertIn(
            "campaign_budget.amount_micros FROM campaign_budget",
            self.queries()[1],
        )

    def test_large_increase_capped(self):
        self.search_results(
            [self._event(old=80_000_000)], [self._current(20_000_000)]
        )
        with self.assertRaisesRegex(ToolError, "safety limit"):
            rm.revert_change(customer_id=CID, change_id=CHANGE_ID)

    def test_changed_again_refused(self):
        self.search_results([self._event()], [self._current(25_000_000)])
        with self.assertRaisesRegex(ToolError, "changed again"):
            rm.revert_change(customer_id=CID, change_id=CHANGE_ID)

    def test_create_and_remove_refused(self):
        for operation, message in (
            ("CREATE", "created the entity"),
            ("REMOVE", "cannot be restored"),
        ):
            with self.subTest(operation=operation):
                self.search_results([self._event(operation)])
                with self.assertRaisesRegex(ToolError, message):
                    rm.revert_change(customer_id=CID, change_id=CHANGE_ID)

    def test_bad_change_id(self):
        with self.assertRaisesRegex(ToolError, "must look like"):
            rm.revert_change(
                customer_id=CID, change_id="customers/999/changeEvents/1"
            )

    def _campaign_event(self, path, old, new):
        values = {
            "change_event.resource_name": CHANGE_ID,
            "change_event.change_resource_type": (
                self.enums.ChangeEventResourceTypeEnum.CAMPAIGN
            ),
            "change_event.change_resource_name": CAMPAIGN_RN,
            "change_event.resource_change_operation": (
                self.enums.ResourceChangeOperationEnum.UPDATE
            ),
        }
        if old is not None:
            values[f"change_event.old_resource.campaign.{path}"] = old
        if new is not None:
            values[f"change_event.new_resource.campaign.{path}"] = new
        row = self.row(values)
        row.change_event.changed_fields = field_mask_pb2.FieldMask(paths=[path])
        return row

    def test_unlisted_field_refused(self):
        self.search_results(
            [self._campaign_event("campaign_budget", BUDGET_RN, None)]
        )
        with self.assertRaisesRegex(
            ToolError, "does not restore.*campaign_budget"
        ):
            rm.revert_change(customer_id=CID, change_id=CHANGE_ID)
        self.assertEqual(len(self.queries()), 1)

    def test_removing_target_refused(self):
        path = "maximize_conversions.target_cpa_micros"
        self.search_results(
            [self._campaign_event(path, None, 40_000_000)],
            [self.row({f"campaign.{path}": 40_000_000})],
        )
        with self.assertRaisesRegex(ToolError, "remove the target_cpa_micros"):
            rm.revert_change(customer_id=CID, change_id=CHANGE_ID)

    def test_restore_status(self):
        self.search_results(
            [
                self._campaign_event(
                    "status",
                    self.enums.CampaignStatusEnum.PAUSED,
                    self.enums.CampaignStatusEnum.ENABLED,
                )
            ],
            [
                self.row(
                    {"campaign.status": self.enums.CampaignStatusEnum.ENABLED}
                )
            ],
        )
        proposal = self.propose(
            rm.revert_change, customer_id=CID, change_id=CHANGE_ID
        )
        self.assertEqual(
            self.items(proposal)["status"]["value_readable"], "PAUSED"
        )
        op = self.request("GoogleAdsService", "mutate").mutate_operations[0]
        self.assertEqual(op.campaign_operation.update.status.name, "PAUSED")


if __name__ == "__main__":
    unittest.main()
