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

"""Tests for account scope, blocked tools, budget ceiling, change history,
location search, policy exemptions and the broad-match guard."""

import importlib
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

from ads_mcp import safety
from ads_mcp.config import ToolsConfig
from ads_mcp.coordinator import initialize_and_mount_tools
from ads_mcp.tools import account_overview as ao
from ads_mcp.tools import campaign_management as cm
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestAccountScope(unittest.IsolatedAsyncioTestCase):

    async def _call(self, arguments):
        context = SimpleNamespace(message=SimpleNamespace(arguments=arguments))
        call_next = AsyncMock(return_value="ok")
        result = await safety.AccountScopeMiddleware().on_call_tool(
            context, call_next
        )
        return result, call_next

    async def test_unrestricted_without_setting(self):
        with patch.dict(os.environ, {}, clear=True):
            result, call_next = await self._call({"customer_id": "999"})
        self.assertEqual(result, "ok")
        call_next.assert_awaited_once()

    async def test_refuses_accounts_outside_the_list(self):
        env = {safety.ACCOUNT_IDS_ENV_VAR: "501-813-6849, 1601002093"}
        with patch.dict(os.environ, env, clear=True):
            result, _ = await self._call(
                {
                    "customer_id": "5018136849",
                    "login_customer_id": "160-100-2093",
                }
            )
            self.assertEqual(result, "ok")
            with self.assertRaisesRegex(
                ToolError, "1234567890 \\(customer_id\\)"
            ):
                await self._call({"customer_id": "1234567890"})
            with self.assertRaisesRegex(ToolError, "login_customer_id"):
                await self._call(
                    {"customer_id": "5018136849", "login_customer_id": "42"}
                )


class TestBlockedTools(unittest.IsolatedAsyncioTestCase):

    async def test_blocked_tools_are_not_mounted(self):
        sub = FastMCP("labels")

        @sub.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def keep() -> str:
            return "a"

        @sub.tool(annotations=ToolAnnotations(readOnlyHint=False))
        def drop() -> str:
            return "b"

        real_import = importlib.import_module
        parent = FastMCP("Test")
        with (
            patch.dict(
                os.environ, {safety.BLOCKED_TOOLS_ENV_VAR: "labels_drop"}
            ),
            patch("pkgutil.iter_modules", return_value=[(None, "fake", False)]),
            patch(
                "importlib.import_module",
                side_effect=lambda name, *a, **k: (
                    SimpleNamespace(sub=sub)
                    if name == "ads_mcp.tools.fake"
                    else real_import(name, *a, **k)
                ),
            ),
            patch(
                "ads_mcp.config.ToolsConfig.load", return_value=ToolsConfig()
            ),
        ):
            initialize_and_mount_tools(parent)
        self.assertEqual(
            [t.name for t in await parent.list_tools()], ["labels_keep"]
        )


class TestBudgetCeiling(MutateToolTestCase):

    def test_ceiling_blocks_budget_updates(self):
        os.environ[safety.MAX_DAILY_BUDGET_ENV_VAR] = "100"
        row = self.row(
            {
                "campaign.name": "Generic",
                "campaign_budget.resource_name": f"customers/{CID}/campaignBudgets/5",
                "campaign_budget.amount_micros": 80_000_000,
                "customer.currency_code": "EUR",
            }
        )
        self.search_results([row])
        with self.assertRaisesRegex(ToolError, "ceiling of 100.00"):
            cm.update_campaign_budget(
                customer_id=CID, campaign_id="111", daily_budget=120
            )
        self.search_results([row])
        proposal = self.propose(
            cm.update_campaign_budget,
            customer_id=CID,
            campaign_id="111",
            daily_budget=100,
        )
        self.assertEqual(proposal["status"], "PROPOSED")

    def test_ceiling_blocks_new_campaigns(self):
        from ads_mcp.tools import campaign_builder as cb

        os.environ[safety.MAX_DAILY_BUDGET_ENV_VAR] = "50"
        with self.assertRaisesRegex(ToolError, "exceeds this server's ceiling"):
            cb.create_search_campaign(
                customer_id=CID,
                name="X",
                daily_budget=75,
                bidding_strategy="MAXIMIZE_CLICKS",
                location_ids=["2056"],
                ad_groups=[],
            )

    def tearDown(self):
        os.environ.pop(safety.MAX_DAILY_BUDGET_ENV_VAR, None)
        super().tearDown()


class TestChangeHistoryAndLocations(MutateToolTestCase):

    def test_change_history(self):
        field_mask = self.real_ga_client.get_type("ChangeEvent").changed_fields
        row = self.row(
            {
                "change_event.change_date_time": "2026-11-20 10:00:00.000000",
                "change_event.change_resource_type": self.enums.ChangeEventResourceTypeEnum.CAMPAIGN_BUDGET,
                "change_event.resource_change_operation": self.enums.ResourceChangeOperationEnum.UPDATE,
                "change_event.user_email": "seppe@converted.be",
                "change_event.client_type": self.enums.ChangeClientTypeEnum.GOOGLE_ADS_API,
                "campaign.name": "Generic",
            }
        )
        row.change_event.changed_fields.paths.append("amount_micros")
        self.search_results([row])
        result = json.loads(
            ao.get_change_history(
                customer_id=CID, days=7, campaign_id="111", limit=50
            )
        )
        change = result["changes"][0]
        self.assertEqual(change["resource_type"], "CAMPAIGN_BUDGET")
        self.assertEqual(change["changed_fields"], ["amount_micros"])
        self.assertEqual(change["via"], "GOOGLE_ADS_API")
        query = self.queries()[0]
        self.assertIn("AND campaign.id = 111", query)
        self.assertIn("LIMIT 50", query)
        self.assertIn("ORDER BY change_event.change_date_time DESC", query)
        with self.assertRaisesRegex(ToolError, "1-30"):
            ao.get_change_history(customer_id=CID, days=45)
        del field_mask

    def test_find_locations(self):
        response = self.real_ga_client.get_type(
            "SuggestGeoTargetConstantsResponse"
        )
        suggestion = self.real_ga_client.get_type("GeoTargetConstantSuggestion")
        suggestion.search_term = "Gent"
        suggestion.reach = 450000
        suggestion.geo_target_constant.id = 1001021
        suggestion.geo_target_constant.canonical_name = "Ghent,Flanders,Belgium"
        suggestion.geo_target_constant.target_type = "City"
        suggestion.geo_target_constant.country_code = "BE"
        response.geo_target_constant_suggestions.append(suggestion)
        self.service(
            "GeoTargetConstantService"
        ).suggest_geo_target_constants.return_value = response
        result = json.loads(ao.find_locations(customer_id=CID, names=["Gent"]))
        self.assertEqual(result["locations"][0]["id"], 1001021)
        self.assertEqual(result["locations"][0]["type"], "City")
        request = self.request(
            "GeoTargetConstantService", "suggest_geo_target_constants"
        )
        self.assertEqual(request.country_code, "BE")
        self.assertEqual(request.locale, "nl")
        self.assertEqual(list(request.location_names.names), ["Gent"])


class TestPolicyExemption(MutateToolTestCase):

    def _exception(self, exemptible=True):
        failure = self.real_ga_client.get_type("GoogleAdsFailure")
        error = self.real_ga_client.get_type("GoogleAdsError")
        error.message = "A policy was violated."
        element = self.real_ga_client.get_type(
            "ErrorLocation"
        ).FieldPathElement()
        element.field_name = "operations"
        element.index = 1
        error.location.field_path_elements.append(element)
        details = error.details.policy_violation_details
        details.external_policy_name = "Healthcare and medicines"
        details.key.policy_name = "HEALTH_IN_PERSONALIZED_ADS"
        details.key.violating_text = "botox"
        details.is_exemptible = exemptible
        failure.errors.append(error)
        return GoogleAdsException(None, MagicMock(), failure, "req-1")

    def _kwargs(self, **extra):
        return dict(
            customer_id=CID,
            ad_group_id="456",
            keywords=["huidverzorging", "botox"],
            match_type="PHRASE",
            **extra,
        )

    def test_without_flag_the_proposal_names_the_keywords(self):
        service = self.service("AdGroupCriterionService")
        service.mutate_ad_group_criteria.side_effect = self._exception()
        with self.assertRaisesRegex(
            ToolError,
            "'botox' \\(HEALTH_IN_PERSONALIZED_ADS\\).*request_policy_exemption",
        ):
            cm.add_keywords(**self._kwargs())

    def test_with_flag_the_exemption_is_attached(self):
        service = self.service("AdGroupCriterionService")
        response = MagicMock()
        response.results = [
            MagicMock(resource_name=f"customers/{CID}/adGroupCriteria/456~1")
        ]
        exc = self._exception()
        # Propose: fail, then succeed with the key. Commit: same, then commit.
        service.mutate_ad_group_criteria.side_effect = [
            exc,
            None,
            exc,
            None,
            response,
        ]
        kwargs = self._kwargs(request_policy_exemption=True)
        proposal = self.propose(cm.add_keywords, **kwargs)
        self.assertIn("Policy exemption", json.dumps(proposal))
        request = self.request(
            "AdGroupCriterionService", "mutate_ad_group_criteria"
        )
        keys = request.operations[1].exempt_policy_violation_keys
        self.assertEqual(keys[0].policy_name, "HEALTH_IN_PERSONALIZED_ADS")
        self.assertEqual(
            len(request.operations[0].exempt_policy_violation_keys), 0
        )
        result = self.commit(cm.add_keywords, proposal, **kwargs)
        self.assertIn("Successfully added 2 keywords", result)

    def test_non_exemptible_is_refused(self):
        service = self.service("AdGroupCriterionService")
        service.mutate_ad_group_criteria.side_effect = self._exception(
            exemptible=False
        )
        with self.assertRaisesRegex(ToolError, "Failed to add keywords"):
            cm.add_keywords(**self._kwargs(request_policy_exemption=True))


class TestBroadMatchGuard(MutateToolTestCase):

    def test_broad_on_manual_cpc_is_refused(self):
        self.search_results(
            [
                self.row(
                    {
                        "campaign.bidding_strategy_type": self.enums.BiddingStrategyTypeEnum.MANUAL_CPC
                    }
                )
            ]
        )
        with self.assertRaisesRegex(ToolError, "BROAD match on a manual CPC"):
            cm.add_keywords(
                customer_id=CID,
                ad_group_id="456",
                keywords=["airco"],
                match_type="BROAD",
            )

    def test_builder_refuses_broad_with_manual_cpc(self):
        from ads_mcp.tools import campaign_builder as cb

        group = {
            "name": "A",
            "keywords": ["airco"],
            "match_type": "BROAD",
            "cpc_bid": 1,
            "ads": [
                {
                    "headlines": ["Een", "Twee", "Drie"],
                    "descriptions": ["Een.", "Twee."],
                    "final_url": "https://a.be",
                }
            ],
        }
        with self.assertRaisesRegex(ToolError, "BROAD match with MANUAL_CPC"):
            cb.create_search_campaign(
                customer_id=CID,
                name="X",
                daily_budget=10,
                bidding_strategy="MANUAL_CPC",
                location_ids=["2056"],
                ad_groups=[group],
            )


if __name__ == "__main__":
    unittest.main()
