"""Gateway token mapping and authorization code management.

Maps gateway-issued tokens and auth codes to internal session IDs so that
MCP clients authenticate with the gateway's own tokens while the gateway
uses the real SSO tokens to talk to MCP servers.

Supports token refresh with rotation: each refresh revokes the old pair
and issues a new access + refresh token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

AUTH_CODE_TTL = 300  # 5 minutes
TOKEN_TTL = 3600  # 1 hour
REFRESH_TOKEN_TTL = 86400  # 24 hours


@dataclass
class AuthCodeEntry:
    code: str
    session_id: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str = "S256"
    expires_at: float = field(default_factory=lambda: time.time() + AUTH_CODE_TTL)

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


@dataclass
class TokenEntry:
    gateway_token: str
    refresh_token: str
    session_id: str
    client_id: str
    created_at: float = field(default_factory=time.time)
    expires_in: int = TOKEN_TTL
    refresh_expires_at: float = field(default_factory=lambda: time.time() + REFRESH_TOKEN_TTL)

    @property
    def is_expired(self) -> bool:
        return time.time() >= (self.created_at + self.expires_in)

    @property
    def is_refresh_expired(self) -> bool:
        return time.time() >= self.refresh_expires_at


class TokenStore:
    """Manages gateway auth codes, access tokens, and refresh tokens."""

    def __init__(self) -> None:
        self._codes: dict[str, AuthCodeEntry] = {}
        self._tokens: dict[str, TokenEntry] = {}
        self._refresh_tokens: dict[str, TokenEntry] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    def start_cleanup(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    def create_auth_code(
        self,
        session_id: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str:
        code = secrets.token_urlsafe(32)
        entry = AuthCodeEntry(
            code=code,
            session_id=session_id,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        self._codes[code] = entry
        logger.debug("Created auth code for session %s, client %s", session_id[:8], client_id[:8])
        return code

    def exchange_code(
        self,
        code: str,
        code_verifier: str,
        client_id: str,
        redirect_uri: str,
    ) -> TokenEntry:
        """Exchange an authorization code for a gateway access + refresh token.

        Validates the code, PKCE verifier, client_id, and redirect_uri.
        Returns a TokenEntry on success. Raises ValueError on failure.
        """
        entry = self._codes.pop(code, None)
        if entry is None:
            raise ValueError("Invalid or expired authorization code")

        if entry.is_expired:
            raise ValueError("Authorization code expired")

        if entry.client_id != client_id:
            raise ValueError("client_id mismatch")

        if entry.redirect_uri != redirect_uri:
            raise ValueError("redirect_uri mismatch")

        if not self._verify_pkce(code_verifier, entry.code_challenge, entry.code_challenge_method):
            raise ValueError("PKCE code_verifier validation failed")

        return self._issue_token_pair(entry.session_id, entry.client_id)

    def refresh(self, refresh_token: str, client_id: str) -> TokenEntry:
        """Exchange a refresh token for a new access + refresh token pair.

        Implements token rotation: the old access and refresh tokens are
        revoked and new ones are issued for the same session.
        Raises ValueError on failure.
        """
        old_entry = self._refresh_tokens.get(refresh_token)
        if old_entry is None:
            raise ValueError("Invalid refresh token")

        if old_entry.is_refresh_expired:
            self._revoke_entry(old_entry)
            raise ValueError("Refresh token expired")

        if old_entry.client_id != client_id:
            raise ValueError("client_id mismatch")

        session_id = old_entry.session_id
        self._revoke_entry(old_entry)

        new_entry = self._issue_token_pair(session_id, client_id)
        logger.debug("Refreshed tokens for session %s, client %s", session_id[:8], client_id[:8])
        return new_entry

    def get_session_id(self, gateway_token: str) -> Optional[str]:
        entry = self._tokens.get(gateway_token)
        if entry is None or entry.is_expired:
            return None
        return entry.session_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _issue_token_pair(self, session_id: str, client_id: str) -> TokenEntry:
        gateway_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)

        token_entry = TokenEntry(
            gateway_token=gateway_token,
            refresh_token=refresh_token,
            session_id=session_id,
            client_id=client_id,
        )
        self._tokens[gateway_token] = token_entry
        self._refresh_tokens[refresh_token] = token_entry
        logger.debug("Issued token pair for session %s", session_id[:8])
        return token_entry

    def _revoke_entry(self, entry: TokenEntry) -> None:
        self._tokens.pop(entry.gateway_token, None)
        self._refresh_tokens.pop(entry.refresh_token, None)

    @staticmethod
    def _verify_pkce(
        code_verifier: str,
        code_challenge: str,
        method: str,
    ) -> bool:
        if method == "S256":
            digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
            computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            return computed == code_challenge
        if method == "plain":
            return code_verifier == code_challenge
        return False

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                self._prune()
        except asyncio.CancelledError:
            pass

    def _prune(self) -> None:
        expired_codes = [k for k, v in self._codes.items() if v.is_expired]
        for k in expired_codes:
            del self._codes[k]

        expired_tokens = [
            entry for entry in self._tokens.values()
            if entry.is_expired and entry.is_refresh_expired
        ]
        for entry in expired_tokens:
            self._revoke_entry(entry)

        if expired_codes or expired_tokens:
            logger.debug(
                "Pruned %d codes, %d token pairs", len(expired_codes), len(expired_tokens)
            )
