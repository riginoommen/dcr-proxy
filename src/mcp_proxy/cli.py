"""CLI argument parsing and entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load_config
from .proxy import McpProxy


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-proxy",
        description="MCP stdio-to-HTTP proxy with OAuth (Auth Code + PKCE)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to a JSON configuration file.",
    )
    parser.add_argument(
        "--mcp-server-url",
        metavar="URL",
        help="URL of the HTTP streamable MCP server.",
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
        "--redirect-port",
        metavar="PORT",
        type=int,
        help="Local port for OAuth callback (0 = auto).",
    )
    parser.add_argument(
        "--token-cache-path",
        metavar="PATH",
        help="File path to persist tokens across restarts.",
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        choices=["debug", "info", "warning", "error"],
        help="Logging level.",
    )

    args = parser.parse_args()

    cli_overrides: dict = {}
    if args.mcp_server_url:
        cli_overrides["mcp_server_url"] = args.mcp_server_url
    if args.oauth_issuer:
        cli_overrides["oauth_issuer"] = args.oauth_issuer
    if args.client_id:
        cli_overrides["client_id"] = args.client_id
    if args.client_secret:
        cli_overrides["client_secret"] = args.client_secret
    if args.scopes:
        cli_overrides["scopes"] = args.scopes
    if args.redirect_port is not None:
        cli_overrides["redirect_port"] = args.redirect_port
    if args.token_cache_path:
        cli_overrides["token_cache_path"] = args.token_cache_path
    if args.log_level:
        cli_overrides["log_level"] = args.log_level

    try:
        config = load_config(args.config, cli_overrides=cli_overrides or None)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    proxy = McpProxy(config)
    try:
        asyncio.run(proxy.run())
    except KeyboardInterrupt:
        pass
