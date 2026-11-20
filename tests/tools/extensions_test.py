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

"""Tests for call, promotion and price extensions."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import extensions as ex
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


def offering(header, price):
    return {
        "header": header,
        "description": "Incl. plaatsing",
        "price": price,
        "final_url": "https://favorcool.be/prijzen",
    }


class TestExtensions(MutateToolTestCase):

    def setUp(self):
        super().setUp()
        # Every call looks up the account currency first.
        self.service("GoogleAdsService").search.return_value = [
            self.row({"customer.currency_code": "EUR"})
        ]

    def _ops(self):
        return self.request("GoogleAdsService", "mutate").mutate_operations

    def test_call_linked_to_campaign(self):
        self.propose(
            ex.create_extension_asset,
            customer_id=CID,
            extension_type="CALL",
            parent_id="111",
            call={"phone_number": "09 123 45 67"},
        )
        asset_op, link_op = self._ops()
        asset = asset_op.asset_operation.create
        self.assertEqual(asset.call_asset.phone_number, "09 123 45 67")
        self.assertEqual(asset.call_asset.country_code, "BE")
        link = link_op.campaign_asset_operation.create
        self.assertEqual(link.asset, asset.resource_name)
        self.assertEqual(link.campaign, f"customers/{CID}/campaigns/111")
        self.assertEqual(link.field_type.name, "CALL")

    def test_promotion_percent_in_micros(self):
        proposal = self.propose(
            ex.create_extension_asset,
            customer_id=CID,
            extension_type="PROMOTION",
            link_level="CUSTOMER",
            promotion={
                "promotion_target": "Airco",
                "final_url": "https://favorcool.be/actie",
                "percent_off": 10,
                "up_to": True,
                "end_date": "2026-12-31",
            },
        )
        self.assertEqual(
            self.items(proposal)["Discount"]["value_readable"], "up to 10%"
        )
        asset_op, link_op = self._ops()
        promo = asset_op.asset_operation.create.promotion_asset
        self.assertEqual(promo.percent_off, 10_000_000)
        self.assertEqual(promo.discount_modifier.name, "UP_TO")
        self.assertEqual(promo.end_date, "2026-12-31")
        self.assertEqual(
            list(asset_op.asset_operation.create.final_urls),
            ["https://favorcool.be/actie"],
        )
        self.assertEqual(
            link_op.customer_asset_operation.create.field_type.name, "PROMOTION"
        )

    def test_price_with_offerings(self):
        self.propose(
            ex.create_extension_asset,
            customer_id=CID,
            extension_type="PRICE",
            link_level="NONE",
            price={
                "type": "services",
                "price_qualifier": "FROM",
                "offerings": [
                    offering("Monosplit", 1499),
                    offering("Multisplit", 2499),
                    {**offering("Onderhoud", 129), "unit": "PER_YEAR"},
                ],
            },
        )
        (asset_op,) = self._ops()
        price = asset_op.asset_operation.create.price_asset
        self.assertEqual(price.type_.name, "SERVICES")
        self.assertEqual(price.price_qualifier.name, "FROM")
        self.assertEqual(
            price.price_offerings[0].price.amount_micros, 1_499_000_000
        )
        self.assertEqual(price.price_offerings[0].price.currency_code, "EUR")
        self.assertEqual(price.price_offerings[2].unit.name, "PER_YEAR")

    def test_validation(self):
        cases = {
            "exactly one of 'percent_off' or 'money_amount_off'": dict(
                extension_type="PROMOTION",
                promotion={
                    "promotion_target": "Airco",
                    "final_url": "https://a.be",
                    "percent_off": 10,
                    "money_amount_off": 50,
                },
            ),
            "maximum is 20": dict(
                extension_type="PROMOTION",
                promotion={
                    "promotion_target": "Airco met gratis onderhoud",
                    "final_url": "https://a.be",
                    "percent_off": 10,
                },
            ),
            "3-8 offerings": dict(
                extension_type="PRICE",
                price={"offerings": [offering("A", 1)]},
            ),
            "Pass only 'call'": dict(
                extension_type="CALL",
                call={"phone_number": "09 123 45 67"},
                price={"offerings": []},
            ),
        }
        for message, kwargs in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    ex.create_extension_asset(
                        customer_id=CID, link_level="NONE", **kwargs
                    )


if __name__ == "__main__":
    unittest.main()
