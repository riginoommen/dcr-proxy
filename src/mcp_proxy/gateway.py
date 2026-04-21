"""HTTP gateway server for the MCP proxy.

Exposes an aiohttp web application that:
  - Authenticates users via OAuth Auth Code + PKCE (/auth/login, /auth/callback)
  - Proxies MCP JSON-RPC to dynamically-specified backend servers (POST /mcp)
  - Passes through SSE streams from backends (GET /mcp)
  - Provides a health endpoint (GET /health)
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Optional

from aiohttp import web

from .config import GatewayConfig
from .session import SessionManager

logger = logging.getLogger(__name__)

SESSION_COOKIE = "mcp_session"


class McpGateway:
    """Multi-client MCP HTTP gateway with per-user OAuth sessions."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._session_mgr = SessionManager(
            issuer=config.oauth_issuer,
            client_id=config.client_id,
            scopes=config.scopes,
            client_secret=config.client_secret,
            session_ttl_minutes=config.session_ttl_minutes,
        )

    async def run(self) -> None:
        _setup_logging(self._config.log_level)
        logger.info("Starting MCP HTTP gateway...")
        logger.info("  Listen:       %s:%d", self._config.host, self._config.port)
        logger.info("  OAuth issuer:  %s", self._config.oauth_issuer)
        logger.info("  Client ID:    %s", self._config.client_id)

        await self._session_mgr.start()

        app = self._build_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._config.host, self._config.port)
        await site.start()

        url = f"http://{self._config.host}:{self._config.port}"
        logger.info("Gateway listening on %s", url)
        print(f"MCP Gateway running on {url}", file=sys.stderr)
        print(f"  Login:  {url}/auth/login", file=sys.stderr)
        print(f"  Health: {url}/health", file=sys.stderr)
        print(f"  MCP:    POST {url}/mcp?target=<mcp-server-url>", file=sys.stderr)

        try:
            while True:
                import asyncio
                await asyncio.sleep(3600)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await self._session_mgr.close()
            await runner.cleanup()
            logger.info("Gateway shut down")

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/auth/login", self._handle_login)
        app.router.add_get("/auth/callback", self._handle_callback)
        app.router.add_post("/mcp", self._handle_mcp_post)
        app.router.add_get("/mcp", self._handle_mcp_get)
        app.router.add_get("/health", self._handle_health)
        return app

    # ------------------------------------------------------------------
    # Auth routes
    # ------------------------------------------------------------------

    async def _handle_login(self, request: web.Request) -> web.Response:
        callback_url = self._callback_url(request)
        session_id, auth_url = self._session_mgr.create_login_session(callback_url)

        resp = web.HTTPFound(auth_url)
        resp.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="Lax")
        return resp

    async def _handle_callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state")
        error = request.query.get("error")

        if error:
            desc = request.query.get("error_description", error)
            return web.Response(
                text=f"Authentication failed: {desc}",
                status=400,
                content_type="text/plain",
            )

        if not code or not state:
            return web.Response(
                text="Missing code or state parameter",
                status=400,
                content_type="text/plain",
            )

        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return web.Response(
                text="No session cookie. Please start login again at /auth/login",
                status=400,
                content_type="text/plain",
            )

        ok = await self._session_mgr.complete_auth(session_id, code, state)
        if not ok:
            return web.Response(
                text="Authentication failed: invalid session or state mismatch. "
                     "Please try /auth/login again.",
                status=400,
                content_type="text/plain",
            )

        return web.Response(
            text="Authentication successful! You can close this tab and use the MCP gateway.",
            content_type="text/plain",
        )

    # ------------------------------------------------------------------
    # MCP proxy routes
    # ------------------------------------------------------------------

    async def _handle_mcp_post(self, request: web.Request) -> web.Response:
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id or not self._session_mgr.is_authenticated(session_id):
            login_url = f"{self._base_url(request)}/auth/login"
            return web.json_response(
                {"error": "unauthorized", "loginUrl": login_url},
                status=401,
            )

        target = request.query.get("target")
        if not target:
            return web.json_response(
                {"error": "missing 'target' query parameter"},
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
            return web.json_response(
                {"error": "invalid JSON body"},
                status=400,
            )

        try:
            token = await self._session_mgr.get_token(session_id)
        except RuntimeError:
            login_url = f"{self._base_url(request)}/auth/login"
            return web.json_response(
                {"error": "session expired, please re-authenticate",
                 "loginUrl": login_url},
                status=401,
            )

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
        """SSE passthrough for server-initiated messages."""
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id or not self._session_mgr.is_authenticated(session_id):
            return web.json_response(
                {"error": "unauthorized"}, status=401
            )

        target = request.query.get("target")
        if not target:
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
            return web.json_response({"error": "session expired"}, status=401)

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

        import json as _json
        async for msg in stream:
            data = _json.dumps(msg, separators=(",", ":"))
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

    def _callback_url(self, request: web.Request) -> str:
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host)
        return f"{scheme}://{host}/auth/callback"

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
