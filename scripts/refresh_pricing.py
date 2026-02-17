#!/usr/bin/env python3
"""Refresh the EC2 pricing cache from public AWS pricing data.

Downloads per-region pricing files from the public AWS Pricing Bulk API.
No AWS credentials required. Uses ETag-based conditional requests to avoid
redundant downloads — only fetches data when AWS publishes new pricing.

The region_index.json (18KB) is checked first. If its ETag hasn't changed,
no downloads are needed. When it has changed, per-region files (~400MB each)
are downloaded only if their individual ETags have changed.

Usage:
    python3 scripts/refresh_pricing.py
    python3 scripts/refresh_pricing.py --regions us-east-1,us-west-2
    python3 scripts/refresh_pricing.py --force
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CACHE_FILE = os.path.join(DATA_DIR, "ec2_pricing.json")
ETAG_FILE = os.path.join(DATA_DIR, ".pricing_etags.json")

PRICING_BASE = "https://pricing.us-east-1.amazonaws.com"
REGION_INDEX_URL = f"{PRICING_BASE}/offers/v1.0/aws/AmazonEC2/current/region_index.json"

DEFAULT_REGIONS = [
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "eu-west-1",
    "eu-west-2",
    "eu-central-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "ap-south-1",
    "ca-central-1",
    "sa-east-1",
]


def load_etags() -> dict:
    if os.path.exists(ETAG_FILE):
        with open(ETAG_FILE) as f:
            return json.load(f)
    return {}


def save_etags(etags: dict) -> None:
    with open(ETAG_FILE, "w") as f:
        json.dump(etags, f, indent=2, sort_keys=True)


def check_etag(url: str, stored_etag: str) -> tuple[bool, str]:
    """HEAD request with If-None-Match. Returns (changed, new_etag)."""
    req = urllib.request.Request(url, method="HEAD")
    if stored_etag:
        req.add_header("If-None-Match", stored_etag)
    try:
        resp = urllib.request.urlopen(req)
        return True, resp.headers.get("ETag", "")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return False, stored_etag
        raise


def download_to_file(url: str, dest: str, etag: str = "") -> tuple[bool, str]:
    """Download a URL to a file with optional ETag check.

    Returns (downloaded, new_etag). If not downloaded (304), returns False.
    """
    req = urllib.request.Request(url)
    if etag:
        req.add_header("If-None-Match", etag)

    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return False, etag
        raise

    new_etag = resp.headers.get("ETag", "")
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0

    with open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                mb = downloaded / (1024 * 1024)
                total_mb = total / (1024 * 1024)
                pct = downloaded * 100 // total
                print(
                    f"\r  Downloading: {mb:.0f}/{total_mb:.0f} MB ({pct}%)",
                    end="",
                    flush=True,
                )
    if total:
        print(flush=True)

    return True, new_etag


def extract_instances(data: dict) -> dict:
    """Extract EC2 instance type pricing from a parsed region pricing file."""
    products = data.get("products", {})
    terms = data.get("terms", {}).get("OnDemand", {})

    instances: dict = {}

    for sku, product in products.items():
        if product.get("productFamily") != "Compute Instance":
            continue

        attrs = product.get("attributes", {})
        if attrs.get("tenancy") != "Shared":
            continue
        if attrs.get("preInstalledSw") != "NA":
            continue
        if attrs.get("capacitystatus") != "Used":
            continue

        instance_type = attrs.get("instanceType")
        os_type = attrs.get("operatingSystem")
        if not instance_type or not os_type:
            continue

        # Extract on-demand hourly price
        term_data = terms.get(sku, {})
        hourly_price = None
        for term in term_data.values():
            for dim in term.get("priceDimensions", {}).values():
                price = dim.get("pricePerUnit", {}).get("USD")
                if price:
                    try:
                        hourly_price = float(price)
                    except ValueError:
                        continue
                    break
            if hourly_price is not None:
                break

        if hourly_price is None or hourly_price == 0:
            continue

        if instance_type not in instances:
            instances[instance_type] = {
                "vcpu": attrs.get("vcpu", ""),
                "memory": attrs.get("memory", ""),
                "gpu": attrs.get("gpu", ""),
                "gpu_memory": attrs.get("gpuMemory", ""),
                "storage": attrs.get("storage", ""),
                "network": attrs.get("networkPerformance", ""),
                "pricing": {},
            }

        instances[instance_type]["pricing"][os_type] = hourly_price

    return instances


def merge_instances(target: dict, region: str, region_instances: dict) -> None:
    """Merge instance data from a region into the combined dict."""
    for itype, idata in region_instances.items():
        if itype not in target:
            target[itype] = {
                "vcpu": idata["vcpu"],
                "memory": idata["memory"],
                "gpu": idata["gpu"],
                "gpu_memory": idata["gpu_memory"],
                "storage": idata["storage"],
                "network": idata["network"],
                "pricing": {},
            }

        if region not in target[itype]["pricing"]:
            target[itype]["pricing"][region] = {}
        target[itype]["pricing"][region].update(idata["pricing"])


def save_cache(instances: dict, regions: list[str]) -> None:
    """Write the pricing cache file."""
    output = {
        "generated_at": datetime.now(UTC).isoformat(),
        "regions": sorted(regions),
        "instance_count": len(instances),
        "instances": dict(sorted(instances.items())),
    }
    with open(CACHE_FILE, "w") as f:
        json.dump(output, f, separators=(",", ":"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh EC2 pricing cache")
    parser.add_argument(
        "--regions",
        default=",".join(DEFAULT_REGIONS),
        help="Comma-separated region codes (default: 13 major regions)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force refresh, ignore cached ETags",
    )
    args = parser.parse_args()

    regions = [r.strip() for r in args.regions.split(",")]
    etags = {} if args.force else load_etags()

    os.makedirs(DATA_DIR, exist_ok=True)

    # Quick check: has the region index changed at all?
    if not args.force:
        print("Checking for pricing updates...", flush=True)
        changed, new_etag = check_etag(REGION_INDEX_URL, etags.get("region_index", ""))
        if not changed:
            print("Pricing data is current. No update needed.")
            return
        print("New pricing data available.\n", flush=True)
        etags["region_index"] = new_etag

    # Load existing cache to preserve unchanged regions
    all_instances: dict = {}
    cached_regions: set[str] = set()
    if os.path.exists(CACHE_FILE) and not args.force:
        with open(CACHE_FILE) as f:
            existing = json.load(f)
        all_instances = existing.get("instances", {})
        cached_regions = set(existing.get("regions", []))

    updated_regions = set(cached_regions)

    for region in regions:
        url = f"{PRICING_BASE}/offers/v1.0/aws/AmazonEC2/current/{region}/index.json"
        etag_key = f"region:{region}"
        stored_etag = etags.get(etag_key, "")

        print(f"[{region}]", flush=True)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            downloaded, new_etag = download_to_file(url, tmp_path, stored_etag)

            if not downloaded:
                print("  No changes (ETag match)\n", flush=True)
                updated_regions.add(region)
                continue

            etags[etag_key] = new_etag

            print("  Parsing...", end="", flush=True)
            with open(tmp_path) as f:
                data = json.load(f)

            region_instances = extract_instances(data)
            del data  # free memory before merging

            merge_instances(all_instances, region, region_instances)
            updated_regions.add(region)
            print(f" {len(region_instances)} instance types\n", flush=True)

            # Save after each region so progress isn't lost on interrupt
            save_cache(all_instances, sorted(updated_regions))
            save_etags(etags)

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    size_mb = os.path.getsize(CACHE_FILE) / (1024 * 1024)
    print(f"Done. {len(all_instances)} instance types across {len(updated_regions)} regions.")
    print(f"Cache: {CACHE_FILE} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Partial results have been saved.", file=sys.stderr)
        sys.exit(1)
