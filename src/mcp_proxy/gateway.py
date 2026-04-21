"""MCP DCR Proxy -- HTTP gateway with OAuth authorization server.

Acts as an MCP-spec-compliant OAuth authorization server with DCR (RFC 7591)
to MCP clients, while being a standard OAuth client to the real SSO.

MCP clients (Cursor, Claude Desktop, MCP Inspector) connect to /mcp with
Bearer tokens. The gateway proxies requests to backend MCP servers using
real SSO tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Optional

from aiohttp import web

from .client_registry import ClientRegistry
from .config import GatewayConfig
from .oauth_server import register_oauth_routes
from .session import SessionManager
from .token_store import TokenStore

logger = logging.getLogger(__name__)


class McpGateway:
    """MCP DCR proxy gateway with per-user OAuth sessions."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._session_mgr = SessionManager(
            issuer=config.oauth_issuer,
            client_id=config.client_id,
            scopes=config.scopes,
            client_secret=config.client_secret,
            session_ttl_minutes=config.session_ttl_minutes,
        )
        self._client_registry = ClientRegistry()
        self._token_store = TokenStore()

    async def run(self) -> None:
        _setup_logging(self._config.log_level)
        logger.info("Starting MCP DCR Proxy...")
        logger.info("  Listen:        %s:%d", self._config.host, self._config.port)
        logger.info("  OAuth issuer:  %s", self._config.oauth_issuer)
        logger.info("  Client ID:     %s", self._config.client_id)
        if self._config.default_target:
            logger.info("  Default target: %s", self._config.default_target)

        await self._session_mgr.start()
        self._token_store.start_cleanup()

        app = self._build_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._config.host, self._config.port)
        await site.start()

        url = f"http://{self._config.host}:{self._config.port}"
        logger.info("DCR Proxy listening on %s", url)
        print(f"MCP DCR Proxy running on {url}", file=sys.stderr)
        print(f"  MCP endpoint:  {url}/mcp", file=sys.stderr)
        print(f"  DCR register:  POST {url}/oauth/register", file=sys.stderr)
        print(f"  OAuth metadata: {url}/.well-known/oauth-authorization-server", file=sys.stderr)
        print(f"  Health:        {url}/health", file=sys.stderr)

        try:
            while True:
                await asyncio.sleep(3600)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await self._token_store.stop()
            await self._session_mgr.close()
            await runner.cleanup()
            logger.info("Gateway shut down")

    def _build_app(self) -> web.Application:
        app = web.Application()

        gateway_issuer = f"http://{self._config.host}:{self._config.port}"
        register_oauth_routes(
            app,
            session_mgr=self._session_mgr,
            client_registry=self._client_registry,
            token_store=self._token_store,
            gateway_issuer=gateway_issuer,
            scopes=self._config.scopes,
        )

        app.router.add_post("/mcp", self._handle_mcp_post)
        app.router.add_get("/mcp", self._handle_mcp_get)
        app.router.add_get("/health", self._handle_health)
        return app

    # ------------------------------------------------------------------
    # MCP proxy routes (Bearer token auth)
    # ------------------------------------------------------------------

    async def _handle_mcp_post(self, request: web.Request) -> web.Response:
        session_id = self._extract_session(request)
        if session_id is None:
            return self._unauthorized_response(request)

        target = self._resolve_target(request)
        if target is None:
            return web.json_response(
                {"error": "missing 'target' query parameter and no defaultTarget configured"},
                status=400,
            )

        if not self._is_target_allowed(target):
            return web.json_response(
                {"error": f"target not in allowed list: {target}"},
                status=403,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        try:
            await self._session_mgr.get_token(session_id)
        except (KeyError, RuntimeError):
            return self._unauthorized_response(request)

        async def _get_token() -> str:
            return await self._session_mgr.get_token(session_id)

        client = await self._session_mgr.get_or_create_client(
            session_id, target, _get_token
        )
        try:
            responses = await client.send(body)
            if len(responses) == 1:
                return web.json_response(responses[0])
            return web.json_response(responses)
        except Exception as exc:
            logger.error("Error proxying to %s: %s", target, exc, exc_info=True)
            return web.json_response(
                {"jsonrpc": "2.0", "id": body.get("id"),
                 "error": {"code": -32603, "message": str(exc)}},
                status=502,
            )

    async def _handle_mcp_get(self, request: web.Request) -> web.StreamResponse:
        session_id = self._extract_session(request)
        if session_id is None:
            return self._unauthorized_response(request)

        target = self._resolve_target(request)
        if target is None:
            return web.json_response(
                {"error": "missing 'target' query parameter"}, status=400
            )

        if not self._is_target_allowed(target):
            return web.json_response(
                {"error": f"target not in allowed list: {target}"}, status=403
            )

        async def _get_token() -> str:
            return await self._session_mgr.get_token(session_id)

        try:
            client = await self._session_mgr.get_or_create_client(
                session_id, target, _get_token
            )
        except (KeyError, RuntimeError):
            return self._unauthorized_response(request)

        stream = await client.initialize_sse_stream()
        if stream is None:
            return web.json_response(
                {"error": "upstream does not support SSE"}, status=502
            )

        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream",
                     "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)

        async for msg in stream:
            data = json.dumps(msg, separators=(",", ":"))
            await resp.write(f"data: {data}\n\n".encode("utf-8"))

        return resp

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_session(self, request: web.Request) -> Optional[str]:
        """Extract session_id from Bearer token."""
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            gateway_token = auth_header[7:]
            return self._token_store.get_session_id(gateway_token)
        return None

    def _unauthorized_response(self, request: web.Request) -> web.Response:
        base = self._base_url(request)
        resource_metadata_url = f"{base}/.well-known/oauth-protected-resource"
        return web.Response(
            status=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer resource_metadata="{resource_metadata_url}"'
                ),
            },
            content_type="application/json",
            text=json.dumps({"error": "unauthorized"}),
        )

    def _resolve_target(self, request: web.Request) -> Optional[str]:
        target = request.query.get("target")
        if target:
            return target
        return self._config.default_target

    def _base_url(self, request: web.Request) -> str:
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host)
        return f"{scheme}://{host}"

    def _is_target_allowed(self, target: str) -> bool:
        if self._config.allowed_targets is None:
            return True
        return any(target.startswith(t) for t in self._config.allowed_targets)


def _setup_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
