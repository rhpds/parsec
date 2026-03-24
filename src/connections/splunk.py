"""Splunk Cloud connection client — async httpx-based."""

import asyncio
import logging
import time
from typing import Any

import httpx

from src.config import get_config

logger = logging.getLogger(__name__)

_client: "SplunkClient | None" = None

# SplunkWeb proxy base path (port 443, bypasses firewalled 8089)
_SPLUNKWEB_API_PREFIX = "/en-US/splunkd/__raw"

# Max results per search
MAX_RESULTS = 500
POLL_INTERVAL = 2
SEARCH_TIMEOUT = 300  # seconds


class SplunkClient:
    """Async Splunk REST API client via SplunkWeb proxy."""

    def __init__(
        self,
        host: str,
        auth_method: str = "token",
        token: str = "",
        username: str = "",
        password: str = "",
        session_cookie: str = "",
        verify_ssl: bool = True,
    ):
        self.host = host.rstrip("/")
        self.auth_method = auth_method
        self.token = token
        self.username = username
        self.password = password
        self.session_cookie = session_cookie
        self.verify_ssl = verify_ssl
        self._http_client: httpx.AsyncClient | None = None

    def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create the persistent httpx client."""
        if self._http_client is None or self._http_client.is_closed:
            headers: dict[str, str] = {}
            cookies: dict[str, str] = {}
            auth: tuple[str, str] | None = None

            if self.auth_method == "token" and self.token:
                headers["Authorization"] = f"Splunk {self.token}"
            elif self.auth_method == "basic" and self.username and self.password:
                auth = (self.username, self.password)
            elif self.auth_method == "cookie" and self.session_cookie:
                cookies["splunkd_8443"] = self.session_cookie
                # SplunkWeb CSRF protection — X-Requested-With is sufficient
                headers["X-Requested-With"] = "XMLHttpRequest"

            self._http_client = httpx.AsyncClient(
                verify=self.verify_ssl,
                timeout=30.0,
                headers=headers,
                cookies=cookies,
                auth=auth,
            )
        return self._http_client

    def _api_url(self, endpoint: str) -> str:
        """Build full URL for Splunk API endpoint.

        Token/basic auth: hits management API on port 8089 (works from OpenShift/VPN).
        Cookie auth: goes through SplunkWeb proxy on port 443 (works externally).
        """
        if self.auth_method in ("token", "basic"):
            return f"{self.host}:8089/services{endpoint}"
        return f"{self.host}{_SPLUNKWEB_API_PREFIX}/services{endpoint}"

    async def _get(self, endpoint: str, params: dict[str, str] | None = None) -> httpx.Response:
        """Make a GET request to Splunk API."""
        url = self._api_url(endpoint)
        if params is None:
            params = {}
        params["output_mode"] = "json"
        client = self._get_http_client()
        return await client.get(url, params=params)

    async def _post(self, endpoint: str, data: dict[str, str]) -> httpx.Response:
        """Make a POST request to Splunk API."""
        url = self._api_url(endpoint)
        data["output_mode"] = "json"
        client = self._get_http_client()
        return await client.post(url, data=data)

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def test_connection(self) -> dict[str, Any]:
        """Test connectivity by fetching server info."""
        try:
            resp = await self._get("/server/info")
            resp.raise_for_status()
            data = resp.json()
            entry = data.get("entry", [{}])[0]
            content = entry.get("content", {})
            return {
                "status": "ok",
                "version": content.get("version", "unknown"),
                "server": content.get("serverName", "unknown"),
                "instance_type": content.get("instance_type", "unknown"),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def create_search_job(
        self,
        query: str,
        earliest: str = "-24h",
        latest: str = "now",
    ) -> str:
        """Create a Splunk search job and return the SID."""
        stripped = query.strip()
        if not stripped.startswith("search ") and not stripped.startswith("|"):
            query = f"search {stripped}"

        data = {
            "search": query,
            "earliest_time": earliest,
            "latest_time": latest,
        }

        resp = await self._post("/search/jobs", data)
        resp.raise_for_status()
        result = resp.json()
        return result.get("sid", "")

    async def wait_for_job(self, sid: str, timeout: int = SEARCH_TIMEOUT) -> dict[str, Any]:
        """Poll until a search job completes."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            resp = await self._get(f"/search/jobs/{sid}")
            resp.raise_for_status()
            result = resp.json()

            if "entry" in result and result["entry"]:
                content = result["entry"][0].get("content", {})
                state = content.get("dispatchState", "")

                if state == "DONE":
                    return {
                        "status": "done",
                        "result_count": content.get("resultCount", 0),
                        "scan_count": content.get("scanCount", 0),
                    }
                elif state == "FAILED":
                    messages = content.get("messages", [])
                    return {"status": "failed", "error": str(messages)}

            await asyncio.sleep(POLL_INTERVAL)

        elapsed = round(time.monotonic() - start, 1)
        return {"status": "timeout", "elapsed": elapsed}

    async def get_results(self, sid: str, count: int = MAX_RESULTS) -> list[dict[str, Any]]:
        """Fetch results from a completed search job."""
        resp = await self._get(
            f"/search/jobs/{sid}/results",
            params={"count": str(count)},
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("results", [])

    async def run_search(
        self,
        query: str,
        earliest: str = "-24h",
        latest: str = "now",
        max_results: int = 200,
    ) -> dict[str, Any]:
        """Run a search end-to-end: create job, wait, fetch results."""
        try:
            sid = await self.create_search_job(query, earliest, latest)
            if not sid:
                return {"error": "Failed to create search job (empty SID)"}

            job_status = await self.wait_for_job(sid)
            if job_status["status"] != "done":
                return {"error": f"Search job {job_status['status']}: {job_status}"}

            results = await self.get_results(sid, count=max_results)
            return {
                "results": results,
                "result_count": len(results),
                "total_count": job_status.get("result_count", 0),
                "truncated": len(results) < job_status.get("result_count", 0),
            }
        except httpx.HTTPStatusError as e:
            return {"error": f"Splunk API error: {e.response.status_code} {e.response.text[:200]}"}
        except Exception as e:
            return {"error": f"Splunk query failed: {e}"}


def init_splunk() -> None:
    """Initialize the Splunk client from config."""
    global _client
    cfg = get_config()
    splunk_cfg = cfg.get("splunk", {})

    host = splunk_cfg.get("host", "")
    if not host:
        logger.info("Splunk not configured (no host) — skipping init")
        return

    token = splunk_cfg.get("token", "")
    username = splunk_cfg.get("username", "")
    password = splunk_cfg.get("password", "")  # noqa: S105
    session_cookie = splunk_cfg.get("session_cookie", "")
    verify_ssl = splunk_cfg.get("verify_ssl", True)

    if token:
        auth_method = "token"
        logger.info("Splunk client initialized (token auth, host=%s)", host)
    elif username and password:
        auth_method = "basic"
        logger.info("Splunk client initialized (basic auth, user=%s, host=%s)", username, host)
    elif session_cookie:
        auth_method = "cookie"
        logger.info("Splunk client initialized (cookie auth, host=%s)", host)
    else:
        logger.warning(
            "Splunk configured but no auth (token, username/password, or session_cookie) "
            "— skipping init"
        )
        return

    _client = SplunkClient(
        host=host,
        auth_method=auth_method,
        token=token,
        username=username,
        password=password,
        session_cookie=session_cookie,
        verify_ssl=verify_ssl,
    )


def get_splunk_client() -> SplunkClient:
    """Get the initialized Splunk client."""
    if _client is None:
        raise RuntimeError("Splunk not initialized — check splunk config")
    return _client
