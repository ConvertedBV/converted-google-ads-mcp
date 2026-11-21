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

"""Tests for upload_customer_match."""

import hashlib
import json
import unittest
from unittest.mock import MagicMock

from fastmcp.exceptions import ToolError

from ads_mcp.tools import customer_match as cm
from tests.tools.mutate_tool_base import CID, MutateToolTestCase

MEMBERS = [
    {"email": " Jan.Peeters@Gmail.com "},
    {"phone_number": "+32 470 12 34 56"},
    {
        "first_name": " An ",
        "last_name": "Janssens",
        "country_code": "be",
        "postal_code": "9000",
    },
]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class TestCustomerMatch(MutateToolTestCase):

    def _kwargs(self, **extra):
        kwargs = dict(
            customer_id=CID,
            members=MEMBERS,
            consent_confirmed=True,
            list_name="Klanten 2026",
        )
        kwargs.update(extra)
        return kwargs

    def test_new_list_propose_and_commit(self):
        proposal = self.propose(cm.upload_customer_match, **self._kwargs())
        text = json.dumps(proposal)
        self.assertNotIn("Peeters", text)
        self.assertNotIn("470", text)
        self.assertIn("1 emails, 1 phones, 1 name + address", text)
        validate = self.request("UserListService", "mutate_user_lists")
        self.assertTrue(validate.validate_only)
        new = validate.operations[0].create
        self.assertEqual(
            new.crm_based_user_list.upload_key_type.name, "CONTACT_INFO"
        )

        list_rn = f"customers/{CID}/userLists/9"
        job_rn = f"customers/{CID}/offlineUserDataJobs/77"
        users = self.service("UserListService")
        users.mutate_user_lists.return_value = MagicMock(
            results=[MagicMock(resource_name=list_rn)]
        )
        jobs = self.service("OfflineUserDataJobService")
        jobs.create_offline_user_data_job.return_value = MagicMock(
            resource_name=job_rn
        )
        jobs.add_offline_user_data_job_operations.return_value = MagicMock(
            partial_failure_error=MagicMock(code=0)
        )
        result = self.commit(
            cm.upload_customer_match, proposal, **self._kwargs()
        )
        self.assertIn("Sent 3 members", result)

        job = self.request(
            "OfflineUserDataJobService", "create_offline_user_data_job"
        ).job
        self.assertEqual(
            job.customer_match_user_list_metadata.user_list, list_rn
        )
        self.assertEqual(
            job.customer_match_user_list_metadata.consent.ad_user_data.name,
            "GRANTED",
        )
        add = self.request(
            "OfflineUserDataJobService", "add_offline_user_data_job_operations"
        )
        self.assertEqual(add.resource_name, job_rn)
        ids = [op.create.user_identifiers[0] for op in add.operations]
        self.assertEqual(ids[0].hashed_email, sha("janpeeters@gmail.com"))
        self.assertEqual(ids[1].hashed_phone_number, sha("+32470123456"))
        self.assertEqual(ids[2].address_info.hashed_first_name, sha("an"))
        self.assertEqual(ids[2].address_info.country_code, "BE")
        jobs.run_offline_user_data_job.assert_called_once_with(
            resource_name=job_rn
        )

    def test_failed_upload_names_created_list(self):
        proposal = self.propose(cm.upload_customer_match, **self._kwargs())
        users = self.service("UserListService")
        users.mutate_user_lists.return_value = MagicMock(
            results=[MagicMock(resource_name=f"customers/{CID}/userLists/9")]
        )
        jobs = self.service("OfflineUserDataJobService")
        jobs.create_offline_user_data_job.return_value = MagicMock(
            resource_name=f"customers/{CID}/offlineUserDataJobs/77"
        )
        jobs.add_offline_user_data_job_operations.side_effect = RuntimeError(
            "quota"
        )
        with self.assertRaisesRegex(
            ToolError, "Job 77 was created but not run.*user_list_id=9"
        ):
            self.commit(cm.upload_customer_match, proposal, **self._kwargs())
        jobs.run_offline_user_data_job.assert_not_called()

    def test_existing_list_remove(self):
        self.search_results(
            [
                self.row(
                    {
                        "user_list.name": "Klanten",
                        "user_list.type_": self.enums.UserListTypeEnum.CRM_BASED,
                    }
                )
            ]
        )
        self.propose(
            cm.upload_customer_match,
            **self._kwargs(
                list_name=None, user_list_id="9", remove_members=True
            ),
        )
        job = self.request(
            "OfflineUserDataJobService", "create_offline_user_data_job"
        )
        self.assertTrue(job.validate_only)

    def test_validation(self):
        cases = {
            "consented": dict(consent_confirmed=False),
            "member #2": dict(members=[{"email": "a@b.be"}, {"email": "geen"}]),
            "postal_code \\(member #1\\)": dict(
                members=[{"first_name": "An", "last_name": "J"}]
            ),
            "either 'user_list_id'": dict(list_name=None),
            "only be removed": dict(remove_members=True),
            "1-540 or 10000": dict(membership_days=600),
        }
        for message, override in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message) as ctx:
                    cm.upload_customer_match(**self._kwargs(**override))
                self.assertNotIn("geen", str(ctx.exception))

    def test_non_crm_list_refused(self):
        self.search_results(
            [
                self.row(
                    {
                        "user_list.name": "Bezoekers",
                        "user_list.type_": self.enums.UserListTypeEnum.RULE_BASED,
                    }
                )
            ]
        )
        with self.assertRaisesRegex(ToolError, "not an editable Customer"):
            cm.upload_customer_match(
                **self._kwargs(list_name=None, user_list_id="9")
            )


if __name__ == "__main__":
    unittest.main()
