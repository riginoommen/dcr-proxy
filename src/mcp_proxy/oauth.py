"""OAuth Authorization Code + PKCE flow manager.

Provides OIDC discovery, PKCE challenge generation, token exchange,
in-memory token caching, and transparent refresh.

In gateway mode the authorization URL is built and returned to the caller
(the gateway HTTP handler) rather than opening a browser directly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

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


@dataclass
class PKCEChallenge:
    """Holds the PKCE verifier/challenge and OAuth state for one auth flow."""
    code_verifier: str
    code_challenge: str
    state: str
    redirect_uri: str


class OAuthManager:
    """Manages OAuth2 Authorization Code + PKCE for a single user session."""

    def __init__(
        self,
        issuer: str,
        client_id: str,
        scopes: list[str],
        *,
        client_secret: Optional[str] = None,
        endpoints: Optional[OIDCEndpoints] = None,
        http_session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._endpoints = endpoints
        self._tokens: Optional[TokenSet] = None
        self._http = http_session
        self._owns_http = http_session is None

    async def start(self) -> None:
        """Initialise HTTP session and run OIDC discovery if not already done."""
        if self._http is None:
            self._http = aiohttp.ClientSession()
            self._owns_http = True
        if self._endpoints is None:
            await self._discover_endpoints()

    async def close(self) -> None:
        if self._owns_http and self._http and not self._http.closed:
            await self._http.close()

    @property
    def endpoints(self) -> Optional[OIDCEndpoints]:
        return self._endpoints

    @property
    def has_valid_token(self) -> bool:
        return self._tokens is not None and not self._tokens.is_expired

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if needed.

        Raises RuntimeError if no tokens are available and refresh fails.
        In gateway mode the caller should redirect to /auth/login instead.
        """
        if self._tokens and not self._tokens.is_expired:
            return self._tokens.access_token

        if self._tokens and self._tokens.refresh_token:
            await self._refresh_token()
            return self._tokens.access_token

        raise RuntimeError("No valid token available; user must re-authenticate")

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
    # PKCE + Authorization URL
    # ------------------------------------------------------------------

    @staticmethod
    def generate_pkce() -> tuple[str, str]:
        """Generate a PKCE code_verifier and S256 code_challenge."""
        verifier = secrets.token_urlsafe(_PKCE_VERIFIER_LENGTH)[:_PKCE_VERIFIER_LENGTH]
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return verifier, challenge

    def build_authorization_url(self, redirect_uri: str) -> tuple[PKCEChallenge, str]:
        """Build an SSO authorization URL and return the PKCE challenge data.

        The caller is responsible for redirecting the user to the URL and
        handling the callback.
        """
        assert self._endpoints is not None
        verifier, challenge = self.generate_pkce()
        state = secrets.token_urlsafe(32)

        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self._scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{self._endpoints.authorization_endpoint}?{urlencode(params)}"

        pkce = PKCEChallenge(
            code_verifier=verifier,
            code_challenge=challenge,
            state=state,
            redirect_uri=redirect_uri,
        )
        logger.debug("Built authorization URL for state=%s", state)
        return pkce, auth_url

    # ------------------------------------------------------------------
    # Token exchange
    # ------------------------------------------------------------------

    async def exchange_code(
        self, code: str, redirect_uri: str, code_verifier: str
    ) -> None:
        """Exchange an authorization code for tokens (public method for gateway)."""
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
