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

"""Tests for payments accounts, billing setups and account budgets."""

import json
import unittest
from unittest.mock import MagicMock

from fastmcp.exceptions import ToolError

from ads_mcp.tools import billing as bl
from tests.tools.mutate_tool_base import CID, MutateToolTestCase

PAYMENTS_ID = "1234-5678-9012-3456"


class TestBilling(MutateToolTestCase):

    def _payments(self, currency="EUR"):
        account = self.real_ga_client.get_type("PaymentsAccount")
        account.resource_name = (
            f"customers/{CID}/paymentsAccounts/{PAYMENTS_ID}"
        )
        account.payments_account_id = PAYMENTS_ID
        account.name = "Converted facturatie"
        account.currency_code = currency
        response = self.real_ga_client.get_type("ListPaymentsAccountsResponse")
        response.payments_accounts.append(account)
        self.service(
            "PaymentsAccountService"
        ).list_payments_accounts.return_value = response

    def _currency(self):
        return [self.row({"customer.currency_code": "EUR"})]

    def test_list(self):
        self._payments()
        result = json.loads(bl.list_payments_accounts(customer_id=CID))
        self.assertEqual(
            result["payments_accounts"][0]["payments_account_id"], PAYMENTS_ID
        )

    def test_billing_setup_commit(self):
        self._payments()
        self.search_results([], self._currency())
        kwargs = dict(customer_id=CID, payments_account_id=PAYMENTS_ID)
        proposal = self.propose(bl.create_billing_setup, **kwargs)
        service = self.service("BillingSetupService")
        service.mutate_billing_setup.assert_not_called()
        self.search_results([], self._currency())
        service.mutate_billing_setup.return_value = MagicMock()
        self.commit(bl.create_billing_setup, proposal, **kwargs)
        request = self.request("BillingSetupService", "mutate_billing_setup")
        self.assertEqual(
            request.operation.create.payments_account,
            f"customers/{CID}/paymentsAccounts/{PAYMENTS_ID}",
        )
        self.assertEqual(request.operation.create.start_time_type.name, "NOW")

    def test_billing_setup_refusals(self):
        self._payments()
        with self.assertRaisesRegex(ToolError, "not available"):
            bl.create_billing_setup(customer_id=CID, payments_account_id="9")
        self.search_results(
            [
                self.row(
                    {
                        "billing_setup.id": 5,
                        "billing_setup.status": self.enums.BillingSetupStatusEnum.APPROVED,
                    }
                )
            ]
        )
        with self.assertRaisesRegex(ToolError, "already has a billing setup"):
            bl.create_billing_setup(
                customer_id=CID, payments_account_id=PAYMENTS_ID
            )
        self._payments("USD")
        self.search_results([], self._currency())
        with self.assertRaisesRegex(ToolError, "USD"):
            bl.create_billing_setup(
                customer_id=CID, payments_account_id=PAYMENTS_ID
            )

    def test_create_budget(self):
        self.search_results(
            self._currency(),
            [
                self.row(
                    {
                        "billing_setup.status": self.enums.BillingSetupStatusEnum.APPROVED
                    }
                )
            ],
        )
        self.propose(
            bl.manage_account_budget,
            customer_id=CID,
            operation="CREATE",
            billing_setup_id="5",
            spending_limit=3000,
            end_date="2026-12-31",
        )
        request = self.request(
            "AccountBudgetProposalService", "mutate_account_budget_proposal"
        )
        self.assertTrue(request.validate_only)
        proposal = request.operation.create
        self.assertEqual(proposal.proposal_type.name, "CREATE")
        self.assertEqual(proposal.proposed_spending_limit_micros, 3_000_000_000)
        self.assertEqual(proposal.proposed_end_date_time, "2026-12-31 23:59:59")

    def _budget(self, limit=1_000_000_000, served=200_000_000):
        return [
            self.row(
                {
                    "account_budget.name": "Q4",
                    "account_budget.status": self.enums.AccountBudgetStatusEnum.APPROVED,
                    "account_budget.approved_spending_limit_micros": limit,
                    "account_budget.amount_served_micros": served,
                }
            )
        ]

    def test_update_budget_capped(self):
        self.search_results(self._currency(), self._budget())
        with self.assertRaisesRegex(ToolError, "safety limit"):
            bl.manage_account_budget(
                customer_id=CID,
                operation="UPDATE",
                account_budget_id="7",
                spending_limit=5000,
            )
        self.search_results(self._currency(), self._budget())
        with self.assertRaisesRegex(ToolError, "already spent"):
            bl.manage_account_budget(
                customer_id=CID,
                operation="UPDATE",
                account_budget_id="7",
                spending_limit=100,
            )

    def test_update_budget(self):
        self.search_results(self._currency(), self._budget())
        self.propose(
            bl.manage_account_budget,
            customer_id=CID,
            operation="UPDATE",
            account_budget_id="7",
            spending_limit=1500,
        )
        request = self.request(
            "AccountBudgetProposalService", "mutate_account_budget_proposal"
        )
        self.assertEqual(
            list(request.operation.update_mask.paths),
            ["proposed_spending_limit_micros"],
        )
        self.assertEqual(
            request.operation.create.account_budget,
            f"customers/{CID}/accountBudgets/7",
        )


if __name__ == "__main__":
    unittest.main()
