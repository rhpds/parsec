"""Babylon cluster connections — httpx-based K8s API clients."""

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
# Parsed cluster configs: {name: {server, token, verify_ssl, ca_data}}
_cluster_configs: dict[str, dict[str, Any]] = {}


def _parse_kubeconfig(path: str) -> dict[str, Any]:
    """Parse a kubeconfig file and extract server URL, token, and TLS settings.

    Returns dict with keys: server, token, verify_ssl, ca_data.
    Uses the current-context if set, otherwise the first context.
    """
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise FileNotFoundError(f"Kubeconfig not found: {expanded}")

    with open(expanded) as f:
        kc = yaml.safe_load(f)

    # Resolve context
    current_ctx = kc.get("current-context", "")
    contexts = kc.get("contexts", [])
    if not contexts:
        raise ValueError(f"No contexts in kubeconfig: {path}")

    ctx = None
    if current_ctx:
        ctx = next((c for c in contexts if c["name"] == current_ctx), None)
    if not ctx:
        ctx = contexts[0]

    ctx_info = ctx["context"]
    cluster_name = ctx_info["cluster"]
    user_name = ctx_info.get("user", "")

    # Find cluster
    clusters = kc.get("clusters", [])
    cluster = next((c for c in clusters if c["name"] == cluster_name), None)
    if not cluster:
        raise ValueError(f"Cluster '{cluster_name}' not found in kubeconfig: {path}")

    cluster_data = cluster["cluster"]
    server = cluster_data["server"].rstrip("/")
    verify_ssl = not cluster_data.get("insecure-skip-tls-verify", False)
    ca_data = cluster_data.get("certificate-authority-data", "")

    # Find user credentials (token or client certificate)
    token = ""  # nosec B105
    client_cert_data = ""
    client_key_data = ""
    if user_name:
        users = kc.get("users", [])
        user = next((u for u in users if u["name"] == user_name), None)
        if user and "user" in user:
            token = user["user"].get("token", "")
            client_cert_data = user["user"].get("client-certificate-data", "")
            client_key_data = user["user"].get("client-key-data", "")

    return {
        "server": server,
        "token": token,
        "verify_ssl": verify_ssl,
        "ca_data": ca_data,
        "client_cert_data": client_cert_data,
        "client_key_data": client_key_data,
    }


def _build_ssl_context(
    cluster_cfg: dict[str, Any],
) -> ssl.SSLContext | bool:
    """Build SSL context from cluster config, including client certificates."""
    import tempfile

    has_ca = bool(cluster_cfg.get("ca_data"))
    has_client_cert = bool(
        cluster_cfg.get("client_cert_data") and cluster_cfg.get("client_key_data")
    )

    if not cluster_cfg["verify_ssl"] and not has_client_cert:
        return False

    if not has_ca and not has_client_cert:
        return True

    # Need a real SSL context for CA and/or client certs
    if cluster_cfg["verify_ssl"]:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    if has_ca:
        ca_bytes = b64decode(cluster_cfg["ca_data"])
        with tempfile.NamedTemporaryFile(suffix=".crt", delete=False) as f:
            f.write(ca_bytes)
            ca_path = f.name
        ctx.load_verify_locations(ca_path)
        os.unlink(ca_path)

    if has_client_cert:
        cert_bytes = b64decode(cluster_cfg["client_cert_data"])
        key_bytes = b64decode(cluster_cfg["client_key_data"])

        with tempfile.NamedTemporaryFile(suffix=".crt", delete=False) as cf:
            cf.write(cert_bytes)
            cert_path = cf.name

        with tempfile.NamedTemporaryFile(suffix=".key", delete=False) as kf:
            kf.write(key_bytes)
            key_path = kf.name

        ctx.load_cert_chain(cert_path, key_path)
        os.unlink(cert_path)
        os.unlink(key_path)

    return ctx


def init_babylon() -> None:
    """Initialize Babylon cluster connections from config."""
    cfg = get_config()
    babylon_cfg = cfg.get("babylon", {})
    clusters = babylon_cfg.get("clusters", {})

    if not clusters:
        logger.info("No Babylon clusters configured — catalog lookups disabled")
        return

    for name, cluster_cfg in clusters.items():
        # Dynaconf uppercases keys — normalize to lowercase
        name_lower = name.lower()
        if isinstance(cluster_cfg, dict):
            kubeconfig_path = cluster_cfg.get("kubeconfig", "") or cluster_cfg.get("KUBECONFIG", "")
        else:
            continue
        if not kubeconfig_path:
            logger.warning("Babylon cluster '%s' has no kubeconfig path", name_lower)
            continue

        try:
            parsed = _parse_kubeconfig(kubeconfig_path)
            _cluster_configs[name_lower] = parsed
            logger.info(
                "Babylon cluster '%s' configured (server=%s)",
                name_lower,
                parsed["server"],
            )
        except Exception:
            logger.exception("Failed to parse kubeconfig for Babylon cluster '%s'", name_lower)

    logger.info("Babylon: %d clusters configured", len(_cluster_configs))


async def _get_client(cluster_name: str) -> httpx.AsyncClient:
    """Get or create an httpx client for a Babylon cluster."""
    if cluster_name in _clients:
        return _clients[cluster_name]

    if cluster_name not in _cluster_configs:
        raise ValueError(
            f"Unknown Babylon cluster: '{cluster_name}'. "
            f"Configured: {list(_cluster_configs.keys())}"
        )

    cluster_cfg = _cluster_configs[cluster_name]
    verify = _build_ssl_context(cluster_cfg)

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
    """Return list of configured Babylon cluster names."""
    return list(_cluster_configs.keys())


def resolve_cluster_from_comment(comment: str) -> str:
    """Resolve a Babylon cluster name from a sandbox DynamoDB comment field.

    The comment field contains the Babylon cluster console URL, e.g.:
      "sandbox-api https://console-openshift-console.apps.ocp-us-east-1.infra.open.redhat.com"

    Extracts the API hostname from the URL and matches it against configured
    cluster server URLs. Returns empty string if no match is found.
    """
    if not comment:
        return ""

    # Extract hostname from the comment URL
    # Format: "sandbox-api https://console-openshift-console.apps.<cluster-domain>"
    # The cluster API server is at "https://api.<cluster-domain>:6443"
    import re

    url_match = re.search(r"https?://console-openshift-console\.apps\.(.+?)(?:\s|$)", comment)
    if not url_match:
        return ""

    cluster_domain = url_match.group(1).rstrip("/").lower()

    # Match against configured cluster server URLs
    for name, cfg in _cluster_configs.items():
        server = cfg.get("server", "").lower()
        # Server is like "https://api.ocp-us-east-1.infra.open.redhat.com:6443"
        # Cluster domain is like "ocp-us-east-1.infra.open.redhat.com"
        if cluster_domain in server:
            return name

    logger.warning(
        "No Babylon cluster configured for domain '%s' (from comment: %s). "
        "Configured servers: %s",
        cluster_domain,
        comment[:100],
        [cfg["server"] for cfg in _cluster_configs.values()],
    )
    return ""


async def k8s_get(cluster_name: str, path: str) -> dict:
    """Make a GET request to the Kubernetes API."""
    client = await _get_client(cluster_name)
    resp = await client.get(path)
    resp.raise_for_status()
    return resp.json()


async def k8s_get_text(cluster_name: str, path: str, params: dict | None = None) -> str:
    """Make a GET request to the Kubernetes API and return raw text (for pod logs)."""
    client = await _get_client(cluster_name)
    resp = await client.get(path, params=params or {})
    resp.raise_for_status()
    return resp.text


async def k8s_list(
    cluster_name: str,
    group: str,
    version: str,
    plural: str,
    namespace: str,
    label_selector: str = "",
    limit: int = 0,
) -> dict:
    """List custom resources from the Kubernetes API."""
    if group:
        path = f"/apis/{group}/{version}/namespaces/{namespace}/{plural}"
    else:
        path = f"/api/{version}/namespaces/{namespace}/{plural}"

    params: dict[str, str | int] = {}
    if label_selector:
        params["labelSelector"] = label_selector
    if limit:
        params["limit"] = limit

    client = await _get_client(cluster_name)
    resp = await client.get(path, params=params)
    resp.raise_for_status()
    return resp.json()


async def k8s_get_resource(
    cluster_name: str,
    group: str,
    version: str,
    plural: str,
    namespace: str,
    name: str,
) -> dict:
    """Get a single custom resource by name."""
    if group:
        path = f"/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}"
    else:
        path = f"/api/{version}/namespaces/{namespace}/{plural}/{name}"

    client = await _get_client(cluster_name)
    resp = await client.get(path)
    resp.raise_for_status()
    return resp.json()


async def k8s_list_cluster_wide(
    cluster_name: str,
    group: str,
    version: str,
    plural: str,
    label_selector: str = "",
    limit: int = 0,
) -> dict:
    """List custom resources across all namespaces (cluster-wide)."""
    path = f"/apis/{group}/{version}/{plural}"

    params: dict[str, str | int] = {}
    if label_selector:
        params["labelSelector"] = label_selector
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
