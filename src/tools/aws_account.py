"""Tool: query_aws_account — inspect individual AWS member accounts (read-only)."""

import asyncio
import contextlib
import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.connections.aws import get_aws_session

logger = logging.getLogger(__name__)

ASSUME_ROLE_NAME = "OrganizationAccountAccessRole"

# Inline session policy: read-only actions only
_SESSION_POLICY = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:Describe*",
                    "cloudtrail:LookupEvents",
                    "iam:List*",
                    "iam:Get*",
                    "ce:GetCost*",
                    "cur:Describe*",
                    "aws-marketplace:Describe*",
                    "aws-marketplace:List*",
                    "aws-marketplace:Get*",
                    "aws-marketplace:SearchAgreements",
                    "aws-marketplace:DescribeAgreement",
                    "aws-marketplace:GetAgreementTerms",
                    "marketplace-entitlement:GetEntitlements",
                ],
                "Resource": "*",
            }
        ],
    }
)

# Cache assumed credentials per account to avoid redundant STS calls
_assumed_creds: dict[str, dict | None] = {}


def _check_account_status(session: boto3.Session, account_id: str) -> tuple[str | None, str | None]:
    """Check if an account is suspended/closed via Organizations API."""
    try:
        org = session.client("organizations", region_name="us-east-1")
        resp = org.describe_account(AccountId=account_id)
        acct = resp.get("Account", {})
        return acct.get("Status"), acct.get("Name")
    except Exception:
        return None, None


def _get_assumed_creds(session: boto3.Session, account_id: str) -> dict | None:
    """Assume role in member account with read-only session policy. Cached."""
    if account_id in _assumed_creds:
        return _assumed_creds[account_id]

    sts = session.client("sts", region_name="us-east-1")
    role_arn = f"arn:aws:iam::{account_id}:role/{ASSUME_ROLE_NAME}"

    try:
        creds = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="parsec-readonly",
            Policy=_SESSION_POLICY,
        )["Credentials"]
        _assumed_creds[account_id] = creds
        return creds
    except ClientError as e:
        logger.warning("Cannot assume role in %s: %s", account_id, e)
        _assumed_creds[account_id] = None
        return None


def _make_client(creds: dict, service: str, region: str):  # type: ignore[return]
    """Create a boto3 client using assumed-role credentials."""
    return boto3.client(  # type: ignore[call-overload]
        service,
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _describe_instances(creds: dict, region: str, filters: dict | None) -> dict:
    """List EC2 instances with optional filters."""
    ec2 = _make_client(creds, "ec2", region)

    kwargs: dict = {}
    ec2_filters = []

    if filters:
        if "state" in filters:
            ec2_filters.append({"Name": "instance-state-name", "Values": [filters["state"]]})
        if "instance_ids" in filters:
            kwargs["InstanceIds"] = filters["instance_ids"]

    if ec2_filters:
        kwargs["Filters"] = ec2_filters

    instances = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(**kwargs):
        for reservation in page.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                instances.append(
                    {
                        "instance_id": inst["InstanceId"],
                        "instance_type": inst["InstanceType"],
                        "state": inst["State"]["Name"],
                        "launch_time": inst["LaunchTime"].isoformat(),
                        "az": inst.get("Placement", {}).get("AvailabilityZone", ""),
                        "public_ip": inst.get("PublicIpAddress", ""),
                        "tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])},
                    }
                )

    return {"instance_count": len(instances), "instances": instances[:500]}


def _lookup_events(creds: dict, region: str, filters: dict | None) -> dict:
    """Look up recent CloudTrail events in the account."""
    ct = _make_client(creds, "cloudtrail", region)

    kwargs: dict = {"MaxResults": 50}
    if filters and "event_name" in filters:
        kwargs["LookupAttributes"] = [
            {"AttributeKey": "EventName", "AttributeValue": filters["event_name"]}
        ]

    events = []
    response = ct.lookup_events(**kwargs)
    for event in response.get("Events", []):
        events.append(
            {
                "event_id": event.get("EventId", ""),
                "event_name": event.get("EventName", ""),
                "event_time": (
                    event.get("EventTime", "").isoformat()
                    if hasattr(event.get("EventTime", ""), "isoformat")
                    else str(event.get("EventTime", ""))
                ),
                "username": event.get("Username", ""),
                "event_source": event.get("EventSource", ""),
                "resources": [
                    {"type": r.get("ResourceType", ""), "name": r.get("ResourceName", "")}
                    for r in event.get("Resources", [])
                ],
            }
        )

    return {"event_count": len(events), "events": events}


def _list_users(creds: dict, region: str, filters: dict | None) -> dict:
    """List IAM users and their access keys."""
    iam = _make_client(creds, "iam", region)

    users = []
    paginator = iam.get_paginator("list_users")
    for page in paginator.paginate():
        for user in page.get("Users", []):
            username = user["UserName"]
            # Get access keys for each user
            keys = []
            try:
                key_resp = iam.list_access_keys(UserName=username)
                for key in key_resp.get("AccessKeyMetadata", []):
                    keys.append(
                        {
                            "access_key_id": key["AccessKeyId"],
                            "status": key["Status"],
                            "created": key["CreateDate"].isoformat(),
                        }
                    )
            except ClientError:
                pass

            users.append(
                {
                    "username": username,
                    "user_id": user["UserId"],
                    "created": user["CreateDate"].isoformat(),
                    "password_last_used": (
                        user.get("PasswordLastUsed", "").isoformat()
                        if hasattr(user.get("PasswordLastUsed", ""), "isoformat")
                        else str(user.get("PasswordLastUsed", ""))
                    ),
                    "access_keys": keys,
                }
            )

    return {"user_count": len(users), "users": users[:500]}


def _classify_agreement(
    term_types: set, estimated_cost: float | None, has_end_date: bool, auto_renew: bool | None
) -> str:
    """Classify an agreement based on its terms (mirrors marketplace investigator)."""
    if "recurringPaymentTerm" in term_types:
        return "SaaS (Auto-Renew)"
    if "renewalTerm" in term_types:
        if auto_renew is False:
            return "SaaS (Auto-Renew Disabled)"
        return "SaaS (Auto-Renew)"
    if "fixedUpfrontPricingTerm" in term_types or "configurableUpfrontPricingTerm" in term_types:
        return "Fixed/Upfront"
    if has_end_date and estimated_cost is not None and estimated_cost > 0:
        return "SaaS (Auto-Renew)"
    return "Pay-As-You-Go"


def _enrich_agreement(mp: Any, agreement_id: str) -> dict:
    """Call describe_agreement + get_agreement_terms for full enrichment."""
    # describe_agreement for cost, dates, product info
    try:
        agreement = mp.describe_agreement(agreementId=agreement_id)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            return {"status": "NOT_FOUND", "error": e.response["Error"]["Message"]}
        if code in ("AccessDeniedException", "ThrottlingException"):
            return {"status": code, "error": e.response["Error"]["Message"]}
        return {"status": f"ERROR_{code}", "error": str(e)}

    status = agreement.get("status", "UNKNOWN")

    # Estimated cost
    estimated_cost = None
    currency = None
    charges = agreement.get("estimatedCharges", {})
    if charges.get("agreementValue"):
        with contextlib.suppress(ValueError, TypeError):
            estimated_cost = float(charges["agreementValue"])
    if charges.get("currencyCode"):
        currency = charges["currencyCode"]

    # Dates
    start_date = agreement.get("startTime")
    end_date = agreement.get("endTime")
    agreement_start = start_date.isoformat() if start_date else ""
    agreement_end = end_date.isoformat() if end_date else ""

    # Product info from proposalSummary
    product_id = ""
    offer_type = ""
    proposal = agreement.get("proposalSummary", {})
    resources = proposal.get("resources", [])
    if resources and isinstance(resources[0], dict):
        product_id = resources[0].get("id", "")
    offer_type = proposal.get("offerType", "")

    # get_agreement_terms for renewal/pricing details
    term_types: set[str] = set()
    auto_renew: bool | None = None
    term_details = []
    try:
        terms_resp = mp.get_agreement_terms(agreementId=agreement_id)
        for term in terms_resp.get("acceptedTerms", []):
            for term_type, term_data in term.items():
                term_types.add(term_type)
                if isinstance(term_data, dict):
                    detail: dict = {"type": term_type}
                    if "currencyCode" in term_data:
                        detail["currency"] = term_data["currencyCode"]
                    if "price" in term_data:
                        detail["price"] = term_data["price"]
                    if "durationValue" in term_data:
                        detail["duration"] = term_data["durationValue"]
                        detail["duration_type"] = term_data.get("durationType", "")
                    # Extract auto-renew config from renewalTerm
                    if term_type == "renewalTerm":
                        config = term_data.get("configuration", {})
                        auto_renew = config.get("enableAutoRenew")
                    term_details.append(detail)
    except ClientError:
        pass

    classification = _classify_agreement(
        term_types, estimated_cost, bool(agreement_end), auto_renew
    )

    entry: dict = {
        "agreement_id": agreement_id,
        "status": status,
        "product_id": product_id,
        "offer_type": offer_type,
        "agreement_start": agreement_start,
        "agreement_end": agreement_end,
        "classification": classification,
    }
    if estimated_cost is not None:
        entry["estimated_cost_usd"] = round(estimated_cost, 2)
    if currency:
        entry["currency"] = currency
    if auto_renew is not None:
        entry["auto_renew"] = auto_renew
    if term_details:
        entry["terms"] = term_details

    return entry


def _describe_marketplace(creds: dict, region: str, filters: dict | None) -> dict:
    """Get marketplace agreement details.

    If filters contains agreement_ids (from CloudTrail), enrich each directly.
    Otherwise try search_agreements (works on some accounts) as discovery.
    """
    mp = _make_client(creds, "marketplace-agreement", "us-east-1")

    agreements = []

    # If specific agreement IDs provided (e.g. from CloudTrail responseElements),
    # enrich each directly — this is the primary flow
    if filters and "agreement_ids" in filters:
        for aid in filters["agreement_ids"]:
            enriched = _enrich_agreement(mp, aid)
            agreements.append(enriched)
        return {"agreement_count": len(agreements), "agreements": agreements}

    # Discovery via search_agreements — may fail on member accounts
    try:
        response = mp.search_agreements(
            catalog="AWSMarketplace",
            filters=[{"name": "AgreementType", "values": ["PurchaseAgreement"]}],
        )
        for summary in response.get("agreementViewSummaries", []):
            agreement_id = summary.get("agreementId", "")
            if not agreement_id:
                continue
            enriched = _enrich_agreement(mp, agreement_id)
            agreements.append(enriched)
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "ValidationException":
            # search_agreements doesn't work on all accounts — guide user to
            # use CloudTrail Lake to discover agreement IDs first
            return {
                "agreement_count": 0,
                "agreements": [],
                "note": (
                    "SearchAgreements is not supported on this account. "
                    "Use query_cloudtrail to find AcceptAgreementRequest events "
                    "for this account, then call describe_marketplace again with "
                    "filters: {agreement_ids: ['agmt-...']} to get details."
                ),
            }
        if error_code in ("AccessDeniedException", "ForbiddenException"):
            return {"error": f"No marketplace-agreement access in account: {e}"}
        raise

    return {"agreement_count": len(agreements), "agreements": agreements}


_ACTION_DISPATCH = {
    "describe_instances": _describe_instances,
    "lookup_events": _lookup_events,
    "list_users": _list_users,
    "describe_marketplace": _describe_marketplace,
}


def _execute_action(account_id: str, action: str, region: str, filters: dict | None) -> dict:
    """Assume role and dispatch to action handler."""
    session = get_aws_session()
    creds = _get_assumed_creds(session, account_id)
    if creds is None:
        # Check if account is suspended/closed (common for retired sandboxes)
        status, name = _check_account_status(session, account_id)
        if status == "SUSPENDED":
            return {
                "error": (
                    f"Account {account_id} ({name or 'unknown'}) is suspended/closed. "
                    "Cannot assume role in suspended accounts."
                ),
                "account_status": "SUSPENDED",
                "account_name": name or "",
            }
        return {
            "error": (
                f"Cannot assume role in account {account_id}. "
                "The account may not have OrganizationAccountAccessRole "
                "or may not be in our organization."
            ),
            "account_status": status or "UNKNOWN",
            "account_name": name or "",
        }

    handler = _ACTION_DISPATCH.get(action)
    if not handler:
        return {"error": f"Unknown action: {action}. Valid: {list(_ACTION_DISPATCH.keys())}"}

    return handler(creds, region, filters)


async def query_aws_account(
    account_id: str,
    action: str,
    region: str = "us-east-1",
    filters: dict | None = None,
) -> dict:
    """Inspect an individual AWS member account (read-only).

    Args:
        account_id: 12-digit AWS account ID.
        action: One of describe_instances, lookup_events, list_users, describe_marketplace.
        region: AWS region. Default: us-east-1.
        filters: Optional action-specific filters.

    Returns:
        Dict with action results.
    """
    try:
        # Validate account ID format
        if not (len(account_id) == 12 and account_id.isdigit()):
            return {"error": f"Invalid AWS account ID: {account_id}. Must be 12 digits."}

        result = await asyncio.to_thread(_execute_action, account_id, action, region, filters)
        result["account_id"] = account_id
        result["action"] = action
        result["region"] = region
        return result

    except Exception as e:
        logger.exception("AWS account query failed for %s/%s", account_id, action)
        return {"error": f"AWS account query failed: {e}"}
