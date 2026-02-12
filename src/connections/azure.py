"""Azure Blob Storage client for billing CSVs."""

import logging

from azure.identity import ClientSecretCredential, DefaultAzureCredential
from azure.storage.blob import ContainerClient

from src.config import get_config

logger = logging.getLogger(__name__)

_container_client: ContainerClient | None = None


def init_azure() -> None:
    """Initialize the Azure Blob Storage container client."""
    global _container_client
    cfg = get_config()
    az = cfg.azure

    storage_account = az.get("storage_account", "")
    container = az.get("container", "")
    if not storage_account or not container:
        logger.warning("Azure storage_account/container not configured — Azure tools disabled")
        return

    account_url = f"https://{storage_account}.blob.core.windows.net"

    client_id = az.get("client_id", "")
    client_secret = az.get("client_secret", "")
    tenant_id = az.get("tenant_id", "")

    if client_id and client_secret and tenant_id:
        credential = ClientSecretCredential(tenant_id, client_id, client_secret)
    else:
        credential = DefaultAzureCredential()

    _container_client = ContainerClient(account_url, container, credential=credential)
    logger.info(
        "Azure blob client initialized (account=%s, container=%s)", storage_account, container
    )


def get_container_client() -> ContainerClient | None:
    """Get the Azure container client (None if not configured)."""
    return _container_client
