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

"""Tests for brand lists, custom segments and account-level negatives."""

import json
import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import account_maintenance as am
from ads_mcp.tools import brand_controls as bc
from ads_mcp.tools import custom_segments as cs
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestBrandLists(MutateToolTestCase):

    def _campaign(self, cid: int, channel: str):
        return self.row(
            {
                "campaign.id": cid,
                "campaign.name": f"Campaign {cid}",
                "campaign.advertising_channel_type": getattr(
                    self.enums.AdvertisingChannelTypeEnum, channel
                ),
            }
        )

    def test_suggest_brands(self):
        service = self.service("BrandSuggestionService")
        response = self.real_ga_client.get_type("SuggestBrandsResponse")
        brand = self.real_ga_client.get_type("BrandSuggestion")
        brand.id = "brand-123"
        brand.name = "Daikin"
        brand.urls.append("daikin.be")
        response.brands.append(brand)
        service.suggest_brands.return_value = response
        result = json.loads(
            bc.suggest_brands(customer_id=CID, brand_prefix=" daik ")
        )
        self.assertEqual(result["brands"][0]["brand_id"], "brand-123")
        request = service.suggest_brands.call_args.kwargs["request"]
        self.assertEqual(request.brand_prefix, "daik")

    def test_create_list_and_exclude_on_pmax(self):
        campaign = self._campaign(111, "PERFORMANCE_MAX")
        self.search_results([campaign], [campaign])
        proposal = self.propose(
            bc.manage_brand_list,
            customer_id=CID,
            operation="CREATE_LIST",
            name="Eigen merk",
            brand_ids=["brand-1", "brand-1", "brand-2"],
            campaign_ids=["111"],
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        self.assertEqual(len(ops), 4)
        self.assertEqual(
            ops[0].shared_set_operation.create.type_.name, "BRANDS"
        )
        self.assertEqual(
            ops[1].shared_criterion_operation.create.brand.entity_id,
            "brand-1",
        )
        attach = ops[3].campaign_criterion_operation.create
        self.assertTrue(attach.negative)
        self.assertEqual(
            attach.brand_list.shared_set, f"customers/{CID}/sharedSets/-1"
        )
        self.assertIn("Brand list exclusion", json.dumps(proposal))

    def test_restrict_only_on_search(self):
        self.search_results(
            [self._campaign(111, "PERFORMANCE_MAX")],
            [self._campaign(111, "PERFORMANCE_MAX")],
        )
        with self.assertRaisesRegex(ToolError, "only on Search"):
            bc.manage_brand_list(
                customer_id=CID,
                operation="CREATE_LIST",
                name="Merk",
                brand_ids=["brand-1"],
                campaign_ids=["111"],
                use="RESTRICT",
            )

    def test_wrong_set_type_refused(self):
        self.search_results(
            [
                self.row(
                    {
                        "shared_set.name": "Negatives",
                        "shared_set.type_": self.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS,
                    }
                )
            ]
        )
        with self.assertRaisesRegex(ToolError, "not a brand list"):
            bc.manage_brand_list(
                customer_id=CID,
                operation="ADD_BRANDS",
                shared_set_id="5",
                brand_ids=["brand-1"],
            )

    def test_detach_requires_attachment(self):
        self.search_results(
            [
                self.row(
                    {
                        "shared_set.name": "Merk",
                        "shared_set.type_": self.enums.SharedSetTypeEnum.BRANDS,
                    }
                )
            ],
            [self._campaign(111, "SEARCH")],
            [],
        )
        with self.assertRaisesRegex(ToolError, "not attached"):
            bc.manage_brand_list(
                customer_id=CID,
                operation="DETACH_CAMPAIGNS",
                shared_set_id="5",
                campaign_ids=["111"],
            )


class TestCustomSegments(MutateToolTestCase):

    def test_create(self):
        proposal = self.propose(
            cs.manage_custom_segment,
            customer_id=CID,
            name="Airco zoekers",
            segment_type="SEARCH",
            keywords=["Airco installateur", "airco installateur", "warmtepomp"],
            urls=["concurrent.be"],
        )
        request = self.request(
            "CustomAudienceService", "mutate_custom_audiences"
        )
        audience = request.operations[0].create
        self.assertEqual(audience.type_.name, "SEARCH")
        members = [
            (m.member_type.name, m.keyword or m.url) for m in audience.members
        ]
        self.assertEqual(
            members,
            [
                ("KEYWORD", "airco installateur"),
                ("KEYWORD", "warmtepomp"),
                ("URL", "https://concurrent.be"),
            ],
        )
        self.assertIn("Custom segment", json.dumps(proposal))

    def test_replace_members(self):
        current = self.row(
            {
                "custom_audience.name": "Oud",
                "custom_audience.type_": self.enums.CustomAudienceTypeEnum.AUTO,
            }
        )
        self.search_results([current])
        self.propose(
            cs.manage_custom_segment,
            customer_id=CID,
            operation="REPLACE_MEMBERS",
            custom_audience_id="77",
            apps=["com.example.app"],
        )
        op = self.request(
            "CustomAudienceService", "mutate_custom_audiences"
        ).operations[0]
        self.assertEqual(list(op.update_mask.paths), ["members"])
        self.assertEqual(op.update.members[0].app, "com.example.app")

    def test_new_segments_auto_or_search(self):
        with self.assertRaisesRegex(ToolError, "AUTO or SEARCH"):
            cs.manage_custom_segment(
                customer_id=CID,
                name="Oud type",
                segment_type="INTEREST",
                keywords=["airco"],
            )

    def test_needs_members(self):
        with self.assertRaisesRegex(ToolError, "keywords"):
            cs.manage_custom_segment(customer_id=CID, name="Leeg")


class TestAccountLevelNegatives(MutateToolTestCase):

    def test_create_account_level_list(self):
        self.propose(
            am.manage_shared_negative_list,
            customer_id=CID,
            operation="CREATE_LIST",
            name="Account negatives",
            keywords=["gratis", "vacature"],
            account_level=True,
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        self.assertEqual(
            ops[0].shared_set_operation.create.type_.name,
            "ACCOUNT_LEVEL_NEGATIVE_KEYWORDS",
        )
        self.assertEqual(
            ops[
                1
            ].customer_negative_criterion_operation.create.negative_keyword_list.shared_set,
            f"customers/{CID}/sharedSets/-1",
        )
        self.assertEqual(len(ops), 4)

    def test_account_level_list_not_per_campaign(self):
        with self.assertRaisesRegex(ToolError, "every campaign"):
            am.manage_shared_negative_list(
                customer_id=CID,
                operation="CREATE_LIST",
                name="X",
                campaign_ids=["111"],
                account_level=True,
            )
        self.search_results(
            [
                self.row(
                    {
                        "shared_set.name": "Account negatives",
                        "shared_set.type_": self.enums.SharedSetTypeEnum.ACCOUNT_LEVEL_NEGATIVE_KEYWORDS,
                    }
                )
            ]
        )
        with self.assertRaisesRegex(ToolError, "account-level list"):
            am.manage_shared_negative_list(
                customer_id=CID,
                operation="ATTACH_CAMPAIGNS",
                shared_set_id="5",
                campaign_ids=["111"],
            )


if __name__ == "__main__":
    unittest.main()
