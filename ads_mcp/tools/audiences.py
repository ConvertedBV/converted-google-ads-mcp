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

"""Audience tools: remarketing lists, audience targeting, demographics.

Performance Max uses audience signals on asset groups instead (see the
campaign builder); these tools are for Search and Display campaigns.
"""

from enum import Enum
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
import ads_mcp.utils as utils

audiences_mcp = FastMCP("audiences")

_TARGETABLE_CHANNELS = ("SEARCH", "DISPLAY")


class AudienceLevel(str, Enum):
    CAMPAIGN = "CAMPAIGN"
    AD_GROUP = "AD_GROUP"


class AudienceOperation(str, Enum):
    ADD = "ADD"
    REMOVE = "REMOVE"


class AudienceMode(str, Enum):
    """OBSERVATION adds audiences for reporting and bid insight without
    limiting reach; TARGETING restricts serving to the audiences."""

    OBSERVATION = "OBSERVATION"
    TARGETING = "TARGETING"


class DemographicOperation(str, Enum):
    EXCLUDE = "EXCLUDE"
    REMOVE_EXCLUSIONS = "REMOVE_EXCLUSIONS"


class AgeRange(str, Enum):
    AGE_RANGE_18_24 = "AGE_RANGE_18_24"
    AGE_RANGE_25_34 = "AGE_RANGE_25_34"
    AGE_RANGE_35_44 = "AGE_RANGE_35_44"
    AGE_RANGE_45_54 = "AGE_RANGE_45_54"
    AGE_RANGE_55_64 = "AGE_RANGE_55_64"
    AGE_RANGE_65_UP = "AGE_RANGE_65_UP"
    AGE_RANGE_UNDETERMINED = "AGE_RANGE_UNDETERMINED"


class Gender(str, Enum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    UNDETERMINED = "UNDETERMINED"


# ---------------------------------------------------------------------------
# Remarketing lists
# ---------------------------------------------------------------------------


@audiences_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_remarketing_list(
    customer_id: str | int,
    name: str,
    url_contains: list[str],
    url_excludes: list[str] | None = None,
    membership_days: int = 30,
    description: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a website remarketing list based on visited page URLs.

    Visitors of a page whose URL contains any of `url_contains` (and none of
    `url_excludes`) join the list. The list is pre-filled with matching
    visitors of the last 30 days. Requires the Google tag on the website.
    Use the returned user list ID with manage_audience_targeting.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        name: Unique list name.
        url_contains: URL fragments, e.g. ["/airco", "/offerte"] (max 20).
        url_excludes: Optional URL fragments that exclude a visitor, e.g.
            ["/bedankt"] to leave out converters (max 20).
        membership_days: How long a visitor stays on the list (1-540).
        description: Optional description.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    list_name = " ".join((name or "").split())
    if not list_name:
        raise ToolError("Error: 'name' is required.")
    includes = mh.clean_texts(url_contains, "url_contains", 20)
    excludes = (
        mh.clean_texts(url_excludes, "url_excludes", 20) if url_excludes else []
    )
    if not 1 <= int(membership_days) <= 540:
        raise ToolError("Error: 'membership_days' must be 1-540.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums

    def _operand(fragment: str) -> Any:
        operand = ga_client.get_type("FlexibleRuleOperandInfo")
        group = ga_client.get_type("UserListRuleItemGroupInfo")
        item = ga_client.get_type("UserListRuleItemInfo")
        item.name = "url__"
        item.string_rule_item.operator = (
            enums.UserListStringRuleItemOperatorEnum.CONTAINS
        )
        item.string_rule_item.value = fragment
        group.rule_items.append(item)
        operand.rule.rule_item_groups.append(group)
        return operand

    op = ga_client.get_type("UserListOperation")
    user_list = op.create
    user_list.name = list_name
    if description:
        user_list.description = description.strip()
    user_list.membership_status = enums.UserListMembershipStatusEnum.OPEN
    user_list.membership_life_span = int(membership_days)
    rule = user_list.rule_based_user_list
    rule.prepopulation_status = enums.UserListPrepopulationStatusEnum.REQUESTED
    flexible = rule.flexible_rule_user_list
    flexible.inclusive_rule_operator = enums.UserListFlexibleRuleOperatorEnum.OR
    flexible.inclusive_operands.extend(_operand(f) for f in includes)
    flexible.exclusive_operands.extend(_operand(f) for f in excludes)

    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Remarketing List",
        primary_key_readable=list_name,
        fields={
            "URL contains (any)": ", ".join(includes),
            "Exclude URL contains": ", ".join(excludes) or None,
            "Membership (days)": int(membership_days),
            "Prefill": "Visitors of the last 30 days",
        },
    )
    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="UserListService",
        request_type="MutateUserListsRequest",
        mutate_method="mutate_user_lists",
        operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_remarketing_list",
            "name": list_name,
            "includes": includes,
            "excludes": excludes,
            "membership_days": int(membership_days),
            "description": description,
        },
        change_items=change_items,
        success_message=f"Created remarketing list '{list_name}'.",
        error_prefix="Failed to create remarketing list",
    )


# ---------------------------------------------------------------------------
# Audience targeting
# ---------------------------------------------------------------------------

_LEVELS = {
    "CAMPAIGN": {
        "resource": "campaign",
        "criterion": "campaign_criterion",
        "criterion_op": "campaign_criterion_operation",
        "entity_op": "campaign_operation",
        "entity_prefix": "campaigns",
        "criterion_prefix": "campaignCriteria",
        "parent_field": "campaign",
    },
    "AD_GROUP": {
        "resource": "ad_group",
        "criterion": "ad_group_criterion",
        "criterion_op": "ad_group_criterion_operation",
        "entity_op": "ad_group_operation",
        "entity_prefix": "adGroups",
        "criterion_prefix": "adGroupCriteria",
        "parent_field": "ad_group",
    },
}
_AUDIENCE_TYPES = ("USER_LIST", "USER_INTEREST", "CUSTOM_AUDIENCE")


@audiences_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_audience_targeting(
    customer_id: str | int,
    level: AudienceLevel,
    entity_id: str,
    operation: AudienceOperation,
    user_list_ids: list[str] | None = None,
    user_interest_ids: list[str] | None = None,
    custom_audience_ids: list[str] | None = None,
    mode: AudienceMode = AudienceMode.OBSERVATION,
    criterion_ids: list[str] | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds or removes audiences on a Search or Display campaign or ad group.

    ADD: pass user list (remarketing, customer match), user interest
    (in-market, affinity; find IDs on the user_interest resource) and/or
    custom audience IDs, plus `mode`. OBSERVATION (default) keeps full reach
    and adds audience reporting; TARGETING limits serving to the audiences.
    The mode applies to all audiences of that campaign or ad group, and the
    proposal shows when it changes. REMOVE: pass `criterion_ids`; only
    audience criteria are removed.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        level: 'CAMPAIGN' or 'AD_GROUP'.
        entity_id: The numeric campaign or ad group ID.
        operation: 'ADD' or 'REMOVE'.
        user_list_ids: User list IDs to add.
        user_interest_ids: User interest IDs to add.
        custom_audience_ids: Custom audience IDs to add.
        mode: OBSERVATION (default) or TARGETING (ADD only).
        criterion_ids: Criterion IDs to remove (REMOVE only).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    level_value = mh.enum_value(level, AudienceLevel, "level")
    op_value = mh.enum_value(operation, AudienceOperation, "operation")
    entity = mh.require_simple_id(entity_id, "entity_id")
    config = _LEVELS[level_value]
    entity_rn = f"customers/{cid}/{config['entity_prefix']}/{entity}"

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        f"SELECT {config['resource']}.name, "
        f"{config['resource']}.targeting_setting.target_restrictions, "
        "campaign.advertising_channel_type "
        f"FROM {config['resource']} WHERE {config['resource']}.id = {entity}",
    )
    if not rows:
        raise ToolError(f"Error: {level_value} {entity} was not found.")
    row = rows[0]
    channel = row.campaign.advertising_channel_type.name
    if channel not in _TARGETABLE_CHANNELS:
        raise ToolError(
            f"Error: Audience criteria apply to Search and Display; this is a "
            f"{channel} campaign (use asset group signals for PMax)."
        )
    entity_message = getattr(row, config["resource"])
    entity_label = f"{entity_message.name} ({entity_rn})"
    operations: list[Any] = []
    change_items: list[pac.ChangeItem] = []

    if op_value == "REMOVE":
        ids = mh.require_ids(criterion_ids, "criterion_ids")
        rns = [
            f"customers/{cid}/{config['criterion_prefix']}/{entity}~{c}"
            for c in ids
        ]
        crit = config["criterion"]
        found_rows = mh.search(
            ga_client,
            cid,
            f"SELECT {crit}.resource_name, {crit}.type, {crit}.display_name "
            f"FROM {crit} WHERE {crit}.resource_name IN "
            f"({mh.quoted_list(rns)}) AND {crit}.type IN "
            f"({mh.quoted_list(list(_AUDIENCE_TYPES))})",
        )
        found = {
            getattr(r, crit).resource_name: getattr(r, crit) for r in found_rows
        }
        missing = [rn for rn in rns if rn not in found]
        if missing:
            raise ToolError(
                f"Error: Not audience criteria of {entity_label}: "
                f"{', '.join(missing)}"
            )
        for rn in rns:
            mutate_op = ga_client.get_type("MutateOperation")
            getattr(mutate_op, config["criterion_op"]).remove = rn
            operations.append(mutate_op)
            change_items.append(
                pac.ChangeItem(
                    "REMOVE",
                    "Audience",
                    entity_label,
                    found[rn].type_.name.replace("_", " ").title(),
                    found[rn].display_name,
                )
            )
        payload: dict[str, Any] = {"criterion_ids": ids}
    else:
        mode_value = mh.enum_value(mode, AudienceMode, "mode")
        audiences = (
            [
                ("user_list", "user_list", f"userLists/{i}")
                for i in mh.dedupe(
                    [
                        mh.require_simple_id(i, "user_list_ids")
                        for i in user_list_ids or []
                    ]
                )
            ]
            + [
                (
                    "user_interest",
                    "user_interest_category",
                    f"userInterests/{i}",
                )
                for i in mh.dedupe(
                    [
                        mh.require_simple_id(i, "user_interest_ids")
                        for i in user_interest_ids or []
                    ]
                )
            ]
            + [
                ("custom_audience", "custom_audience", f"customAudiences/{i}")
                for i in mh.dedupe(
                    [
                        mh.require_simple_id(i, "custom_audience_ids")
                        for i in custom_audience_ids or []
                    ]
                )
            ]
        )
        if not audiences:
            raise ToolError("Error: Pass at least one audience ID to ADD.")
        if len(audiences) > mh.MAX_ENTITIES_PER_CALL:
            raise ToolError("Error: Too many audiences in one call.")

        # Keep other targeting dimensions; set AUDIENCE to the chosen mode.
        bid_only = mode_value == "OBSERVATION"
        restrictions = list(
            entity_message.targeting_setting.target_restrictions
        )
        current = next(
            (
                r
                for r in restrictions
                if r.targeting_dimension.name == "AUDIENCE"
            ),
            None,
        )
        current_mode = (
            None
            if current is None
            else ("OBSERVATION" if current.bid_only else "TARGETING")
        )
        if current_mode != mode_value:
            entity_op = ga_client.get_type("MutateOperation")
            update = getattr(entity_op, config["entity_op"]).update
            update.resource_name = entity_rn
            for restriction in restrictions:
                if restriction.targeting_dimension.name == "AUDIENCE":
                    continue
                kept = ga_client.get_type("TargetRestriction")
                kept.targeting_dimension = restriction.targeting_dimension
                kept.bid_only = restriction.bid_only
                update.targeting_setting.target_restrictions.append(kept)
            audience_restriction = ga_client.get_type("TargetRestriction")
            audience_restriction.targeting_dimension = (
                ga_client.enums.TargetingDimensionEnum.AUDIENCE
            )
            audience_restriction.bid_only = bid_only
            update.targeting_setting.target_restrictions.append(
                audience_restriction
            )
            getattr(entity_op, config["entity_op"]).update_mask.paths.append(
                "targeting_setting.target_restrictions"
            )
            operations.append(entity_op)
            change_items.append(
                pac.ChangeItem(
                    "UPDATE",
                    "Audience mode",
                    entity_label,
                    "Mode",
                    mode_value,
                    current_mode or "not set (OBSERVATION)",
                )
            )

        for kind, field, suffix in audiences:
            mutate_op = ga_client.get_type("MutateOperation")
            criterion = getattr(mutate_op, config["criterion_op"]).create
            setattr(criterion, config["parent_field"], entity_rn)
            setattr(
                getattr(criterion, kind), field, f"customers/{cid}/{suffix}"
            )
            operations.append(mutate_op)
            change_items.append(
                pac.ChangeItem(
                    "CREATE",
                    "Audience",
                    entity_label,
                    kind.replace("_", " ").title(),
                    suffix.rsplit("/", 1)[-1],
                )
            )
        payload = {"mode": mode_value, "audiences": [a[2] for a in audiences]}

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_audience_targeting",
            "level": level_value,
            "entity_id": entity,
            "operation": op_value,
            **payload,
        },
        change_items=change_items,
        success_message=f"Updated audiences of {entity_label}.",
        error_prefix="Failed to update audiences",
    )


# ---------------------------------------------------------------------------
# Demographics
# ---------------------------------------------------------------------------


@audiences_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_demographic_exclusions(
    customer_id: str | int,
    ad_group_id: str,
    operation: DemographicOperation,
    age_ranges: list[AgeRange] | None = None,
    genders: list[Gender] | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Excludes (or re-includes) age ranges and genders in an ad group.

    EXCLUDE stops showing ads to those demographics; REMOVE_EXCLUSIONS lifts
    existing exclusions for the given values. Works for Search, Display and
    Demand Gen ad groups.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        ad_group_id: The numeric ad group ID.
        operation: 'EXCLUDE' or 'REMOVE_EXCLUSIONS'.
        age_ranges: e.g. ["AGE_RANGE_18_24"].
        genders: e.g. ["UNDETERMINED"].
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    ad_group = mh.require_simple_id(ad_group_id, "ad_group_id")
    op_value = mh.enum_value(operation, DemographicOperation, "operation")
    ages = mh.dedupe(
        [mh.enum_value(a, AgeRange, "age_ranges") for a in age_ranges or []]
    )
    sexes = mh.dedupe(
        [mh.enum_value(g, Gender, "genders") for g in genders or []]
    )
    if not ages and not sexes:
        raise ToolError("Error: Pass at least one age range or gender.")
    wanted = [("AGE_RANGE", a) for a in ages] + [("GENDER", g) for g in sexes]
    ad_group_rn = f"customers/{cid}/adGroups/{ad_group}"

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT ad_group_criterion.resource_name, ad_group_criterion.type, "
        "ad_group_criterion.age_range.type, ad_group_criterion.gender.type, "
        "ad_group.name FROM ad_group_criterion "
        f"WHERE ad_group.id = {ad_group} "
        "AND ad_group_criterion.type IN ('AGE_RANGE', 'GENDER') "
        "AND ad_group_criterion.negative = TRUE",
    )
    existing = {}
    group_name = ad_group_rn
    for row in rows:
        criterion = row.ad_group_criterion
        group_name = row.ad_group.name or group_name
        if criterion.type_.name == "AGE_RANGE":
            existing[("AGE_RANGE", criterion.age_range.type_.name)] = (
                criterion.resource_name
            )
        else:
            existing[("GENDER", criterion.gender.type_.name)] = (
                criterion.resource_name
            )

    operations = []
    change_items: list[pac.ChangeItem] = []
    for kind, value in wanted:
        op = ga_client.get_type("AdGroupCriterionOperation")
        if op_value == "EXCLUDE":
            if (kind, value) in existing:
                continue
            op.create.ad_group = ad_group_rn
            op.create.negative = True
            if kind == "AGE_RANGE":
                op.create.age_range.type_ = getattr(
                    ga_client.enums.AgeRangeTypeEnum, value
                )
            else:
                op.create.gender.type_ = getattr(
                    ga_client.enums.GenderTypeEnum, value
                )
        else:
            if (kind, value) not in existing:
                raise ToolError(
                    f"Error: {value} is not excluded in ad group {ad_group}."
                )
            op.remove = existing[(kind, value)]
        operations.append(op)
        change_items.append(
            pac.ChangeItem(
                "CREATE" if op_value == "EXCLUDE" else "REMOVE",
                "Demographic exclusion",
                group_name,
                kind.replace("_", " ").title(),
                value,
            )
        )
    if not operations:
        raise ToolError("Error: These demographics are already excluded.")

    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="AdGroupCriterionService",
        request_type="MutateAdGroupCriteriaRequest",
        mutate_method="mutate_ad_group_criteria",
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_demographic_exclusions",
            "ad_group_id": ad_group,
            "operation": op_value,
            "wanted": [f"{k}:{v}" for k, v in wanted],
        },
        change_items=change_items,
        success_message=f"Updated demographic exclusions of {group_name}.",
        error_prefix="Failed to update demographic exclusions",
    )
