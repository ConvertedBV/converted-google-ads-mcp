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

"""Tests for server instructions and prompts."""

import unittest

from ads_mcp import server


class TestInstructionsAndPrompts(unittest.IsolatedAsyncioTestCase):

    def test_instructions_cover_the_way_of_working(self):
        instructions = server.mcp.instructions
        for phrase in (
            "get_account_overview",
            "validate_only=True",
            "explicitly approves",
            "PAUSED",
            "Keep clients apart",
        ):
            self.assertIn(phrase, instructions)

    async def test_prompts(self):
        names = {p.name for p in await server.mcp.list_prompts()}
        self.assertEqual(names, {"account_health_check", "plan_new_campaign"})
        result = await server.mcp.render_prompt(
            "plan_new_campaign",
            {
                "customer_id": "5018136849",
                "goal": "Airco-leads",
                "landing_page": "https://favorcool.be/airco",
            },
        )
        text = result.messages[0].content.text
        self.assertIn("Airco-leads", text)
        self.assertIn("forecast_keywords", text)
        self.assertIn("Daily budget: to be proposed", text)


if __name__ == "__main__":
    unittest.main()
