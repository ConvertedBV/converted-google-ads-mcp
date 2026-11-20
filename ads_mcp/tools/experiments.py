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

"""Campaign experiments (A/B tests) for Search and Display campaigns.

Flow: create_experiment creates the experiment with a control arm (the base
campaign) and a treatment arm, which gets a draft copy of the campaign.
Change that draft with the campaign_management tools, then SCHEDULE it with
manage_experiment. After the test, END it, or PROMOTE the treatment to apply
its changes to the base campaign.
"""

from enum import Enum
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
import ads_mcp.utils as utils

experiments_mcp = FastMCP("experiments")

_EXPERIMENT_TYPES = {"SEARCH": "SEARCH_CUSTOM", "DISPLAY": "DISPLAY_CUSTOM"}


class ExperimentAction(str, Enum):
    SCHEDULE = "SCHEDULE"
    END = "END"
    PROMOTE = "PROMOTE"


@experiments_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_experiment(
    customer_id: str | int,
    base_campaign_id: str,
    name: str,
    traffic_split_percent: int = 50,
    start_date: str | None = None,
    end_date: str | None = None,
    description: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates an A/B experiment for a Search or Display campaign.

    The treatment arm gets `traffic_split_percent` of the traffic and a draft
    copy of the campaign; the result returns its campaign ID. Change that
    draft (bidding, keywords, ads, ...) with the campaign_management tools,
    then start the test with manage_experiment SCHEDULE. Nothing serves
    until it is scheduled. The dry run validates the experiment; the arms
    are validated when committing (if they fail, the experiment is removed).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        base_campaign_id: The campaign to test against.
        name: Unique experiment name.
        traffic_split_percent: Share of traffic for the treatment (1-99).
        start_date: Optional start date YYYY-MM-DD (default: when scheduled).
        end_date: Optional end date YYYY-MM-DD.
        description: Optional description of the hypothesis.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaign_id = mh.require_simple_id(base_campaign_id, "base_campaign_id")
    experiment_name = " ".join((name or "").split())
    if not experiment_name:
        raise ToolError("Error: 'name' is required.")
    split = int(traffic_split_percent)
    if not 1 <= split <= 99:
        raise ToolError("Error: 'traffic_split_percent' must be 1-99.")
    start = mh.require_date(start_date, "start_date")
    end = mh.require_date(end_date, "end_date")
    if start and end and end <= start:
        raise ToolError("Error: 'end_date' must be after 'start_date'.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign.name, campaign.advertising_channel_type FROM campaign "
        f"WHERE campaign.id = {campaign_id}",
    )
    if not rows:
        raise ToolError(f"Error: Campaign {campaign_id} was not found.")
    channel = rows[0].campaign.advertising_channel_type.name
    if channel not in _EXPERIMENT_TYPES:
        raise ToolError(
            f"Error: Experiments here support Search and Display campaigns; "
            f"this is {channel}."
        )
    campaign_rn = f"customers/{cid}/campaigns/{campaign_id}"
    enums = ga_client.enums

    experiment_op = ga_client.get_type("ExperimentOperation")
    experiment = experiment_op.create
    experiment.name = experiment_name
    experiment.type_ = getattr(
        enums.ExperimentTypeEnum, _EXPERIMENT_TYPES[channel]
    )
    experiment.status = enums.ExperimentStatusEnum.SETUP
    experiment.suffix = "[experiment]"
    if description:
        experiment.description = description.strip()
    if start:
        experiment.start_date = start
    if end:
        experiment.end_date = end

    experiment_service = utils.get_googleads_service_from_client(
        ga_client, "ExperimentService"
    )
    arm_service = utils.get_googleads_service_from_client(
        ga_client, "ExperimentArmService"
    )

    def _experiment_request(validate: bool) -> Any:
        request = ga_client.get_type("MutateExperimentsRequest")
        request.customer_id = cid
        request.operations.append(experiment_op)
        request.validate_only = validate
        return request

    def _validate() -> None:
        experiment_service.mutate_experiments(request=_experiment_request(True))

    def _commit() -> str:
        response = experiment_service.mutate_experiments(
            request=_experiment_request(False)
        )
        experiment_rn = response.results[0].resource_name
        arms_request = ga_client.get_type("MutateExperimentArmsRequest")
        arms_request.customer_id = cid
        arms_request.response_content_type = (
            enums.ResponseContentTypeEnum.MUTABLE_RESOURCE
        )
        control = ga_client.get_type("ExperimentArmOperation")
        control.create.experiment = experiment_rn
        control.create.name = "control"
        control.create.control = True
        control.create.traffic_split = 100 - split
        control.create.campaigns.append(campaign_rn)
        treatment = ga_client.get_type("ExperimentArmOperation")
        treatment.create.experiment = experiment_rn
        treatment.create.name = "treatment"
        treatment.create.control = False
        treatment.create.traffic_split = split
        arms_request.operations.extend([control, treatment])
        try:
            arms = arm_service.mutate_experiment_arms(request=arms_request)
        except Exception:
            cleanup = ga_client.get_type("MutateExperimentsRequest")
            cleanup.customer_id = cid
            remove = ga_client.get_type("ExperimentOperation")
            remove.remove = experiment_rn
            cleanup.operations.append(remove)
            try:
                experiment_service.mutate_experiments(request=cleanup)
            except (
                Exception
            ):  # Report the original error; cleanup is best effort.
                pass
            raise
        drafts = [
            draft
            for result in arms.results
            for draft in result.experiment_arm.in_design_campaigns
        ]
        draft_ids = ", ".join(utils.extract_id(d) for d in drafts) or "unknown"
        return (
            f"Created experiment {utils.extract_id(experiment_rn)}. Treatment "
            f"draft campaign ID: {draft_ids}. Change that campaign with the "
            "campaign_management tools, then run manage_experiment with "
            "action SCHEDULE to start the test."
        )

    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Experiment",
        primary_key_readable=experiment_name,
        fields={
            "Base campaign": f"{rows[0].campaign.name} ({campaign_id})",
            "Type": _EXPERIMENT_TYPES[channel],
            "Traffic split": f"{100 - split}% control / {split}% treatment",
            "Start": start or "when scheduled",
            "End": end,
            "Description": description,
        },
    )
    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_experiment",
            "campaign_id": campaign_id,
            "name": experiment_name,
            "split": split,
            "start": start,
            "end": end,
            "description": description,
        },
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to create experiment",
    )


@experiments_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_experiment(
    customer_id: str | int,
    experiment_id: str,
    action: ExperimentAction,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Schedules, ends or promotes an experiment.

    SCHEDULE starts the test (Google creates the treatment campaign in the
    background). END stops it without changing the base campaign. PROMOTE
    applies the treatment's changes to the base campaign and ends the test;
    it cannot be undone. Find experiments with the search tool on the
    experiment resource.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        experiment_id: The numeric experiment ID.
        action: 'SCHEDULE', 'END' or 'PROMOTE'.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    experiment_id = mh.require_simple_id(experiment_id, "experiment_id")
    action_value = mh.enum_value(action, ExperimentAction, "action")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT experiment.name, experiment.status, experiment.start_date, "
        "experiment.end_date FROM experiment "
        f"WHERE experiment.experiment_id = {experiment_id}",
    )
    if not rows:
        raise ToolError(f"Error: Experiment {experiment_id} was not found.")
    experiment = rows[0].experiment
    rn = f"customers/{cid}/experiments/{experiment_id}"
    service = utils.get_googleads_service_from_client(
        ga_client, "ExperimentService"
    )

    def _call(validate: bool) -> Any:
        if action_value == "SCHEDULE":
            request = ga_client.get_type("ScheduleExperimentRequest")
            request.resource_name = rn
            request.validate_only = validate
            return service.schedule_experiment(request=request)
        if action_value == "END":
            request = ga_client.get_type("EndExperimentRequest")
            request.experiment = rn
            request.validate_only = validate
            return service.end_experiment(request=request)
        request = ga_client.get_type("PromoteExperimentRequest")
        request.resource_name = rn
        request.validate_only = validate
        return service.promote_experiment(request=request)

    def _commit() -> str:
        _call(False)
        follow_up = {
            "SCHEDULE": "Scheduling runs in the background; check "
            "experiment.status until it is ENABLED.",
            "END": "The experiment has ended; the base campaign is unchanged.",
            "PROMOTE": "Promotion runs in the background; the base campaign "
            "takes over the treatment's changes.",
        }[action_value]
        return f"{action_value.title()} requested for {rn}. {follow_up}"

    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Experiment",
        primary_key_readable=f"{experiment.name} ({rn})",
        fields={"Action": action_value},
        original_fields={"Action": f"status {experiment.status.name}"},
    )
    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_experiment",
            "experiment_id": experiment_id,
            "action": action_value,
        },
        change_items=change_items,
        validate_fn=lambda: _call(True),
        commit_fn=_commit,
        error_prefix=f"Failed to {action_value.lower()} experiment",
    )
