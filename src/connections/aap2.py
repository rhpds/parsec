"""AAP2 controller connections — httpx-based REST API clients."""

import logging
from urllib.parse import urlparse

import httpx

from src.config import get_config

logger = logging.getLogger(__name__)

# Parsed cluster configs: {name: {url, username, password}}
_cluster_configs: dict[str, dict[str, str]] = {}
# Cached clients keyed by cluster name
_clients: dict[str, httpx.AsyncClient] = {}


def init_aap2() -> None:
    """Initialize AAP2 controller connections from config."""
    cfg = get_config()
    aap2_cfg = cfg.get("aap2", {})
    clusters = aap2_cfg.get("clusters", {})

    if not clusters:
        logger.info("No AAP2 controllers configured — job lookups disabled")
        return

    for name, cluster_cfg in clusters.items():
        name_lower = name.lower()
        if not isinstance(cluster_cfg, dict):
            continue
        url = cluster_cfg.get("url", "") or cluster_cfg.get("URL", "")
        username = cluster_cfg.get("username", "") or cluster_cfg.get("USERNAME", "")
        password = cluster_cfg.get("password", "") or cluster_cfg.get("PASSWORD", "")  # noqa: S105
        if not url:
            logger.warning("AAP2 cluster '%s' has no URL", name_lower)
            continue
        if not username or not password:
            logger.warning("AAP2 cluster '%s' has no credentials", name_lower)
            continue

        _cluster_configs[name_lower] = {
            "url": url.rstrip("/"),
            "username": username,
            "password": password,
        }
        logger.info("AAP2 cluster '%s' configured (url=%s)", name_lower, url)

    logger.info("AAP2: %d controllers configured", len(_cluster_configs))


def get_configured_controllers() -> list[str]:
    """Return list of configured AAP2 controller names."""
    return list(_cluster_configs.keys())


def resolve_controller(controller: str) -> str:
    """Resolve a controller input to a configured cluster name.

    Accepts:
      - Short name: "east" -> exact match against config keys
      - Full hostname: "aap2-prod-us-east-2.aap.infra.demo.redhat.com"
        -> contains match against configured URLs

    Returns the cluster name, or raises ValueError if not found.
    """
    if not controller:
        raise ValueError(
            "No controller specified. " f"Configured: {', '.join(_cluster_configs.keys())}"
        )

    key = controller.lower().strip()

    # Exact match on cluster name
    if key in _cluster_configs:
        return key

    # Contains match on URL hostname
    for name, cfg in _cluster_configs.items():
        parsed = urlparse(cfg["url"])
        hostname = (parsed.hostname or "").lower()
        if key in hostname or hostname in key:
            return name

    raise ValueError(
        f"Unknown AAP2 controller: '{controller}'. "
        f"Configured: {', '.join(_cluster_configs.keys())}"
    )


async def _get_client(cluster_name: str) -> httpx.AsyncClient:
    """Get or create an httpx client for an AAP2 controller."""
    if cluster_name in _clients:
        return _clients[cluster_name]

    if cluster_name not in _cluster_configs:
        raise ValueError(
            f"Unknown AAP2 controller: '{cluster_name}'. "
            f"Configured: {list(_cluster_configs.keys())}"
        )

    cfg = _cluster_configs[cluster_name]
    client = httpx.AsyncClient(
        base_url=cfg["url"],
        auth=httpx.BasicAuth(cfg["username"], cfg["password"]),
        timeout=30.0,
        headers={"Accept": "application/json"},
    )
    _clients[cluster_name] = client
    return client


async def api_get(cluster_name: str, path: str, params: dict | None = None) -> dict:
    """Make a GET request to the AAP2 REST API.

    Returns the JSON response body. Raises on HTTP errors with clear messages.
    """
    client = await _get_client(cluster_name)
    resp = await client.get(path, params=params or {})

    if resp.status_code == 401:
        raise PermissionError(f"Authentication failed for controller '{cluster_name}' (HTTP 401)")
    if resp.status_code == 404:
        raise LookupError(f"Not found on controller '{cluster_name}': {path} (HTTP 404)")

    resp.raise_for_status()
    return resp.json()


async def api_paginate(
    cluster_name: str,
    path: str,
    params: dict | None = None,
    max_results: int = 50,
) -> list[dict]:
    """Paginate through AAP2 API results.

    The AAP2 API returns paginated responses with 'next' URLs.
    Collects up to max_results items.
    """
    params = dict(params or {})
    params.setdefault("page_size", min(max_results, 200))

    results: list[dict] = []
    data = await api_get(cluster_name, path, params)
    results.extend(data.get("results", []))

    while len(results) < max_results and data.get("next"):
        next_url = data["next"]
        parsed = urlparse(next_url)
        next_path = parsed.path
        if parsed.query:
            next_path += f"?{parsed.query}"

        data = await api_get(cluster_name, next_path)
        results.extend(data.get("results", []))

    return results[:max_results]


async def close_clients() -> None:
    """Close all httpx clients."""
    for client in _clients.values():
        await client.aclose()
    _clients.clear()
