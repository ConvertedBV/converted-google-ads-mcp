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

"""Tests for client accounts, user invitations and Merchant Center links."""

import json
import unittest
from unittest.mock import MagicMock

from fastmcp.exceptions import ToolError

from ads_mcp.tools import account_admin as aa
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestCreateClientAccount(MutateToolTestCase):

    def _manager(self, manager=True):
        return [
            self.row(
                {
                    "customer.manager": manager,
                    "customer.descriptive_name": "Converted MCC",
                }
            )
        ]

    def test_propose_and_commit(self):
        self.search_results(self._manager(), [])
        kwargs = dict(customer_id=CID, name="  Klant   BV ")
        proposal = self.propose(aa.create_client_account, **kwargs)
        self.assertIn("cannot change later", json.dumps(proposal))
        request = self.request("CustomerService", "create_customer_client")
        self.assertTrue(request.validate_only)
        self.assertEqual(request.customer_client.descriptive_name, "Klant BV")
        self.assertEqual(request.customer_client.currency_code, "EUR")
        self.assertEqual(request.customer_client.time_zone, "Europe/Brussels")

        self.search_results(self._manager(), [])
        self.service("CustomerService").create_customer_client.return_value = (
            MagicMock(resource_name="customers/555")
        )
        result = self.commit(aa.create_client_account, proposal, **kwargs)
        self.assertIn("(555)", result)
        self.assertFalse(
            self.request(
                "CustomerService", "create_customer_client"
            ).validate_only
        )

    def test_not_a_manager(self):
        self.search_results(self._manager(False))
        with self.assertRaisesRegex(ToolError, "not a manager"):
            aa.create_client_account(customer_id=CID, name="Klant")

    def test_duplicate_name(self):
        self.search_results(
            self._manager(),
            [
                self.row(
                    {
                        "customer_client.id": 777,
                        "customer_client.descriptive_name": "klant bv",
                    }
                )
            ],
        )
        with self.assertRaisesRegex(ToolError, "already has an account"):
            aa.create_client_account(customer_id=CID, name="Klant BV")

    def test_validation(self):
        with self.assertRaisesRegex(ToolError, "currency_code"):
            aa.create_client_account(
                customer_id=CID, name="X", currency_code="euro"
            )
        with self.assertRaisesRegex(ToolError, "time_zone"):
            aa.create_client_account(
                customer_id=CID, name="X", time_zone="CET; DROP"
            )


class TestInviteUser(MutateToolTestCase):

    def test_commit_only_after_confirm(self):
        kwargs = dict(customer_id=CID, email_address=" An@Example.be ")
        proposal = self.propose(aa.invite_user, **kwargs)
        service = self.service("CustomerUserAccessInvitationService")
        service.mutate_customer_user_access_invitation.assert_not_called()
        service.mutate_customer_user_access_invitation.return_value = (
            MagicMock()
        )
        self.commit(aa.invite_user, proposal, **kwargs)
        request = self.request(
            "CustomerUserAccessInvitationService",
            "mutate_customer_user_access_invitation",
        )
        self.assertEqual(
            request.operation.create.email_address, "an@example.be"
        )
        self.assertEqual(request.operation.create.access_role.name, "STANDARD")

    def test_bad_email(self):
        with self.assertRaisesRegex(ToolError, "email_address"):
            aa.invite_user(customer_id=CID, email_address="geen-mail")


class TestMerchantCenterLink(MutateToolTestCase):

    def test_request_link(self):
        self.search_results([])
        kwargs = dict(
            customer_id=CID, action="REQUEST", merchant_center_id="999"
        )
        proposal = self.propose(aa.manage_merchant_center_link, **kwargs)
        self.search_results([])
        service = self.service("ProductLinkInvitationService")
        service.create_product_link_invitation.return_value = MagicMock(
            resource_name=f"customers/{CID}/productLinkInvitations/1"
        )
        self.commit(aa.manage_merchant_center_link, proposal, **kwargs)
        request = self.request(
            "ProductLinkInvitationService", "create_product_link_invitation"
        )
        self.assertEqual(
            request.product_link_invitation.merchant_center.merchant_center_id,
            999,
        )

    def test_already_linked(self):
        self.search_results(
            [self.row({"product_link.merchant_center.merchant_center_id": 999})]
        )
        with self.assertRaisesRegex(ToolError, "already linked"):
            aa.manage_merchant_center_link(
                customer_id=CID, action="REQUEST", merchant_center_id="999"
            )

    def test_accept_pending_only(self):
        self.search_results(
            [
                self.row(
                    {
                        "product_link_invitation.status": self.enums.ProductLinkInvitationStatusEnum.ACCEPTED,
                    }
                )
            ]
        )
        with self.assertRaisesRegex(ToolError, "ACCEPTED"):
            aa.manage_merchant_center_link(
                customer_id=CID, action="ACCEPT", invitation_id="5"
            )

    def test_accept(self):
        pending = [
            self.row(
                {
                    "product_link_invitation.status": self.enums.ProductLinkInvitationStatusEnum.PENDING_APPROVAL,
                    "product_link_invitation.merchant_center.merchant_center_id": 999,
                }
            )
        ]
        kwargs = dict(customer_id=CID, action="ACCEPT", invitation_id="5")
        self.search_results(pending)
        proposal = self.propose(aa.manage_merchant_center_link, **kwargs)
        self.search_results(pending)
        self.commit(aa.manage_merchant_center_link, proposal, **kwargs)
        request = self.request(
            "ProductLinkInvitationService", "update_product_link_invitation"
        )
        self.assertEqual(
            request.product_link_invitation_status.name, "ACCEPTED"
        )


if __name__ == "__main__":
    unittest.main()
