"""OAuth Authorization Code + PKCE flow manager.

Handles OIDC discovery, PKCE challenge generation, a temporary local callback
server for the redirect, token exchange, in-memory caching, optional disk
persistence, and transparent refresh.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

_PKCE_VERIFIER_LENGTH = 128


@dataclass
class TokenSet:
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: float = 0.0
    id_token: Optional[str] = None
    scope: Optional[str] = None

    @property
    def is_expired(self) -> bool:
        return time.time() >= (self.expires_at - 30)


@dataclass
class OIDCEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    issuer: str
    registration_endpoint: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    revocation_endpoint: Optional[str] = None


class OAuthManager:
    """Manages the full OAuth2 Authorization Code + PKCE lifecycle."""

    def __init__(
        self,
        issuer: str,
        client_id: str,
        scopes: list[str],
        *,
        client_secret: Optional[str] = None,
        redirect_port: int = 0,
        token_cache_path: Optional[str] = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._redirect_port = redirect_port
        self._token_cache_path = token_cache_path
        self._endpoints: Optional[OIDCEndpoints] = None
        self._tokens: Optional[TokenSet] = None
        self._http: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self._http = aiohttp.ClientSession()
        await self._discover_endpoints()
        self._load_cached_tokens()

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing or re-authenticating as needed."""
        if self._tokens and not self._tokens.is_expired:
            return self._tokens.access_token

        if self._tokens and self._tokens.refresh_token:
            try:
                await self._refresh_token()
                return self._tokens.access_token
            except Exception:
                logger.warning("Token refresh failed, falling back to full auth flow")

        await self._authorize()
        assert self._tokens is not None
        return self._tokens.access_token

    # ------------------------------------------------------------------
    # OIDC Discovery
    # ------------------------------------------------------------------

    async def _discover_endpoints(self) -> None:
        url = f"{self._issuer}/.well-known/openid-configuration"
        logger.debug("Discovering OIDC endpoints from %s", url)
        assert self._http is not None
        async with self._http.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        self._endpoints = OIDCEndpoints(
            authorization_endpoint=data["authorization_endpoint"],
            token_endpoint=data["token_endpoint"],
            issuer=data["issuer"],
            registration_endpoint=data.get("registration_endpoint"),
            userinfo_endpoint=data.get("userinfo_endpoint"),
            revocation_endpoint=data.get("revocation_endpoint"),
        )
        logger.info("OIDC discovery complete: issuer=%s", self._endpoints.issuer)

    # ------------------------------------------------------------------
    # PKCE helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_pkce() -> tuple[str, str]:
        """Generate a PKCE code_verifier and S256 code_challenge."""
        verifier = secrets.token_urlsafe(_PKCE_VERIFIER_LENGTH)[:_PKCE_VERIFIER_LENGTH]
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return verifier, challenge

    # ------------------------------------------------------------------
    # Authorization Code flow
    # ------------------------------------------------------------------

    async def _authorize(self) -> None:
        assert self._endpoints is not None
        code_verifier, code_challenge = self._generate_pkce()
        state = secrets.token_urlsafe(32)

        auth_code_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        received_state: dict[str, str] = {}

        async def _callback_handler(request: web.Request) -> web.Response:
            error = request.query.get("error")
            if error:
                desc = request.query.get("error_description", "unknown error")
                auth_code_future.set_exception(
                    RuntimeError(f"OAuth error: {error} - {desc}")
                )
                return web.Response(
                    text="Authentication failed. You can close this tab.",
                    content_type="text/plain",
                )
            code = request.query.get("code")
            received_state["state"] = request.query.get("state", "")
            if code:
                auth_code_future.set_result(code)
            else:
                auth_code_future.set_exception(
                    RuntimeError("No authorization code in callback")
                )
            return web.Response(
                text="Authentication successful! You can close this tab.",
                content_type="text/plain",
            )

        app = web.Application()
        app.router.add_get("/callback", _callback_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self._redirect_port)
        await site.start()

        bound_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        redirect_uri = f"http://127.0.0.1:{bound_port}/callback"
        logger.info("OAuth callback server listening on %s", redirect_uri)

        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self._scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{self._endpoints.authorization_endpoint}?{urlencode(params)}"

        logger.info("Opening browser for authentication...")
        _log_to_stderr(
            f"\n>>> Opening browser for SSO login...\n>>> If it doesn't open, visit:\n>>> {auth_url}\n"
        )
        webbrowser.open(auth_url)

        try:
            auth_code = await asyncio.wait_for(auth_code_future, timeout=300)
        except asyncio.TimeoutError:
            raise RuntimeError("OAuth flow timed out after 5 minutes")
        finally:
            await runner.cleanup()

        if received_state.get("state") != state:
            raise RuntimeError("OAuth state mismatch – possible CSRF attack")

        await self._exchange_code(auth_code, redirect_uri, code_verifier)

    async def _exchange_code(
        self, code: str, redirect_uri: str, code_verifier: str
    ) -> None:
        assert self._endpoints is not None and self._http is not None
        payload: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "code_verifier": code_verifier,
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret

        async with self._http.post(
            self._endpoints.token_endpoint,
            data=payload,
            headers={"Accept": "application/json"},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Token exchange failed ({resp.status}): {body}")
            data = await resp.json()

        self._store_tokens(data)
        logger.info("OAuth token exchange successful")

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def _refresh_token(self) -> None:
        assert (
            self._endpoints is not None
            and self._http is not None
            and self._tokens is not None
            and self._tokens.refresh_token is not None
        )
        payload: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": self._tokens.refresh_token,
            "client_id": self._client_id,
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret

        async with self._http.post(
            self._endpoints.token_endpoint,
            data=payload,
            headers={"Accept": "application/json"},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Token refresh failed ({resp.status}): {body}")
            data = await resp.json()

        self._store_tokens(data)
        logger.info("Token refreshed successfully")

    # ------------------------------------------------------------------
    # Token storage
    # ------------------------------------------------------------------

    def _store_tokens(self, data: dict[str, Any]) -> None:
        expires_in = data.get("expires_in", 300)
        self._tokens = TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=time.time() + expires_in,
            id_token=data.get("id_token"),
            scope=data.get("scope"),
        )
        self._save_cached_tokens()

    def _save_cached_tokens(self) -> None:
        if not self._token_cache_path or not self._tokens:
            return
        try:
            cache = {
                "access_token": self._tokens.access_token,
                "refresh_token": self._tokens.refresh_token,
                "expires_at": self._tokens.expires_at,
                "id_token": self._tokens.id_token,
                "scope": self._tokens.scope,
            }
            Path(self._token_cache_path).write_text(
                json.dumps(cache, indent=2), encoding="utf-8"
            )
            logger.debug("Tokens persisted to %s", self._token_cache_path)
        except Exception:
            logger.warning("Failed to persist tokens to disk", exc_info=True)

    def _load_cached_tokens(self) -> None:
        if not self._token_cache_path:
            return
        path = Path(self._token_cache_path)
        if not path.exists():
            return
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
            self._tokens = TokenSet(
                access_token=cache["access_token"],
                refresh_token=cache.get("refresh_token"),
                expires_at=cache.get("expires_at", 0),
                id_token=cache.get("id_token"),
                scope=cache.get("scope"),
            )
            if self._tokens.is_expired and not self._tokens.refresh_token:
                logger.debug("Cached token expired and no refresh token; discarding")
                self._tokens = None
            else:
                logger.info("Loaded cached tokens from %s", self._token_cache_path)
        except Exception:
            logger.warning("Failed to load cached tokens", exc_info=True)
            self._tokens = None


def _log_to_stderr(msg: str) -> None:
    """Write directly to stderr so stdout stays clean for MCP JSON-RPC."""
    import sys
    sys.stderr.write(msg)
    sys.stderr.flush()
