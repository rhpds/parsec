"""OCPV cluster connections — httpx-based K8s API clients for CNV infrastructure."""

import logging
import os
import ssl
from base64 import b64decode
from typing import Any

import httpx
import yaml

from src.config import get_config

logger = logging.getLogger(__name__)

# Cached clients keyed by cluster name
_clients: dict[str, httpx.AsyncClient] = {}
# Parsed cluster configs: {name: {server, token, verify_ssl, ca_data, ...}}
_cluster_configs: dict[str, dict[str, Any]] = {}


def _parse_kubeconfig(path: str) -> dict[str, Any]:
    """Parse a kubeconfig file and extract server URL, token, and TLS settings."""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise FileNotFoundError(f"Kubeconfig not found: {expanded}")

    with open(expanded) as f:
        kc = yaml.safe_load(f)

    contexts = kc.get("contexts", [])
    if not contexts:
        raise ValueError(f"No contexts in kubeconfig: {path}")

    current_ctx = kc.get("current-context", "")
    ctx = None
    if current_ctx:
        ctx = next((c for c in contexts if c["name"] == current_ctx), None)
    if not ctx:
        ctx = contexts[0]

    ctx_info = ctx["context"]
    cluster_name = ctx_info["cluster"]
    user_name = ctx_info.get("user", "")

    clusters = kc.get("clusters", [])
    cluster = next((c for c in clusters if c["name"] == cluster_name), None)
    if not cluster:
        raise ValueError(f"Cluster '{cluster_name}' not found in kubeconfig: {path}")

    cluster_data = cluster["cluster"]
    server = cluster_data["server"].rstrip("/")
    verify_ssl = not cluster_data.get("insecure-skip-tls-verify", False)
    ca_data = cluster_data.get("certificate-authority-data", "")

    token = ""  # nosec B105
    if user_name:
        users = kc.get("users", [])
        user = next((u for u in users if u["name"] == user_name), None)
        if user and "user" in user:
            token = user["user"].get("token", "")

    return {
        "server": server,
        "token": token,
        "verify_ssl": verify_ssl,
        "ca_data": ca_data,
    }


def init_ocpv() -> None:
    """Initialize OCPV cluster connections from config."""
    cfg = get_config()
    ocpv_cfg = cfg.get("ocpv", {})
    clusters = ocpv_cfg.get("clusters", {})

    if not clusters:
        logger.info("No OCPV clusters configured — CNV inspection disabled")
        return

    for name, cluster_cfg in clusters.items():
        name_lower = name.lower()
        if isinstance(cluster_cfg, dict):
            kubeconfig_path = cluster_cfg.get("kubeconfig", "") or cluster_cfg.get("KUBECONFIG", "")
        else:
            continue
        if not kubeconfig_path:
            logger.warning("OCPV cluster '%s' has no kubeconfig path", name_lower)
            continue

        try:
            parsed = _parse_kubeconfig(kubeconfig_path)
            _cluster_configs[name_lower] = parsed
            logger.info(
                "OCPV cluster '%s' configured (server=%s)",
                name_lower,
                parsed["server"],
            )
        except Exception:
            logger.exception("Failed to parse kubeconfig for OCPV cluster '%s'", name_lower)

    logger.info("OCPV: %d clusters configured", len(_cluster_configs))


async def _get_client(cluster_name: str) -> httpx.AsyncClient:
    """Get or create an httpx client for an OCPV cluster."""
    if cluster_name in _clients:
        return _clients[cluster_name]

    if cluster_name not in _cluster_configs:
        raise ValueError(
            f"Unknown OCPV cluster: '{cluster_name}'. "
            f"Configured: {list(_cluster_configs.keys())}"
        )

    cluster_cfg = _cluster_configs[cluster_name]

    verify: ssl.SSLContext | bool
    if not cluster_cfg["verify_ssl"]:
        verify = False
    elif cluster_cfg.get("ca_data"):
        import tempfile

        ctx = ssl.create_default_context()
        ca_bytes = b64decode(cluster_cfg["ca_data"])
        with tempfile.NamedTemporaryFile(suffix=".crt", delete=False) as f:
            f.write(ca_bytes)
            ca_path = f.name
        ctx.load_verify_locations(ca_path)
        os.unlink(ca_path)
        verify = ctx
    else:
        verify = True

    headers: dict[str, str] = {"Accept": "application/json"}
    if cluster_cfg.get("token"):
        headers["Authorization"] = f"Bearer {cluster_cfg['token']}"

    client = httpx.AsyncClient(
        base_url=cluster_cfg["server"],
        headers=headers,
        verify=verify,
        timeout=30.0,
    )
    _clients[cluster_name] = client
    return client


def get_configured_clusters() -> list[str]:
    """Return list of configured OCPV cluster names."""
    return list(_cluster_configs.keys())


def resolve_cluster_from_comment(comment: str) -> str:
    """Resolve an OCPV cluster name from a sandbox DynamoDB comment field.

    The comment field may contain the OCPV cluster console URL. Extracts the
    hostname and matches against configured cluster server URLs.
    Returns empty string if no match.
    """
    if not comment:
        return ""

    import re

    url_match = re.search(r"https?://console-openshift-console\.apps\.(.+?)(?:\s|$)", comment)
    if not url_match:
        return ""

    cluster_domain = url_match.group(1).rstrip("/").lower()

    for name, cfg in _cluster_configs.items():
        server = cfg.get("server", "").lower()
        if cluster_domain in server:
            return name

    return ""


async def k8s_get(cluster_name: str, path: str) -> dict:
    """Make a GET request to the Kubernetes API."""
    client = await _get_client(cluster_name)
    resp = await client.get(path)
    resp.raise_for_status()
    return resp.json()


async def k8s_get_text(cluster_name: str, path: str, params: dict | None = None) -> str:
    """Make a GET request and return raw text (for pod logs)."""
    client = await _get_client(cluster_name)
    resp = await client.get(path, params=params or {})
    resp.raise_for_status()
    return resp.text


async def k8s_list_namespaced(
    cluster_name: str,
    group: str,
    version: str,
    plural: str,
    namespace: str,
    limit: int = 0,
) -> dict:
    """List resources in a namespace."""
    if group:
        path = f"/apis/{group}/{version}/namespaces/{namespace}/{plural}"
    else:
        path = f"/api/{version}/namespaces/{namespace}/{plural}"

    params: dict[str, str | int] = {}
    if limit:
        params["limit"] = limit

    client = await _get_client(cluster_name)
    resp = await client.get(path, params=params)
    resp.raise_for_status()
    return resp.json()


async def k8s_list_cluster(
    cluster_name: str,
    group: str,
    version: str,
    plural: str,
    limit: int = 0,
) -> dict:
    """List cluster-scoped resources."""
    path = f"/apis/{group}/{version}/{plural}" if group else f"/api/{version}/{plural}"

    params: dict[str, str | int] = {}
    if limit:
        params["limit"] = limit

    client = await _get_client(cluster_name)
    resp = await client.get(path, params=params)
    resp.raise_for_status()
    return resp.json()


async def close_clients() -> None:
    """Close all httpx clients."""
    for client in _clients.values():
        await client.aclose()
    _clients.clear()
