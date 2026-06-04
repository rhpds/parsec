"""Azure Cosmos DB client for pool subscription data."""

import logging

from azure.cosmos import CosmosClient

from src.config import get_config

logger = logging.getLogger(__name__)

_cosmos_client: CosmosClient | None = None


def init_azure_cosmos() -> None:
    """Initialize the Azure Cosmos DB client."""
    global _cosmos_client
    cfg = get_config()
    cosmos_cfg = cfg.get("azure_cosmos", {})

    endpoint = cosmos_cfg.get("endpoint", "")
    key = cosmos_cfg.get("key", "")
    if not endpoint or not key:
        logger.warning("Azure Cosmos endpoint/key not configured — pool tools disabled")
        return

    _cosmos_client = CosmosClient(endpoint, credential=key)
    logger.info(
        "Azure Cosmos client initialized (endpoint=%s)", endpoint.split("//")[1].split("/")[0]
    )


def get_cosmos_container(database: str = "pools", container: str = "lists"):
    """Get a Cosmos DB container client. Returns None if not configured."""
    if _cosmos_client is None:
        return None
    return _cosmos_client.get_database_client(database).get_container_client(container)
