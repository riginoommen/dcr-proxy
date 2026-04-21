"""Main proxy orchestrator.

Wires the stdio handler, OAuth manager, and HTTP client together into a
single request-processing loop with graceful startup and shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Optional

from .config import ProxyConfig
from .http_client import McpHttpClient
from .oauth import OAuthManager
from .stdio_handler import StdioHandler

logger = logging.getLogger(__name__)


class McpProxy:
    """Stdio-to-HTTP MCP proxy with OAuth authentication."""

    def __init__(self, config: ProxyConfig) -> None:
        self._config = config
        self._oauth = OAuthManager(
            issuer=config.oauth_issuer,
            client_id=config.client_id,
            scopes=config.scopes,
            client_secret=config.client_secret,
            redirect_port=config.redirect_port,
            token_cache_path=config.token_cache_path,
        )
        self._http_client = McpHttpClient(
            server_url=config.mcp_server_url,
            get_token=self._oauth.get_access_token,
        )
        self._stdio = StdioHandler()
        self._shutdown_event = asyncio.Event()
        self._sse_task: Optional[asyncio.Task] = None

    async def run(self) -> None:
        """Start all components and enter the main request loop."""
        _setup_logging(self._config.log_level)
        self._install_signal_handlers()

        logger.info("Starting MCP OAuth proxy...")
        logger.info("  MCP server:   %s", self._config.mcp_server_url)
        logger.info("  OAuth issuer:  %s", self._config.oauth_issuer)
        logger.info("  Client ID:    %s", self._config.client_id)

        try:
            await self._oauth.start()
            await self._http_client.start()
            await self._stdio.start()

            _log_stderr("MCP OAuth proxy ready. Authenticating...\n")
            await self._oauth.get_access_token()
            _log_stderr("Authentication complete. Proxy is active.\n")

            self._sse_task = asyncio.create_task(self._sse_listener())

            await self._request_loop()
        except KeyboardInterrupt:
            pass
        except Exception:
            logger.error("Fatal error in proxy", exc_info=True)
        finally:
            await self._shutdown()

    async def _request_loop(self) -> None:
        """Read JSON-RPC from stdin and forward to the HTTP MCP server."""
        async for message in self._stdio.read_messages():
            if self._shutdown_event.is_set():
                break

            method = message.get("method", "")
            req_id = message.get("id")
            logger.debug("Processing %s (id=%s)", method, req_id)

            try:
                responses = await self._http_client.send(message)
                for resp in responses:
                    await self._stdio.write_message(resp)
            except Exception as exc:
                logger.error("Error forwarding request: %s", exc, exc_info=True)
                if req_id is not None:
                    await self._stdio.write_error(req_id, -32603, str(exc))

    async def _sse_listener(self) -> None:
        """Listen for server-initiated messages via SSE (if supported)."""
        try:
            stream = await self._http_client.initialize_sse_stream()
            if stream is None:
                logger.debug("SSE stream not available, skipping")
                return
            async for msg in stream:
                await self._stdio.write_message(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("SSE listener ended", exc_info=True)

    async def _shutdown(self) -> None:
        logger.info("Shutting down...")
        self._shutdown_event.set()
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
        await self._http_client.close()
        await self._oauth.close()
        logger.info("Shutdown complete")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
            except NotImplementedError:
                pass


def _setup_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _log_stderr(msg: str) -> None:
    sys.stderr.write(msg)
    sys.stderr.flush()
