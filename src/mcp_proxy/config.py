"""Configuration model with CLI args > env vars > config file priority."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ProxyConfig(BaseModel):
    """Configuration for the MCP OAuth proxy."""

    mcp_server_url: str = Field(
        description="URL of the HTTP streamable MCP server to proxy to.",
    )
    oauth_issuer: str = Field(
        default="https://sso.stage.redhat.com/auth/realms/redhat-external",
        description="OAuth/OIDC issuer URL. Used for .well-known discovery.",
    )
    client_id: str = Field(
        description="Pre-registered OAuth client ID in Keycloak.",
    )
    client_secret: Optional[str] = Field(
        default=None,
        description="OAuth client secret. Omit for public clients using PKCE.",
    )
    scopes: list[str] = Field(
        default=["openid"],
        description="OAuth scopes to request.",
    )
    redirect_port: int = Field(
        default=0,
        description="Local port for the OAuth callback server. 0 = auto-select.",
    )
    token_cache_path: Optional[str] = Field(
        default=None,
        description="File path to persist tokens across restarts.",
    )
    log_level: str = Field(
        default="info",
        description="Logging level: debug, info, warning, error.",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"debug", "info", "warning", "error"}
        v = v.lower()
        if v not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got '{v}'")
        return v

    @field_validator("mcp_server_url", "oauth_issuer")
    @classmethod
    def validate_urls(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with http:// or https://, got '{v}'")
        return v.rstrip("/")


def load_config(
    config_path: Optional[str] = None,
    *,
    cli_overrides: Optional[dict] = None,
) -> ProxyConfig:
    """Load configuration with priority: CLI args > env vars > config file.

    Args:
        config_path: Path to a JSON config file.
        cli_overrides: Dict of CLI argument overrides (None values are ignored).
    """
    base: dict = {}

    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(path) as f:
            raw = json.load(f)
        base = _normalize_keys(raw)

    env_map = {
        "MCP_SERVER_URL": "mcp_server_url",
        "OAUTH_ISSUER": "oauth_issuer",
        "CLIENT_ID": "client_id",
        "CLIENT_SECRET": "client_secret",
        "SCOPES": "scopes",
        "REDIRECT_PORT": "redirect_port",
        "TOKEN_CACHE_PATH": "token_cache_path",
        "LOG_LEVEL": "log_level",
    }
    for env_key, config_key in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            if config_key == "scopes":
                base[config_key] = [s.strip() for s in val.split(",")]
            elif config_key == "redirect_port":
                base[config_key] = int(val)
            else:
                base[config_key] = val

    if cli_overrides:
        for key, val in cli_overrides.items():
            if val is not None:
                base[key] = val

    return ProxyConfig(**base)


def _normalize_keys(data: dict) -> dict:
    """Convert camelCase keys from JSON config to snake_case."""
    mapping = {
        "mcpServerUrl": "mcp_server_url",
        "oauthIssuer": "oauth_issuer",
        "clientId": "client_id",
        "clientSecret": "client_secret",
        "scopes": "scopes",
        "redirectPort": "redirect_port",
        "tokenCachePath": "token_cache_path",
        "logLevel": "log_level",
    }
    result = {}
    for key, val in data.items():
        normalized = mapping.get(key, key)
        result[normalized] = val
    return result
