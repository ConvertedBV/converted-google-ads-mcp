# Copyright 2026 Google LLC.
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

"""Integration tests for dynamic tool mounting and namespacing based on configuration."""

import importlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from ads_mcp.coordinator import initialize_and_mount_tools
from ads_mcp.config import ToolsConfig


class TestToolsMounting(unittest.IsolatedAsyncioTestCase):
    """Verifies that tools are mounted and namespaced according to ToolsConfig."""

    @patch("ads_mcp.config.ToolsConfig.load")
    async def test_mounting_all_enabled_default_prefixes(self, mock_load):
        """Tests mounting with all categories enabled and default namespace prefixes."""
        # Mock config to enable everything with defaults
        mock_load.return_value = ToolsConfig(
            {
                "namespaces": {
                    "customers": True,
                    "search": True,
                    "metadata": True,
                    "labels": True,
                    "assets": True,
                }
            }
        )

        parent = FastMCP("Test Parent")
        initialize_and_mount_tools(parent)

        tools = await parent.list_tools()
        tool_names = [t.name for t in tools]

        # Expected tools with default prefixes
        self.assertIn("customers_list_accessible_customers", tool_names)
        self.assertIn("search_search", tool_names)
        self.assertIn("metadata_get_resource_metadata", tool_names)
        self.assertIn("labels_manage_labels", tool_names)
        self.assertIn("labels_manage_label_associations", tool_names)
        self.assertIn("assets_create_asset", tool_names)
        self.assertIn("assets_upload_video_asset", tool_names)

    @patch("ads_mcp.config.ToolsConfig.load")
    async def test_campaign_management_mounted_by_default(self, mock_load):
        """The bundled config exposes the Converted write tools."""
        mock_load.return_value = ToolsConfig()

        parent = FastMCP("Test Parent")
        initialize_and_mount_tools(parent)

        tool_names = [t.name for t in await parent.list_tools()]
        for name in (
            "update_entity_status",
            "update_campaign_budget",
            "update_cpc_bid",
            "add_keywords",
            "manage_negative_keywords",
            "update_campaign_bidding",
            "update_campaign_settings",
            "add_campaign_targeting",
            "remove_campaign_targeting",
            "update_bid_modifiers",
            "manage_shared_negative_list",
            "link_assets",
            "manage_recommendations",
            "update_responsive_search_ad",
            "replace_ad_text",
        ):
            self.assertIn(f"campaign_management_{name}", tool_names)
        for name in (
            "create_search_campaign",
            "create_ad_group",
            "create_responsive_search_ad",
        ):
            self.assertIn(f"campaign_builder_{name}", tool_names)
        for name in (
            "keyword_planning_generate_keyword_ideas",
            "keyword_planning_get_keyword_metrics",
            "keyword_planning_forecast_keywords",
            "conversions_get_conversion_setup",
            "conversions_create_conversion_action",
            "conversions_update_conversion_action",
            "conversions_set_conversion_goals",
            "conversions_upload_offline_conversions",
            "campaign_builder_create_performance_max_campaign",
            "campaign_builder_create_asset_group",
            "campaign_builder_create_demand_gen_campaign",
            "campaign_builder_create_display_campaign",
            "campaign_builder_create_shopping_campaign",
            "campaign_builder_create_image_assets",
            "audiences_create_remarketing_list",
            "audiences_suggest_audiences",
            "audiences_find_audience_segments",
            "audiences_analyze_audience_composition",
            "audiences_manage_audience_targeting",
            "audiences_manage_demographic_exclusions",
            "audiences_upload_customer_match",
            "audiences_manage_custom_segment",
            "campaign_management_suggest_brands",
            "campaign_management_manage_brand_list",
            "account_create_client_account",
            "account_invite_user",
            "account_manage_merchant_center_link",
            "campaign_management_manage_account_exclusions",
            "campaign_management_manage_bidding_adjustments",
            "campaign_management_update_account_settings",
            "campaign_management_manage_portfolio_bidding_strategy",
            "campaign_management_manage_shared_budget",
            "campaign_management_remove_entities",
            "campaign_management_revert_change",
            "campaign_management_create_extension_asset",
            "account_get_account_overview",
            "account_get_change_history",
            "account_find_locations",
            "experiments_create_experiment",
            "experiments_manage_experiment",
        ):
            self.assertIn(name, tool_names)

    async def test_read_only_mode_drops_mutating_tools(self):
        """GOOGLE_ADS_MCP_READ_ONLY removes every tool not marked read-only."""
        sub = FastMCP("labels")

        @sub.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def reader() -> str:
            return "read"

        @sub.tool(annotations=ToolAnnotations(readOnlyHint=False))
        def writer() -> str:
            return "write"

        @sub.tool()
        def unannotated() -> str:
            return "unknown"

        fake_module = SimpleNamespace(sub=sub)
        real_import_module = importlib.import_module

        def import_module(name, *args, **kwargs):
            if name == "ads_mcp.tools.fake_mod":
                return fake_module
            return real_import_module(name, *args, **kwargs)

        parent = FastMCP("Test Parent")
        with (
            patch.dict(os.environ, {"GOOGLE_ADS_MCP_READ_ONLY": "true"}),
            patch(
                "pkgutil.iter_modules",
                return_value=[(None, "fake_mod", False)],
            ),
            patch("importlib.import_module", side_effect=import_module),
            patch(
                "ads_mcp.config.ToolsConfig.load",
                return_value=ToolsConfig(),
            ),
        ):
            initialize_and_mount_tools(parent)

        tool_names = [t.name for t in await parent.list_tools()]
        self.assertEqual(tool_names, ["labels_reader"])

    @patch("ads_mcp.config.ToolsConfig.load")
    async def test_mounting_disabled_namespaces(self, mock_load):
        """Tests that disabled namespaces are completely excluded."""
        # Mock config: disable search and metadata
        mock_load.return_value = ToolsConfig(
            {
                "namespaces": {
                    "customers": True,
                    "search": False,
                    "metadata": False,
                }
            }
        )

        parent = FastMCP("Test Parent")
        initialize_and_mount_tools(parent)

        tools = await parent.list_tools()
        tool_names = [t.name for t in tools]

        self.assertIn("customers_list_accessible_customers", tool_names)
        self.assertNotIn("search_search", tool_names)
        self.assertNotIn("metadata_get_resource_metadata", tool_names)

    @patch("ads_mcp.config.ToolsConfig.load")
    async def test_mounting_custom_prefixes(self, mock_load):
        """Tests namespaces with custom prefixes."""
        mock_load.return_value = ToolsConfig(
            {
                "namespaces": {
                    "customers": "accounts",
                    "search": "query",
                    "metadata": "info",
                }
            }
        )

        parent = FastMCP("Test Parent")
        initialize_and_mount_tools(parent)

        tools = await parent.list_tools()
        tool_names = [t.name for t in tools]

        self.assertIn("accounts_list_accessible_customers", tool_names)
        self.assertIn("query_search", tool_names)
        self.assertIn("info_get_resource_metadata", tool_names)

    @patch("ads_mcp.config.ToolsConfig.load")
    async def test_mounting_fine_grained_tool_enablement(self, mock_load):
        """Tests disabling individual tools under an enabled namespace."""
        mock_load.return_value = ToolsConfig(
            {
                "namespaces": {
                    "customers": {
                        "enabled": True,
                        "prefix": "accounts",
                        "enabled_tools": [
                            {
                                "list_accessible_customers": False
                            }  # Explicitly disable this tool
                        ],
                    },
                    "search": True,
                }
            }
        )

        parent = FastMCP("Test Parent")
        initialize_and_mount_tools(parent)

        tools = await parent.list_tools()
        tool_names = [t.name for t in tools]

        self.assertNotIn("accounts_list_accessible_customers", tool_names)
        self.assertIn("search_search", tool_names)

    @patch("importlib.import_module")
    @patch("pkgutil.iter_modules")
    def test_conflicting_fastmcp_instances_raise_value_error(
        self, mock_iter_modules, mock_import_module
    ):
        """Tests that two distinct FastMCP instances with the same category name raise ValueError."""
        from types import SimpleNamespace

        mock_iter_modules.return_value = [
            (None, "mod_a", False),
            (None, "mod_b", False),
        ]
        mock_import_module.side_effect = [
            SimpleNamespace(mcp_a=FastMCP("assets")),
            SimpleNamespace(mcp_b=FastMCP("assets")),
        ]

        parent = FastMCP("Test Parent")
        with self.assertRaisesRegex(
            ValueError, "Conflicting FastMCP sub-servers"
        ):
            initialize_and_mount_tools(parent)
