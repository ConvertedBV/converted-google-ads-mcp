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

"""Tests for data exclusions and seasonality adjustments."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import bidding_adjustments as ba
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestBiddingAdjustments(MutateToolTestCase):

    def test_data_exclusion_for_broken_tracking(self):
        proposal = self.propose(
            ba.manage_bidding_adjustments,
            customer_id=CID,
            kind="DATA_EXCLUSION",
            operation="CREATE",
            name="Tag Bereken_Airco kapot",
            start="2026-11-01",
            end="2026-11-03",
            campaign_ids=["111", "222"],
        )
        items = self.items(proposal)
        self.assertEqual(
            items["Period"]["value_readable"],
            "2026-11-01 00:00:00 - 2026-11-03 23:59:59",
        )
        item = (
            self.request(
                "BiddingDataExclusionService", "mutate_bidding_data_exclusions"
            )
            .operations[0]
            .create
        )
        self.assertEqual(item.scope.name, "CAMPAIGN")
        self.assertEqual(
            list(item.campaigns),
            [
                f"customers/{CID}/campaigns/111",
                f"customers/{CID}/campaigns/222",
            ],
        )

    def test_seasonality_for_channel(self):
        self.propose(
            ba.manage_bidding_adjustments,
            customer_id=CID,
            kind="SEASONALITY_ADJUSTMENT",
            operation="CREATE",
            name="Black Friday",
            start="2026-11-27 00:00:00",
            end="2026-11-30",
            channels=["SEARCH", "PERFORMANCE_MAX"],
            conversion_rate_modifier=1.5,
        )
        item = (
            self.request(
                "BiddingSeasonalityAdjustmentService",
                "mutate_bidding_seasonality_adjustments",
            )
            .operations[0]
            .create
        )
        self.assertEqual(item.scope.name, "CHANNEL")
        self.assertEqual(
            [c.name for c in item.advertising_channel_types],
            ["SEARCH", "PERFORMANCE_MAX"],
        )
        self.assertEqual(item.conversion_rate_modifier, 1.5)

    def test_validation(self):
        base = dict(
            customer_id=CID,
            kind="SEASONALITY_ADJUSTMENT",
            operation="CREATE",
            name="X",
            start="2026-12-01",
            end="2026-12-05",
            conversion_rate_modifier=1.2,
        )
        cases = {
            "at most 14 days": {"end": "2026-12-20"},
            "must be after": {"end": "2026-11-30"},
            "0.1-10": {"conversion_rate_modifier": 20},
            "is required for a seasonality": {"conversion_rate_modifier": None},
            "either 'campaign_ids' or 'channels'": {
                "campaign_ids": ["1"],
                "channels": ["SEARCH"],
            },
            "only applies to SEASONALITY_ADJUSTMENT": {
                "kind": "DATA_EXCLUSION"
            },
            "must be 'YYYY-MM-DD'": {"start": "01/12/2026"},
        }
        for message, override in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    ba.manage_bidding_adjustments(**{**base, **override})

    def test_remove(self):
        self.search_results(
            [
                self.row(
                    {
                        "bidding_data_exclusion.name": "Tag kapot",
                        "bidding_data_exclusion.start_date_time": "2026-11-01 00:00:00",
                        "bidding_data_exclusion.end_date_time": "2026-11-03 23:59:59",
                    }
                )
            ]
        )
        self.propose(
            ba.manage_bidding_adjustments,
            customer_id=CID,
            kind="DATA_EXCLUSION",
            operation="REMOVE",
            adjustment_id="55",
        )
        op = self.request(
            "BiddingDataExclusionService", "mutate_bidding_data_exclusions"
        ).operations[0]
        self.assertEqual(op.remove, f"customers/{CID}/biddingDataExclusions/55")


if __name__ == "__main__":
    unittest.main()
