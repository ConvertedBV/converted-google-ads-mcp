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

"""Account administration: client accounts, user access, product links.

Registered on the ``account`` namespace (read-only deployments drop these
write tools). Billing, payments and terms of service stay in the Google Ads
interface: they need a person.
"""

from enum import Enum
import re

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.account_overview import account_mcp
from ads_mcp.tools.campaign_settings import clean_final_url_suffix
from ads_mcp.tools.campaign_settings import clean_tracking_template
import ads_mcp.utils as utils

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_TIME_ZONE_RE = re.compile(r"^[A-Za-z]+(?:/[A-Za-z0-9_+-]+){1,2}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


@account_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def create_client_account(
    customer_id: str | int,
    name: str,
    currency_code: str = "EUR",
    time_zone: str = "Europe/Brussels",
    tracking_url_template: str | None = None,
    final_url_suffix: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a new client (advertiser) account under a manager account.

    Currency and time zone cannot be changed after creation, so confirm
    them with the user. The new account has no billing yet: someone sets up
    billing (or consolidated billing) and accepts any terms in Google Ads
    before ads can run. Afterwards turn on auto-tagging with
    update_account_settings and build campaigns in the new account (with
    the manager as login_customer_id).
    {workflow}
    Args:
        customer_id: The manager (MCC) account to create it under. With
            GOOGLE_ADS_MCP_ACCOUNT_IDS set, add the new account to that
            list before working in it.
        name: Account name, e.g. 'Klantnaam BV'.
        currency_code: ISO 4217 code, default EUR (cannot change later).
        time_zone: Time zone ID, default Europe/Brussels (cannot change
            later).
        tracking_url_template: Optional account tracking template.
        final_url_suffix: Optional account final URL suffix.
        login_customer_id: Optional higher manager for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    manager = mh.require_customer_id(customer_id)
    clean_name = " ".join((name or "").split())
    if not clean_name or len(clean_name) > 255:
        raise ToolError("Error: 'name' is required (max 255 characters).")
    currency = (currency_code or "").strip().upper()
    if not _CURRENCY_RE.match(currency):
        raise ToolError("Error: 'currency_code' must be like EUR or USD.")
    zone = (time_zone or "").strip()
    if not _TIME_ZONE_RE.match(zone):
        raise ToolError("Error: 'time_zone' must be like Europe/Brussels.")
    template = clean_tracking_template(tracking_url_template) or None
    suffix = clean_final_url_suffix(final_url_suffix) or None

    ga_client = utils.get_googleads_client(
        login_customer_id=login_customer_id or manager
    )
    rows = mh.search(
        ga_client,
        manager,
        "SELECT customer.manager, customer.descriptive_name FROM customer",
    )
    if not rows or not rows[0].customer.manager:
        raise ToolError(
            f"Error: {manager} is not a manager account; client accounts "
            "are created under a manager (MCC)."
        )
    existing = mh.search(
        ga_client,
        manager,
        "SELECT customer_client.id, customer_client.descriptive_name "
        "FROM customer_client WHERE customer_client.level = 1",
    )
    duplicates = [
        str(r.customer_client.id)
        for r in existing
        if (r.customer_client.descriptive_name or "").strip().lower()
        == clean_name.lower()
    ]
    if duplicates:
        raise ToolError(
            f"Error: The manager already has an account named '{clean_name}' "
            f"({', '.join(duplicates)}). Use it, or choose another name."
        )

    service = utils.get_googleads_service_from_client(
        ga_client, "CustomerService"
    )

    def _request(validate: bool):
        request = ga_client.get_type("CreateCustomerClientRequest")
        request.customer_id = manager
        request.validate_only = validate
        customer = request.customer_client
        customer.descriptive_name = clean_name
        customer.currency_code = currency
        customer.time_zone = zone
        if template:
            customer.tracking_url_template = template
        if suffix:
            customer.final_url_suffix = suffix
        return request

    def _validate() -> None:
        service.create_customer_client(request=_request(True))

    def _commit() -> str:
        response = service.create_customer_client(request=_request(False))
        new_id = utils.extract_id(response.resource_name)
        return (
            f"Created client account '{clean_name}' ({new_id}) under "
            f"{manager}. Next: billing in Google Ads, then auto-tagging with "
            "update_account_settings."
        )

    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Client account",
        primary_key_readable=f"{clean_name} (under {rows[0].customer.descriptive_name} {manager})",
        fields={
            "Currency": f"{currency} (cannot change later)",
            "Time zone": f"{zone} (cannot change later)",
            "Tracking template": template or "(none)",
            "Final URL suffix": suffix or "(none)",
        },
    )
    return pac.propose_and_confirm(
        customer_id=manager,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "create_client_account",
            "name": clean_name,
            "currency": currency,
            "time_zone": zone,
            "tracking_url_template": template,
            "final_url_suffix": suffix,
        },
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to create client account",
    )


class AccessRole(str, Enum):
    ADMIN = "ADMIN"
    STANDARD = "STANDARD"
    READ_ONLY = "READ_ONLY"
    EMAIL_ONLY = "EMAIL_ONLY"


@account_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def invite_user(
    customer_id: str | int,
    email_address: str,
    access_role: AccessRole = AccessRole.STANDARD,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Invites a person to an account by email (Google sends the email).

    Use it to give a client or colleague access. ADMIN can manage users and
    billing; STANDARD can edit campaigns; READ_ONLY can view; EMAIL_ONLY
    only gets notifications. The API has no dry run for invitations, so the
    proposal only checks the input; confirm the address with the user.
    Pending invitations: search on customer_user_access_invitation.
    {workflow}
    Args:
        customer_id: Account to give access to.
        email_address: The person's Google account email.
        access_role: STANDARD (default), ADMIN, READ_ONLY or EMAIL_ONLY.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    email = (email_address or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ToolError("Error: 'email_address' is not a valid address.")
    role = mh.enum_value(access_role, AccessRole, "access_role")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    service = utils.get_googleads_service_from_client(
        ga_client, "CustomerUserAccessInvitationService"
    )

    def _commit() -> str:
        request = ga_client.get_type(
            "MutateCustomerUserAccessInvitationRequest"
        )
        request.customer_id = cid
        invitation = request.operation.create
        invitation.email_address = email
        invitation.access_role = getattr(ga_client.enums.AccessRoleEnum, role)
        response = service.mutate_customer_user_access_invitation(
            request=request
        )
        return (
            f"Invited {email} as {role} to {cid} "
            f"({response.result.resource_name})."
        )

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "invite_user",
            "email_address": email,
            "access_role": role,
        },
        change_items=pac.build_change_items(
            operation="CREATE",
            entity_readable="User invitation",
            primary_key_readable=f"customers/{cid}",
            fields={"Email": email, "Access": role},
        ),
        validate_fn=lambda: None,
        commit_fn=_commit,
        error_prefix="Failed to invite user",
    )


class LinkAction(str, Enum):
    REQUEST = "REQUEST"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


@account_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def manage_merchant_center_link(
    customer_id: str | int,
    action: LinkAction,
    merchant_center_id: str | None = None,
    invitation_id: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Links a Merchant Center account for Shopping and retail PMax.

    - REQUEST: ask `merchant_center_id` for a link; someone accepts it in
      Merchant Center.
    - ACCEPT / REJECT: answer a link request Merchant Center sent to this
      account (`invitation_id` from the search tool on
      product_link_invitation, status PENDING_APPROVAL).
    Existing links: search on product_link. The API has no dry run here, so
    the proposal checks the input and the current invitation state.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        action: REQUEST, ACCEPT or REJECT.
        merchant_center_id: Merchant Center ID (REQUEST).
        invitation_id: product_link_invitation ID (ACCEPT / REJECT).
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    act = mh.enum_value(action, LinkAction, "action")
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    service = utils.get_googleads_service_from_client(
        ga_client, "ProductLinkInvitationService"
    )
    enums = ga_client.enums

    if act == "REQUEST":
        merchant = mh.require_simple_id(
            merchant_center_id, "merchant_center_id"
        )
        links = mh.search(
            ga_client,
            cid,
            "SELECT product_link.merchant_center.merchant_center_id "
            "FROM product_link WHERE product_link.type = 'MERCHANT_CENTER'",
        )
        if any(
            str(r.product_link.merchant_center.merchant_center_id) == merchant
            for r in links
        ):
            raise ToolError(
                f"Error: Merchant Center {merchant} is already linked."
            )

        def _commit() -> str:
            request = ga_client.get_type("CreateProductLinkInvitationRequest")
            request.customer_id = cid
            request.product_link_invitation.merchant_center.merchant_center_id = int(
                merchant
            )
            response = service.create_product_link_invitation(request=request)
            return (
                f"Requested a link with Merchant Center {merchant} "
                f"({response.resource_name}); accept it in Merchant Center."
            )

        payload = {"merchant_center_id": merchant}
        fields = {"Merchant Center": merchant, "Status": "request sent"}
    else:
        invite = mh.require_simple_id(invitation_id, "invitation_id")
        rn = f"customers/{cid}/productLinkInvitations/{invite}"
        rows = mh.search(
            ga_client,
            cid,
            "SELECT product_link_invitation.status, "
            "product_link_invitation.merchant_center.merchant_center_id "
            "FROM product_link_invitation "
            f"WHERE product_link_invitation.resource_name = '{rn}'",
        )
        if not rows:
            raise ToolError(f"Error: Invitation {invite} was not found.")
        status = mh.enum_name(rows[0].product_link_invitation.status)
        if status != "PENDING_APPROVAL":
            raise ToolError(
                f"Error: Invitation {invite} is {status}; only invitations "
                "waiting for this account's approval can be answered."
            )
        new_status = "ACCEPTED" if act == "ACCEPT" else "REJECTED"

        def _commit() -> str:
            request = ga_client.get_type("UpdateProductLinkInvitationRequest")
            request.customer_id = cid
            request.resource_name = rn
            request.product_link_invitation_status = getattr(
                enums.ProductLinkInvitationStatusEnum, new_status
            )
            service.update_product_link_invitation(request=request)
            return f"Invitation {invite} {new_status.lower()}."

        merchant = str(
            rows[0].product_link_invitation.merchant_center.merchant_center_id
        )
        payload = {"invitation_id": invite}
        fields = {"Merchant Center": merchant, "Status": new_status}

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "manage_merchant_center_link",
            "action": act,
            **payload,
        },
        change_items=pac.build_change_items(
            operation="CREATE" if act == "REQUEST" else "UPDATE",
            entity_readable="Merchant Center link",
            primary_key_readable=f"customers/{cid}",
            fields=fields,
        ),
        validate_fn=lambda: None,
        commit_fn=_commit,
        error_prefix="Failed to manage Merchant Center link",
    )
