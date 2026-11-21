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

"""Tests for the Google Analytics 4 tools."""

import datetime as dt
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.analytics.admin_v1alpha import types as admin_types
from google.analytics.data_v1beta import types as data_types

from ads_mcp.analytics import client as ga
from ads_mcp.analytics import registry as reg
from ads_mcp.analytics import reports as rep
from ads_mcp.tools import (
    analytics_admin,
    analytics_insights,
    analytics_management,
    analytics_reporting,
)
from unittest.mock import create_autospec
from google.analytics.admin_v1alpha import AnalyticsAdminServiceClient
from google.analytics.data_v1beta import BetaAnalyticsDataClient


def _spec_client(cls):
    """Mock that only accepts the real client's method signatures."""
    return create_autospec(cls, instance=True)


def _report(dims, mets, rows, totals=None, row_count=None):
    """Builds a real RunReportResponse."""
    resp = data_types.RunReportResponse(
        dimension_headers=[{"name": d} for d in dims],
        metric_headers=[{"name": m, "type_": "TYPE_INTEGER"} for m in mets],
        rows=[
            {
                "dimension_values": [{"value": str(v)} for v in r[: len(dims)]],
                "metric_values": [{"value": str(v)} for v in r[len(dims) :]],
            }
            for r in rows
        ],
        totals=[
            {
                "dimension_values": [{"value": "RESERVED_TOTAL"} for _ in dims],
                "metric_values": [{"value": str(v)} for v in t],
            }
            for t in (totals or [])
        ],
        row_count=row_count if row_count is not None else len(rows),
    )
    return resp


class ModeTest(unittest.TestCase):
    def test_modes_and_scopes(self):
        with patch.dict(os.environ, {"GOOGLE_ADS_MCP_ANALYTICS": ""}):
            self.assertEqual(ga.analytics_mode(), "off")
            self.assertEqual(ga.analytics_scopes(), [])
            self.assertFalse(ga.is_namespace_allowed("analytics_reporting"))
            self.assertTrue(ga.is_namespace_allowed("campaign_management"))
        with patch.dict(os.environ, {"GOOGLE_ADS_MCP_ANALYTICS": "read"}):
            self.assertEqual(ga.analytics_scopes(), [ga.SCOPE_READONLY])
            self.assertTrue(ga.is_namespace_allowed("analytics_admin"))
            self.assertFalse(ga.is_namespace_allowed("analytics_management"))
        with patch.dict(os.environ, {"GOOGLE_ADS_MCP_ANALYTICS": "full"}):
            self.assertIn(ga.SCOPE_EDIT, ga.analytics_scopes())
            self.assertIn(ga.SCOPE_MANAGE_USERS, ga.analytics_scopes())
            self.assertTrue(ga.is_namespace_allowed("analytics_management"))

    def test_ids(self):
        self.assertEqual(ga.property_name("properties/123"), "properties/123")
        self.assertEqual(ga.property_name(456), "properties/456")
        self.assertEqual(
            ga.data_stream_name("1", "dataStreams/9"),
            "properties/1/dataStreams/9",
        )
        with self.assertRaises(ToolError):
            ga.property_name("abc")


class MountingTest(unittest.IsolatedAsyncioTestCase):
    async def _names(self, mode):
        from ads_mcp.config import ToolsConfig
        from ads_mcp.coordinator import initialize_and_mount_tools

        with (
            patch.dict(os.environ, {"GOOGLE_ADS_MCP_ANALYTICS": mode}),
            patch(
                "ads_mcp.config.ToolsConfig.load", return_value=ToolsConfig()
            ),
        ):
            parent = FastMCP("t")
            initialize_and_mount_tools(parent)
            return [t.name for t in await parent.list_tools()]

    async def test_off(self):
        names = await self._names("off")
        self.assertFalse([n for n in names if n.startswith("analytics_")])

    async def test_read(self):
        names = await self._names("read")
        self.assertIn("analytics_reporting_run_report", names)
        self.assertIn("analytics_admin_get_property_setup", names)
        self.assertNotIn("analytics_management_manage_key_event", names)

    async def test_full(self):
        names = await self._names("full")
        self.assertIn("analytics_management_manage_resource", names)
        self.assertIn("analytics_insights_channel_performance", names)


class RegistryTest(unittest.TestCase):
    def test_every_operation_exists_in_client(self):
        cls = reg._client_class()
        for key, rt in reg.registry().items():
            for op in rt.allowed:
                method = (
                    f"list_{rt.plural_method}"
                    if op == "list"
                    else f"{op}_{key}"
                )
                self.assertTrue(hasattr(cls, method), method)
                if op in ("create", "update"):
                    reg.body_field(rt, op)

    def test_blocked(self):
        self.assertNotIn("delete", reg.get_type("account").allowed)
        self.assertNotIn("delete", reg.get_type("property").allowed)

    def test_paths(self):
        cd = reg.get_type("custom_dimensions")
        self.assertEqual(
            reg.resource_path(cd, property_id="1", resource_id="2"),
            "properties/1/customDimensions/2",
        )
        em = reg.get_type("enhanced_measurement_settings")
        self.assertEqual(
            reg.resource_path(em, property_id="1", data_stream_id="7"),
            "properties/1/dataStreams/7/enhancedMeasurementSettings",
        )
        ab = reg.get_type("access_binding")
        self.assertEqual(reg.parent_path(ab, account_id="5"), "accounts/5")
        with self.assertRaises(ToolError):
            reg.get_type("nonsense")

    def test_schema(self):
        fields = reg.describe_fields(
            reg.message_class(reg.get_type("key_event"))
        )
        self.assertEqual(fields["event_name"]["type"], "string")
        self.assertIn("ONCE_PER_SESSION", fields["counting_method"]["values"])


class ReportsHelpersTest(unittest.TestCase):
    def test_presets(self):
        today = dt.date(2026, 11, 21)
        self.assertEqual(
            rep.resolve_preset("LAST_MONTH", today),
            ("2026-10-01", "2026-10-31"),
        )
        self.assertEqual(
            rep.resolve_preset("LAST_12_MONTHS", today),
            ("2025-11-01", "2026-10-31"),
        )
        self.assertEqual(
            rep.resolve_preset("LAST_7_DAYS"), ("7daysAgo", "yesterday")
        )
        with self.assertRaises(ToolError):
            rep.resolve_preset("NEXT_WEEK")

    def test_date_ranges(self):
        self.assertEqual(
            rep.build_date_ranges("2026-01-01", "2026-01-31", None, None),
            [{"start_date": "2026-01-01", "end_date": "2026-01-31"}],
        )
        multi = rep.build_date_ranges(
            None,
            None,
            None,
            '[{"start_date":"2026-01-01","end_date":"2026-01-31","name":"a"},'
            '{"period":"YESTERDAY"}]',
        )
        self.assertEqual(len(multi), 2)
        with self.assertRaises(ToolError):
            rep.build_date_ranges("2026-13-01", "2026-01-31", None, None)
        with self.assertRaises(ToolError):
            rep.build_date_ranges("2026-01-01", None, None, None)

    def test_filters_build_valid_proto(self):
        dim = rep.build_filter(
            [
                {
                    "field": "sessionDefaultChannelGroup",
                    "op": "IN_LIST",
                    "values": ["Organic Search", "Paid Search"],
                },
                {
                    "field": "pagePath",
                    "op": "CONTAINS",
                    "value": "/fr/",
                    "not": True,
                },
                {
                    "or": [
                        {
                            "field": "deviceCategory",
                            "op": "EXACT",
                            "value": "mobile",
                        },
                        {
                            "field": "deviceCategory",
                            "op": "EXACT",
                            "value": "tablet",
                        },
                    ]
                },
            ],
            "f",
        )
        met = rep.build_filter(
            [
                {"field": "sessions", "op": "GT", "value": 10},
                {
                    "field": "purchaseRevenue",
                    "op": "BETWEEN",
                    "from": 1.5,
                    "to": 99,
                },
            ],
            "m",
        )
        req = data_types.RunReportRequest(
            property="properties/1",
            metrics=[{"name": "sessions"}],
            dimension_filter=dim,
            metric_filter=met,
            order_bys=rep.build_order_bys(["-sessions"], ["sessions"], []),
        )
        self.assertEqual(len(req.dimension_filter.and_group.expressions), 3)
        self.assertTrue(req.order_bys[0].desc)
        with self.assertRaises(ToolError):
            rep.build_filter([{"field": "x", "op": "LIKE", "value": 1}], "f")
        with self.assertRaises(ToolError):
            rep.build_order_bys(["clicks"], ["sessions"], [])

    def test_flatten(self):
        resp = _report(
            ["sessionDefaultChannelGroup"],
            ["sessions", "ecommercePurchases"],
            [["Organic Search", 100, 4], ["Paid Search", 80, 3]],
            totals=[[180, 7]],
        )
        out = rep.flatten_report(resp)
        self.assertEqual(out["rows"][0]["sessions"], 100)
        self.assertEqual(out["totals"][0]["ecommercePurchases"], 7)


class ReportingToolsTest(unittest.TestCase):
    def setUp(self):
        self.data = _spec_client(BetaAnalyticsDataClient)
        self.admin = _spec_client(AnalyticsAdminServiceClient)
        patcher_d = patch.object(ga, "data_client", return_value=self.data)
        patcher_a = patch.object(ga, "admin_client", return_value=self.admin)
        self.addCleanup(patcher_d.stop)
        self.addCleanup(patcher_a.stop)
        patcher_d.start()
        patcher_a.start()

    def test_run_report(self):
        self.data.run_report.return_value = _report(
            ["yearMonth", "sessionDefaultChannelGroup"],
            ["sessions", "purchaseRevenue"],
            [["202610", "Organic Search", 900, 5000]],
            row_count=3,
        )
        out = json.loads(
            analytics_reporting.run_report(
                property_id="123",
                metrics=["sessions", "purchaseRevenue"],
                dimensions=["yearMonth", "sessionDefaultChannelGroup"],
                start_date="2026-01-01",
                end_date="2026-10-31",
                dimension_filters=[
                    {
                        "field": "sessionDefaultChannelGroup",
                        "op": "EXACT",
                        "value": "Organic Search",
                    }
                ],
                order_by=["yearMonth"],
            )
        )
        req = self.data.run_report.call_args.kwargs["request"]
        self.assertEqual(req.property, "properties/123")
        self.assertEqual(req.date_ranges[0].start_date, "2026-01-01")
        self.assertEqual(out["rows"][0]["purchaseRevenue"], 5000)
        self.assertIn("paging", out)

    def test_run_report_validation(self):
        with self.assertRaises(ToolError):
            analytics_reporting.run_report(property_id="1", metrics=[])
        with self.assertRaises(ToolError):
            analytics_reporting.run_report(
                property_id="1", metrics=["sessions", "sessions"]
            )

    def test_list_properties(self):
        summary = admin_types.AccountSummary(
            account="accounts/9",
            display_name="Crealingua",
            property_summaries=[
                {"property": "properties/111", "display_name": "P01 site"},
            ],
        )
        other = admin_types.AccountSummary(
            account="accounts/8",
            display_name="Other",
            property_summaries=[
                {"property": "properties/222", "display_name": "X"}
            ],
        )
        self.admin.list_account_summaries.return_value = [summary, other]
        out = json.loads(analytics_reporting.list_properties("crealingua"))
        self.assertEqual(out["property_count"], 1)
        self.assertEqual(
            out["accounts"][0]["properties"][0]["property_id"], "111"
        )
        out_all = json.loads(analytics_reporting.list_properties())
        self.assertEqual(out_all["property_count"], 2)

    def test_funnel_steps(self):
        steps = [
            analytics_reporting._funnel_step(
                {"event_name": "begin_checkout"}, 0
            ),
            analytics_reporting._funnel_step(
                {"name": "Offerte", "page_path_contains": "/Request"}, 1
            ),
        ]
        from google.analytics.data_v1alpha import types as alpha

        req = alpha.RunFunnelReportRequest(
            property="properties/1", funnel={"steps": steps}
        )
        self.assertEqual(req.funnel.steps[0].name, "Step 1")
        with self.assertRaises(ToolError):
            analytics_reporting._funnel_step({"name": "x"}, 0)


class ChannelPerformanceTest(unittest.TestCase):
    def test_merges_metric_sets(self):
        data = MagicMock()
        admin = MagicMock()
        admin.list_key_events.return_value = [
            admin_types.KeyEvent(event_name="purchase"),
            admin_types.KeyEvent(event_name="generate_lead"),
        ]
        base = _report(
            ["sessionDefaultChannelGroup"],
            [
                "sessions",
                "engagedSessions",
                "totalUsers",
                "keyEvents",
                "ecommercePurchases",
                "purchaseRevenue",
                "totalRevenue",
            ],
            [
                ["Organic Search", 100, 90, 80, 10, 4, 400, 400],
                ["Paid Search", 50, 45, 40, 5, 2, 100, 100],
            ],
            totals=[[150, 135, 120, 15, 6, 500, 500]],
        )
        events = _report(
            ["sessionDefaultChannelGroup"],
            ["keyEvents:purchase", "keyEvents:generate_lead"],
            [["Organic Search", 4, 6], ["Paid Search", 2, 3]],
            totals=[[6, 9]],
        )
        data.run_report.side_effect = [base, events]
        with (
            patch.object(ga, "data_client", return_value=data),
            patch.object(ga, "admin_client", return_value=admin),
        ):
            out = json.loads(
                analytics_insights.channel_performance(
                    property_id="1", period="LAST_MONTH", by_month=False
                )
            )
        organic = [
            r
            for r in out["rows"]
            if r["sessionDefaultChannelGroup"] == "Organic Search"
        ][0]
        self.assertEqual(organic["keyEvents:generate_lead"], 6)
        self.assertEqual(organic["share_of_total_revenue"], 0.8)
        self.assertEqual(
            out["key_events_counted"], ["purchase", "generate_lead"]
        )


class ManagementTest(unittest.TestCase):
    def setUp(self):
        self.admin = _spec_client(AnalyticsAdminServiceClient)
        patcher = patch.object(ga, "admin_client", return_value=self.admin)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_create_key_event_propose_and_commit(self):
        kwargs = dict(
            property_id="123",
            operation="CREATE",
            event_name="generate_lead",
            counting_method="once_per_session",
            default_value=20,
            currency_code="eur",
        )
        proposal = json.loads(analytics_management.manage_key_event(**kwargs))
        self.assertEqual(proposal["status"], "PROPOSED")
        self.admin.create_key_event.assert_not_called()
        fields = {
            i["field_readable"]: i["value_readable"]
            for i in proposal["change_items"]
        }
        self.assertEqual(fields["event_name"], "generate_lead")
        self.assertEqual(fields["default_value.currency_code"], "EUR")

        self.admin.create_key_event.return_value = admin_types.KeyEvent(
            name="properties/123/keyEvents/9", event_name="generate_lead"
        )
        result = json.loads(
            analytics_management.manage_key_event(
                **kwargs, validate_only=False, checksum=proposal["checksum"]
            )
        )
        self.assertEqual(result["status"], "DONE")
        req = self.admin.create_key_event.call_args.kwargs["request"]
        self.assertEqual(req.parent, "properties/123")
        self.assertEqual(req.key_event.counting_method.name, "ONCE_PER_SESSION")
        self.assertEqual(req.key_event.default_value.numeric_value, 20)

    def test_commit_requires_matching_checksum(self):
        with self.assertRaises(ToolError):
            analytics_management.manage_key_event(
                property_id="123",
                operation="CREATE",
                event_name="x",
                validate_only=False,
                checksum="0" * 64,
            )
        self.admin.create_key_event.assert_not_called()

    def test_update_shows_current_value(self):
        self.admin.get_data_retention_settings.return_value = (
            admin_types.DataRetentionSettings(
                name="properties/5/dataRetentionSettings",
                event_data_retention="TWO_MONTHS",
            )
        )
        proposal = json.loads(
            analytics_management.update_data_retention(
                property_id="5", event_data_retention="fourteen_months"
            )
        )
        item = proposal["change_items"][0]
        self.assertEqual(item["value_readable"], "FOURTEEN_MONTHS")
        self.assertEqual(item["original_value_readable"], "TWO_MONTHS")
        self.admin.update_data_retention_settings.return_value = (
            admin_types.DataRetentionSettings(
                event_data_retention="FOURTEEN_MONTHS"
            )
        )
        analytics_management.update_data_retention(
            property_id="5",
            event_data_retention="fourteen_months",
            validate_only=False,
            checksum=proposal["checksum"],
        )
        req = self.admin.update_data_retention_settings.call_args.kwargs[
            "request"
        ]
        self.assertEqual(list(req.update_mask.paths), ["event_data_retention"])
        self.assertEqual(
            req.data_retention_settings.name,
            "properties/5/dataRetentionSettings",
        )

    def test_invalid_enum_rejected_before_api(self):
        with self.assertRaises(ToolError):
            analytics_management.manage_custom_dimension(
                property_id="1",
                operation="CREATE",
                parameter_name="lead_type",
                display_name="Lead type",
                scope="GALAXY",
            )

    def test_generic_resource_and_blocked_ops(self):
        proposal = json.loads(
            analytics_management.manage_resource(
                resource_type="calculated_metric",
                operation="CREATE",
                property_id="1",
                calculated_metric_id="revenue_per_lead",
                fields='{"display_name":"Revenue per lead",'
                '"formula":"{{purchaseRevenue}} / {{keyEvents:generate_lead}}",'
                '"metric_unit":"CURRENCY"}',
            )
        )
        self.assertEqual(proposal["status"], "PROPOSED")
        with self.assertRaises(ToolError):
            analytics_management.manage_resource(
                resource_type="property", operation="DELETE", property_id="1"
            )
        with self.assertRaises(ToolError):
            analytics_management.manage_resource(
                resource_type="custom_dimension",
                operation="CREATE",
                property_id="1",
                fields={"no_such_field": 1},
            )

    def test_user_access_roles(self):
        proposal = json.loads(
            analytics_management.manage_user_access(
                operation="CREATE",
                property_id="1",
                user_email="a@example.com",
                roles=["Analyst"],
            )
        )
        values = [i["value_readable"] for i in proposal["change_items"]]
        self.assertIn('["predefinedRoles/analyst"]', values)
        with self.assertRaises(ToolError):
            analytics_management.manage_user_access(
                operation="CREATE",
                property_id="1",
                user_email="a@b.c",
                roles=["god"],
            )

    def test_annotation_dates(self):
        proposal = json.loads(
            analytics_management.manage_annotation(
                property_id="1",
                operation="CREATE",
                title="FR extensies vervangen",
                date="2026-11-21",
                color="blue",
            )
        )
        fields = {
            i["field_readable"]: i["value_readable"]
            for i in proposal["change_items"]
        }
        self.assertEqual(fields["annotation_date.day"], "21")
        with self.assertRaises(ToolError):
            analytics_management.manage_annotation(
                property_id="1", operation="CREATE", title="x"
            )


class AdminToolsTest(unittest.TestCase):
    def test_list_and_get(self):
        admin = MagicMock()
        admin.list_key_events.return_value = [
            admin_types.KeyEvent(
                name="properties/1/keyEvents/2", event_name="purchase"
            )
        ]
        admin.get_attribution_settings.return_value = (
            admin_types.AttributionSettings(
                name="properties/1/attributionSettings"
            )
        )
        with patch.object(ga, "admin_client", return_value=admin):
            out = json.loads(
                analytics_admin.list_resources("key_events", property_id="1")
            )
            self.assertEqual(out["items"][0]["event_name"], "purchase")
            got = json.loads(
                analytics_admin.get_resource(
                    "attribution_settings", property_id="1"
                )
            )
            self.assertEqual(got["name"], "properties/1/attributionSettings")
            schema = json.loads(analytics_admin.get_resource_schema("audience"))
            self.assertIn("filter_clauses", schema["fields"])
            types_out = json.loads(analytics_admin.list_resource_types())
            self.assertTrue(types_out["resource_types"])


class SafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_property_allowlist(self):
        from types import SimpleNamespace
        from ads_mcp.safety import AccountScopeMiddleware

        mw = AccountScopeMiddleware()
        ctx = SimpleNamespace(
            message=SimpleNamespace(arguments={"property_id": "999"})
        )

        async def _next(_):
            return "ok"

        with patch.dict(
            os.environ, {"GOOGLE_ADS_MCP_ANALYTICS_PROPERTY_IDS": "111"}
        ):
            with self.assertRaises(ToolError):
                await mw.on_call_tool(ctx, _next)
            ctx.message.arguments = {"property_id": "properties/111"}
            self.assertEqual(await mw.on_call_tool(ctx, _next), "ok")


class OutputTest(unittest.TestCase):
    def test_to_json_trims_lists(self):
        payload = {"rows": [{"a": "x" * 100} for _ in range(5000)]}
        text = ga.to_json(payload, limit=20_000)
        self.assertLessEqual(len(text), 20_000)
        self.assertIn("truncated", json.loads(text))

    def test_api_errors(self):
        from google.api_core import exceptions as gexc

        msg = ga.format_api_error(gexc.PermissionDenied("nope"))
        self.assertIn("Viewer", msg)
        with self.assertRaises(ToolError):
            ga.call_api(MagicMock(side_effect=gexc.NotFound("gone")))


if __name__ == "__main__":
    unittest.main()
