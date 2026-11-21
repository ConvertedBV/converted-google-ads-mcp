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

"""Account-level setup: settings, portfolio bid strategies, shared budgets.

Registered on the ``campaign_management`` namespace. All tools use the
propose-and-confirm workflow and one atomic GoogleAdsService.Mutate, so a new
portfolio strategy or shared budget and its campaign assignments succeed or
fail together.
"""

from enum import Enum
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import image_assets as ia
from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.safety import enforce_budget_ceiling
from ads_mcp.tools.campaign_management import campaign_management_mcp
from ads_mcp.tools.campaign_settings import _CAMPAIGN_FIELDS
from ads_mcp.tools.campaign_settings import _current_target
from ads_mcp.tools.campaign_settings import clean_final_url_suffix
from ads_mcp.tools.campaign_settings import clean_tracking_template
import ads_mcp.utils as utils


def _show(value: Any) -> Any:
    return "(none)" if value in ("", None) else value


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def update_account_settings(
    customer_id: str | int,
    auto_tagging_enabled: bool | None = None,
    tracking_url_template: str | None = None,
    final_url_suffix: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Changes account-wide tracking: auto-tagging, tracking template, suffix.

    Auto-tagging adds the gclid that GA4 and offline conversion imports need;
    get_account_overview flags accounts where it is off. The tracking
    template and final URL suffix apply to every campaign without its own.
    Pass only the settings to change; an empty string removes a template or
    suffix. Campaign-level values are set with update_campaign_settings.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        auto_tagging_enabled: Turn auto-tagging (gclid) on or off.
        tracking_url_template: e.g. {{lpurl}}?src=ads, or '' to remove.
        final_url_suffix: e.g. utm_source=google&utm_medium=cpc, or '' to
            remove.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    template = clean_tracking_template(tracking_url_template)
    suffix = clean_final_url_suffix(final_url_suffix)
    if auto_tagging_enabled is None and template is None and suffix is None:
        raise ToolError("Error: Pass at least one setting to change.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mh.search(
        ga_client,
        cid,
        "SELECT customer.descriptive_name, customer.auto_tagging_enabled, "
        "customer.tracking_url_template, customer.final_url_suffix "
        "FROM customer",
    )
    if not rows:
        raise ToolError(f"Error: Customer {cid} was not found.")
    current = rows[0].customer

    mutate_op = ga_client.get_type("MutateOperation")
    op = mutate_op.customer_operation
    op.update.resource_name = f"customers/{cid}"
    fields: dict[str, Any] = {}
    original: dict[str, Any] = {}
    updates = [
        (
            "Auto-tagging",
            "auto_tagging_enabled",
            auto_tagging_enabled,
            current.auto_tagging_enabled,
        ),
        (
            "Tracking template",
            "tracking_url_template",
            template,
            current.tracking_url_template,
        ),
        (
            "Final URL suffix",
            "final_url_suffix",
            suffix,
            current.final_url_suffix,
        ),
    ]
    for label, path, value, old in updates:
        if value is None:
            continue
        setattr(op.update, path, value)
        op.update_mask.paths.append(path)
        fields[label] = _show(value)
        original[label] = _show(old)

    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Account settings",
        primary_key_readable=f"{current.descriptive_name} (customers/{cid})",
        fields=fields,
        original_fields=original,
    )
    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=[mutate_op],
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "update_account_settings",
            "auto_tagging_enabled": auto_tagging_enabled,
            "tracking_url_template": template,
            "final_url_suffix": suffix,
        },
        change_items=change_items,
        success_message=f"Updated the account settings of {cid}.",
        error_prefix="Failed to update account settings",
    )


# ---------------------------------------------------------------------------
# Portfolio bid strategies
# ---------------------------------------------------------------------------


class PortfolioStrategy(str, Enum):
    MAXIMIZE_CONVERSIONS = "MAXIMIZE_CONVERSIONS"
    MAXIMIZE_CONVERSION_VALUE = "MAXIMIZE_CONVERSION_VALUE"
    MAXIMIZE_CLICKS = "MAXIMIZE_CLICKS"
    TARGET_IMPRESSION_SHARE = "TARGET_IMPRESSION_SHARE"


class ImpressionShareLocation(str, Enum):
    ANYWHERE_ON_PAGE = "ANYWHERE_ON_PAGE"
    TOP_OF_PAGE = "TOP_OF_PAGE"
    ABSOLUTE_TOP_OF_PAGE = "ABSOLUTE_TOP_OF_PAGE"


_PORTFOLIO_FIELDS = (
    "bidding_strategy.resource_name, bidding_strategy.name, "
    "bidding_strategy.type, bidding_strategy.campaign_count, "
    "bidding_strategy.maximize_conversions.target_cpa_micros, "
    "bidding_strategy.target_cpa.target_cpa_micros, "
    "bidding_strategy.maximize_conversion_value.target_roas, "
    "bidding_strategy.target_roas.target_roas, "
    "bidding_strategy.target_spend.cpc_bid_ceiling_micros, "
    "bidding_strategy.target_impression_share.cpc_bid_ceiling_micros"
)


def _fetch_campaigns(
    ga_client: Any, cid: str, campaign_ids: list[str], extra: str = ""
) -> dict[str, Any]:
    rows = mh.search(
        ga_client,
        cid,
        f"SELECT campaign.id, {_CAMPAIGN_FIELDS}{extra} FROM campaign "
        f"WHERE campaign.id IN ({', '.join(campaign_ids)})",
    )
    found = {str(row.campaign.id): row for row in rows}
    missing = [c for c in campaign_ids if c not in found]
    if missing:
        raise ToolError(
            f"Error: Campaigns not found in customer {cid}: "
            f"{', '.join(missing)}"
        )
    return found


def _portfolio_targets(
    strategy: Any,
) -> tuple[int | None, float | None, int | None]:
    cpa = (
        strategy.maximize_conversions.target_cpa_micros
        or strategy.target_cpa.target_cpa_micros
        or None
    )
    roas = (
        strategy.maximize_conversion_value.target_roas
        or strategy.target_roas.target_roas
        or None
    )
    ceiling = (
        strategy.target_spend.cpc_bid_ceiling_micros
        or strategy.target_impression_share.cpc_bid_ceiling_micros
        or None
    )
    return cpa, roas, ceiling


def _current_limits(
    row: Any, portfolios: dict[str, tuple]
) -> tuple[int | None, float | None, int | None] | None:
    """(target CPA, target ROAS, max CPC) a campaign bids with today.

    Campaigns on a portfolio take them from the portfolio; returns None when
    that portfolio could not be read (callers then fail closed).
    """
    campaign = row.campaign
    if campaign.bidding_strategy:
        return portfolios.get(campaign.bidding_strategy)
    strategy_type = mh.enum_name(campaign.bidding_strategy_type)
    kind, value = _current_target(campaign, strategy_type)
    ceiling = {
        "TARGET_SPEND": campaign.target_spend.cpc_bid_ceiling_micros,
        "TARGET_IMPRESSION_SHARE": (
            campaign.target_impression_share.cpc_bid_ceiling_micros
        ),
    }.get(strategy_type)
    return (
        value if kind == "cpa" else None,
        value if kind == "roas" else None,
        ceiling or None,
    )


_MANUAL_STRATEGIES = ("MANUAL_CPC", "ENHANCED_CPC", "MANUAL_CPM", "MANUAL_CPV")


def _guard_targets(
    rows: dict[str, Any],
    portfolios: dict[str, tuple],
    cpa: int | None,
    roas: float | None,
    ceiling: int | None,
    allow_removing_targets: bool,
) -> None:
    """Applies the per-change safety limit against each campaign's limits.

    A tighter or similar target passes; a much looser one is capped; and a
    campaign whose target CPA, target ROAS or max CPC would disappear, or
    whose current limits cannot be read, is refused unless the user
    explicitly allowed it.
    """
    dropped = []
    for row in rows.values():
        label = f"campaign {row.campaign.id}"
        limits = _current_limits(row, portfolios)
        if limits is None:
            dropped.append(f"{label} (current portfolio unreadable)")
            continue
        cur_cpa, cur_roas, cur_ceiling = limits
        if cur_cpa:
            if cpa:
                mh.enforce_increase_guardrail(
                    cur_cpa, cpa, f"target CPA of {label}"
                )
            else:
                dropped.append(f"{label} (target CPA)")
        if cur_roas:
            if roas:
                mh.enforce_decrease_guardrail(
                    cur_roas, roas, f"target ROAS of {label}"
                )
            else:
                dropped.append(f"{label} (target ROAS)")
        if cur_ceiling:
            if ceiling:
                mh.enforce_increase_guardrail(
                    cur_ceiling, ceiling, f"max CPC limit of {label}"
                )
            else:
                dropped.append(f"{label} (max CPC limit)")
        manual = (
            not row.campaign.bidding_strategy
            and mh.enum_name(row.campaign.bidding_strategy_type)
            in _MANUAL_STRATEGIES
        )
        if manual and not ceiling:
            # Manual bids cap every click; a portfolio without a max CPC
            # limit lets Google bid above them.
            dropped.append(f"{label} (manual CPC bids)")
    if dropped and not allow_removing_targets:
        raise ToolError(
            "Error: Moving these campaigns would drop their current limit: "
            f"{', '.join(dropped)}. Give the portfolio a comparable target, "
            "or confirm with the user and pass allow_removing_targets=True."
        )


def _build_portfolio(
    ga_client: Any,
    strategy: Any,
    name: str,
    kind: str,
    target_cpa: float | None,
    target_roas_percent: float | None,
    max_cpc_limit: float | None,
    impression_share_location: Any,
    impression_share_percent: float | None,
    currency: str,
) -> dict[str, Any]:
    """Fills a new BiddingStrategy; returns a normalized description."""
    enums = ga_client.enums
    strategy.name = name
    cpa = mh.to_micros(target_cpa, "target_cpa") if target_cpa else None
    roas = None
    if target_roas_percent:
        if float(target_roas_percent) <= 0:
            raise ToolError("Error: 'target_roas_percent' must be > 0.")
        roas = round(float(target_roas_percent) / 100, 4)
    ceiling = (
        mh.to_micros(max_cpc_limit, "max_cpc_limit") if max_cpc_limit else None
    )
    allowed = {
        "MAXIMIZE_CONVERSIONS": {"target_cpa"},
        "MAXIMIZE_CONVERSION_VALUE": {"target_roas_percent"},
        "MAXIMIZE_CLICKS": {"max_cpc_limit"},
        "TARGET_IMPRESSION_SHARE": {
            "max_cpc_limit",
            "impression_share_location",
            "impression_share_percent",
        },
    }[kind]
    given = {
        "target_cpa": cpa,
        "target_roas_percent": roas,
        "max_cpc_limit": ceiling,
        "impression_share_location": impression_share_location,
        "impression_share_percent": impression_share_percent,
    }
    wrong = [k for k, v in given.items() if v is not None and k not in allowed]
    if wrong:
        raise ToolError(
            f"Error: {', '.join(wrong)} does not apply to a {kind} portfolio."
        )
    described: dict[str, Any] = {"strategy": kind, "name": name}
    if kind == "MAXIMIZE_CONVERSIONS":
        strategy.maximize_conversions = ga_client.get_type(
            "MaximizeConversions"
        )
        if cpa:
            strategy.maximize_conversions.target_cpa_micros = cpa
            described["target"] = f"tCPA {mh.format_money(cpa, currency)}"
    elif kind == "MAXIMIZE_CONVERSION_VALUE":
        strategy.maximize_conversion_value = ga_client.get_type(
            "MaximizeConversionValue"
        )
        if roas:
            strategy.maximize_conversion_value.target_roas = roas
            described["target"] = f"tROAS {roas * 100:.0f}%"
    elif kind == "MAXIMIZE_CLICKS":
        strategy.target_spend = ga_client.get_type("TargetSpend")
        if ceiling:
            strategy.target_spend.cpc_bid_ceiling_micros = ceiling
            described["target"] = (
                f"max CPC {mh.format_money(ceiling, currency)}"
            )
    else:
        location = mh.enum_value(
            impression_share_location or "ANYWHERE_ON_PAGE",
            ImpressionShareLocation,
            "impression_share_location",
        )
        if impression_share_percent is None or not (
            0 < float(impression_share_percent) <= 100
        ):
            raise ToolError(
                "Error: TARGET_IMPRESSION_SHARE needs "
                "'impression_share_percent' between 0 and 100."
            )
        if not ceiling:
            raise ToolError(
                "Error: TARGET_IMPRESSION_SHARE needs 'max_cpc_limit' as a "
                "safety cap on CPCs."
            )
        info = strategy.target_impression_share
        info.location = getattr(
            enums.TargetImpressionShareLocationEnum, location
        )
        info.location_fraction_micros = int(
            round(float(impression_share_percent) * 10_000)
        )
        info.cpc_bid_ceiling_micros = ceiling
        described["target"] = (
            f"{float(impression_share_percent):g}% {location.lower()}, "
            f"max CPC {mh.format_money(ceiling, currency)}"
        )
    described["cpa_micros"] = cpa
    described["ceiling_micros"] = ceiling
    described["roas"] = roas
    return described


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_portfolio_bidding_strategy(
    customer_id: str | int,
    campaign_ids: list[str] | None = None,
    bidding_strategy_id: str | None = None,
    name: str | None = None,
    strategy: PortfolioStrategy | None = None,
    target_cpa: float | None = None,
    target_roas_percent: float | None = None,
    max_cpc_limit: float | None = None,
    impression_share_location: ImpressionShareLocation | None = None,
    impression_share_percent: float | None = None,
    allow_removing_targets: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a portfolio bid strategy and/or moves campaigns onto one.

    A portfolio shares one bid strategy (and target) across campaigns, useful
    when campaigns are too small to learn on their own. Two uses:
    - New portfolio: pass `name` and `strategy` with an optional target, and
      optionally `campaign_ids` to move onto it in the same step.
    - Existing portfolio: pass `bidding_strategy_id` and `campaign_ids`.

    Moving a campaign replaces its own strategy and restarts learning. A
    target that is much looser than a campaign's current target is capped
    by the server's safety limit. TARGET_IMPRESSION_SHARE (e.g. brand
    campaigns) needs `impression_share_percent` and `max_cpc_limit`.
    Find existing portfolios with the search tool on bidding_strategy.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_ids: Campaign IDs to move onto the portfolio.
        bidding_strategy_id: Existing portfolio strategy ID.
        name: Name for a new portfolio.
        strategy: MAXIMIZE_CONVERSIONS, MAXIMIZE_CONVERSION_VALUE,
            MAXIMIZE_CLICKS or TARGET_IMPRESSION_SHARE (new portfolio).
        target_cpa: Optional target CPA (MAXIMIZE_CONVERSIONS).
        target_roas_percent: Optional target ROAS, e.g. 400 for 400%
            (MAXIMIZE_CONVERSION_VALUE).
        max_cpc_limit: Max CPC (MAXIMIZE_CLICKS; required for
            TARGET_IMPRESSION_SHARE).
        impression_share_location: ANYWHERE_ON_PAGE (default), TOP_OF_PAGE
            or ABSOLUTE_TOP_OF_PAGE.
        impression_share_percent: Target impression share, e.g. 90.
        allow_removing_targets: Only after the user agreed: allow moving
            campaigns whose target CPA / ROAS / max CPC the portfolio
            does not keep.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaigns = (
        mh.dedupe(mh.require_ids(campaign_ids, "campaign_ids"))
        if campaign_ids
        else []
    )
    clean_name = " ".join((name or "").split())
    existing = (
        mh.require_simple_id(bidding_strategy_id, "bidding_strategy_id")
        if bidding_strategy_id
        else None
    )
    if bool(existing) == bool(clean_name):
        raise ToolError(
            "Error: Pass either 'bidding_strategy_id' (existing portfolio) or "
            "'name' and 'strategy' (new portfolio)."
        )
    if existing and not campaigns:
        raise ToolError(
            "Error: Pass 'campaign_ids' to move onto the existing portfolio."
        )
    if clean_name and strategy is None:
        raise ToolError("Error: A new portfolio needs a 'strategy'.")
    if existing and (
        strategy
        or target_cpa
        or target_roas_percent
        or max_cpc_limit
        or impression_share_percent
    ):
        raise ToolError(
            "Error: Targets only apply to a new portfolio. Change an "
            "existing portfolio's target with update_campaign_bidding."
        )

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency_rows = mh.search(
        ga_client, cid, "SELECT customer.currency_code FROM customer"
    )
    currency = currency_rows[0].customer.currency_code if currency_rows else ""
    rows = (
        _fetch_campaigns(
            ga_client,
            cid,
            campaigns,
            ", campaign.target_impression_share.cpc_bid_ceiling_micros",
        )
        if campaigns
        else {}
    )
    current_portfolios = sorted(
        {r.campaign.bidding_strategy for r in rows.values()} - {""}
    )
    portfolios: dict[str, tuple] = {}
    if current_portfolios:
        for found_row in mh.search(
            ga_client,
            cid,
            f"SELECT {_PORTFOLIO_FIELDS} FROM bidding_strategy WHERE "
            "bidding_strategy.resource_name IN "
            f"({mh.quoted_list(current_portfolios)})",
        ):
            strategy_row = found_row.bidding_strategy
            portfolios[strategy_row.resource_name] = _portfolio_targets(
                strategy_row
            )

    operations: list[Any] = []
    change_items: list[pac.ChangeItem] = []
    if existing:
        strategy_rn = f"customers/{cid}/biddingStrategies/{existing}"
        found = mh.search(
            ga_client,
            cid,
            f"SELECT {_PORTFOLIO_FIELDS} FROM bidding_strategy "
            f"WHERE bidding_strategy.id = {existing}",
        )
        if not found:
            raise ToolError(
                f"Error: Portfolio strategy {existing} was not found."
            )
        portfolio = found[0].bidding_strategy
        cpa, roas, ceiling = _portfolio_targets(portfolio)
        label = (
            f"{portfolio.name} ({mh.enum_name(portfolio.type_)}, "
            f"{portfolio.campaign_count} campaigns)"
        )
        payload: dict[str, Any] = {"bidding_strategy_id": existing}
    else:
        kind = mh.enum_value(strategy, PortfolioStrategy, "strategy")
        strategy_rn = ia.TempIds(cid).next("biddingStrategies")
        mutate_op = ga_client.get_type("MutateOperation")
        new = mutate_op.bidding_strategy_operation.create
        new.resource_name = strategy_rn
        described = _build_portfolio(
            ga_client,
            new,
            clean_name,
            kind,
            target_cpa,
            target_roas_percent,
            max_cpc_limit,
            impression_share_location,
            impression_share_percent,
            currency,
        )
        operations.append(mutate_op)
        cpa, roas = described.pop("cpa_micros"), described.pop("roas")
        ceiling = described.pop("ceiling_micros")
        label = clean_name
        change_items.extend(
            pac.build_change_items(
                operation="CREATE",
                entity_readable="Portfolio bid strategy",
                primary_key_readable=clean_name,
                fields={
                    "Strategy": kind,
                    "Target": described.get("target", "none"),
                },
            )
        )
        payload = {"new": described}

    _guard_targets(rows, portfolios, cpa, roas, ceiling, allow_removing_targets)
    for campaign_id in campaigns:
        row = rows[campaign_id]
        mutate_op = ga_client.get_type("MutateOperation")
        op = mutate_op.campaign_operation
        op.update.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        op.update.bidding_strategy = strategy_rn
        op.update_mask.paths.append("bidding_strategy")
        operations.append(mutate_op)
        current = row.campaign.bidding_strategy or mh.enum_name(
            row.campaign.bidding_strategy_type
        )
        change_items.append(
            pac.ChangeItem(
                "UPDATE",
                "Campaign bid strategy",
                f"{row.campaign.name} ({campaign_id})",
                "Bid strategy",
                f"{current} -> portfolio {label}",
            )
        )

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_portfolio_bidding_strategy",
            "campaign_ids": campaigns,
            "allow_removing_targets": allow_removing_targets,
            **payload,
        },
        change_items=change_items,
        success_message=(
            f"Portfolio '{label}' ready; {len(campaigns)} campaign(s) moved."
        ),
        error_prefix="Failed to manage portfolio bid strategy",
    )


# ---------------------------------------------------------------------------
# Shared budgets
# ---------------------------------------------------------------------------


@campaign_management_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_shared_budget(
    customer_id: str | int,
    campaign_ids: list[str],
    budget_id: str | None = None,
    name: str | None = None,
    daily_budget: float | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a shared daily budget and/or moves campaigns onto one.

    A shared budget lets Google spread one daily amount over several
    campaigns. Pass `name` and `daily_budget` for a new shared budget, or
    `budget_id` for an existing one, plus the campaigns to move. A new
    shared budget may not exceed the campaigns' current combined daily
    budgets by more than the server's safety limit. Performance Max and
    some other campaign types cannot use shared budgets; the dry run says
    so. Find shared budgets with the search tool on campaign_budget
    (campaign_budget.explicitly_shared = TRUE).
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        campaign_ids: Campaign IDs to move onto the shared budget.
        budget_id: Existing shared budget ID.
        name: Name for a new shared budget.
        daily_budget: Daily amount for a new shared budget.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    campaigns = mh.dedupe(mh.require_ids(campaign_ids, "campaign_ids"))
    clean_name = " ".join((name or "").split())
    existing = (
        mh.require_simple_id(budget_id, "budget_id") if budget_id else None
    )
    if bool(existing) == bool(clean_name):
        raise ToolError(
            "Error: Pass either 'budget_id' (existing shared budget) or "
            "'name' and 'daily_budget' (new shared budget)."
        )
    if existing and daily_budget is not None:
        raise ToolError(
            "Error: Change an existing budget's amount with "
            "update_campaign_budget."
        )
    if clean_name and not daily_budget:
        raise ToolError("Error: A new shared budget needs 'daily_budget'.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = _fetch_campaigns(
        ga_client,
        cid,
        campaigns,
        ", campaign_budget.resource_name, campaign_budget.amount_micros, "
        "campaign_budget.explicitly_shared, "
        "campaign_budget.reference_count",
    )
    currency = next(iter(rows.values())).customer.currency_code

    # Baseline: every distinct budget these campaigns use today, counted
    # once (a shared budget used by several of them counts once).
    # A budget also used by campaigns outside this move only counts for
    # the moved campaigns' share of it.
    moved_refs: dict[str, int] = {}
    budget_rows: dict[str, Any] = {}
    for row in rows.values():
        rn = row.campaign_budget.resource_name
        moved_refs[rn] = moved_refs.get(rn, 0) + 1
        budget_rows[rn] = row.campaign_budget
    current_budgets = {
        rn: int(
            int(budget.amount_micros or 0)
            * moved_refs[rn]
            / max(int(budget.reference_count or 0), moved_refs[rn])
        )
        for rn, budget in budget_rows.items()
    }
    current_total = sum(current_budgets.values())
    if current_total <= 0:
        raise ToolError(
            "Error: These campaigns have no daily budget to compare with "
            "(e.g. a total budget). Set their budgets in Google Ads first."
        )

    operations: list[Any] = []
    change_items: list[pac.ChangeItem] = []
    if existing:
        budget_rn = f"customers/{cid}/campaignBudgets/{existing}"
        found = mh.search(
            ga_client,
            cid,
            "SELECT campaign_budget.name, campaign_budget.amount_micros, "
            "campaign_budget.explicitly_shared, "
            "campaign_budget.reference_count FROM campaign_budget "
            f"WHERE campaign_budget.id = {existing}",
        )
        if not found:
            raise ToolError(f"Error: Budget {existing} was not found.")
        budget = found[0].campaign_budget
        if not budget.explicitly_shared:
            raise ToolError(f"Error: Budget {existing} is not a shared budget.")
        amount = int(budget.amount_micros)
        if budget_rn in current_budgets:
            raise ToolError(
                f"Error: Some campaigns already use budget {existing}."
            )
        enforce_budget_ceiling(amount)
        # The moved campaigns can now spend up to the shared amount.
        mh.enforce_increase_guardrail(
            current_total, amount, "daily budget available to these campaigns"
        )
        label = (
            f"{budget.name} ({mh.format_money(amount, currency)}/day, "
            f"{budget.reference_count} campaigns)"
        )
    else:
        amount = mh.to_micros(daily_budget, "daily_budget")
        enforce_budget_ceiling(amount)
        mh.enforce_increase_guardrail(
            current_total, amount, "combined daily budget"
        )
        budget_rn = ia.TempIds(cid).next("campaignBudgets")
        mutate_op = ga_client.get_type("MutateOperation")
        new = mutate_op.campaign_budget_operation.create
        new.resource_name = budget_rn
        new.name = clean_name
        new.amount_micros = amount
        new.explicitly_shared = True
        new.delivery_method = ga_client.enums.BudgetDeliveryMethodEnum.STANDARD
        operations.append(mutate_op)
        label = f"{clean_name} ({mh.format_money(amount, currency)}/day)"
        change_items.extend(
            pac.build_change_items(
                operation="CREATE",
                entity_readable="Shared budget",
                primary_key_readable=clean_name,
                fields={
                    "Daily budget": mh.format_money(amount, currency),
                    "Current combined budgets": mh.format_money(
                        current_total, currency
                    ),
                },
            )
        )

    for campaign_id in campaigns:
        row = rows[campaign_id]
        mutate_op = ga_client.get_type("MutateOperation")
        op = mutate_op.campaign_operation
        op.update.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        op.update.campaign_budget = budget_rn
        op.update_mask.paths.append("campaign_budget")
        operations.append(mutate_op)
        old = mh.format_money(
            int(row.campaign_budget.amount_micros or 0), currency
        )
        change_items.append(
            pac.ChangeItem(
                "UPDATE",
                "Campaign budget",
                f"{row.campaign.name} ({campaign_id})",
                "Budget",
                f"own {old}/day -> shared {label}",
            )
        )

    return mh.run_google_ads_mutation(
        ga_client=ga_client,
        customer_id=cid,
        mutate_operations=operations,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_shared_budget",
            "campaign_ids": campaigns,
            "budget_id": existing,
            "name": clean_name or None,
            "amount_micros": amount,
        },
        change_items=change_items,
        success_message=(
            f"Shared budget '{label}' ready; {len(campaigns)} campaign(s) "
            "moved."
        ),
        error_prefix="Failed to manage shared budget",
    )
