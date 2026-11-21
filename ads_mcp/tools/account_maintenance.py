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

"""Account maintenance tools: shared negative lists, asset links,
recommendations and responsive search ad edits.

Registered on the ``campaign_management`` namespace. All tools use the
propose-and-confirm workflow.
"""

from enum import Enum
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import ad_builders as ab
from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.campaign_management import campaign_management_mcp
import ads_mcp.utils as utils


class SharedListOperation(str, Enum):
    """Operations on shared negative keyword lists."""

    CREATE_LIST = "CREATE_LIST"
    ADD_KEYWORDS = "ADD_KEYWORDS"
    REMOVE_KEYWORDS = "REMOVE_KEYWORDS"
    ATTACH_CAMPAIGNS = "ATTACH_CAMPAIGNS"
    DETACH_CAMPAIGNS = "DETACH_CAMPAIGNS"


def _fetch_shared_set(ga_client: Any, cid: str, shared_set_id: str) -> Any:
    rows = mh.search(
        ga_client,
        cid,
        "SELECT shared_set.name, shared_set.type, shared_set.member_count, "
        "shared_set.reference_count FROM shared_set "
        f"WHERE shared_set.id = {shared_set_id}",
    )
    if not rows or mh.enum_name(rows[0].shared_set.type_) not in (
        "NEGATIVE_KEYWORDS",
        "ACCOUNT_LEVEL_NEGATIVE_KEYWORDS",
    ):
        raise ToolError(
            f"Error: Shared set {shared_set_id} is not a negative keyword "
            "list of this account."
        )
    return rows[0].shared_set


def _campaign_names(
    ga_client: Any, cid: str, campaign_ids: list[str]
) -> dict[str, str]:
    rows = mh.search(
        ga_client,
        cid,
        "SELECT campaign.id, campaign.name FROM campaign WHERE campaign.id IN "
        f"({', '.join(campaign_ids)})",
    )
    names = {str(row.campaign.id): row.campaign.name for row in rows}
    missing = [c for c in campaign_ids if c not in names]
    if missing:
        raise ToolError(f"Error: Campaigns not found: {', '.join(missing)}.")
    return names


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_shared_negative_list(
    customer_id: str | int,
    operation: SharedListOperation,
    shared_set_id: str | None = None,
    name: str | None = None,
    keywords: list[str] | None = None,
    match_type: ab.KeywordMatchType = ab.KeywordMatchType.EXACT,
    criterion_ids: list[str] | None = None,
    campaign_ids: list[str] | None = None,
    account_level: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Manages shared negative keyword lists and the campaigns using them.

    Operations:
    - CREATE_LIST: `name` plus optional `keywords` (+ `match_type`) and
      `campaign_ids` to attach right away; created atomically. With
      `account_level=True` the list applies to every Search, Shopping and
      Performance Max campaign of the account (one per account) instead.
    - ADD_KEYWORDS / REMOVE_KEYWORDS: `shared_set_id` plus `keywords` or
      `criterion_ids` (shared_criterion.criterion_id).
    - ATTACH_CAMPAIGNS / DETACH_CAMPAIGNS: `shared_set_id` plus `campaign_ids`.
    Find lists with the search tool on the shared_set resource.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        operation: One of the operations above.
        shared_set_id: Numeric ID of an existing list.
        name: Name of the new list (CREATE_LIST).
        keywords: Negative keyword texts (max 500).
        match_type: 'EXACT' (default), 'PHRASE' or 'BROAD'.
        criterion_ids: Keyword criterion IDs to remove from the list.
        campaign_ids: Campaign IDs to attach or detach.
        account_level: CREATE_LIST only: an account-wide list.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    op_value = mh.enum_value(operation, SharedListOperation, "operation")
    match_value = mh.enum_value(match_type, ab.KeywordMatchType, "match_type")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    payload: dict[str, Any] = {
        "tool": "manage_shared_negative_list",
        "operation": op_value,
    }
    change_items: list[pac.ChangeItem] = []

    if op_value == "CREATE_LIST":
        list_name = " ".join((name or "").split())
        if not list_name:
            raise ToolError("Error: 'name' is required for CREATE_LIST.")
        texts = (
            mh.clean_texts(keywords, "keywords", mh.MAX_KEYWORDS_PER_CALL)
            if keywords
            else []
        )
        campaigns = (
            mh.require_ids(campaign_ids, "campaign_ids") if campaign_ids else []
        )
        if account_level and campaigns:
            raise ToolError(
                "Error: An account-level list applies to every campaign; "
                "do not pass 'campaign_ids'."
            )
        names = _campaign_names(ga_client, cid, campaigns) if campaigns else {}
        set_rn = f"customers/{cid}/sharedSets/-1"
        mutate_ops = []
        set_op = ga_client.get_type("MutateOperation")
        shared_set = set_op.shared_set_operation.create
        shared_set.resource_name = set_rn
        shared_set.name = list_name
        set_types = ga_client.enums.SharedSetTypeEnum
        shared_set.type_ = (
            set_types.ACCOUNT_LEVEL_NEGATIVE_KEYWORDS
            if account_level
            else set_types.NEGATIVE_KEYWORDS
        )
        mutate_ops.append(set_op)
        if account_level:
            account_op = ga_client.get_type("MutateOperation")
            account_op.customer_negative_criterion_operation.create.negative_keyword_list.shared_set = (
                set_rn
            )
            mutate_ops.append(account_op)
            change_items.append(
                pac.ChangeItem(
                    "CREATE",
                    "List attachment",
                    list_name,
                    "Account",
                    f"all campaigns of {cid}",
                )
            )
        change_items.append(
            pac.ChangeItem(
                "CREATE", "Negative Keyword List", list_name, "Name", list_name
            )
        )
        for text in texts:
            kw_op = ga_client.get_type("MutateOperation")
            criterion = kw_op.shared_criterion_operation.create
            criterion.shared_set = set_rn
            ab.fill_keyword(ga_client, criterion, text, match_value)
            mutate_ops.append(kw_op)
            change_items.append(
                pac.ChangeItem(
                    "CREATE",
                    "Negative Keyword",
                    list_name,
                    "Keyword",
                    f"{text} ({match_value})",
                )
            )
        for campaign in campaigns:
            link_op = ga_client.get_type("MutateOperation")
            link = link_op.campaign_shared_set_operation.create
            link.campaign = f"customers/{cid}/campaigns/{campaign}"
            link.shared_set = set_rn
            mutate_ops.append(link_op)
            change_items.append(
                pac.ChangeItem(
                    "CREATE",
                    "List attachment",
                    list_name,
                    "Campaign",
                    f"{names[campaign]} ({campaign})",
                )
            )
        payload.update(
            name=list_name,
            keywords=texts,
            match_type=match_value,
            campaign_ids=campaigns,
        )
        if account_level:
            payload["account_level"] = True
        return mh.run_google_ads_mutation(
            ga_client=ga_client,
            customer_id=cid,
            mutate_operations=mutate_ops,
            validate_only=validate_only,
            checksum=checksum,
            operation_payload=payload,
            change_items=change_items,
            success_message=f"Created negative keyword list '{list_name}'.",
            error_prefix="Failed to create negative keyword list",
        )

    set_id = mh.require_simple_id(shared_set_id, "shared_set_id")
    shared_set = _fetch_shared_set(ga_client, cid, set_id)
    set_rn = f"customers/{cid}/sharedSets/{set_id}"
    label = f"{shared_set.name} ({set_rn})"
    payload["shared_set_id"] = set_id
    operations: list[Any] = []

    if op_value == "ADD_KEYWORDS":
        texts = mh.clean_texts(keywords, "keywords", mh.MAX_KEYWORDS_PER_CALL)
        for text in texts:
            op = ga_client.get_type("SharedCriterionOperation")
            op.create.shared_set = set_rn
            ab.fill_keyword(ga_client, op.create, text, match_value)
            operations.append(op)
            change_items.append(
                pac.ChangeItem(
                    "CREATE",
                    "Negative Keyword",
                    label,
                    "Keyword",
                    f"{text} ({match_value})",
                )
            )
        payload.update(keywords=texts, match_type=match_value)
        service = (
            "SharedCriterionService",
            "MutateSharedCriteriaRequest",
            "mutate_shared_criteria",
        )
    elif op_value == "REMOVE_KEYWORDS":
        ids = mh.require_ids(
            criterion_ids, "criterion_ids", max_items=mh.MAX_KEYWORDS_PER_CALL
        )
        rns = [f"customers/{cid}/sharedCriteria/{set_id}~{c}" for c in ids]
        rows = mh.search(
            ga_client,
            cid,
            "SELECT shared_criterion.resource_name, "
            "shared_criterion.keyword.text, "
            "shared_criterion.keyword.match_type FROM shared_criterion "
            "WHERE shared_criterion.resource_name IN "
            f"({mh.quoted_list(rns)})",
        )
        found = {r.shared_criterion.resource_name: r for r in rows}
        missing = [rn for rn in rns if rn not in found]
        if missing:
            raise ToolError(
                f"Error: Not in list {set_id}: {', '.join(missing)}"
            )
        for rn in rns:
            op = ga_client.get_type("SharedCriterionOperation")
            op.remove = rn
            operations.append(op)
            keyword = found[rn].shared_criterion.keyword
            change_items.append(
                pac.ChangeItem(
                    "REMOVE",
                    "Negative Keyword",
                    label,
                    "Keyword",
                    f"{keyword.text} ({keyword.match_type.name})",
                )
            )
        payload["criterion_ids"] = ids
        service = (
            "SharedCriterionService",
            "MutateSharedCriteriaRequest",
            "mutate_shared_criteria",
        )
    else:
        if mh.enum_name(shared_set.type_) == "ACCOUNT_LEVEL_NEGATIVE_KEYWORDS":
            raise ToolError(
                "Error: This is the account-level list; it already applies "
                "to every campaign and cannot be attached per campaign."
            )
        campaigns = mh.require_ids(campaign_ids, "campaign_ids")
        names = _campaign_names(ga_client, cid, campaigns)
        attach = op_value == "ATTACH_CAMPAIGNS"
        for campaign in campaigns:
            op = ga_client.get_type("CampaignSharedSetOperation")
            if attach:
                op.create.campaign = f"customers/{cid}/campaigns/{campaign}"
                op.create.shared_set = set_rn
            else:
                op.remove = (
                    f"customers/{cid}/campaignSharedSets/{campaign}~{set_id}"
                )
            operations.append(op)
            change_items.append(
                pac.ChangeItem(
                    "CREATE" if attach else "REMOVE",
                    "List attachment",
                    label,
                    "Campaign",
                    f"{names[campaign]} ({campaign})",
                )
            )
        payload["campaign_ids"] = campaigns
        service = (
            "CampaignSharedSetService",
            "MutateCampaignSharedSetsRequest",
            "mutate_campaign_shared_sets",
        )

    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name=service[0],
        request_type=service[1],
        mutate_method=service[2],
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=payload,
        change_items=change_items,
        success_message=f"Successfully applied {op_value} on {set_rn}.",
        error_prefix=f"Failed to {op_value.lower()}",
    )


class AssetLevel(str, Enum):
    """Where an asset is linked."""

    CUSTOMER = "CUSTOMER"
    CAMPAIGN = "CAMPAIGN"
    AD_GROUP = "AD_GROUP"


class AssetLinkOperation(str, Enum):
    LINK = "LINK"
    UNLINK = "UNLINK"


class AssetFieldType(str, Enum):
    """Asset roles that can be linked at account, campaign or ad group level."""

    SITELINK = "SITELINK"
    CALLOUT = "CALLOUT"
    STRUCTURED_SNIPPET = "STRUCTURED_SNIPPET"
    CALL = "CALL"
    PRICE = "PRICE"
    PROMOTION = "PROMOTION"
    LEAD_FORM = "LEAD_FORM"
    MOBILE_APP = "MOBILE_APP"
    AD_IMAGE = "AD_IMAGE"
    BUSINESS_NAME = "BUSINESS_NAME"
    BUSINESS_LOGO = "BUSINESS_LOGO"


_ASSET_LEVELS = {
    "CUSTOMER": ("CustomerAsset", "customer_asset_operation", "customerAssets"),
    "CAMPAIGN": ("CampaignAsset", "campaign_asset_operation", "campaignAssets"),
    "AD_GROUP": ("AdGroupAsset", "ad_group_asset_operation", "adGroupAssets"),
}


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def link_assets(
    customer_id: str | int,
    operation: AssetLinkOperation,
    level: AssetLevel,
    asset_ids: list[str],
    field_type: AssetFieldType,
    parent_id: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Links or unlinks existing assets (sitelinks, callouts, ...).

    Create assets first with the assets tools, then link them to the account
    (CUSTOMER), a campaign or an ad group. Unlinking keeps the asset itself.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        operation: 'LINK' or 'UNLINK'.
        level: 'CUSTOMER', 'CAMPAIGN' or 'AD_GROUP'.
        asset_ids: Numeric asset IDs (max 200).
        field_type: The role of the assets, e.g. 'SITELINK' or 'CALLOUT'.
        parent_id: Campaign or ad group ID (not used for CUSTOMER).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    op_value = mh.enum_value(operation, AssetLinkOperation, "operation")
    level_value = mh.enum_value(level, AssetLevel, "level")
    field_value = mh.enum_value(field_type, AssetFieldType, "field_type")
    ids = mh.require_ids(asset_ids, "asset_ids")
    parent = None
    if level_value != "CUSTOMER":
        parent = mh.require_simple_id(parent_id, "parent_id")
    type_name, op_field, prefix = _ASSET_LEVELS[level_value]

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    asset_rns = [f"customers/{cid}/assets/{a}" for a in ids]
    rows = mh.search(
        ga_client,
        cid,
        "SELECT asset.resource_name, asset.type, asset.name, "
        "asset.sitelink_asset.link_text, asset.callout_asset.callout_text "
        "FROM asset WHERE asset.resource_name IN "
        f"({mh.quoted_list(asset_rns)})",
    )
    assets = {row.asset.resource_name: row.asset for row in rows}
    missing = [rn for rn in asset_rns if rn not in assets]
    if missing:
        raise ToolError(f"Error: Assets not found: {', '.join(missing)}")

    if level_value == "CUSTOMER":
        parent_rn, parent_key = f"customers/{cid}", ""
    elif level_value == "CAMPAIGN":
        parent_rn = f"customers/{cid}/campaigns/{parent}"
        parent_key = f"{parent}~"
    else:
        parent_rn = f"customers/{cid}/adGroups/{parent}"
        parent_key = f"{parent}~"

    operations = []
    change_items: list[pac.ChangeItem] = []
    for asset_id, asset_rn in zip(ids, asset_rns):
        op = ga_client.get_type(f"{type_name}Operation")
        if op_value == "LINK":
            op.create.asset = asset_rn
            op.create.field_type = getattr(
                ga_client.enums.AssetFieldTypeEnum, field_value
            )
            if level_value == "CAMPAIGN":
                op.create.campaign = parent_rn
            elif level_value == "AD_GROUP":
                op.create.ad_group = parent_rn
        else:
            op.remove = (
                f"customers/{cid}/{prefix}/{parent_key}{asset_id}~{field_value}"
            )
        operations.append(op)
        asset = assets[asset_rn]
        readable = (
            asset.sitelink_asset.link_text
            or asset.callout_asset.callout_text
            or asset.name
            or asset.type_.name
        )
        change_items.append(
            pac.ChangeItem(
                "CREATE" if op_value == "LINK" else "REMOVE",
                f"{field_value.title()} link",
                parent_rn,
                "Asset",
                f"{readable} ({asset_id})",
            )
        )

    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name=f"{type_name}Service",
        request_type=f"Mutate{type_name}sRequest",
        mutate_method=f"mutate_{op_field.removesuffix('_operation')}s",
        operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "link_assets",
            "operation": op_value,
            "level": level_value,
            "parent_id": parent,
            "field_type": field_value,
            "asset_ids": ids,
        },
        change_items=change_items,
        success_message=(
            f"Successfully {'linked' if op_value == 'LINK' else 'unlinked'} "
            f"{len(ids)} assets on {parent_rn}."
        ),
        error_prefix="Failed to update asset links",
    )


class RecommendationOperation(str, Enum):
    APPLY = "APPLY"
    DISMISS = "DISMISS"


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_recommendations(
    customer_id: str | int,
    operation: RecommendationOperation,
    recommendation_ids: list[str],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Applies or dismisses Google Ads recommendations.

    Find open recommendations with the search tool, e.g. `SELECT
    recommendation.resource_name, recommendation.type, campaign.name FROM
    recommendation`. APPLY uses Google's recommended values (e.g. the
    suggested budget). The Google Ads API has no dry run for recommendations:
    the proposal step only verifies they exist and are still open, so review
    the type and campaign carefully before confirming.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        operation: 'APPLY' or 'DISMISS'.
        recommendation_ids: Recommendation IDs or full resource names
            (max 50).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    op_value = mh.enum_value(operation, RecommendationOperation, "operation")
    tokens = mh.dedupe(
        [ab.require_token(r, "recommendation_id") for r in recommendation_ids]
    )
    if not tokens or len(tokens) > 50:
        raise ToolError("Error: Pass 1-50 recommendation IDs.")
    rns = [f"customers/{cid}/recommendations/{t}" for t in tokens]

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)

    def _fetch() -> dict[str, Any]:
        rows = mh.search(
            ga_client,
            cid,
            "SELECT recommendation.resource_name, recommendation.type, "
            "recommendation.dismissed, campaign.name FROM recommendation "
            f"WHERE recommendation.resource_name IN ({mh.quoted_list(rns)})",
        )
        found = {row.recommendation.resource_name: row for row in rows}
        unavailable = [
            rn
            for rn in rns
            if rn not in found or found[rn].recommendation.dismissed
        ]
        if unavailable:
            raise ToolError(
                "Error: These recommendations are not open (anymore): "
                f"{', '.join(unavailable)}"
            )
        return found

    found = _fetch()
    change_items = [
        pac.ChangeItem(
            op_value,
            "Recommendation",
            rn,
            "Type",
            f"{found[rn].recommendation.type_.name}"
            + (
                f" (campaign: {found[rn].campaign.name})"
                if found[rn].campaign.name
                else ""
            ),
        )
        for rn in rns
    ]
    service = utils.get_googleads_service_from_client(
        ga_client, "RecommendationService"
    )

    def _commit() -> str:
        if op_value == "APPLY":
            request = ga_client.get_type("ApplyRecommendationRequest")
            for rn in rns:
                op = ga_client.get_type("ApplyRecommendationOperation")
                op.resource_name = rn
                request.operations.append(op)
            request.customer_id = cid
            response = service.apply_recommendation(request=request)
        else:
            request = ga_client.get_type("DismissRecommendationRequest")
            for rn in rns:
                # Nested message type; accessed on the class because proto-plus
                # instances resolve attributes as fields.
                op = type(request).DismissRecommendationOperation()
                op.resource_name = rn
                request.operations.append(op)
            request.customer_id = cid
            response = service.dismiss_recommendation(request=request)
        done = [r.resource_name.rsplit("/", 1)[-1] for r in response.results]
        verb = "Applied" if op_value == "APPLY" else "Dismissed"
        return f"{verb} {len(done)} recommendations: {', '.join(done)}"

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_recommendations",
            "operation": op_value,
            "recommendations": tokens,
        },
        change_items=change_items,
        validate_fn=None,
        commit_fn=_commit,
        error_prefix=f"Failed to {op_value.lower()} recommendations",
    )


def _pin(asset: Any) -> int | None:
    """Maps a pinned_field such as HEADLINE_2 to its position (2)."""
    name = asset.pinned_field.name if asset.pinned_field else ""
    suffix = name.rsplit("_", 1)[-1]
    return int(suffix) if suffix.isdigit() else None


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def update_responsive_search_ad(
    customer_id: str | int,
    ad_id: str,
    ad: ab.ResponsiveSearchAdInput,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Replaces the texts, final URL and paths of a responsive search ad.

    The ad keeps its ID and statistics but goes through policy review again.
    Pass the complete new set of headlines and descriptions, including pins
    and dynamic insertions you want to keep; the current texts and pins are
    shown in the proposal for comparison. To change a single text, use
    replace_ad_text instead, which leaves everything else untouched.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        ad_id: The numeric ad ID (ad_group_ad.ad.id).
        ad: The new ad content (headlines, descriptions, final_url, paths).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    ad_id = mh.require_simple_id(ad_id, "ad_id")
    rsa = ab.normalize_rsa(ad)

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT ad_group_ad.ad.type, ad_group_ad.ad.final_urls, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        "ad_group_ad.ad.responsive_search_ad.path1, "
        "ad_group_ad.ad.responsive_search_ad.path2, ad_group.name "
        f"FROM ad_group_ad WHERE ad_group_ad.ad.id = {ad_id}",
    )
    if not rows:
        raise ToolError(f"Error: Ad {ad_id} was not found in customer {cid}.")
    current = rows[0].ad_group_ad.ad
    if current.type_.name != "RESPONSIVE_SEARCH_AD":
        raise ToolError(
            f"Error: Ad {ad_id} is a {current.type_.name}, not a responsive "
            "search ad."
        )
    current_rsa = {
        "headlines": [
            {"text": h.text, "pin": _pin(h)}
            for h in current.responsive_search_ad.headlines
        ],
        "descriptions": [
            {"text": d.text, "pin": _pin(d)}
            for d in current.responsive_search_ad.descriptions
        ],
        "final_url": current.final_urls[0] if current.final_urls else "",
        "path1": current.responsive_search_ad.path1 or None,
        "path2": current.responsive_search_ad.path2 or None,
    }

    rn = f"customers/{cid}/ads/{ad_id}"
    op = ga_client.get_type("AdOperation")
    op.update.resource_name = rn
    ab.fill_rsa(ga_client, op.update, rsa)
    op.update_mask.paths.extend(
        [
            "final_urls",
            "responsive_search_ad.headlines",
            "responsive_search_ad.descriptions",
            "responsive_search_ad.path1",
            "responsive_search_ad.path2",
        ]
    )
    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Responsive Search Ad",
        primary_key_readable=f"{rows[0].ad_group.name} ({rn})",
        fields=ab.rsa_change_fields(rsa),
        original_fields=ab.rsa_change_fields(current_rsa),
    )
    return mh.run_mutation(
        ga_client=ga_client,
        customer_id=cid,
        service_name="AdService",
        request_type="MutateAdsRequest",
        mutate_method="mutate_ads",
        operations=[op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_responsive_search_ad",
            "ad_id": ad_id,
            "ad": rsa,
        },
        change_items=change_items,
        success_message=f"Successfully updated responsive search ad {ad_id}.",
        error_prefix="Failed to update responsive search ad",
    )
