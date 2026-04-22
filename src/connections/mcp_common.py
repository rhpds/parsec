"""Shared MCP session context manager for streamable HTTP connections."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx

_DEFAULT_TIMEOUT_SECONDS = 30


@asynccontextmanager
async def mcp_session(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> AsyncGenerator[tuple[Any, Any], None]:
    """Open an authenticated MCP session with proper resource cleanup.

    Yields (session, init_result).
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx.AsyncClient(headers=headers or {}, timeout=timeout) as client,
        streamable_http_client(url=url, http_client=client) as (
            read_stream,
            write_stream,
            _,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        init_result = await session.initialize()
        yield session, init_result
