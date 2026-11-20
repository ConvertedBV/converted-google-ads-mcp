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

"""Test cases for the campaign management tools."""

import json
import os
import unittest
from unittest.mock import MagicMock, create_autospec, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v26.services.services.ad_group_criterion_service import (
    AdGroupCriterionServiceClient,
)
from google.ads.googleads.v26.services.services.campaign_budget_service import (
    CampaignBudgetServiceClient,
)
from google.ads.googleads.v26.services.services.campaign_criterion_service import (
    CampaignCriterionServiceClient,
)
from google.ads.googleads.v26.services.services.campaign_service import (
    CampaignServiceClient,
)
from google.ads.googleads.v26.services.services.google_ads_service import (
    GoogleAdsServiceClient,
)
from google.auth.credentials import AnonymousCredentials

from ads_mcp.tools import campaign_management as cm

CID = "1234567890"


def _mutate_response(*resource_names: str) -> MagicMock:
    response = MagicMock()
    response.results = [MagicMock(resource_name=rn) for rn in resource_names]
    return response


class TestCampaignManagement(unittest.TestCase):
    """Test cases for the campaign management tools."""

    @classmethod
    def setUpClass(cls):
        cls.real_ga_client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="fake-token",
            use_proto_plus=True,
            version="v26",
        )

    def setUp(self):
        self.services = {
            "GoogleAdsService": create_autospec(
                GoogleAdsServiceClient, instance=True
            ),
            "CampaignService": create_autospec(
                CampaignServiceClient, instance=True
            ),
            "CampaignBudgetService": create_autospec(
                CampaignBudgetServiceClient, instance=True
            ),
            "AdGroupCriterionService": create_autospec(
                AdGroupCriterionServiceClient, instance=True
            ),
            "CampaignCriterionService": create_autospec(
                CampaignCriterionServiceClient, instance=True
            ),
        }
        self.mock_client = MagicMock()
        self.mock_client.get_service.side_effect = (
            lambda name, **kwargs: self.services[name]
        )
        self.mock_client.get_type.side_effect = self.real_ga_client.get_type
        self.mock_client.enums = self.real_ga_client.enums

        patcher = patch(
            "ads_mcp.utils.get_googleads_client", return_value=self.mock_client
        )
        self.mock_get_client = patcher.start()
        self.addCleanup(patcher.stop)

        env_patcher = patch.dict(os.environ, {}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop(cm.MAX_INCREASE_PCT_ENV_VAR, None)

    def _row(self) -> object:
        return self.real_ga_client.get_type("GoogleAdsRow")

    def _set_search_rows(self, rows: list) -> None:
        self.services["GoogleAdsService"].search.return_value = rows

    def _search_query(self) -> str:
        return self.services["GoogleAdsService"].search.call_args.kwargs[
            "query"
        ]

    # ------------------------------------------------------------------
    # update_entity_status
    # ------------------------------------------------------------------

    def _campaign_row(self, campaign_id: str, name: str, status: str):
        row = self._row()
        row.campaign.resource_name = f"customers/{CID}/campaigns/{campaign_id}"
        row.campaign.name = name
        row.campaign.status = getattr(
            self.real_ga_client.enums.CampaignStatusEnum, status
        )
        return row

    def test_pause_campaigns_propose_and_commit(self):
        self._set_search_rows(
            [
                self._campaign_row("111", "Brand", "ENABLED"),
                self._campaign_row("222", "Generic", "ENABLED"),
            ]
        )
        service = self.services["CampaignService"]
        service.mutate_campaigns.return_value = _mutate_response(
            f"customers/{CID}/campaigns/111", f"customers/{CID}/campaigns/222"
        )

        proposal = json.loads(
            cm.update_entity_status(
                customer_id="123-456-7890",
                entity_type="CAMPAIGN",
                entity_ids=["111", "222", "111"],
                status="PAUSED",
                login_customer_id="999",
            )
        )
        self.mock_get_client.assert_called_with(login_customer_id="999")
        self.assertEqual(proposal["status"], "PROPOSED")
        self.assertEqual(len(proposal["change_items"]), 2)
        first = proposal["change_items"][0]
        self.assertEqual(first["value_readable"], "PAUSED")
        self.assertEqual(first["original_value_readable"], "ENABLED")
        self.assertIn("Brand", first["primary_key_readable"])
        self.assertIn(
            "campaign.resource_name IN "
            f"('customers/{CID}/campaigns/111', "
            f"'customers/{CID}/campaigns/222')",
            self._search_query(),
        )

        request = service.mutate_campaigns.call_args.kwargs["request"]
        self.assertTrue(request.validate_only)
        self.assertEqual(len(request.operations), 2)
        op = request.operations[0]
        self.assertEqual(op.update.status.name, "PAUSED")
        self.assertEqual(list(op.update_mask.paths), ["status"])

        result = cm.update_entity_status(
            customer_id=CID,
            entity_type="CAMPAIGN",
            entity_ids=["111", "222"],
            status="PAUSED",
            validate_only=False,
            checksum=proposal["checksum"],
        )
        commit_request = service.mutate_campaigns.call_args.kwargs["request"]
        self.assertFalse(commit_request.validate_only)
        self.assertIn("Successfully set 2 CAMPAIGN entities to PAUSED", result)
        self.assertIn("111, 222", result)

    def test_commit_with_wrong_checksum_is_rejected(self):
        self._set_search_rows([self._campaign_row("111", "Brand", "ENABLED")])
        with self.assertRaisesRegex(ToolError, "mismatched checksum"):
            cm.update_entity_status(
                customer_id=CID,
                entity_type="CAMPAIGN",
                entity_ids=["111"],
                status="PAUSED",
                validate_only=False,
                checksum="0" * 64,
            )
        self.services["CampaignService"].mutate_campaigns.assert_not_called()

    def test_status_change_for_missing_entity_fails(self):
        self._set_search_rows([self._campaign_row("111", "Brand", "ENABLED")])
        with self.assertRaisesRegex(ToolError, "not found.*campaigns/999"):
            cm.update_entity_status(
                customer_id=CID,
                entity_type="CAMPAIGN",
                entity_ids=["111", "999"],
                status="PAUSED",
            )

    def test_keyword_status_requires_composite_id(self):
        with self.assertRaisesRegex(ToolError, "ad_group_id.*criterion_id"):
            cm.update_entity_status(
                customer_id=CID,
                entity_type="KEYWORD",
                entity_ids=["123"],
                status="PAUSED",
            )

    def test_removed_status_is_not_allowed(self):
        with self.assertRaisesRegex(ToolError, "Invalid status"):
            cm.update_entity_status(
                customer_id=CID,
                entity_type="CAMPAIGN",
                entity_ids=["111"],
                status="REMOVED",
            )

    def test_injection_in_ids_is_rejected(self):
        with self.assertRaisesRegex(ToolError, "Invalid ID"):
            cm.update_entity_status(
                customer_id=CID,
                entity_type="CAMPAIGN",
                entity_ids=["111') OR ('1'='1"],
                status="PAUSED",
            )
        self.services["GoogleAdsService"].search.assert_not_called()

    # ------------------------------------------------------------------
    # update_campaign_budget
    # ------------------------------------------------------------------

    def _budget_row(self, amount_micros: int, shared: bool = False):
        row = self._row()
        row.campaign.name = "Generic"
        row.campaign_budget.resource_name = (
            f"customers/{CID}/campaignBudgets/555"
        )
        row.campaign_budget.amount_micros = amount_micros
        row.campaign_budget.explicitly_shared = shared
        row.campaign_budget.reference_count = 3 if shared else 1
        row.customer.currency_code = "EUR"
        return row

    def test_update_campaign_budget(self):
        self._set_search_rows([self._budget_row(40_000_000, shared=True)])
        service = self.services["CampaignBudgetService"]
        service.mutate_campaign_budgets.return_value = _mutate_response(
            f"customers/{CID}/campaignBudgets/555"
        )

        proposal = json.loads(
            cm.update_campaign_budget(
                customer_id=CID, campaign_id="111", daily_budget=50.004
            )
        )
        items = {i["field_readable"]: i for i in proposal["change_items"]}
        self.assertEqual(items["Daily budget"]["value_readable"], "50.00 EUR")
        self.assertEqual(
            items["Daily budget"]["original_value_readable"], "40.00 EUR"
        )
        self.assertIn("3 campaigns", items["Shared budget"]["value_readable"])

        request = service.mutate_campaign_budgets.call_args.kwargs["request"]
        self.assertTrue(request.validate_only)
        op = request.operations[0]
        self.assertEqual(op.update.amount_micros, 50_000_000)
        self.assertEqual(list(op.update_mask.paths), ["amount_micros"])

        result = cm.update_campaign_budget(
            customer_id=CID,
            campaign_id="111",
            daily_budget=50.004,
            validate_only=False,
            checksum=proposal["checksum"],
        )
        self.assertIn("50.00 EUR", result)

    def test_budget_increase_guardrail(self):
        self._set_search_rows([self._budget_row(40_000_000)])
        with self.assertRaisesRegex(ToolError, "150% higher.*100% safety"):
            cm.update_campaign_budget(
                customer_id=CID, campaign_id="111", daily_budget=100
            )
        self.services[
            "CampaignBudgetService"
        ].mutate_campaign_budgets.assert_not_called()

        os.environ[cm.MAX_INCREASE_PCT_ENV_VAR] = "off"
        proposal = json.loads(
            cm.update_campaign_budget(
                customer_id=CID, campaign_id="111", daily_budget=100
            )
        )
        self.assertEqual(proposal["status"], "PROPOSED")

    def test_budget_decrease_is_always_allowed(self):
        os.environ[cm.MAX_INCREASE_PCT_ENV_VAR] = "10"
        self._set_search_rows([self._budget_row(40_000_000)])
        proposal = json.loads(
            cm.update_campaign_budget(
                customer_id=CID, campaign_id="111", daily_budget=5
            )
        )
        self.assertEqual(proposal["status"], "PROPOSED")

    def test_budget_for_unknown_campaign_fails(self):
        self._set_search_rows([])
        with self.assertRaisesRegex(ToolError, "Campaign 111 was not found"):
            cm.update_campaign_budget(
                customer_id=CID, campaign_id="111", daily_budget=10
            )

    def test_budget_must_be_positive(self):
        with self.assertRaisesRegex(ToolError, "greater than 0"):
            cm.update_campaign_budget(
                customer_id=CID, campaign_id="111", daily_budget=0
            )

    # ------------------------------------------------------------------
    # update_cpc_bid
    # ------------------------------------------------------------------

    def test_update_keyword_cpc_bid(self):
        row = self._row()
        row.ad_group_criterion.keyword.text = "waterverzachter kopen"
        row.ad_group_criterion.cpc_bid_micros = 1_000_000
        row.customer.currency_code = "EUR"
        self._set_search_rows([row])
        service = self.services["AdGroupCriterionService"]
        service.mutate_ad_group_criteria.return_value = _mutate_response(
            f"customers/{CID}/adGroupCriteria/456~789"
        )

        proposal = json.loads(
            cm.update_cpc_bid(
                customer_id=CID,
                entity_type="KEYWORD",
                entity_id="456~789",
                cpc_bid=1.5,
            )
        )
        item = proposal["change_items"][0]
        self.assertEqual(item["value_readable"], "1.50 EUR")
        self.assertEqual(item["original_value_readable"], "1.00 EUR")
        self.assertIn("waterverzachter kopen", item["primary_key_readable"])
        self.assertIn(
            f"ad_group_criterion.resource_name = "
            f"'customers/{CID}/adGroupCriteria/456~789'",
            self._search_query(),
        )
        op = service.mutate_ad_group_criteria.call_args.kwargs[
            "request"
        ].operations[0]
        self.assertEqual(op.update.cpc_bid_micros, 1_500_000)
        self.assertEqual(list(op.update_mask.paths), ["cpc_bid_micros"])

    def test_cpc_bid_guardrail(self):
        row = self._row()
        row.ad_group.name = "Ad group"
        row.ad_group.cpc_bid_micros = 1_000_000
        row.customer.currency_code = "EUR"
        self._set_search_rows([row])
        with self.assertRaisesRegex(ToolError, "safety limit"):
            cm.update_cpc_bid(
                customer_id=CID,
                entity_type="AD_GROUP",
                entity_id="456",
                cpc_bid=15,
            )

    # ------------------------------------------------------------------
    # add_keywords
    # ------------------------------------------------------------------

    def test_add_keywords(self):
        service = self.services["AdGroupCriterionService"]
        service.mutate_ad_group_criteria.return_value = _mutate_response(
            f"customers/{CID}/adGroupCriteria/456~1",
            f"customers/{CID}/adGroupCriteria/456~2",
        )

        proposal = json.loads(
            cm.add_keywords(
                customer_id=CID,
                ad_group_id="456",
                keywords=[
                    "Boiler  onderhoud",
                    "boiler prijs",
                    "Boiler onderhoud",
                    " ",
                ],
                match_type="PHRASE",
                cpc_bid=2,
                status="PAUSED",
            )
        )
        keywords = [
            i["value_readable"]
            for i in proposal["change_items"]
            if i["field_readable"] == "Keyword"
        ]
        self.assertEqual(keywords, ["Boiler onderhoud", "boiler prijs"])

        request = service.mutate_ad_group_criteria.call_args.kwargs["request"]
        self.assertTrue(request.validate_only)
        self.assertEqual(len(request.operations), 2)
        criterion = request.operations[0].create
        self.assertEqual(criterion.ad_group, f"customers/{CID}/adGroups/456")
        self.assertEqual(criterion.keyword.match_type.name, "PHRASE")
        self.assertEqual(criterion.status.name, "PAUSED")
        self.assertEqual(criterion.cpc_bid_micros, 2_000_000)
        self.assertFalse(criterion.negative)

    def test_add_keywords_requires_keywords(self):
        with self.assertRaisesRegex(ToolError, "at least one keyword"):
            cm.add_keywords(
                customer_id=CID,
                ad_group_id="456",
                keywords=["  "],
                match_type="EXACT",
            )

    # ------------------------------------------------------------------
    # manage_negative_keywords
    # ------------------------------------------------------------------

    def test_add_campaign_negative_keywords(self):
        service = self.services["CampaignCriterionService"]
        service.mutate_campaign_criteria.return_value = _mutate_response(
            f"customers/{CID}/campaignCriteria/111~9"
        )

        proposal = json.loads(
            cm.manage_negative_keywords(
                customer_id=CID,
                level="CAMPAIGN",
                parent_id="111",
                operation="CREATE",
                keywords=["gratis"],
            )
        )
        self.assertEqual(proposal["status"], "PROPOSED")
        criterion = (
            service.mutate_campaign_criteria.call_args.kwargs["request"]
            .operations[0]
            .create
        )
        self.assertTrue(criterion.negative)
        self.assertEqual(criterion.campaign, f"customers/{CID}/campaigns/111")
        self.assertEqual(criterion.keyword.text, "gratis")
        self.assertEqual(criterion.keyword.match_type.name, "EXACT")

        result = cm.manage_negative_keywords(
            customer_id=CID,
            level="CAMPAIGN",
            parent_id="111",
            operation="CREATE",
            keywords=["gratis"],
            validate_only=False,
            checksum=proposal["checksum"],
        )
        self.assertIn("added 1 negative keywords", result)

    def test_remove_negative_keyword_only_touches_negatives(self):
        # The verification query returns nothing, e.g. because the criterion
        # is a positive keyword: nothing may be removed.
        self._set_search_rows([])
        with self.assertRaisesRegex(ToolError, "not negative keywords"):
            cm.manage_negative_keywords(
                customer_id=CID,
                level="AD_GROUP",
                parent_id="456",
                operation="REMOVE",
                criterion_ids=["789"],
            )
        query = self._search_query()
        self.assertIn("ad_group_criterion.negative = TRUE", query)
        self.assertIn("ad_group_criterion.type = 'KEYWORD'", query)
        self.services[
            "AdGroupCriterionService"
        ].mutate_ad_group_criteria.assert_not_called()

    def test_remove_negative_keyword(self):
        row = self._row()
        row.ad_group_criterion.resource_name = (
            f"customers/{CID}/adGroupCriteria/456~789"
        )
        row.ad_group_criterion.keyword.text = "vacature"
        row.ad_group_criterion.keyword.match_type = (
            self.real_ga_client.enums.KeywordMatchTypeEnum.PHRASE
        )
        self._set_search_rows([row])

        proposal = json.loads(
            cm.manage_negative_keywords(
                customer_id=CID,
                level="AD_GROUP",
                parent_id="456",
                operation="REMOVE",
                criterion_ids=["789"],
            )
        )
        items = {i["field_readable"]: i for i in proposal["change_items"]}
        self.assertEqual(items["Keyword"]["value_readable"], "vacature")
        self.assertEqual(items["Match type"]["value_readable"], "PHRASE")
        op = (
            self.services["AdGroupCriterionService"]
            .mutate_ad_group_criteria.call_args.kwargs["request"]
            .operations[0]
        )
        self.assertEqual(op.remove, f"customers/{CID}/adGroupCriteria/456~789")

    def test_negative_keyword_argument_mixups_are_rejected(self):
        with self.assertRaisesRegex(ToolError, "only used for REMOVE"):
            cm.manage_negative_keywords(
                customer_id=CID,
                level="CAMPAIGN",
                parent_id="111",
                operation="CREATE",
                keywords=["gratis"],
                criterion_ids=["1"],
            )
        with self.assertRaisesRegex(ToolError, "criterion_ids"):
            cm.manage_negative_keywords(
                customer_id=CID,
                level="CAMPAIGN",
                parent_id="111",
                operation="REMOVE",
                keywords=["gratis"],
            )


class TestMaxIncreasePct(unittest.TestCase):
    """Parsing of the GOOGLE_ADS_MCP_MAX_INCREASE_PCT guardrail setting."""

    def test_values(self):
        cases = {
            None: 100.0,
            "": 100.0,
            "50": 50.0,
            "off": None,
            "0": None,
        }
        for raw, expected in cases.items():
            env = {} if raw is None else {cm.MAX_INCREASE_PCT_ENV_VAR: raw}
            with self.subTest(raw=raw), patch.dict(os.environ, env, clear=True):
                self.assertEqual(cm.get_max_increase_pct(), expected)

    def test_invalid_value(self):
        with patch.dict(
            os.environ, {cm.MAX_INCREASE_PCT_ENV_VAR: "lots"}, clear=True
        ):
            with self.assertRaises(ToolError):
                cm.get_max_increase_pct()


if __name__ == "__main__":
    unittest.main()
