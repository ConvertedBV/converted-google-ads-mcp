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

"""Customer Match: upload hashed customer lists for targeting and exclusion.

Registered on the ``audiences`` namespace. Contact details are normalized
and SHA-256 hashed on this server before anything leaves it; the raw values
are never sent to Google, stored in the proposal or written to the audit log
(the argument keys are on the audit redaction list).
"""

import hashlib
from typing import Any

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ads_mcp import mutate_helpers as mh
from ads_mcp import propose_and_confirm as pac
from ads_mcp.tools.audiences import audiences_mcp
from ads_mcp.tools.conversions import normalize_email_hash
from ads_mcp.tools.conversions import normalize_phone_hash
import ads_mcp.utils as utils

MAX_MEMBERS_PER_CALL = 10_000


class CustomerMatchMember(BaseModel):
    """One person; give an email, a phone number and/or name + address."""

    email: str | None = Field(default=None, description="Email address.")
    phone_number: str | None = Field(
        default=None, description="Phone in E.164 format, e.g. +32470123456."
    )
    first_name: str | None = Field(default=None, description="First name.")
    last_name: str | None = Field(default=None, description="Last name.")
    country_code: str | None = Field(
        default=None, description="ISO country code, e.g. BE (with address)."
    )
    postal_code: str | None = Field(
        default=None, description="Postal code (with name and country)."
    )


def _hash_name(value: str) -> str:
    return hashlib.sha256(" ".join(value.lower().split()).encode()).hexdigest()


def _normalize_member(
    member: CustomerMatchMember | dict, index: int
) -> dict[str, Any]:
    if isinstance(member, dict):
        member = CustomerMatchMember(**member)
    row: dict[str, Any] = {}
    try:
        if member.email and member.email.strip():
            row["email"] = normalize_email_hash(member.email)
        if member.phone_number and member.phone_number.strip():
            row["phone"] = normalize_phone_hash(member.phone_number)
    except ToolError as exc:
        # The message never contains the value itself.
        raise ToolError(f"{exc} (member #{index + 1})") from exc
    address_parts = (
        member.first_name,
        member.last_name,
        member.country_code,
        member.postal_code,
    )
    if any(p and p.strip() for p in address_parts):
        if not all(p and p.strip() for p in address_parts):
            raise ToolError(
                "Error: A name match needs first_name, last_name, "
                f"country_code and postal_code (member #{index + 1})."
            )
        country = member.country_code.strip().upper()
        if len(country) != 2 or not country.isalpha():
            raise ToolError(
                f"Error: Invalid country_code (member #{index + 1})."
            )
        row["address"] = {
            "first": _hash_name(member.first_name),
            "last": _hash_name(member.last_name),
            "country": country,
            "postal": member.postal_code.strip().replace(" ", ""),
        }
    if not row:
        raise ToolError(
            "Error: Every member needs an email, a phone number or a name "
            f"with address (member #{index + 1})."
        )
    return row


@audiences_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
@mh.with_workflow_doc
def upload_customer_match(
    customer_id: str | int,
    members: list[CustomerMatchMember],
    consent_confirmed: bool,
    user_list_id: str | None = None,
    list_name: str | None = None,
    description: str | None = None,
    membership_days: int = 540,
    remove_members: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Uploads customers to a Customer Match list (new or existing).

    Use it to target or exclude existing customers and leads, or as an
    audience signal for Performance Max. Emails, phone numbers and names are
    normalized and SHA-256 hashed on this server; only hashes go to Google.
    In the EU the customers must have consented to ad use of their data:
    pass `consent_confirmed=True` only when the user confirms that. The
    account must accept the Customer Match terms and meet Google's policy
    requirements; the dry run shows if it does not. Matching takes 6-48
    hours; follow it with the search tool on offline_user_data_job and
    user_list (size_for_search, match_rate_percentage). Lists need about
    1,000 matched members before they can serve.
    {workflow}
    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        members: Up to 10,000 people, each with email, phone_number and/or
            first_name + last_name + country_code + postal_code.
        consent_confirmed: The user confirms these people consented to ad
            use of their data (required; sent to Google as GRANTED).
        user_list_id: Existing Customer Match list to add to or remove from.
        list_name: Name for a new Customer Match list (instead of the ID).
        description: Optional description of a new list.
        membership_days: How long members stay in a new list, 1-540
            (default 540), or 10000 for no expiry.
        remove_members: True removes these people from the list.
        login_customer_id: Optional manager customer ID for the
            login-customer-id header.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior `validate_only=True` call.
    """
    cid = mh.require_customer_id(customer_id)
    if consent_confirmed is not True:
        raise ToolError(
            "Error: Customer Match needs the user's confirmation that these "
            "people consented to ad use of their data (consent_confirmed)."
        )
    if not members:
        raise ToolError("Error: Pass at least one member.")
    if len(members) > MAX_MEMBERS_PER_CALL:
        raise ToolError(
            f"Error: At most {MAX_MEMBERS_PER_CALL} members per call; split "
            "the upload."
        )
    existing = (
        mh.require_simple_id(user_list_id, "user_list_id")
        if user_list_id
        else None
    )
    clean_name = " ".join((list_name or "").split())
    if bool(existing) == bool(clean_name):
        raise ToolError(
            "Error: Pass either 'user_list_id' or 'list_name' (new list)."
        )
    if clean_name and remove_members:
        raise ToolError("Error: Members can only be removed from a list.")
    days = int(membership_days)
    if not (1 <= days <= 540 or days == 10000):
        raise ToolError("Error: 'membership_days' must be 1-540 or 10000.")
    rows = [_normalize_member(m, i) for i, m in enumerate(members)]

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums
    change_items: list[pac.ChangeItem] = []
    list_op = None
    if existing:
        list_rn = f"customers/{cid}/userLists/{existing}"
        found = mh.search(
            ga_client,
            cid,
            "SELECT user_list.name, user_list.type, user_list.read_only "
            f"FROM user_list WHERE user_list.id = {existing}",
        )
        if not found:
            raise ToolError(f"Error: User list {existing} was not found.")
        user_list = found[0].user_list
        if mh.enum_name(user_list.type_) != "CRM_BASED" or user_list.read_only:
            raise ToolError(
                f"Error: User list {existing} is not an editable Customer "
                "Match (CRM-based) list."
            )
        label = f"{user_list.name} ({list_rn})"
    else:
        list_op = ga_client.get_type("UserListOperation")
        new = list_op.create
        new.name = clean_name
        if description:
            new.description = description.strip()
        new.membership_status = enums.UserListMembershipStatusEnum.OPEN
        new.membership_life_span = days
        new.crm_based_user_list.upload_key_type = (
            enums.CustomerMatchUploadKeyTypeEnum.CONTACT_INFO
        )
        new.crm_based_user_list.data_source_type = (
            enums.UserListCrmDataSourceTypeEnum.FIRST_PARTY
        )
        label = clean_name
        change_items.extend(
            pac.build_change_items(
                operation="CREATE",
                entity_readable="Customer Match list",
                primary_key_readable=clean_name,
                fields={
                    "Membership (days)": (
                        "no expiry" if days == 10000 else days
                    ),
                },
            )
        )

    counts = {
        "emails": sum("email" in r for r in rows),
        "phones": sum("phone" in r for r in rows),
        "addresses": sum("address" in r for r in rows),
    }
    change_items.append(
        pac.ChangeItem(
            "REMOVE" if remove_members else "CREATE",
            "Customer Match members",
            label,
            "Members",
            f"{len(rows)} people ({counts['emails']} emails, "
            f"{counts['phones']} phones, {counts['addresses']} name + "
            "address), SHA-256 hashed; consent GRANTED",
        )
    )

    def _job(list_rn: str, validate: bool) -> Any:
        request = ga_client.get_type("CreateOfflineUserDataJobRequest")
        request.customer_id = cid
        request.validate_only = validate
        job = request.job
        job.type_ = enums.OfflineUserDataJobTypeEnum.CUSTOMER_MATCH_USER_LIST
        metadata = job.customer_match_user_list_metadata
        metadata.user_list = list_rn
        metadata.consent.ad_user_data = enums.ConsentStatusEnum.GRANTED
        metadata.consent.ad_personalization = enums.ConsentStatusEnum.GRANTED
        return request

    def _operations() -> list[Any]:
        result = []
        for row in rows:
            op = ga_client.get_type("OfflineUserDataJobOperation")
            data = op.remove if remove_members else op.create
            if "email" in row:
                ident = ga_client.get_type("UserIdentifier")
                ident.hashed_email = row["email"]
                data.user_identifiers.append(ident)
            if "phone" in row:
                ident = ga_client.get_type("UserIdentifier")
                ident.hashed_phone_number = row["phone"]
                data.user_identifiers.append(ident)
            if "address" in row:
                ident = ga_client.get_type("UserIdentifier")
                info = ident.address_info
                info.hashed_first_name = row["address"]["first"]
                info.hashed_last_name = row["address"]["last"]
                info.country_code = row["address"]["country"]
                info.postal_code = row["address"]["postal"]
                data.user_identifiers.append(ident)
            result.append(op)
        return result

    list_service = utils.get_googleads_service_from_client(
        ga_client, "UserListService"
    )
    job_service = utils.get_googleads_service_from_client(
        ga_client, "OfflineUserDataJobService"
    )

    def _list_request(validate: bool) -> Any:
        request = ga_client.get_type("MutateUserListsRequest")
        request.customer_id = cid
        request.operations.append(list_op)
        request.validate_only = validate
        return request

    def _validate() -> None:
        if list_op is not None:
            list_service.mutate_user_lists(request=_list_request(True))
        else:
            job_service.create_offline_user_data_job(
                request=_job(list_rn, True)
            )

    def _commit() -> str:
        created_rn = None
        if list_op is not None:
            response = list_service.mutate_user_lists(
                request=_list_request(False)
            )
            target_rn = created_rn = response.results[0].resource_name
        else:
            target_rn = list_rn
        job_rn = None
        try:
            job_rn = job_service.create_offline_user_data_job(
                request=_job(target_rn, False)
            ).resource_name
            add = ga_client.get_type("AddOfflineUserDataJobOperationsRequest")
            add.resource_name = job_rn
            add.enable_partial_failure = True
            add.operations.extend(_operations())
            added = job_service.add_offline_user_data_job_operations(
                request=add
            )
            failed = bool(added.partial_failure_error.code)
            job_service.run_offline_user_data_job(resource_name=job_rn)
        except Exception as exc:
            if created_rn is None and job_rn is None:
                raise
            # Say what already exists so a retry does not duplicate it.
            detail = (
                utils.format_google_ads_error(exc)
                if isinstance(exc, GoogleAdsException)
                else str(exc)
            )
            notes = ["Error: Uploading the members failed."]
            if job_rn:
                notes.append(
                    f"Job {utils.extract_id(job_rn)} was created but not run."
                )
            if created_rn:
                notes.append(
                    f"List {created_rn} was created; do not create it again "
                    "but retry with "
                    f"user_list_id={utils.extract_id(created_rn)}."
                )
            raise ToolError(" ".join(notes) + "\n" + detail) from exc
        return (
            f"Sent {len(rows)} members to "
            f"{target_rn} (job {utils.extract_id(job_rn)}, running). "
            "Matching takes 6-48 hours."
            + (
                " Some members were rejected (invalid identifiers)."
                if failed
                else ""
            )
        )

    digest = hashlib.sha256(
        repr(rows).encode()
    ).hexdigest()  # binds the proposal to these exact hashed members
    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload={
            "tool": "upload_customer_match",
            "user_list_id": existing,
            "list_name": clean_name or None,
            "description": description,
            "membership_days": days,
            "remove": remove_members,
            "members": len(rows),
            "members_digest": digest,
        },
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to upload Customer Match members",
    )
