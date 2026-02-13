"""AWS boto3 session and Cost Explorer client."""

import logging

import boto3

from src.config import get_config

logger = logging.getLogger(__name__)

_session = None
_ce_client = None


def init_aws() -> None:
    """Initialize the AWS session and Cost Explorer client."""
    global _session, _ce_client
    cfg = get_config()
    aws_cfg = cfg.aws

    profile = aws_cfg.get("profile")
    region = aws_cfg.get("region", "us-east-1")
    access_key_id = aws_cfg.get("access_key_id", "")
    secret_access_key = aws_cfg.get("secret_access_key", "")

    if access_key_id and secret_access_key:
        # Key-based auth (OpenShift / CI)
        session = boto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )
        logger.info("AWS session initialized (key-based, region=%s)", region)
    else:
        # Profile-based auth (local dev)
        session = boto3.Session(profile_name=profile, region_name=region)
        logger.info("AWS session initialized (profile=%s, region=%s)", profile, region)

    _session = session
    _ce_client = session.client("ce")


def get_aws_session() -> boto3.Session:
    """Get the shared boto3 Session for creating service clients."""
    if _session is None:
        raise RuntimeError("AWS not initialized — call init_aws() first")
    return _session


def get_ce_client():
    """Get the Cost Explorer client."""
    if _ce_client is None:
        raise RuntimeError("AWS not initialized — call init_aws() first")
    return _ce_client
