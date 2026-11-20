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

"""Tests for audiences, account exclusions and experiments."""

import unittest
from unittest.mock import MagicMock

from fastmcp.exceptions import ToolError

from ads_mcp.tools import account_exclusions as ax
from ads_mcp.tools import audiences as au
from ads_mcp.tools import experiments as ex
from tests.tools.mutate_tool_base import CID, MutateToolTestCase


class TestRemarketingList(MutateToolTestCase):

    def test_create(self):
        proposal = self.propose(
            au.create_remarketing_list,
            customer_id=CID,
            name="Bezoekers airco",
            url_contains=["/airco", "/offerte"],
            url_excludes=["/bedankt"],
            membership_days=90,
        )
        self.assertEqual(
            self.items(proposal)["URL contains (any)"]["value_readable"],
            "/airco, /offerte",
        )
        user_list = (
            self.request("UserListService", "mutate_user_lists")
            .operations[0]
            .create
        )
        self.assertEqual(user_list.membership_life_span, 90)
        rule = user_list.rule_based_user_list
        self.assertEqual(rule.prepopulation_status.name, "REQUESTED")
        flexible = rule.flexible_rule_user_list
        self.assertEqual(flexible.inclusive_rule_operator.name, "OR")
        self.assertEqual(len(flexible.inclusive_operands), 2)
        item = (
            flexible.inclusive_operands[0]
            .rule.rule_item_groups[0]
            .rule_items[0]
        )
        self.assertEqual(item.name, "url__")
        self.assertEqual(item.string_rule_item.operator.name, "CONTAINS")
        self.assertEqual(item.string_rule_item.value, "/airco")
        excluded = (
            flexible.exclusive_operands[0]
            .rule.rule_item_groups[0]
            .rule_items[0]
        )
        self.assertEqual(excluded.string_rule_item.value, "/bedankt")

    def test_membership_bounds(self):
        with self.assertRaisesRegex(ToolError, "1-540"):
            au.create_remarketing_list(
                customer_id=CID,
                name="X",
                url_contains=["/a"],
                membership_days=600,
            )


class TestAudienceTargeting(MutateToolTestCase):

    def _campaign(self, channel="SEARCH", restrictions=()):
        row = self.row(
            {
                "campaign.name": "Generic",
                "campaign.advertising_channel_type": getattr(
                    self.enums.AdvertisingChannelTypeEnum, channel
                ),
            }
        )
        for dimension, bid_only in restrictions:
            restriction = self.real_ga_client.get_type("TargetRestriction")
            restriction.targeting_dimension = getattr(
                self.enums.TargetingDimensionEnum, dimension
            )
            restriction.bid_only = bid_only
            row.campaign.targeting_setting.target_restrictions.append(
                restriction
            )
        return row

    def test_add_observation_keeps_other_dimensions(self):
        self.search_results([self._campaign(restrictions=[("TOPIC", False)])])
        proposal = self.propose(
            au.manage_audience_targeting,
            customer_id=CID,
            level="CAMPAIGN",
            entity_id="111",
            operation="ADD",
            user_list_ids=["5"],
            user_interest_ids=["80432"],
        )
        mode = self.items(proposal)["Mode"]
        self.assertEqual(mode["value_readable"], "OBSERVATION")
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        update = ops[0].campaign_operation
        dims = {
            r.targeting_dimension.name: r.bid_only
            for r in update.update.targeting_setting.target_restrictions
        }
        self.assertEqual(dims, {"TOPIC": False, "AUDIENCE": True})
        self.assertEqual(
            list(update.update_mask.paths),
            ["targeting_setting.target_restrictions"],
        )
        self.assertEqual(
            ops[1].campaign_criterion_operation.create.user_list.user_list,
            f"customers/{CID}/userLists/5",
        )
        self.assertEqual(
            ops[
                2
            ].campaign_criterion_operation.create.user_interest.user_interest_category,
            f"customers/{CID}/userInterests/80432",
        )

    def test_no_mode_change_when_already_set(self):
        self.search_results([self._campaign(restrictions=[("AUDIENCE", True)])])
        self.propose(
            au.manage_audience_targeting,
            customer_id=CID,
            level="CAMPAIGN",
            entity_id="111",
            operation="ADD",
            user_list_ids=["5"],
        )
        ops = self.request("GoogleAdsService", "mutate").mutate_operations
        self.assertEqual(len(ops), 1)

    def test_pmax_refused(self):
        self.search_results([self._campaign(channel="PERFORMANCE_MAX")])
        with self.assertRaisesRegex(ToolError, "asset group signals"):
            au.manage_audience_targeting(
                customer_id=CID,
                level="CAMPAIGN",
                entity_id="111",
                operation="ADD",
                user_list_ids=["5"],
            )

    def test_remove_only_audiences(self):
        self.search_results([self._campaign()], [])
        with self.assertRaisesRegex(ToolError, "Not audience criteria"):
            au.manage_audience_targeting(
                customer_id=CID,
                level="CAMPAIGN",
                entity_id="111",
                operation="REMOVE",
                criterion_ids=["9"],
            )
        self.assertIn(
            "'USER_LIST', 'USER_INTEREST', 'CUSTOM_AUDIENCE'", self.queries()[1]
        )


class TestDemographics(MutateToolTestCase):

    def test_exclude_skips_existing(self):
        existing = self.row(
            {
                "ad_group_criterion.resource_name": f"customers/{CID}/adGroupCriteria/5~503001",
                "ad_group_criterion.type_": self.enums.CriterionTypeEnum.AGE_RANGE,
                "ad_group_criterion.age_range.type_": self.enums.AgeRangeTypeEnum.AGE_RANGE_18_24,
                "ad_group.name": "Airco",
            }
        )
        self.search_results([existing])
        proposal = self.propose(
            au.manage_demographic_exclusions,
            customer_id=CID,
            ad_group_id="5",
            operation="EXCLUDE",
            age_ranges=["AGE_RANGE_18_24", "AGE_RANGE_65_UP"],
            genders=["UNDETERMINED"],
        )
        self.assertEqual(len(proposal["change_items"]), 2)
        ops = self.request(
            "AdGroupCriterionService", "mutate_ad_group_criteria"
        ).operations
        self.assertTrue(ops[0].create.negative)
        self.assertEqual(ops[0].create.age_range.type_.name, "AGE_RANGE_65_UP")
        self.assertEqual(ops[1].create.gender.type_.name, "UNDETERMINED")

    def test_remove_missing_exclusion(self):
        self.search_results([])
        with self.assertRaisesRegex(ToolError, "is not excluded"):
            au.manage_demographic_exclusions(
                customer_id=CID,
                ad_group_id="5",
                operation="REMOVE_EXCLUSIONS",
                genders=["MALE"],
            )


class TestAccountExclusions(MutateToolTestCase):

    def test_add(self):
        self.propose(
            ax.manage_account_exclusions,
            customer_id=CID,
            operation="ADD",
            placements=["https://Spam-Site.com/page", "games.example.org"],
            youtube_channel_ids=["UCabcdefghijklmnopqrstuv"],
            content_labels=["PARKED_DOMAIN"],
        )
        ops = self.request(
            "CustomerNegativeCriterionService",
            "mutate_customer_negative_criteria",
        ).operations
        self.assertEqual(ops[0].create.placement.url, "spam-site.com/page")
        self.assertEqual(
            ops[2].create.youtube_channel.channel_id, "UCabcdefghijklmnopqrstuv"
        )
        self.assertEqual(
            ops[3].create.content_label.type_.name, "PARKED_DOMAIN"
        )

    def test_invalid_inputs(self):
        with self.assertRaisesRegex(ToolError, "Invalid placement"):
            ax.manage_account_exclusions(
                customer_id=CID, operation="ADD", placements=["not a domain"]
            )
        with self.assertRaisesRegex(ToolError, "Invalid YouTube channel"):
            ax.manage_account_exclusions(
                customer_id=CID, operation="ADD", youtube_channel_ids=["abc"]
            )


class TestExperiments(MutateToolTestCase):

    def _campaign(self, channel="SEARCH"):
        return self.row(
            {
                "campaign.name": "Generic",
                "campaign.advertising_channel_type": getattr(
                    self.enums.AdvertisingChannelTypeEnum, channel
                ),
            }
        )

    def test_create_returns_treatment_draft(self):
        self.search_results([self._campaign()], [self._campaign()])
        experiments = self.service("ExperimentService")
        created = MagicMock()
        created.results = [
            MagicMock(resource_name=f"customers/{CID}/experiments/42")
        ]
        experiments.mutate_experiments.return_value = created
        arms = self.service("ExperimentArmService")
        arm_result = self.real_ga_client.get_type(
            "MutateExperimentArmsResponse"
        )
        for drafts in ([], [f"customers/{CID}/campaigns/999"]):
            result = self.real_ga_client.get_type("MutateExperimentArmResult")
            result.experiment_arm.in_design_campaigns.extend(drafts)
            arm_result.results.append(result)
        arms.mutate_experiment_arms.return_value = arm_result

        kwargs = dict(
            customer_id=CID,
            base_campaign_id="111",
            name="tCPA test",
            traffic_split_percent=30,
        )
        proposal = self.propose(ex.create_experiment, **kwargs)
        self.assertEqual(
            self.items(proposal)["Traffic split"]["value_readable"],
            "70% control / 30% treatment",
        )
        validate_request = self.request(
            "ExperimentService", "mutate_experiments"
        )
        self.assertTrue(validate_request.validate_only)
        self.assertEqual(
            validate_request.operations[0].create.type_.name, "SEARCH_CUSTOM"
        )
        arms.mutate_experiment_arms.assert_not_called()

        result = self.commit(ex.create_experiment, proposal, **kwargs)
        self.assertIn("Created experiment 42", result)
        self.assertIn("Treatment draft campaign ID: 999", result)
        arms_request = self.request(
            "ExperimentArmService", "mutate_experiment_arms"
        )
        control, treatment = arms_request.operations
        self.assertTrue(control.create.control)
        self.assertEqual(control.create.traffic_split, 70)
        self.assertEqual(
            list(control.create.campaigns), [f"customers/{CID}/campaigns/111"]
        )
        self.assertEqual(treatment.create.traffic_split, 30)

    def test_failed_arms_remove_the_experiment(self):
        self.search_results([self._campaign()], [self._campaign()])
        experiments = self.service("ExperimentService")
        created = MagicMock()
        created.results = [
            MagicMock(resource_name=f"customers/{CID}/experiments/42")
        ]
        experiments.mutate_experiments.return_value = created
        self.service(
            "ExperimentArmService"
        ).mutate_experiment_arms.side_effect = RuntimeError("arm error")
        kwargs = dict(customer_id=CID, base_campaign_id="111", name="X")
        proposal = self.propose(ex.create_experiment, **kwargs)
        with self.assertRaisesRegex(ToolError, "arm error"):
            self.commit(ex.create_experiment, proposal, **kwargs)
        cleanup = self.request("ExperimentService", "mutate_experiments")
        self.assertEqual(
            cleanup.operations[0].remove, f"customers/{CID}/experiments/42"
        )

    def test_pmax_not_supported(self):
        self.search_results([self._campaign("PERFORMANCE_MAX")])
        with self.assertRaisesRegex(ToolError, "Search and Display"):
            ex.create_experiment(
                customer_id=CID, base_campaign_id="111", name="X"
            )

    def test_schedule_and_promote(self):
        row = self.row(
            {
                "experiment.name": "tCPA test",
                "experiment.status": self.enums.ExperimentStatusEnum.SETUP,
            }
        )
        self.search_results([row], [row])
        service = self.service("ExperimentService")
        kwargs = dict(customer_id=CID, experiment_id="42", action="SCHEDULE")
        proposal = self.propose(ex.manage_experiment, **kwargs)
        self.assertTrue(
            self.request(
                "ExperimentService", "schedule_experiment"
            ).validate_only
        )
        result = self.commit(ex.manage_experiment, proposal, **kwargs)
        self.assertFalse(
            self.request(
                "ExperimentService", "schedule_experiment"
            ).validate_only
        )
        self.assertIn("check experiment.status", result)
        self.assertEqual(service.schedule_experiment.call_count, 2)


if __name__ == "__main__":
    unittest.main()
