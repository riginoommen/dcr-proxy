"""MCP-spec-compliant OAuth authorization server endpoints.

Implements:
  - Protected Resource Metadata (RFC 9728)
  - Authorization Server Metadata (RFC 8414)
  - Dynamic Client Registration (RFC 7591)
  - Authorization Code + PKCE (OAuth 2.1)
  - Token endpoint

The gateway acts as an OAuth authorization server to MCP clients while
being an OAuth client to the real SSO (Red Hat SSO / Keycloak).
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode, parse_qs, urlparse

from aiohttp import web

from .client_registry import ClientRegistry
from .session import SessionManager
from .token_store import TokenStore

logger = logging.getLogger(__name__)

# Stored per in-flight authorization request so we can map the SSO callback
# back to the original MCP client's authorize request.
_pending_authorizations: dict[str, dict] = {}


def register_oauth_routes(
    app: web.Application,
    *,
    session_mgr: SessionManager,
    client_registry: ClientRegistry,
    token_store: TokenStore,
    gateway_issuer: str,
    scopes: list[str],
) -> None:
    """Register all OAuth/discovery routes on the aiohttp application."""

    ctx = _OAuthContext(
        session_mgr=session_mgr,
        client_registry=client_registry,
        token_store=token_store,
        gateway_issuer=gateway_issuer,
        scopes=scopes,
    )

    app.router.add_get(
        "/.well-known/oauth-protected-resource",
        ctx.handle_protected_resource_metadata,
    )
    app.router.add_get(
        "/.well-known/oauth-authorization-server",
        ctx.handle_authorization_server_metadata,
    )
    app.router.add_post("/oauth/register", ctx.handle_register)
    app.router.add_get("/oauth/authorize", ctx.handle_authorize)
    app.router.add_get("/auth/callback", ctx.handle_callback)
    app.router.add_post("/oauth/token", ctx.handle_token)


class _OAuthContext:
    """Holds shared state for all OAuth route handlers."""

    def __init__(
        self,
        *,
        session_mgr: SessionManager,
        client_registry: ClientRegistry,
        token_store: TokenStore,
        gateway_issuer: str,
        scopes: list[str],
    ) -> None:
        self._session_mgr = session_mgr
        self._clients = client_registry
        self._tokens = token_store
        self._issuer = gateway_issuer
        self._scopes = scopes

    # ------------------------------------------------------------------
    # Discovery endpoints
    # ------------------------------------------------------------------

    async def handle_protected_resource_metadata(
        self, request: web.Request
    ) -> web.Response:
        """RFC 9728 -- tells MCP clients where the authorization server is."""
        base = self._base_url(request)
        return web.json_response({
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "scopes_supported": self._scopes,
            "bearer_methods_supported": ["header"],
        })

    async def handle_authorization_server_metadata(
        self, request: web.Request
    ) -> web.Response:
        """RFC 8414 -- OAuth authorization server metadata."""
        base = self._base_url(request)
        return web.json_response({
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "scopes_supported": self._scopes,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "none",
            ],
            "code_challenge_methods_supported": ["S256"],
            "service_documentation": f"{base}/health",
        })

    # ------------------------------------------------------------------
    # Dynamic Client Registration (RFC 7591)
    # ------------------------------------------------------------------

    async def handle_register(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            raw = await request.text()
            logger.warning("DCR: invalid JSON body: %s", raw[:500])
            return web.json_response(
                {"error": "invalid_client_metadata", "error_description": "Invalid JSON body"},
                status=400,
            )

        logger.info("DCR register request: %s", {k: v for k, v in body.items()})

        try:
            client = self._clients.register(body)
        except ValueError as exc:
            logger.warning("DCR registration failed: %s", exc)
            return web.json_response(
                {"error": "invalid_client_metadata", "error_description": str(exc)},
                status=400,
            )

        return web.json_response(
            self._clients.to_registration_response(client),
            status=201,
        )

    # ------------------------------------------------------------------
    # Authorization endpoint
    # ------------------------------------------------------------------

    async def handle_authorize(self, request: web.Request) -> web.Response:
        """OAuth authorize endpoint.

        Validates the MCP client's request, then proxies to real SSO.
        On SSO callback, issues a gateway auth code and redirects back
        to the MCP client.
        """
        client_id = request.query.get("client_id")
        redirect_uri = request.query.get("redirect_uri")
        response_type = request.query.get("response_type")
        state = request.query.get("state")
        code_challenge = request.query.get("code_challenge")
        code_challenge_method = request.query.get("code_challenge_method", "S256")

        if response_type != "code":
            return self._auth_error("unsupported_response_type", "Only 'code' is supported")

        if not client_id or not redirect_uri:
            return self._auth_error("invalid_request", "client_id and redirect_uri are required")

        client = self._clients.get(client_id)
        if client is None:
            if redirect_uri and redirect_uri.startswith(("http://127.0.0.1", "http://localhost")):
                client = self._clients.register({
                    "client_name": f"auto:{client_id[:32]}",
                    "redirect_uris": [redirect_uri],
                })
                logger.info("Auto-registered client at authorize for client_id=%s", client_id[:16])
                _pending_authorizations["_client_alias:" + client_id] = client.client_id
                client_id = client.client_id
            else:
                return self._auth_error("invalid_client", "Unknown client_id; register via /oauth/register first")

        if not self._clients.validate_redirect_uri(client_id, redirect_uri):
            return self._auth_error("invalid_request", "redirect_uri not registered")

        if not code_challenge:
            return self._auth_error("invalid_request", "code_challenge is required (PKCE)")

        base = self._base_url(request)
        sso_callback = f"{base}/auth/callback"

        session_id, sso_auth_url = self._session_mgr.create_login_session(sso_callback)

        _pending_authorizations[session_id] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        }

        logger.info(
            "Authorize: client=%s -> SSO login (session %s)",
            client_id[:8], session_id[:8],
        )
        resp = web.HTTPFound(sso_auth_url)
        resp.set_cookie("_dcr_session", session_id, httponly=True, samesite="Lax")
        return resp

    # ------------------------------------------------------------------
    # SSO callback (internal)
    # ------------------------------------------------------------------

    async def handle_callback(self, request: web.Request) -> web.Response:
        """Receives the callback from real SSO after user authenticates.

        Exchanges the SSO code for SSO tokens, then issues a gateway auth
        code and redirects back to the MCP client's redirect_uri.
        """
        error = request.query.get("error")
        if error:
            desc = request.query.get("error_description", error)
            return web.Response(text=f"SSO authentication failed: {desc}", status=400)

        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            return web.Response(text="Missing code or state from SSO", status=400)

        session_id = request.cookies.get("_dcr_session")
        if not session_id:
            return web.Response(text="Missing session cookie", status=400)

        ok = await self._session_mgr.complete_auth(session_id, code, state)
        if not ok:
            return web.Response(text="SSO authentication failed (state mismatch)", status=400)

        pending = _pending_authorizations.pop(session_id, None)
        if pending is None:
            return web.Response(text="No pending authorization for this session", status=400)

        gateway_code = self._tokens.create_auth_code(
            session_id=session_id,
            client_id=pending["client_id"],
            redirect_uri=pending["redirect_uri"],
            code_challenge=pending["code_challenge"],
            code_challenge_method=pending["code_challenge_method"],
        )

        params = {"code": gateway_code}
        if pending["state"]:
            params["state"] = pending["state"]

        sep = "&" if "?" in pending["redirect_uri"] else "?"
        redirect_url = f"{pending['redirect_uri']}{sep}{urlencode(params)}"

        logger.info(
            "SSO auth complete, redirecting to client with gateway code (session %s)",
            session_id[:8],
        )
        return web.HTTPFound(redirect_url)

    # ------------------------------------------------------------------
    # Token endpoint
    # ------------------------------------------------------------------

    async def handle_token(self, request: web.Request) -> web.Response:
        """Exchange a gateway auth code or refresh token for tokens."""
        try:
            body = await request.post()
        except Exception:
            return web.json_response(
                {"error": "invalid_request", "error_description": "Cannot parse body"},
                status=400,
            )

        grant_type = body.get("grant_type")

        if grant_type == "authorization_code":
            return await self._handle_authorization_code_grant(body)
        elif grant_type == "refresh_token":
            return await self._handle_refresh_token_grant(body)
        else:
            return web.json_response(
                {"error": "unsupported_grant_type",
                 "error_description": "Supported: authorization_code, refresh_token"},
                status=400,
            )

    async def _handle_authorization_code_grant(self, body) -> web.Response:
        code = body.get("code")
        code_verifier = body.get("code_verifier")
        client_id = body.get("client_id")
        redirect_uri = body.get("redirect_uri")

        if not all([code, code_verifier, client_id, redirect_uri]):
            return web.json_response(
                {"error": "invalid_request",
                 "error_description": "code, code_verifier, client_id, redirect_uri are all required"},
                status=400,
            )

        try:
            token_entry = self._tokens.exchange_code(
                code=code,
                code_verifier=code_verifier,
                client_id=client_id,
                redirect_uri=redirect_uri,
            )
        except ValueError as exc:
            return web.json_response(
                {"error": "invalid_grant", "error_description": str(exc)},
                status=400,
            )

        logger.info("Issued gateway tokens for client %s", client_id[:8])
        return self._token_response(token_entry)

    async def _handle_refresh_token_grant(self, body) -> web.Response:
        refresh_token = body.get("refresh_token")
        client_id = body.get("client_id")

        if not refresh_token or not client_id:
            return web.json_response(
                {"error": "invalid_request",
                 "error_description": "refresh_token and client_id are required"},
                status=400,
            )

        try:
            token_entry = self._tokens.refresh(
                refresh_token=refresh_token,
                client_id=client_id,
            )
        except ValueError as exc:
            return web.json_response(
                {"error": "invalid_grant", "error_description": str(exc)},
                status=400,
            )

        logger.info("Refreshed gateway tokens for client %s", client_id[:8])
        return self._token_response(token_entry)

    def _token_response(self, token_entry) -> web.Response:
        return web.json_response({
            "access_token": token_entry.gateway_token,
            "refresh_token": token_entry.refresh_token,
            "token_type": "Bearer",
            "expires_in": token_entry.expires_in,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_url(self, request: web.Request) -> str:
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host)
        return f"{scheme}://{host}"

    def _auth_error(self, error: str, description: str) -> web.Response:
        return web.json_response(
            {"error": error, "error_description": description},
            status=400,
        )
