#!/usr/bin/env python3
"""Refresh the Azure billing SQLite cache from Azure Blob Storage.

Downloads billing CSVs from Azure Blob Storage and ingests them into a local
SQLite database for fast querying. Uses incremental processing — only new or
changed blobs (by ETag) are downloaded and parsed.

The database uses WAL mode for concurrent reads during writes, so the app
can query the cache while the CronJob is refreshing it.

Usage:
    python3 scripts/refresh_azure_billing.py
    python3 scripts/refresh_azure_billing.py --force
    python3 scripts/refresh_azure_billing.py --init-only
"""

import argparse
import csv
import logging
import os
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_FILE = os.path.join(DATA_DIR, "azure_billing.db")

SCHEMA_VERSION = "1"
BATCH_SIZE = 10_000


def get_azure_client():
    """Create an Azure Blob Storage container client from env vars."""
    # Import here so the script fails fast with a clear message
    try:
        from azure.identity import ClientSecretCredential, DefaultAzureCredential
        from azure.storage.blob import ContainerClient
    except ImportError:
        logger.error("azure-storage-blob and azure-identity packages required")
        sys.exit(1)

    storage_account = os.environ.get("PARSEC_AZURE__STORAGE_ACCOUNT", "")
    container = os.environ.get("PARSEC_AZURE__CONTAINER", "")
    if not storage_account or not container:
        logger.error("PARSEC_AZURE__STORAGE_ACCOUNT and PARSEC_AZURE__CONTAINER must be set")
        sys.exit(1)

    account_url = f"https://{storage_account}.blob.core.windows.net"

    client_id = os.environ.get("PARSEC_AZURE__CLIENT_ID", "")
    client_secret = os.environ.get("PARSEC_AZURE__CLIENT_SECRET", "")
    tenant_id = os.environ.get("PARSEC_AZURE__TENANT_ID", "")

    if client_id and client_secret and tenant_id:
        credential = ClientSecretCredential(tenant_id, client_id, client_secret)
    else:
        credential = DefaultAzureCredential()

    return ContainerClient(account_url, container, credential=credential)


def init_db(db_path: str) -> sqlite3.Connection:
    """Create or open the SQLite database and ensure schema exists."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS billing_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_name TEXT NOT NULL,
            date TEXT NOT NULL,
            meter_category TEXT NOT NULL DEFAULT '',
            meter_subcategory TEXT NOT NULL DEFAULT '',
            cost REAL NOT NULL DEFAULT 0.0,
            blob_name TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_billing_sub_date
            ON billing_rows (subscription_name, date);
        CREATE INDEX IF NOT EXISTS idx_billing_date_category
            ON billing_rows (date, meter_category);
        CREATE INDEX IF NOT EXISTS idx_billing_date_subcategory
            ON billing_rows (date, meter_subcategory);
        CREATE INDEX IF NOT EXISTS idx_billing_blob
            ON billing_rows (blob_name);

        CREATE TABLE IF NOT EXISTS processed_blobs (
            blob_name TEXT PRIMARY KEY,
            etag TEXT,
            last_modified TEXT,
            row_count INTEGER DEFAULT 0,
            processed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS cache_metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """
    )

    conn.execute(
        "INSERT OR REPLACE INTO cache_metadata (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    return conn


def blob_line_iterator(blob_client) -> Iterator[str]:
    """Yield text lines from a blob, streaming chunk by chunk.

    Only one chunk (~4 MB) plus a partial-line buffer are held in memory
    at a time, instead of loading the entire blob.
    """
    stream = blob_client.download_blob()
    buffer = ""
    first_chunk = True
    for chunk in stream.chunks():
        encoding = "utf-8-sig" if first_chunk else "utf-8"
        first_chunk = False
        buffer += chunk.decode(encoding)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    if buffer:
        yield buffer


def parse_date(date_str: str) -> str | None:
    """Parse a billing CSV date into YYYY-MM-DD format."""
    if not date_str:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(date_str.split(" ")[0] if " " in date_str else date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def ingest_blob(
    conn: sqlite3.Connection,
    container_client,
    blob_name: str,
    etag: str,
    last_modified: str,
) -> int:
    """Download and ingest a single billing CSV blob into SQLite.

    Returns the number of rows inserted.
    """
    # Delete old rows for this blob (handles re-ingestion of changed blobs)
    conn.execute("DELETE FROM billing_rows WHERE blob_name = ?", (blob_name,))

    blob_client = container_client.get_blob_client(blob_name)
    reader = csv.DictReader(blob_line_iterator(blob_client))

    batch: list[tuple] = []
    total_rows = 0

    for row in reader:
        sub_name = row.get("SubscriptionName", row.get("subscriptionName", ""))
        date_str = row.get("Date", row.get("date", row.get("UsageDateTime", "")))
        parsed_date = parse_date(date_str)
        if not parsed_date:
            continue

        meter_category = row.get("MeterCategory", row.get("meterCategory", ""))
        meter_subcategory = row.get("MeterSubCategory", row.get("meterSubCategory", ""))

        cost_str = row.get(
            "CostInBillingCurrency",
            row.get("costInBillingCurrency", row.get("Cost", "0")),
        )
        try:
            cost = float(cost_str)
        except (ValueError, TypeError):
            cost = 0.0

        batch.append((sub_name, parsed_date, meter_category, meter_subcategory, cost, blob_name))

        if len(batch) >= BATCH_SIZE:
            conn.executemany(
                "INSERT INTO billing_rows "
                "(subscription_name, date, meter_category, meter_subcategory, cost, blob_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                batch,
            )
            total_rows += len(batch)
            batch.clear()

    # Insert remaining rows
    if batch:
        conn.executemany(
            "INSERT INTO billing_rows "
            "(subscription_name, date, meter_category, meter_subcategory, cost, blob_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            batch,
        )
        total_rows += len(batch)

    # Record blob as processed
    conn.execute(
        "INSERT OR REPLACE INTO processed_blobs "
        "(blob_name, etag, last_modified, row_count, processed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            blob_name,
            etag,
            last_modified,
            total_rows,
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()
    return total_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh Azure billing SQLite cache")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess all blobs, ignoring cached ETags",
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="Create database schema and exit (used by init containers)",
    )
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    if args.init_only:
        if os.path.exists(DB_FILE):
            logger.info("Azure billing cache already exists at %s, skipping init", DB_FILE)
        else:
            logger.info("Initializing Azure billing cache schema at %s", DB_FILE)
            conn = init_db(DB_FILE)
            conn.close()
            logger.info("Schema created successfully")
        return

    logger.info("Starting Azure billing cache refresh")
    container_client = get_azure_client()
    conn = init_db(DB_FILE)

    # Load processed blob ETags
    if args.force:
        processed: dict[str, str] = {}
        logger.info("Force mode: will reprocess all blobs")
    else:
        cursor = conn.execute("SELECT blob_name, etag FROM processed_blobs")
        processed = {row[0]: row[1] for row in cursor.fetchall()}
        logger.info("Found %d previously processed blobs", len(processed))

    # List all billing CSV blobs
    blobs_to_process: list[tuple[str, str, str]] = []
    total_blobs = 0

    for blob in container_client.list_blobs():
        name = blob.name
        if "part_1" not in name or not name.endswith(".csv"):
            continue
        total_blobs += 1

        blob_etag = blob.etag or ""
        blob_modified = str(blob.last_modified or "")

        if not args.force and name in processed and processed[name] == blob_etag:
            continue

        blobs_to_process.append((name, blob_etag, blob_modified))

    logger.info(
        "Found %d total billing blobs, %d need processing",
        total_blobs,
        len(blobs_to_process),
    )

    if not blobs_to_process:
        logger.info("All blobs are current. No update needed.")
        conn.execute(
            "INSERT OR REPLACE INTO cache_metadata (key, value) VALUES ('last_refresh', ?)",
            (datetime.now(UTC).isoformat(),),
        )
        conn.commit()
        conn.close()
        return

    total_rows_inserted = 0
    for i, (blob_name, etag, last_modified) in enumerate(blobs_to_process, 1):
        logger.info(
            "[%d/%d] Processing %s",
            i,
            len(blobs_to_process),
            blob_name,
        )
        try:
            rows = ingest_blob(conn, container_client, blob_name, etag, last_modified)
            total_rows_inserted += rows
            logger.info("  Inserted %d rows", rows)
        except Exception:
            logger.exception("  Failed to process %s", blob_name)
            continue

    # Update last_refresh timestamp
    conn.execute(
        "INSERT OR REPLACE INTO cache_metadata (key, value) VALUES ('last_refresh', ?)",
        (datetime.now(UTC).isoformat(),),
    )
    conn.commit()

    # Final stats
    cursor = conn.execute("SELECT COUNT(*) FROM billing_rows")
    total_rows = cursor.fetchone()[0]
    cursor = conn.execute("SELECT COUNT(*) FROM processed_blobs")
    total_processed = cursor.fetchone()[0]
    conn.close()

    db_size_mb = os.path.getsize(DB_FILE) / (1024 * 1024)
    logger.info(
        "Done. %d rows inserted this run. Total: %d rows from %d blobs (%.1f MB)",
        total_rows_inserted,
        total_rows,
        total_processed,
        db_size_mb,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(1)
