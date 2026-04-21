"""Configuration model with CLI args > env vars > config file priority."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class GatewayConfig(BaseModel):
    """Configuration for the MCP HTTP gateway."""

    host: str = Field(
        default="127.0.0.1",
        description="Host address to bind the gateway server to.",
    )
    port: int = Field(
        default=8080,
        description="Port for the gateway HTTP server.",
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
    session_ttl_minutes: int = Field(
        default=480,
        description="Session idle timeout in minutes before automatic cleanup.",
    )
    default_target: Optional[str] = Field(
        default=None,
        description="Default MCP server URL when no ?target= param is provided.",
    )
    allowed_targets: Optional[list[str]] = Field(
        default=None,
        description="Whitelist of allowed MCP server target URLs. null = allow any.",
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

    @field_validator("oauth_issuer")
    @classmethod
    def validate_urls(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with http:// or https://, got '{v}'")
        return v.rstrip("/")


def load_config(
    config_path: Optional[str] = None,
    *,
    cli_overrides: Optional[dict] = None,
) -> GatewayConfig:
    """Load configuration with priority: CLI args > env vars > config file."""
    base: dict = {}

    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(path) as f:
            raw = json.load(f)
        base = _normalize_keys(raw)

    env_map = {
        "HOST": "host",
        "PORT": "port",
        "OAUTH_ISSUER": "oauth_issuer",
        "CLIENT_ID": "client_id",
        "CLIENT_SECRET": "client_secret",
        "SCOPES": "scopes",
        "SESSION_TTL_MINUTES": "session_ttl_minutes",
        "DEFAULT_TARGET": "default_target",
        "ALLOWED_TARGETS": "allowed_targets",
        "LOG_LEVEL": "log_level",
    }
    for env_key, config_key in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            if config_key == "scopes":
                base[config_key] = [s.strip() for s in val.split(",")]
            elif config_key == "allowed_targets":
                base[config_key] = [s.strip() for s in val.split(",")]
            elif config_key in ("port", "session_ttl_minutes"):
                base[config_key] = int(val)
            else:
                base[config_key] = val

    if cli_overrides:
        for key, val in cli_overrides.items():
            if val is not None:
                base[key] = val

    return GatewayConfig(**base)


def _normalize_keys(data: dict) -> dict:
    """Convert camelCase keys from JSON config to snake_case."""
    mapping = {
        "host": "host",
        "port": "port",
        "oauthIssuer": "oauth_issuer",
        "clientId": "client_id",
        "clientSecret": "client_secret",
        "scopes": "scopes",
        "sessionTtlMinutes": "session_ttl_minutes",
        "defaultTarget": "default_target",
        "allowedTargets": "allowed_targets",
        "logLevel": "log_level",
    }
    result = {}
    for key, val in data.items():
        normalized = mapping.get(key, key)
        result[normalized] = val
    return result
