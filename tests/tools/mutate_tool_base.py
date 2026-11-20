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

"""Shared test scaffolding for the Converted mutate tools.

Uses real v26 proto types and enums, and autospecced service clients created
on demand, so tests exercise the exact messages sent to the Google Ads API.
"""

import json
import os
from typing import Any
import unittest
from unittest.mock import MagicMock, create_autospec, patch

from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from ads_mcp import mutate_helpers as mh

CID = "1234567890"


class MutateToolTestCase(unittest.TestCase):
    """Base class: patches the Google Ads client with real types and mocks."""

    @classmethod
    def setUpClass(cls):
        cls.real_ga_client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="fake-token",
            use_proto_plus=True,
            version="v26",
        )
        cls.enums = cls.real_ga_client.enums

    def setUp(self):
        self.services: dict[str, Any] = {}
        self.mock_client = MagicMock()
        self.mock_client.get_service.side_effect = (
            lambda name, **kwargs: self.service(name)
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
        os.environ.pop(mh.MAX_INCREASE_PCT_ENV_VAR, None)

    def service(self, name: str) -> Any:
        if name not in self.services:
            real = self.real_ga_client.get_service(name)
            self.services[name] = create_autospec(type(real), instance=True)
        return self.services[name]

    # -- GAQL rows -----------------------------------------------------------

    def row(self, values: dict[str, Any] | None = None) -> Any:
        """Builds a GoogleAdsRow from {'campaign.name': 'x', ...}."""
        row = self.real_ga_client.get_type("GoogleAdsRow")
        for path, value in (values or {}).items():
            *parents, leaf = path.split(".")
            target = row
            for part in parents:
                target = getattr(target, part)
            if isinstance(value, list):
                getattr(target, leaf).extend(value)
            else:
                setattr(target, leaf, value)
        return row

    def search_results(self, *results: list[Any]) -> None:
        """Queues the results of consecutive GoogleAdsService.search calls."""
        self.service("GoogleAdsService").search.side_effect = list(results)

    def queries(self) -> list[str]:
        return [
            c.kwargs["query"]
            for c in self.service("GoogleAdsService").search.call_args_list
        ]

    # -- Requests ------------------------------------------------------------

    def request(self, service: str, method: str) -> Any:
        return getattr(self.service(service), method).call_args.kwargs[
            "request"
        ]

    def propose(self, fn, **kwargs) -> dict[str, Any]:
        proposal = json.loads(fn(**kwargs))
        self.assertEqual(proposal["status"], "PROPOSED")
        return proposal

    def commit(self, fn, proposal: dict[str, Any], **kwargs) -> str:
        return fn(**kwargs, validate_only=False, checksum=proposal["checksum"])

    @staticmethod
    def items(proposal: dict[str, Any]) -> dict[str, dict[str, str]]:
        """Maps field_readable -> change item (last one wins)."""
        return {i["field_readable"]: i for i in proposal["change_items"]}

    def mutate_response(self, *results: tuple[str, str]) -> Any:
        """Builds a real MutateGoogleAdsResponse from (result_field, rn)."""
        response = self.real_ga_client.get_type("MutateGoogleAdsResponse")
        for field, resource_name in results:
            op_response = self.real_ga_client.get_type(
                "MutateOperationResponse"
            )
            getattr(op_response, field).resource_name = resource_name
            response.mutate_operation_responses.append(op_response)
        return response
