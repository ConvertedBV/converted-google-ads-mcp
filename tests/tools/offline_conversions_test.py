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

"""Tests for offline conversion uploads."""

import hashlib
import json
import unittest

from fastmcp.exceptions import ToolError
from google.protobuf import any_pb2
from google.rpc import status_pb2

from ads_mcp.tools import conversions as cv
from tests.tools.mutate_tool_base import CID, MutateToolTestCase

GCLID = "Cj0KCQiA_test_gclid_123"
EMAIL = "Jan.Peeters@Gmail.com"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class TestHashing(unittest.TestCase):

    def test_email_normalization(self):
        self.assertEqual(
            cv.normalize_email_hash(EMAIL), sha("janpeeters@gmail.com")
        )
        self.assertEqual(
            cv.normalize_email_hash(" Info.Desk@FavorCool.be "),
            sha("info.desk@favorcool.be"),
        )
        with self.assertRaises(ToolError):
            cv.normalize_email_hash("geen-email")

    def test_phone_normalization(self):
        self.assertEqual(
            cv.normalize_phone_hash("+32 470 12 34 56"), sha("+32470123456")
        )
        with self.assertRaisesRegex(ToolError, "E.164"):
            cv.normalize_phone_hash("0470 12 34 56")


class TestUpload(MutateToolTestCase):

    def _action(self, action_type="UPLOAD_CLICKS"):
        return [
            self.row(
                {
                    "conversion_action.name": "Qualified lead",
                    "conversion_action.type_": getattr(
                        self.enums.ConversionActionTypeEnum, action_type
                    ),
                    "conversion_action.status": self.enums.ConversionActionStatusEnum.ENABLED,
                    "customer.currency_code": "EUR",
                }
            )
        ]

    def _rows(self):
        return [
            {
                "conversion_time": "2026-11-18 10:15:00+01:00",
                "gclid": GCLID,
                "value": 150,
            },
            {
                "conversion_time": "2026-11-19T09:00:00+01:00",
                "email": EMAIL,
                "phone_number": "+32 470 12 34 56",
                "order_id": "lead-42",
            },
        ]

    def _partial_failure(self, index: int, message: str):
        failure = self.real_ga_client.get_type("GoogleAdsFailure")
        error = self.real_ga_client.get_type("GoogleAdsError")
        error.message = message
        element = self.real_ga_client.get_type(
            "ErrorLocation"
        ).FieldPathElement()
        element.field_name = "conversions"
        element.index = index
        error.location.field_path_elements.append(element)
        failure.errors.append(error)
        detail = any_pb2.Any(
            type_url="type.googleapis.com/google.ads.googleads.v26.errors.GoogleAdsFailure",
            value=type(failure).serialize(failure),
        )
        return status_pb2.Status(code=3, message=message, details=[detail])

    def _response(self, status=None):
        response = self.real_ga_client.get_type(
            "UploadClickConversionsResponse"
        )
        if status is not None:
            response.partial_failure_error = status
        return response

    def test_upload_hashes_and_sets_consent(self):
        self.search_results(self._action(), self._action())
        service = self.service("ConversionUploadService")
        service.upload_click_conversions.return_value = self._response()
        kwargs = dict(
            customer_id=CID,
            conversion_action_id="7606602220",
            conversions=self._rows(),
            ad_user_data_consent="GRANTED",
        )
        proposal = self.propose(cv.upload_offline_conversions, **kwargs)
        raw = json.dumps(proposal)
        self.assertNotIn("Peeters", raw)
        self.assertNotIn("470", raw.replace("2026", ""))
        self.assertIn(
            "2 conversions, total value 150.00 EUR, consent GRANTED", raw
        )

        request = self.request(
            "ConversionUploadService", "upload_click_conversions"
        )
        self.assertTrue(request.partial_failure)
        self.assertTrue(request.validate_only)
        first, second = request.conversions
        self.assertEqual(first.gclid, GCLID)
        self.assertEqual(first.conversion_value, 150)
        self.assertEqual(first.currency_code, "EUR")
        self.assertEqual(first.consent.ad_user_data.name, "GRANTED")
        self.assertEqual(
            second.conversion_date_time, "2026-11-19 09:00:00+01:00"
        )
        self.assertEqual(
            second.user_identifiers[0].hashed_email, sha("janpeeters@gmail.com")
        )
        self.assertEqual(
            second.user_identifiers[1].hashed_phone_number, sha("+32470123456")
        )
        self.assertEqual(second.order_id, "lead-42")
        self.assertNotIn("Peeters", str(request))

        result = self.commit(cv.upload_offline_conversions, proposal, **kwargs)
        self.assertIn("Uploaded 2 of 2 conversions to 'Qualified lead'", result)

    def test_dry_run_rejection_is_reported_per_row(self):
        self.search_results(self._action())
        self.service(
            "ConversionUploadService"
        ).upload_click_conversions.return_value = self._response(
            self._partial_failure(1, "The click is too old.")
        )
        with self.assertRaisesRegex(ToolError, "row 1: The click is too old"):
            cv.upload_offline_conversions(
                customer_id=CID,
                conversion_action_id="1",
                conversions=self._rows(),
                ad_user_data_consent="GRANTED",
            )

    def test_wrong_action_type(self):
        self.search_results(self._action("WEBPAGE"))
        with self.assertRaisesRegex(ToolError, "UPLOAD_CLICKS"):
            cv.upload_offline_conversions(
                customer_id=CID,
                conversion_action_id="1",
                conversions=self._rows(),
                ad_user_data_consent="GRANTED",
            )

    def test_row_validation(self):
        cases = {
            "conversion_time must look like": {
                "conversion_time": "18/11/2026",
                "gclid": GCLID,
            },
            "needs a gclid": {"conversion_time": "2026-11-18 10:15:00+01:00"},
            "only one click ID": {
                "conversion_time": "2026-11-18 10:15:00+01:00",
                "gclid": GCLID,
                "wbraid": "wbraid_123456789",
            },
        }
        for message, row in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    cv.upload_offline_conversions(
                        customer_id=CID,
                        conversion_action_id="1",
                        conversions=[row],
                        ad_user_data_consent="GRANTED",
                    )


if __name__ == "__main__":
    unittest.main()
