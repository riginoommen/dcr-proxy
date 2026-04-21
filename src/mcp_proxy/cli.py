"""CLI argument parsing and entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load_config
from .gateway import McpGateway


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-gateway",
        description="Multi-client MCP HTTP gateway with OAuth (Auth Code + PKCE)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to a JSON configuration file.",
    )
    parser.add_argument(
        "--host",
        metavar="ADDR",
        help="Host address to bind the gateway to (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        metavar="PORT",
        type=int,
        help="Port for the gateway HTTP server (default: 8080).",
    )
    parser.add_argument(
        "--oauth-issuer",
        metavar="URL",
        help="OAuth/OIDC issuer URL (for .well-known discovery).",
    )
    parser.add_argument(
        "--client-id",
        metavar="ID",
        help="Pre-registered OAuth client ID.",
    )
    parser.add_argument(
        "--client-secret",
        metavar="SECRET",
        help="OAuth client secret (omit for public PKCE clients).",
    )
    parser.add_argument(
        "--scopes",
        metavar="SCOPE",
        nargs="+",
        help="OAuth scopes to request (space-separated).",
    )
    parser.add_argument(
        "--session-ttl",
        metavar="MINUTES",
        type=int,
        help="Session idle timeout in minutes (default: 480).",
    )
    parser.add_argument(
        "--allowed-targets",
        metavar="URL",
        nargs="+",
        help="Whitelist of allowed MCP server target URLs (space-separated).",
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        choices=["debug", "info", "warning", "error"],
        help="Logging level.",
    )

    args = parser.parse_args()

    cli_overrides: dict = {}
    if args.host:
        cli_overrides["host"] = args.host
    if args.port is not None:
        cli_overrides["port"] = args.port
    if args.oauth_issuer:
        cli_overrides["oauth_issuer"] = args.oauth_issuer
    if args.client_id:
        cli_overrides["client_id"] = args.client_id
    if args.client_secret:
        cli_overrides["client_secret"] = args.client_secret
    if args.scopes:
        cli_overrides["scopes"] = args.scopes
    if args.session_ttl is not None:
        cli_overrides["session_ttl_minutes"] = args.session_ttl
    if args.allowed_targets:
        cli_overrides["allowed_targets"] = args.allowed_targets
    if args.log_level:
        cli_overrides["log_level"] = args.log_level

    try:
        config = load_config(args.config, cli_overrides=cli_overrides or None)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    gateway = McpGateway(config)
    try:
        asyncio.run(gateway.run())
    except KeyboardInterrupt:
        pass
