"""Dynamic Client Registration (RFC 7591) in-memory client store."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class RegisteredClient:
    client_id: str
    client_secret: str
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str] = field(default_factory=lambda: ["authorization_code"])
    response_types: list[str] = field(default_factory=lambda: ["code"])
    token_endpoint_auth_method: str = "client_secret_post"
    created_at: float = field(default_factory=time.time)


class ClientRegistry:
    """In-memory store for dynamically registered OAuth clients."""

    def __init__(self) -> None:
        self._clients: dict[str, RegisteredClient] = {}

    def register(self, body: dict[str, Any]) -> RegisteredClient:
        """Process a DCR request (RFC 7591) and return the registered client.

        Generates client_id and client_secret, and stores the client in memory.
        Accepts requests with or without redirect_uris -- MCP clients may
        register with localhost callbacks or use dynamic redirect URIs.
        """
        logger.debug("DCR request body: %s", body)

        redirect_uris = body.get("redirect_uris", [])
        if isinstance(redirect_uris, str):
            redirect_uris = [redirect_uris]
        if not isinstance(redirect_uris, list):
            redirect_uris = []

        client_id = secrets.token_urlsafe(24)
        client_secret = secrets.token_urlsafe(32)

        client = RegisteredClient(
            client_id=client_id,
            client_secret=client_secret,
            client_name=body.get("client_name", "MCP Client"),
            redirect_uris=redirect_uris,
            grant_types=body.get("grant_types", ["authorization_code"]),
            response_types=body.get("response_types", ["code"]),
            token_endpoint_auth_method=body.get(
                "token_endpoint_auth_method", "client_secret_post"
            ),
        )
        self._clients[client_id] = client
        logger.info("Registered DCR client: %s (%s)", client_id[:8], client.client_name)
        return client

    def get(self, client_id: str) -> Optional[RegisteredClient]:
        return self._clients.get(client_id)

    def validate_redirect_uri(self, client_id: str, uri: str) -> bool:
        client = self._clients.get(client_id)
        if client is None:
            return False
        if not client.redirect_uris:
            return uri.startswith(("http://127.0.0.1", "http://localhost"))
        return uri in client.redirect_uris

    def to_registration_response(self, client: RegisteredClient) -> dict[str, Any]:
        """Format the DCR response per RFC 7591 Section 3.2."""
        return {
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "grant_types": client.grant_types,
            "response_types": client.response_types,
            "token_endpoint_auth_method": client.token_endpoint_auth_method,
            "client_id_issued_at": int(client.created_at),
        }
