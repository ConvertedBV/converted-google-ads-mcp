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

"""MCP prompts: ready-made starting points users can pick in their client."""

from ads_mcp.coordinator import mcp


@mcp.prompt(
    name="account_health_check",
    description="Audit a Google Ads account and propose prioritized fixes.",
)
def account_health_check(customer_id: str, period: str = "LAST_30_DAYS") -> str:
    return f"""Run a health check of Google Ads account {customer_id} over {period}.

1. Call get_account_overview and get_conversion_setup.
2. With the search tool, look at: search terms with spend but no
   conversions (search_term_view), disapproved or limited ads and assets,
   and campaigns limited by budget or by rank.
3. Report in this order: what is broken (tracking, disapprovals), what wastes
   money, what limits growth, and quick wins. Separate facts from
   assumptions and give the numbers behind each point.
4. End with a prioritized list of concrete changes. Do not change anything:
   for each change I approve, use the matching tool with validate_only=True
   first and show me the proposal.
"""


@mcp.prompt(
    name="plan_new_campaign",
    description="Plan and build a new campaign step by step, paused for review.",
)
def plan_new_campaign(
    customer_id: str,
    goal: str,
    landing_page: str,
    daily_budget: str = "",
    locations: str = "",
) -> str:
    return f"""Plan a new Google Ads campaign for account {customer_id}.

Goal: {goal}
Landing page: {landing_page}
Daily budget: {daily_budget or "to be proposed"}
Locations: {locations or "to be confirmed"}

Work in these steps and wait for my answer after steps 1 and 4:
1. Ask for anything essential that is missing (offer/USP, languages, target
   CPA or ROAS, exclusions). Check get_account_overview and
   get_conversion_setup so bidding fits the conversion tracking.
2. Research keywords with generate_keyword_ideas (seeded with the landing
   page and core terms) and validate the selection with get_keyword_metrics.
3. Size the budget with forecast_keywords (compare two or three budgets).
4. Propose the plan in the chat: campaign type and why, structure (ad groups
   or asset groups), keywords and negatives, ad texts within the character
   limits, bidding, targeting and the campaign name following the account's
   naming pattern.
5. After I approve the plan, call the matching campaign_builder tool with
   validate_only=True and show me the change_items; only commit after my
   explicit approval. The campaign is created PAUSED; tell me what to check
   before enabling it.
"""
