"""HTTP client for MCP streamable HTTP transport with OAuth Bearer auth.

Forwards JSON-RPC messages to the remote MCP server.  Supports both
single-response and SSE-streamed responses.  Automatically retries once
on 401 after refreshing the access token.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Callable, Coroutine, Optional

import aiohttp

logger = logging.getLogger(__name__)

_SSE_CONTENT_TYPE = "text/event-stream"


class McpHttpClient:
    """HTTP transport layer that speaks MCP streamable HTTP to a remote server."""

    def __init__(
        self,
        server_url: str,
        get_token: Callable[[], Coroutine[Any, Any, str]],
    ) -> None:
        """
        Args:
            server_url: Base URL of the HTTP streamable MCP server.
            get_token: Async callable returning a current Bearer token.
        """
        self._server_url = server_url.rstrip("/")
        self._get_token = get_token
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_id: Optional[str] = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Send a JSON-RPC message and return the response(s).

        Returns a list because SSE streams can yield multiple messages
        (e.g. progress notifications followed by the final result).
        """
        responses = await self._do_send(message)
        if responses is None:
            logger.info("Retrying after token refresh...")
            responses = await self._do_send(message)
        return responses or []

    async def initialize_sse_stream(
        self,
    ) -> Optional[AsyncIterator[dict[str, Any]]]:
        """Open a persistent GET-based SSE stream for server-initiated messages."""
        assert self._session is not None
        token = await self._get_token()
        headers = self._build_headers(token)
        headers["Accept"] = _SSE_CONTENT_TYPE

        resp = await self._session.get(self._server_url, headers=headers)
        if resp.status != 200:
            logger.warning("SSE GET returned %d, skipping", resp.status)
            return None

        if self._session_id is None:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid

        return _parse_sse_stream(resp)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _do_send(
        self, message: dict[str, Any]
    ) -> Optional[list[dict[str, Any]]]:
        assert self._session is not None
        token = await self._get_token()
        headers = self._build_headers(token)
        headers["Accept"] = f"application/json, {_SSE_CONTENT_TYPE}"

        body = json.dumps(message, separators=(",", ":"))
        logger.debug("HTTP POST %s  body=%s", self._server_url, _trunc(body))

        async with self._session.post(
            self._server_url,
            data=body,
            headers=headers,
        ) as resp:
            if resp.status == 401:
                logger.warning("401 from MCP server, refreshing token")
                return None

            if resp.status == 202:
                return []

            if resp.status != 200:
                text = await resp.text()
                logger.error(
                    "MCP server returned %d: %s", resp.status, _trunc(text)
                )
                return [_make_rpc_error(message.get("id"), -32000, f"HTTP {resp.status}")]

            if self._session_id is None:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                    logger.debug("Captured Mcp-Session-Id: %s", sid)

            content_type = resp.content_type or ""
            if _SSE_CONTENT_TYPE in content_type:
                return await _collect_sse(resp)
            else:
                data = await resp.json()
                if isinstance(data, list):
                    return data
                return [data]

    def _build_headers(self, token: str) -> dict[str, str]:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers


async def _collect_sse(resp: aiohttp.ClientResponse) -> list[dict[str, Any]]:
    """Read an entire SSE response and collect all JSON-RPC messages."""
    results: list[dict[str, Any]] = []
    async for msg in _parse_sse_stream(resp):
        results.append(msg)
    return results


async def _parse_sse_stream(
    resp: aiohttp.ClientResponse,
) -> AsyncIterator[dict[str, Any]]:
    """Parse an SSE stream yielding JSON-RPC messages from 'data:' lines."""
    buffer = ""
    async for raw_line in resp.content:
        line = raw_line.decode("utf-8", errors="replace")
        if line.startswith("data: "):
            buffer += line[6:]
        elif line.strip() == "" and buffer:
            try:
                parsed = json.loads(buffer)
                if isinstance(parsed, list):
                    for item in parsed:
                        yield item
                else:
                    yield parsed
            except json.JSONDecodeError:
                logger.warning("Invalid JSON in SSE data: %s", _trunc(buffer))
            buffer = ""


def _make_rpc_error(
    req_id: Any, code: int, message: str
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _trunc(text: str, max_len: int = 200) -> str:
    return text if len(text) <= max_len else text[:max_len] + "..."
