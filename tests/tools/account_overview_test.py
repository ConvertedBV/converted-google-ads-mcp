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

"""Tests for the account overview."""

import json
import unittest

from ads_mcp.tools import account_overview as ao
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestAccountOverview(MutateToolTestCase):

    def _campaign(
        self,
        cid,
        name,
        cost,
        conversions,
        lost=0.0,
        status="ENABLED",
        primary="ELIGIBLE",
        impressions=100,
    ):
        return self.row(
            {
                "campaign.id": cid,
                "campaign.name": name,
                "campaign.status": getattr(
                    self.enums.CampaignStatusEnum, status
                ),
                "campaign.primary_status": getattr(
                    self.enums.CampaignPrimaryStatusEnum, primary
                ),
                "campaign.advertising_channel_type": self.enums.AdvertisingChannelTypeEnum.SEARCH,
                "campaign_budget.amount_micros": 50_000_000,
                "metrics.cost_micros": int(cost * 1_000_000),
                "metrics.conversions": conversions,
                "metrics.conversions_value": conversions * 100,
                "metrics.impressions": impressions,
                "metrics.search_budget_lost_impression_share": lost,
            }
        )

    def test_overview_and_flags(self):
        self.search_results(
            [
                self.row(
                    {
                        "customer.descriptive_name": "FavorCool BE",
                        "customer.currency_code": "EUR",
                        "customer.auto_tagging_enabled": False,
                    }
                )
            ],
            [
                self._campaign(1, "Search - Airco", 1200, 40, lost=0.25),
                self._campaign(2, "Search - Brand", 300, 0),
                self._campaign(3, "Oud", 0, 0, status="REMOVED", impressions=0),
                self._campaign(4, "PMax", 50, 1, primary="LIMITED"),
            ],
            [self.row({"campaign.id": 2}), self.row({"campaign.id": 2})],
        )
        result = json.loads(ao.get_account_overview(customer_id=CID))
        self.assertEqual(result["account"]["name"], "FavorCool BE")
        names = [c["name"] for c in result["campaigns"]]
        self.assertEqual(names, ["Search - Airco", "Search - Brand", "PMax"])
        airco = result["campaigns"][0]
        self.assertEqual(airco["cpa"], 30.0)
        self.assertEqual(airco["roas"], 3.33)
        self.assertEqual(result["totals"]["cost"], 1550.0)
        flags = "\n".join(result["flags"])
        self.assertIn("Auto-tagging is off", flags)
        self.assertIn("'Search - Airco' loses 25% of search impressions", flags)
        self.assertIn(
            "'Search - Brand' spent 300.00 EUR without conversions", flags
        )
        self.assertIn("'Search - Brand' has 2 disapproved", flags)
        self.assertIn("'PMax' is LIMITED", flags)
        self.assertIn("DURING LAST_30_DAYS", self.queries()[1])


if __name__ == "__main__":
    unittest.main()
