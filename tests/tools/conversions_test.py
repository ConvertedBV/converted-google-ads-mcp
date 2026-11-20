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

"""Tests for the conversion tracking tools."""

import json
import unittest
from unittest.mock import MagicMock

from fastmcp.exceptions import ToolError

from ads_mcp.tools import conversions as cv
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestConversionSetup(MutateToolTestCase):

    def _action(
        self, action_id, name, category, primary=True, counting="ONE_PER_CLICK"
    ):
        return self.row(
            {
                "conversion_action.resource_name": f"customers/{CID}/conversionActions/{action_id}",
                "conversion_action.id": action_id,
                "conversion_action.name": name,
                "conversion_action.category": getattr(
                    self.enums.ConversionActionCategoryEnum, category
                ),
                "conversion_action.origin": self.enums.ConversionOriginEnum.WEBSITE,
                "conversion_action.type_": self.enums.ConversionActionTypeEnum.WEBPAGE,
                "conversion_action.primary_for_goal": primary,
                "conversion_action.counting_type": getattr(
                    self.enums.ConversionActionCountingTypeEnum, counting
                ),
            }
        )

    def _goal(self, category, biddable):
        return self.row(
            {
                "customer_conversion_goal.category": getattr(
                    self.enums.ConversionActionCategoryEnum, category
                ),
                "customer_conversion_goal.origin": self.enums.ConversionOriginEnum.WEBSITE,
                "customer_conversion_goal.biddable": biddable,
            }
        )

    def test_overview_and_warnings(self):
        stats = self.row(
            {
                "segments.conversion_action": f"customers/{CID}/conversionActions/1",
                "metrics.all_conversions": 42.0,
            }
        )
        self.search_results(
            [
                self._action(1, "Offerte aanvraag", "REQUEST_QUOTE"),
                self._action(
                    2,
                    "Contactformulier",
                    "SUBMIT_LEAD_FORM",
                    counting="MANY_PER_CLICK",
                ),
                # Primary, but its goal is not biddable: no warnings.
                self._action(
                    3, "Nieuwsbrief", "SIGNUP", counting="MANY_PER_CLICK"
                ),
            ],
            [stats],
            [
                self._goal("REQUEST_QUOTE", True),
                self._goal("SUBMIT_LEAD_FORM", True),
                self._goal("SIGNUP", False),
                self._goal("PURCHASE", True),
            ],
        )
        result = json.loads(cv.get_conversion_setup(customer_id=CID))
        actions = {a["name"]: a for a in result["conversion_actions"]}
        self.assertEqual(
            actions["Offerte aanvraag"]["conversions_last_30_days"], 42.0
        )
        self.assertEqual(
            actions["Contactformulier"]["conversions_last_30_days"], 0.0
        )
        self.assertTrue(actions["Contactformulier"]["used_for_bidding"])
        self.assertFalse(actions["Nieuwsbrief"]["used_for_bidding"])
        warnings = "\n".join(result["warnings"])
        self.assertIn(
            "'Contactformulier' is used for bidding but recorded no", warnings
        )
        self.assertIn(
            "'Contactformulier' is used for bidding and counts every", warnings
        )
        self.assertNotIn("Nieuwsbrief", warnings)
        self.assertIn(
            "PURCHASE / WEBSITE is used for bidding but has no", warnings
        )
        self.assertNotIn("Offerte aanvraag' recorded", warnings)
        self.assertIn("DURING LAST_30_DAYS", self.queries()[1])

    def test_no_primary_actions(self):
        self.search_results(
            [self._action(1, "Pageview", "PAGE_VIEW", primary=False)], [], []
        )
        result = json.loads(
            cv.get_conversion_setup(customer_id=CID, period="LAST_7_DAYS")
        )
        self.assertIn(
            "No enabled primary conversion actions", result["warnings"][-1]
        )


class TestConversionActions(MutateToolTestCase):

    def test_create_webpage_action_returns_send_to(self):
        currency = [self.row({"customer.currency_code": "EUR"})]
        snippet = self.real_ga_client.get_type("TagSnippet")
        snippet.event_snippet = (
            "<script>gtag('event', 'conversion', {'send_to': "
            "'AW-123456789/AbCdEfGh'});</script>"
        )
        tag_row = self.row({"conversion_action.tag_snippets": [snippet]})
        self.search_results(currency, currency, [tag_row])
        service = self.service("ConversionActionService")
        response = MagicMock()
        response.results = [
            MagicMock(resource_name=f"customers/{CID}/conversionActions/77")
        ]
        service.mutate_conversion_actions.return_value = response

        kwargs = dict(
            customer_id=CID,
            name="Offerte aanvraag",
            category="REQUEST_QUOTE",
            default_value=50,
        )
        proposal = self.propose(cv.create_conversion_action, **kwargs)
        items = self.items(proposal)
        self.assertEqual(items["Default value"]["value_readable"], "50.00 EUR")
        self.assertEqual(
            items["Primary (used for bidding)"]["value_readable"], "Yes"
        )
        action = (
            self.request("ConversionActionService", "mutate_conversion_actions")
            .operations[0]
            .create
        )
        self.assertEqual(action.type_.name, "WEBPAGE")
        self.assertEqual(action.counting_type.name, "ONE_PER_CLICK")
        self.assertEqual(action.value_settings.default_currency_code, "EUR")
        self.assertEqual(
            action.attribution_model_settings.attribution_model.name,
            "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
        )

        result = self.commit(cv.create_conversion_action, proposal, **kwargs)
        self.assertIn("(ID 77)", result)
        self.assertIn("send_to = AW-123456789/AbCdEfGh", result)
        self.assertIn("Conversion ID 123456789", result)
        self.assertIn("Conversion Label AbCdEfGh", result)

    def test_create_validation(self):
        with self.assertRaisesRegex(ToolError, "1-90 days"):
            cv.create_conversion_action(
                customer_id=CID,
                name="X",
                category="PURCHASE",
                click_through_lookback_days=120,
            )

    def test_update_to_secondary(self):
        self.search_results(
            [
                self.row(
                    {
                        "conversion_action.name": "Pageview",
                        "conversion_action.primary_for_goal": True,
                        "conversion_action.counting_type": self.enums.ConversionActionCountingTypeEnum.MANY_PER_CLICK,
                    }
                )
            ]
        )
        proposal = self.propose(
            cv.update_conversion_action,
            customer_id=CID,
            conversion_action_id="9",
            primary_for_goal=False,
            counting_type="ONE_PER_CLICK",
        )
        items = self.items(proposal)
        self.assertEqual(
            items["Primary for goal"]["original_value_readable"], "True"
        )
        self.assertEqual(
            items["Counting type"]["original_value_readable"], "MANY_PER_CLICK"
        )
        op = self.request(
            "ConversionActionService", "mutate_conversion_actions"
        ).operations[0]
        self.assertFalse(op.update.primary_for_goal)
        self.assertEqual(op.update.counting_type.name, "ONE_PER_CLICK")
        self.assertEqual(
            list(op.update_mask.paths), ["primary_for_goal", "counting_type"]
        )


class TestConversionGoals(MutateToolTestCase):

    def test_account_goals(self):
        self.search_results(
            [
                self.row(
                    {
                        "customer_conversion_goal.category": self.enums.ConversionActionCategoryEnum.PAGE_VIEW,
                        "customer_conversion_goal.origin": self.enums.ConversionOriginEnum.WEBSITE,
                        "customer_conversion_goal.biddable": True,
                    }
                )
            ]
        )
        proposal = self.propose(
            cv.set_conversion_goals,
            customer_id=CID,
            goals=[{"category": "page_view", "biddable": False}],
        )
        self.assertEqual(
            proposal["change_items"][0]["original_value_readable"], "Yes"
        )
        op = self.request(
            "CustomerConversionGoalService", "mutate_customer_conversion_goals"
        ).operations[0]
        self.assertEqual(
            op.update.resource_name,
            f"customers/{CID}/customerConversionGoals/PAGE_VIEW~WEBSITE",
        )
        self.assertFalse(op.update.biddable)

    def test_campaign_goal_and_missing_goal(self):
        row = self.row(
            {
                "campaign_conversion_goal.category": self.enums.ConversionActionCategoryEnum.REQUEST_QUOTE,
                "campaign_conversion_goal.origin": self.enums.ConversionOriginEnum.WEBSITE,
                "campaign_conversion_goal.biddable": False,
                "campaign.name": "Generic",
            }
        )
        self.search_results([row], [row])
        self.propose(
            cv.set_conversion_goals,
            customer_id=CID,
            campaign_id="111",
            goals=[{"category": "REQUEST_QUOTE", "biddable": True}],
        )
        op = self.request(
            "CampaignConversionGoalService", "mutate_campaign_conversion_goals"
        ).operations[0]
        self.assertEqual(
            op.update.resource_name,
            f"customers/{CID}/campaignConversionGoals/111~REQUEST_QUOTE~WEBSITE",
        )
        with self.assertRaisesRegex(ToolError, "PURCHASE / WEBSITE"):
            cv.set_conversion_goals(
                customer_id=CID,
                campaign_id="111",
                goals=[{"category": "PURCHASE", "biddable": True}],
            )


if __name__ == "__main__":
    unittest.main()
