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

"""Billing: payments accounts, billing setups and account budgets.

Registered on the ``account`` namespace. These tools connect an account to
an existing payments account (for example a manager's consolidated
billing) and set account budgets (spending caps). They never enter payment
details: a payments account and profile are created by a person in Google
Ads or Google Payments.
"""

from enum import Enum
import json
from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.account_overview import account_mcp
import ads_mcp.utils as utils


def _payments_accounts(ga_client: Any, cid: str) -> list[Any]:
    service = utils.get_googleads_service_from_client(
        ga_client, "PaymentsAccountService"
    )
    try:
        response = service.list_payments_accounts(customer_id=cid)
    except GoogleAdsException as exc:
        raise ToolError(
            "Listing payments accounts failed:\n"
            + utils.format_google_ads_error(exc)
        ) from exc
    return list(response.payments_accounts)


@account_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_payments_accounts(
    customer_id: str | int,
    login_customer_id: str | int | None = None,
) -> str:
    """Lists the payments accounts this account can be billed to.

    Use the payments_account_id in create_billing_setup. For consolidated
    billing, call it on the client account with the paying manager as
    login_customer_id. Billing setups and account budgets are visible with
    the search tool (billing_setup, account_budget).

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
    """
    cid = mh.require_customer_id(customer_id)
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    accounts = [
        {
            "payments_account_id": a.payments_account_id,
            "name": a.name,
            "currency": a.currency_code,
            "payments_profile_id": a.payments_profile_id,
            "paying_manager": a.paying_manager_customer or None,
        }
        for a in _payments_accounts(ga_client, cid)
    ]
    return json.dumps({"payments_accounts": accounts}, indent=2)


@account_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_billing_setup(
    customer_id: str | int,
    payments_account_id: str,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Connects the account to an existing payments account, starting now.

    Needed before a new account can run ads (unless billing was set up in
    the interface). Pick the payments account with list_payments_accounts;
    its currency must match the account. The API has no dry run for billing
    setups, so the proposal checks the input and that the account has no
    active or pending setup. Then set a spending cap with
    manage_account_budget when the payments account uses monthly invoicing.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        payments_account_id: ID from list_payments_accounts, e.g.
            1234-5678-9012-3456.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    account_id = (payments_account_id or "").strip()
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    accounts = {
        a.payments_account_id: a for a in _payments_accounts(ga_client, cid)
    }
    if account_id not in accounts:
        raise ToolError(
            f"Error: Payments account '{account_id}' is not available to "
            f"this account; choose one from list_payments_accounts."
        )
    payments = accounts[account_id]
    rows = mh.search(
        ga_client,
        cid,
        "SELECT billing_setup.id, billing_setup.status, "
        "customer.currency_code FROM billing_setup "
        "WHERE billing_setup.status IN ('PENDING', 'APPROVED', "
        "'APPROVED_HELD')",
    )
    if rows:
        raise ToolError(
            f"Error: The account already has a billing setup "
            f"({rows[0].billing_setup.id}, "
            f"{mh.enum_name(rows[0].billing_setup.status)})."
        )
    currency_rows = mh.search(
        ga_client, cid, "SELECT customer.currency_code FROM customer"
    )
    currency = currency_rows[0].customer.currency_code if currency_rows else ""
    if (
        payments.currency_code
        and currency
        and payments.currency_code != currency
    ):
        raise ToolError(
            f"Error: The payments account is in {payments.currency_code}, the "
            f"account in {currency}."
        )
    service = utils.get_googleads_service_from_client(
        ga_client, "BillingSetupService"
    )

    def _commit() -> str:
        request = ga_client.get_type("MutateBillingSetupRequest")
        request.customer_id = cid
        setup = request.operation.create
        setup.payments_account = payments.resource_name
        setup.start_time_type = ga_client.enums.TimeTypeEnum.NOW
        response = service.mutate_billing_setup(request=request)
        return (
            f"Created billing setup {response.result.resource_name} "
            f"(payments account {account_id}); Google approves it shortly."
        )

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_billing_setup",
            "payments_account_id": account_id,
        },
        change_items=pac.build_change_items(
            operation="CREATE",
            entity_readable="Billing setup",
            primary_key_readable=f"customers/{cid}",
            fields={
                "Payments account": f"{payments.name} ({account_id})",
                "Currency": payments.currency_code,
                "Start": "now",
            },
        ),
        validate_fn=lambda: None,
        commit_fn=_commit,
        error_prefix="Failed to create billing setup",
    )


class AccountBudgetOperation(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    END = "END"


@account_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
@mh.with_workflow_doc
def manage_account_budget(
    customer_id: str | int,
    operation: AccountBudgetOperation,
    spending_limit: float | None = None,
    billing_setup_id: str | None = None,
    account_budget_id: str | None = None,
    name: str | None = None,
    end_date: str | None = None,
    purchase_order_number: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Sets an account budget: the most the account may spend in total.

    For accounts on monthly invoicing (consolidated billing). Ads stop when
    the limit is reached. CREATE: `billing_setup_id`, `spending_limit` and
    optional `end_date`, `name`, `purchase_order_number`; it starts now.
    UPDATE: `account_budget_id` with a new `spending_limit` and/or
    `end_date` (a higher limit is capped by the safety limit per change).
    END: stop the budget now. Changes are proposals that Google approves
    (usually immediately); follow them with the search tool on
    account_budget_proposal.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        operation: CREATE, UPDATE or END.
        spending_limit: Total spending limit in the account currency.
        billing_setup_id: Approved billing setup (CREATE).
        account_budget_id: Existing account budget (UPDATE, END).
        name: Budget name (CREATE).
        end_date: Optional last day YYYY-MM-DD; none = runs until ended.
        purchase_order_number: Optional PO number shown on invoices.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    op_value = mh.enum_value(operation, AccountBudgetOperation, "operation")
    limit = (
        mh.to_micros(spending_limit, "spending_limit")
        if spending_limit is not None
        else None
    )
    end = mh.require_date(end_date, "end_date")
    po = " ".join((purchase_order_number or "").split()) or None
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    currency_rows = mh.search(
        ga_client, cid, "SELECT customer.currency_code FROM customer"
    )
    currency = currency_rows[0].customer.currency_code if currency_rows else ""

    request = ga_client.get_type("MutateAccountBudgetProposalRequest")
    request.customer_id = cid
    proposal = request.operation.create
    fields: dict[str, Any] = {}
    original: dict[str, Any] = {}
    if op_value == "CREATE":
        setup_id = mh.require_simple_id(billing_setup_id, "billing_setup_id")
        if limit is None:
            raise ToolError("Error: CREATE needs a 'spending_limit'.")
        rows = mh.search(
            ga_client,
            cid,
            "SELECT billing_setup.status FROM billing_setup "
            f"WHERE billing_setup.id = {setup_id}",
        )
        if not rows or mh.enum_name(rows[0].billing_setup.status) != "APPROVED":
            raise ToolError(
                f"Error: Billing setup {setup_id} is not approved for this "
                "account."
            )
        proposal.proposal_type = enums.AccountBudgetProposalTypeEnum.CREATE
        proposal.billing_setup = f"customers/{cid}/billingSetups/{setup_id}"
        proposal.proposed_name = (
            " ".join((name or "").split()) or "Account budget"
        )
        proposal.proposed_start_time_type = enums.TimeTypeEnum.NOW
        if end:
            proposal.proposed_end_date_time = f"{end} 23:59:59"
        else:
            proposal.proposed_end_time_type = enums.TimeTypeEnum.FOREVER
        proposal.proposed_spending_limit_micros = limit
        if po:
            proposal.proposed_purchase_order_number = po
        fields = {
            "Name": proposal.proposed_name,
            "Spending limit": mh.format_money(limit, currency),
            "Start": "now",
            "End": end or "until ended",
            "PO number": po,
        }
        label = f"billing setup {setup_id}"
    else:
        budget_id = mh.require_simple_id(account_budget_id, "account_budget_id")
        rn = f"customers/{cid}/accountBudgets/{budget_id}"
        rows = mh.search(
            ga_client,
            cid,
            "SELECT account_budget.name, account_budget.status, "
            "account_budget.approved_spending_limit_micros, "
            "account_budget.approved_spending_limit_type, "
            "account_budget.amount_served_micros, "
            "account_budget.approved_end_date_time FROM account_budget "
            f"WHERE account_budget.id = {budget_id}",
        )
        if not rows:
            raise ToolError(f"Error: Account budget {budget_id} not found.")
        budget = rows[0].account_budget
        if mh.enum_name(budget.status) != "APPROVED":
            raise ToolError(
                f"Error: Account budget {budget_id} is "
                f"{mh.enum_name(budget.status)}; only approved budgets can "
                "be changed."
            )
        proposal.account_budget = rn
        label = f"{budget.name} ({rn})"
        current_limit = int(budget.approved_spending_limit_micros or 0)
        current_text = (
            "unlimited"
            if mh.enum_name(budget.approved_spending_limit_type) == "INFINITE"
            else mh.format_money(current_limit, currency)
        )
        if op_value == "END":
            proposal.proposal_type = enums.AccountBudgetProposalTypeEnum.END
            fields = {"End": "now"}
            original = {"End": budget.approved_end_date_time or "until ended"}
        else:
            if limit is None and not end:
                raise ToolError(
                    "Error: UPDATE needs a new 'spending_limit' and/or "
                    "'end_date'."
                )
            proposal.proposal_type = enums.AccountBudgetProposalTypeEnum.UPDATE
            # The mask names the budget fields this UPDATE proposal changes.
            paths = request.operation.update_mask.paths
            if limit is not None:
                if limit < int(budget.amount_served_micros or 0):
                    raise ToolError(
                        "Error: The new limit is below what was already "
                        "spent ("
                        f"{mh.format_money(int(budget.amount_served_micros), currency)})."
                    )
                if current_limit:
                    mh.enforce_increase_guardrail(
                        current_limit, limit, "account spending limit"
                    )
                proposal.proposed_spending_limit_micros = limit
                paths.append("proposed_spending_limit_micros")
                fields["Spending limit"] = mh.format_money(limit, currency)
                original["Spending limit"] = current_text
            if end:
                proposal.proposed_end_date_time = f"{end} 23:59:59"
                paths.append("proposed_end_date_time")
                fields["End"] = end
                original["End"] = budget.approved_end_date_time or "until ended"
    service = utils.get_googleads_service_from_client(
        ga_client, "AccountBudgetProposalService"
    )

    def _validate() -> None:
        request.validate_only = True
        service.mutate_account_budget_proposal(request=request)

    def _commit() -> str:
        request.validate_only = False
        response = service.mutate_account_budget_proposal(request=request)
        return (
            f"Submitted account budget proposal "
            f"{response.result.resource_name} ({op_value})."
        )

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_account_budget",
            "operation": op_value,
            "billing_setup_id": billing_setup_id,
            "account_budget_id": account_budget_id,
            "spending_limit_micros": limit,
            "end": end,
            "name": name,
            "po": po,
        },
        change_items=pac.build_change_items(
            operation="CREATE" if op_value == "CREATE" else "UPDATE",
            entity_readable="Account budget",
            primary_key_readable=label,
            fields=fields,
            original_fields=original or None,
        ),
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to manage account budget",
    )
