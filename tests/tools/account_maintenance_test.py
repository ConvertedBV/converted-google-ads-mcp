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

"""Tests for shared lists, asset links, recommendations and RSA edits."""

import unittest
from unittest.mock import MagicMock

from fastmcp.exceptions import ToolError

from ads_mcp.tools import account_maintenance as am
from tests.tools.mutate_tool_base import CID, MutateToolTestCase

AD = {
    "headlines": ["Nieuwe kop", "Tweede kop", "Derde kop"],
    "descriptions": ["Beschrijving een.", "Beschrijving twee."],
    "final_url": "https://example.be/nieuw",
}


class TestSharedNegativeLists(MutateToolTestCase):

    def _set(self, set_type="NEGATIVE_KEYWORDS"):
        return self.row(
            {
                "shared_set.name": "Merk-uitsluitingen",
                "shared_set.type_": getattr(
                    self.enums.SharedSetTypeEnum, set_type
                ),
            }
        )

    def test_create_list_atomically(self):
        campaign = self.row({"campaign.id": 111, "campaign.name": "Generic"})
        # One lookup for the proposal and one for the commit call.
        self.search_results([campaign], [campaign])
        service = self.service("GoogleAdsService")
        service.mutate.return_value = self.mutate_response(
            ("shared_set_result", f"customers/{CID}/sharedSets/900"),
            (
                "shared_criterion_result",
                f"customers/{CID}/sharedCriteria/900~1",
            ),
            (
                "shared_criterion_result",
                f"customers/{CID}/sharedCriteria/900~2",
            ),
            (
                "campaign_shared_set_result",
                f"customers/{CID}/campaignSharedSets/111~900",
            ),
        )
        kwargs = dict(
            customer_id=CID,
            operation="CREATE_LIST",
            name="Vacatures",
            keywords=["vacature", "job"],
            match_type="PHRASE",
            campaign_ids=["111"],
        )
        proposal = self.propose(am.manage_shared_negative_list, **kwargs)
        self.assertEqual(len(proposal["change_items"]), 4)
        request = self.request("GoogleAdsService", "mutate")
        self.assertTrue(request.validate_only)
        ops = request.mutate_operations
        temp_rn = f"customers/{CID}/sharedSets/-1"
        self.assertEqual(
            ops[0].shared_set_operation.create.resource_name, temp_rn
        )
        self.assertEqual(
            ops[0].shared_set_operation.create.type_.name, "NEGATIVE_KEYWORDS"
        )
        self.assertEqual(
            ops[1].shared_criterion_operation.create.shared_set, temp_rn
        )
        self.assertEqual(
            ops[2].shared_criterion_operation.create.keyword.match_type.name,
            "PHRASE",
        )
        self.assertEqual(
            ops[3].campaign_shared_set_operation.create.shared_set, temp_rn
        )

        result = self.commit(am.manage_shared_negative_list, proposal, **kwargs)
        self.assertFalse(
            self.request("GoogleAdsService", "mutate").validate_only
        )
        self.assertIn("- shared_set: 900", result)
        self.assertIn("- shared_criterion: 900~1, 900~2", result)

    def test_add_keywords_requires_negative_list(self):
        self.search_results([self._set("NEGATIVE_PLACEMENTS")])
        with self.assertRaisesRegex(ToolError, "not a negative keyword list"):
            am.manage_shared_negative_list(
                customer_id=CID,
                operation="ADD_KEYWORDS",
                shared_set_id="900",
                keywords=["gratis"],
            )

    def test_detach_campaigns(self):
        self.search_results(
            [self._set()],
            [self.row({"campaign.id": 111, "campaign.name": "Generic"})],
        )
        self.propose(
            am.manage_shared_negative_list,
            customer_id=CID,
            operation="DETACH_CAMPAIGNS",
            shared_set_id="900",
            campaign_ids=["111"],
        )
        op = self.request(
            "CampaignSharedSetService", "mutate_campaign_shared_sets"
        ).operations[0]
        self.assertEqual(
            op.remove, f"customers/{CID}/campaignSharedSets/111~900"
        )

    def test_remove_keyword_not_in_list(self):
        self.search_results([self._set()], [])
        with self.assertRaisesRegex(ToolError, "Not in list 900"):
            am.manage_shared_negative_list(
                customer_id=CID,
                operation="REMOVE_KEYWORDS",
                shared_set_id="900",
                criterion_ids=["5"],
            )


class TestLinkAssets(MutateToolTestCase):

    def _asset(self, asset_id: str, text: str):
        return self.row(
            {
                "asset.resource_name": f"customers/{CID}/assets/{asset_id}",
                "asset.sitelink_asset.link_text": text,
            }
        )

    def test_link_sitelinks_to_campaign(self):
        self.search_results(
            [self._asset("1", "Contact"), self._asset("2", "Prijzen")]
        )
        proposal = self.propose(
            am.link_assets,
            customer_id=CID,
            operation="LINK",
            level="CAMPAIGN",
            parent_id="111",
            asset_ids=["1", "2"],
            field_type="SITELINK",
        )
        self.assertEqual(
            proposal["change_items"][0]["value_readable"], "Contact (1)"
        )
        op = self.request(
            "CampaignAssetService", "mutate_campaign_assets"
        ).operations[0]
        self.assertEqual(op.create.campaign, f"customers/{CID}/campaigns/111")
        self.assertEqual(op.create.asset, f"customers/{CID}/assets/1")
        self.assertEqual(op.create.field_type.name, "SITELINK")

    def test_unlink_customer_asset(self):
        self.search_results([self._asset("1", "Contact")])
        self.propose(
            am.link_assets,
            customer_id=CID,
            operation="UNLINK",
            level="CUSTOMER",
            asset_ids=["1"],
            field_type="SITELINK",
        )
        op = self.request(
            "CustomerAssetService", "mutate_customer_assets"
        ).operations[0]
        self.assertEqual(
            op.remove, f"customers/{CID}/customerAssets/1~SITELINK"
        )

    def test_missing_asset(self):
        self.search_results([])
        with self.assertRaisesRegex(ToolError, "Assets not found"):
            am.link_assets(
                customer_id=CID,
                operation="LINK",
                level="AD_GROUP",
                parent_id="5",
                asset_ids=["1"],
                field_type="CALLOUT",
            )


class TestRecommendations(MutateToolTestCase):

    def _rec(self, token: str, dismissed=False):
        return self.row(
            {
                "recommendation.resource_name": f"customers/{CID}/recommendations/{token}",
                "recommendation.type_": self.enums.RecommendationTypeEnum.KEYWORD,
                "recommendation.dismissed": dismissed,
                "campaign.name": "Generic",
            }
        )

    def test_apply_has_no_dry_run_but_needs_confirmation(self):
        self.search_results([self._rec("abc~1")], [self._rec("abc~1")])
        service = self.service("RecommendationService")
        response = MagicMock()
        response.results = [
            MagicMock(resource_name=f"customers/{CID}/recommendations/abc~1")
        ]
        service.apply_recommendation.return_value = response

        kwargs = dict(
            customer_id=CID,
            operation="APPLY",
            recommendation_ids=[f"customers/{CID}/recommendations/abc~1"],
        )
        proposal = self.propose(am.manage_recommendations, **kwargs)
        self.assertEqual(
            proposal["change_items"][0]["value_readable"],
            "KEYWORD (campaign: Generic)",
        )
        service.apply_recommendation.assert_not_called()

        result = self.commit(am.manage_recommendations, proposal, **kwargs)
        request = self.request("RecommendationService", "apply_recommendation")
        self.assertEqual(
            request.operations[0].resource_name,
            f"customers/{CID}/recommendations/abc~1",
        )
        self.assertIn("Applied 1 recommendations", result)

    def test_dismiss_commit(self):
        self.search_results([self._rec("abc")], [self._rec("abc")])
        service = self.service("RecommendationService")
        response = MagicMock()
        response.results = [
            MagicMock(resource_name=f"customers/{CID}/recommendations/abc")
        ]
        service.dismiss_recommendation.return_value = response
        kwargs = dict(
            customer_id=CID, operation="DISMISS", recommendation_ids=["abc"]
        )
        proposal = self.propose(am.manage_recommendations, **kwargs)
        result = self.commit(am.manage_recommendations, proposal, **kwargs)
        request = self.request(
            "RecommendationService", "dismiss_recommendation"
        )
        self.assertEqual(
            request.operations[0].resource_name,
            f"customers/{CID}/recommendations/abc",
        )
        self.assertIn("Dismissed 1 recommendations", result)

    def test_dismissed_recommendation_refused(self):
        self.search_results([self._rec("abc", dismissed=True)])
        with self.assertRaisesRegex(ToolError, "not open"):
            am.manage_recommendations(
                customer_id=CID, operation="DISMISS", recommendation_ids=["abc"]
            )

    def test_injection_refused(self):
        with self.assertRaisesRegex(ToolError, "Invalid recommendation_id"):
            am.manage_recommendations(
                customer_id=CID,
                operation="APPLY",
                recommendation_ids=["abc') OR ('1'='1"],
            )


class TestUpdateResponsiveSearchAd(MutateToolTestCase):

    def _ad_row(self, ad_type="RESPONSIVE_SEARCH_AD"):
        headline = self.real_ga_client.get_type("AdTextAsset")
        headline.text = "Oude kop"
        return self.row(
            {
                "ad_group_ad.ad.type_": getattr(self.enums.AdTypeEnum, ad_type),
                "ad_group_ad.ad.final_urls": ["https://example.be/oud"],
                "ad_group_ad.ad.responsive_search_ad.headlines": [headline],
                "ad_group.name": "Onderhoud",
            }
        )

    def test_update(self):
        self.search_results([self._ad_row()])
        proposal = self.propose(
            am.update_responsive_search_ad, customer_id=CID, ad_id="42", ad=AD
        )
        items = self.items(proposal)
        self.assertEqual(
            items["Headlines"]["original_value_readable"], "Oude kop"
        )
        self.assertEqual(
            items["Final URL"]["value_readable"], "https://example.be/nieuw"
        )
        op = self.request("AdService", "mutate_ads").operations[0]
        self.assertEqual(op.update.resource_name, f"customers/{CID}/ads/42")
        self.assertEqual(len(op.update.responsive_search_ad.headlines), 3)
        self.assertIn("responsive_search_ad.headlines", op.update_mask.paths)

    def test_non_rsa_refused(self):
        self.search_results([self._ad_row("EXPANDED_TEXT_AD")])
        with self.assertRaisesRegex(ToolError, "not a responsive search ad"):
            am.update_responsive_search_ad(customer_id=CID, ad_id="42", ad=AD)


if __name__ == "__main__":
    unittest.main()
