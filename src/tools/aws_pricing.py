"""Tool: query_aws_pricing — look up on-demand pricing for AWS instance types."""

import json
import logging

import boto3

from src.config import get_config

logger = logging.getLogger(__name__)

_pricing_client = None


def _get_pricing_client():
    """Get or create the AWS Pricing client (always us-east-1)."""
    global _pricing_client
    if _pricing_client is None:
        cfg = get_config()
        aws_cfg = cfg.aws
        profile = aws_cfg.get("profile")
        access_key_id = aws_cfg.get("access_key_id", "")
        secret_access_key = aws_cfg.get("secret_access_key", "")

        if access_key_id and secret_access_key:
            session = boto3.Session(
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name="us-east-1",
            )
        else:
            session = boto3.Session(profile_name=profile, region_name="us-east-1")

        # Pricing API is only available in us-east-1 and ap-south-1
        _pricing_client = session.client("pricing", region_name="us-east-1")
    return _pricing_client


async def query_aws_pricing(
    instance_type: str,
    region: str = "us-east-1",
    os_type: str = "Linux",
) -> dict:
    """Look up on-demand pricing for an AWS EC2 instance type.

    Args:
        instance_type: EC2 instance type (e.g. g4dn.xlarge, m5.large).
        region: AWS region code (e.g. us-east-1). Default: us-east-1.
        os_type: Operating system. Default: Linux.

    Returns:
        Dict with pricing details.
    """
    # Map region codes to Pricing API location names
    region_map = {
        "us-east-1": "US East (N. Virginia)",
        "us-east-2": "US East (Ohio)",
        "us-west-1": "US West (N. California)",
        "us-west-2": "US West (Oregon)",
        "eu-west-1": "EU (Ireland)",
        "eu-west-2": "EU (London)",
        "eu-central-1": "EU (Frankfurt)",
        "ap-southeast-1": "Asia Pacific (Singapore)",
        "ap-southeast-2": "Asia Pacific (Sydney)",
        "ap-northeast-1": "Asia Pacific (Tokyo)",
        "ap-south-1": "Asia Pacific (Mumbai)",
        "ca-central-1": "Canada (Central)",
        "sa-east-1": "South America (Sao Paulo)",
    }

    location = region_map.get(region, region)

    try:
        client = _get_pricing_client()

        filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": os_type},
            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
            {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
            {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
        ]

        response = client.get_products(
            ServiceCode="AmazonEC2",
            Filters=filters,
            MaxResults=10,
        )

        price_list = response.get("PriceList", [])
        if not price_list:
            return {
                "instance_type": instance_type,
                "region": region,
                "error": f"No pricing found for {instance_type} in {location}",
            }

        results = []
        for price_json in price_list:
            product = json.loads(price_json) if isinstance(price_json, str) else price_json
            attrs = product.get("product", {}).get("attributes", {})
            terms = product.get("terms", {}).get("OnDemand", {})

            # Extract on-demand hourly price
            hourly_price = None
            for term in terms.values():
                for dim in term.get("priceDimensions", {}).values():
                    price_per_unit = dim.get("pricePerUnit", {}).get("USD")
                    if price_per_unit:
                        hourly_price = float(price_per_unit)
                        break

            if hourly_price is not None:
                results.append({
                    "instance_type": attrs.get("instanceType", instance_type),
                    "vcpu": attrs.get("vcpu", ""),
                    "memory": attrs.get("memory", ""),
                    "gpu": attrs.get("gpu", ""),
                    "gpu_memory": attrs.get("gpuMemory", ""),
                    "storage": attrs.get("storage", ""),
                    "network": attrs.get("networkPerformance", ""),
                    "hourly_price_usd": hourly_price,
                    "daily_price_usd": round(hourly_price * 24, 2),
                    "monthly_price_usd": round(hourly_price * 730, 2),
                    "os": os_type,
                    "region": region,
                })

        if not results:
            return {
                "instance_type": instance_type,
                "region": region,
                "error": f"No on-demand pricing found for {instance_type} in {location}",
            }

        return {
            "instance_type": instance_type,
            "region": region,
            "pricing": results[0],
        }

    except Exception as e:
        logger.exception("AWS Pricing API query failed")
        return {"error": f"AWS Pricing API query failed: {e}"}
