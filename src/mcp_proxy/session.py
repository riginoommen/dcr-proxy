"""Per-user session manager for the MCP HTTP gateway.

Each authenticated user gets a Session containing their own OAuthManager
instance.  Sessions are identified by a secure random cookie value and
automatically pruned after an idle timeout.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from .http_client import McpHttpClient
from .oauth import OAuthManager, OIDCEndpoints, PKCEChallenge

logger = logging.getLogger(__name__)


@dataclass
class Session:
    session_id: str
    oauth: OAuthManager
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    pkce: Optional[PKCEChallenge] = None
    auth_url: Optional[str] = None
    mcp_clients: dict[str, McpHttpClient] = field(default_factory=dict)

    def touch(self) -> None:
        self.last_active = time.time()


class SessionManager:
    """Manages per-user OAuth sessions with automatic cleanup."""

    def __init__(
        self,
        issuer: str,
        client_id: str,
        scopes: list[str],
        *,
        client_secret: Optional[str] = None,
        session_ttl_minutes: int = 480,
    ) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._session_ttl = session_ttl_minutes * 60
        self._sessions: dict[str, Session] = {}
        self._endpoints: Optional[OIDCEndpoints] = None
        self._http: Optional[aiohttp.ClientSession] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Perform OIDC discovery (once) and start the cleanup task."""
        self._http = aiohttp.ClientSession()
        discovery_oauth = OAuthManager(
            self._issuer,
            self._client_id,
            self._scopes,
            client_secret=self._client_secret,
            http_session=self._http,
        )
        await discovery_oauth.start()
        self._endpoints = discovery_oauth.endpoints
        logger.info("SessionManager started, OIDC endpoints discovered")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def close(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        for session in self._sessions.values():
            for client in session.mcp_clients.values():
                await client.close()
            await session.oauth.close()
        self._sessions.clear()
        if self._http and not self._http.closed:
            await self._http.close()

    def create_login_session(self, redirect_uri: str) -> tuple[str, str]:
        """Create a new session and return (session_id, authorization_url).

        The caller should set ``session_id`` as a cookie and redirect the
        user's browser to the authorization URL.
        """
        session_id = secrets.token_urlsafe(32)
        oauth = OAuthManager(
            self._issuer,
            self._client_id,
            self._scopes,
            client_secret=self._client_secret,
            endpoints=self._endpoints,
            http_session=self._http,
        )
        pkce, auth_url = oauth.build_authorization_url(redirect_uri)
        session = Session(
            session_id=session_id,
            oauth=oauth,
            pkce=pkce,
            auth_url=auth_url,
        )
        self._sessions[session_id] = session
        logger.info("Created login session %s", session_id[:8])
        return session_id, auth_url

    async def complete_auth(
        self, session_id: str, code: str, state: str
    ) -> bool:
        """Exchange the authorization code for tokens.

        Returns True on success, False if session is unknown or state mismatches.
        """
        session = self._sessions.get(session_id)
        if session is None or session.pkce is None:
            logger.warning("complete_auth: unknown session %s", session_id[:8])
            return False

        if session.pkce.state != state:
            logger.warning("State mismatch for session %s", session_id[:8])
            return False

        await session.oauth.exchange_code(
            code,
            session.pkce.redirect_uri,
            session.pkce.code_verifier,
        )
        session.pkce = None
        session.touch()
        logger.info("Session %s authenticated", session_id[:8])
        return True

    async def get_token(self, session_id: str) -> str:
        """Return a valid Bearer token for the given session.

        Raises KeyError if session not found, RuntimeError if token unavailable.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session not found: {session_id[:8]}")
        session.touch()
        return await session.oauth.get_access_token()

    def is_valid(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        if session.oauth.has_valid_token:
            return True
        if session.pkce is not None:
            return True
        return False

    async def get_or_create_client(
        self,
        session_id: str,
        target_url: str,
        get_token: Any,
    ) -> McpHttpClient:
        """Return a persistent McpHttpClient for this session + target.

        Creates and starts a new client on first use; reuses it on subsequent
        calls so that the Mcp-Session-Id header is preserved across requests.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session not found: {session_id[:8]}")
        session.touch()

        client = session.mcp_clients.get(target_url)
        if client is None:
            client = McpHttpClient(server_url=target_url, get_token=get_token)
            await client.start()
            session.mcp_clients[target_url] = client
            logger.debug(
                "Created MCP client for session %s -> %s",
                session_id[:8], target_url,
            )
        return client

    def is_authenticated(self, session_id: str) -> bool:
        """Return True if the session exists and has completed auth."""
        session = self._sessions.get(session_id)
        if session is None:
            return False
        return session.oauth.has_valid_token or session.pkce is None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                await self._prune_expired()
        except asyncio.CancelledError:
            pass

    async def _prune_expired(self) -> None:
        now = time.time()
        expired = [
            sid
            for sid, s in self._sessions.items()
            if (now - s.last_active) > self._session_ttl
        ]
        for sid in expired:
            session = self._sessions.pop(sid)
            for client in session.mcp_clients.values():
                await client.close()
            logger.info("Pruned expired session %s", sid[:8])
