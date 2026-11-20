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

"""Tests for replace_ad_text across Search, PMax and Demand Gen."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import ad_text
from tests.tools.mutate_tool_base import CID, MutateToolTestCase

OLD = "Installatie binnen 3 - 4 weken"
NEW = "Bestel nu, klaar vóór de zomer"  # exactly 30 characters
LONG_NEW = "Nu bestellen, klaar vóór de zomer"  # 33 characters


class TestReplaceAdText(MutateToolTestCase):

    def _text(self, text, pin=None):
        asset = self.real_ga_client.get_type("AdTextAsset")
        asset.text = text
        if pin:
            asset.pinned_field = getattr(
                self.enums.ServedAssetFieldTypeEnum, pin
            )
        return asset

    def _rsa_row(self, ad_id=1):
        return self.row(
            {
                "campaign.name": "S | BE - NL/NL - SEARCH - Airco",
                "ad_group.name": "general",
                "ad_group_ad.ad.id": ad_id,
                "ad_group_ad.ad.type_": self.enums.AdTypeEnum.RESPONSIVE_SEARCH_AD,
                "ad_group_ad.ad.responsive_search_ad.headlines": [
                    self._text("{KeyWord:Airco laten plaatsen}"),
                    self._text(OLD.lower(), "HEADLINE_2"),
                    self._text("{LOCATION(City):Uw regio} airco"),
                ],
                "ad_group_ad.ad.responsive_search_ad.descriptions": [
                    self._text("Vaste prijs."),
                    self._text("Eigen installateurs."),
                ],
            }
        )

    def _demand_gen_row(self):
        return self.row(
            {
                "campaign.name": "S | BE - NL/NL - Demand_gen - airco",
                "ad_group.name": "General_airco",
                "ad_group_ad.ad.id": 3,
                "ad_group_ad.ad.type_": self.enums.AdTypeEnum.DEMAND_GEN_MULTI_ASSET_AD,
                "ad_group_ad.ad.demand_gen_multi_asset_ad.headlines": [
                    self._text(OLD),
                    self._text("Snelle plaatsing"),
                ],
            }
        )

    def _pmax_row(self, group_id, link_id, text, field="HEADLINE"):
        return self.row(
            {
                "campaign.name": "S | BE - NL/NL - P.MAX - Airco",
                "asset_group.name": f"Airco_{group_id}",
                "asset_group_asset.resource_name": (
                    f"customers/{CID}/assetGroupAssets/{group_id}~{link_id}~{field}"
                ),
                "asset_group_asset.asset_group": f"customers/{CID}/assetGroups/{group_id}",
                "asset_group_asset.field_type": getattr(
                    self.enums.AssetFieldTypeEnum, field
                ),
                "asset.text_asset.text": text,
            }
        )

    def _pmax_rows(self):
        rows = [self._pmax_row(10, 100, OLD), self._pmax_row(10, 101, "Snel")]
        # Asset group 20 already has the maximum of 15 headlines.
        rows.append(self._pmax_row(20, 200, OLD))
        rows += [self._pmax_row(20, 300 + i, f"Kop {i}") for i in range(14)]
        return rows

    def test_replaces_only_the_claim_everywhere(self):
        rsa, dg, pmax = (
            [self._rsa_row()],
            [self._demand_gen_row()],
            self._pmax_rows(),
        )
        self.search_results([*rsa, *dg], pmax, [*rsa, *dg], pmax)
        kwargs = dict(customer_id=CID, old_text=OLD, new_text=NEW)

        proposal = self.propose(ad_text.replace_ad_text, **kwargs)
        self.assertEqual(len(proposal["change_items"]), 4)
        self.assertTrue(
            all(i["value_readable"] == NEW for i in proposal["change_items"])
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        kinds = [type(op).pb(op).WhichOneof("operation") for op in ops]
        self.assertEqual(
            kinds,
            ["asset_operation"]
            + ["asset_group_asset_operation"] * 4
            + ["ad_operation"] * 2,
        )

        # One new text asset, linked in the same field.
        new_asset = ops[0].asset_operation.create
        self.assertEqual(new_asset.text_asset.text, NEW)
        self.assertEqual(
            ops[1].asset_group_asset_operation.create.asset,
            new_asset.resource_name,
        )
        self.assertEqual(
            ops[1].asset_group_asset_operation.create.field_type.name,
            "HEADLINE",
        )
        # Group 10 has room: add first. Group 20 is full: remove first.
        self.assertEqual(
            ops[2].asset_group_asset_operation.remove,
            f"customers/{CID}/assetGroupAssets/10~100~HEADLINE",
        )
        self.assertEqual(
            ops[3].asset_group_asset_operation.remove,
            f"customers/{CID}/assetGroupAssets/20~200~HEADLINE",
        )
        self.assertEqual(
            ops[4].asset_group_asset_operation.create.asset_group,
            f"customers/{CID}/assetGroups/20",
        )

        # The RSA keeps its dynamic headlines and the pin of the claim.
        rsa_update = ops[5].ad_operation
        headlines = rsa_update.update.responsive_search_ad.headlines
        self.assertEqual(
            [h.text for h in headlines],
            [
                "{KeyWord:Airco laten plaatsen}",
                NEW,
                "{LOCATION(City):Uw regio} airco",
            ],
        )
        self.assertEqual(headlines[1].pinned_field.name, "HEADLINE_2")
        self.assertEqual(
            list(rsa_update.update_mask.paths),
            ["responsive_search_ad.headlines"],
        )
        self.assertEqual(
            rsa_update.update.resource_name, f"customers/{CID}/ads/1"
        )

        dg_update = ops[6].ad_operation
        self.assertEqual(
            dg_update.update.demand_gen_multi_asset_ad.headlines[0].text, NEW
        )

        result = self.commit(ad_text.replace_ad_text, proposal, **kwargs)
        self.assertFalse(
            self.request("GoogleAdsService", "mutate").validate_only
        )
        self.assertIn("Replaced the text in 4 ads and asset groups", result)

    def test_too_long_for_search_but_fine_for_demand_gen(self):
        self.search_results([self._rsa_row(), self._demand_gen_row()], [])
        with self.assertRaisesRegex(
            ToolError, "headline max 30 characters"
        ) as ctx:
            ad_text.replace_ad_text(
                customer_id=CID, old_text=OLD, new_text=LONG_NEW
            )
        self.assertNotIn("Demand_gen", str(ctx.exception))

        self.search_results([self._demand_gen_row()])
        proposal = self.propose(
            ad_text.replace_ad_text,
            customer_id=CID,
            old_text=OLD,
            new_text=LONG_NEW,
            channels=["DEMAND_GEN"],
        )
        self.assertEqual(len(proposal["change_items"]), 1)
        self.assertIn("'DEMAND_GEN_MULTI_ASSET_AD'", self.queries()[-1])

    def test_dynamic_insertion_length(self):
        self.search_results([self._rsa_row()], [])
        proposal = self.propose(
            ad_text.replace_ad_text,
            customer_id=CID,
            old_text=OLD,
            new_text="{KeyWord:Airco} vóór de zomer",
            channels=["SEARCH"],
        )
        self.assertEqual(len(proposal["change_items"]), 1)

    def test_not_found_and_campaign_filter(self):
        self.search_results([], [])
        with self.assertRaisesRegex(ToolError, "was not found"):
            ad_text.replace_ad_text(
                customer_id=CID,
                old_text=OLD,
                new_text=NEW,
                campaign_ids=["111", "222"],
            )
        self.assertTrue(
            all("campaign.id IN (111, 222)" in q for q in self.queries())
        )

    def test_duplicate_in_same_ad(self):
        row = self._rsa_row()
        row.ad_group_ad.ad.responsive_search_ad.headlines.append(
            self._text(NEW)
        )
        self.search_results([row])
        with self.assertRaisesRegex(ToolError, "already has"):
            ad_text.replace_ad_text(
                customer_id=CID, old_text=OLD, new_text=NEW, channels=["SEARCH"]
            )


if __name__ == "__main__":
    unittest.main()
