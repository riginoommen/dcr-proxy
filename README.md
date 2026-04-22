# MCP DCR Proxy

An OAuth gateway with **Dynamic Client Registration (RFC 7591)** for the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). Enables any MCP client to connect to your MCP servers through Red Hat SSO authentication -- no DCR support needed on the SSO side.



## Architecture

![Proxy Architecture](docs/assets/Proxy_Architecture.png)

## Quick Start

```bash
# 1. Install
cd /path/to/dcr-proxy
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure (edit with your SSO client ID and MCP server URL)
cp config.example.json config.json

# 3. Start
PYTHONPATH=src python -m mcp_proxy --config config.json

# 4. Connect from Cursor -- just add this to .cursor/mcp.json:
#    { "mcpServers": { "my-server": { "url": "http://127.0.0.1:8080/mcp" } } }
```

## How It Works

![Workflow Diagram](docs/assets/Workflow_Diagram.jpg)

1. MCP client connects to `http://proxy:8080/mcp`
2. Proxy returns `401` with `WWW-Authenticate` header pointing to OAuth metadata
3. Client auto-discovers `/.well-known/oauth-authorization-server`
4. Client registers via **DCR** at `POST /oauth/register` (gets a `client_id`)
5. Client starts **Authorization Code + PKCE** flow at `/oauth/authorize`
6. Proxy redirects user's browser to **Red Hat SSO** for login
7. After SSO login, proxy issues a **gateway access token** to the client
8. Client uses the gateway token for all MCP requests (`Authorization: Bearer <token>`)
9. Proxy forwards requests to the backend MCP server using the **real SSO token**

### Two Token Layers

The proxy maintains two separate token layers. MCP clients never see the real SSO token.

| Layer | Held by | Issued by | Used for |
|---|---|---|---|
| **Gateway token** | MCP client (Cursor, etc.) | DCR Proxy | Authenticating to the proxy |
| **SSO token** | DCR Proxy (internal) | sso.redhat.com | Authenticating to backend MCP servers |

## Client Setup

### Cursor

Add to `.cursor/mcp.json` (project or global):

```json
{
  "mcpServers": {
    "security-mcp": {
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

Cursor will auto-discover OAuth, register via DCR, and open a browser for SSO login.

### Claude Desktop

Add to Claude Desktop's MCP config:

```json
{
  "mcpServers": {
    "security-mcp": {
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

1. Select **Streamable HTTP** transport
2. Enter URL: `http://127.0.0.1:8080/mcp`
3. In Auth Settings Perform Quick Auth Flow
3. Click **Connect**

The Inspector will auto-discover OAuth, register, and open a browser for SSO login.

### Targeting a Different MCP Server

By default, requests go to the `defaultTarget` from config. To target a different backend, pass `?target=`:

```
http://127.0.0.1:8080/mcp?target=https://other-mcp-server.example.com/mcp
```

Each new target requires an `initialize` handshake (see [Manual Testing](#manual-testing-with-curl)).

## Prerequisites

- **Python 3.10+**
- **A pre-registered OAuth client** in Red Hat SSO (see [Keycloak Client Setup](#keycloak-client-setup))
- **One or more HTTP MCP servers** that accept Bearer token authentication

## Installation

```bash
cd /path/to/dcr-proxy
python -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Keycloak Client Setup

Your SSO team needs to create a client with these settings:

| Setting                        | Value                                          |
| ------------------------------ | ---------------------------------------------- |
| **Client Protocol**            | `openid-connect`                               |
| **Client ID**                  | e.g. `mcp-gateway-client`                      |
| **Access Type**                | `public` (recommended with PKCE)               |
| **Standard Flow Enabled**      | `ON` (Authorization Code)                      |
| **Direct Access Grants**       | `OFF`                                          |
| **Valid Redirect URIs**        | `http://127.0.0.1:8080/oauth/callback`         |
| **Web Origins**                | `http://127.0.0.1:8080`                        |
| **PKCE Code Challenge Method** | `S256`                                         |
| **Scopes**                     | `openid` + any scopes your MCP server requires |

If you run on multiple ports, add redirect URIs for each (e.g., `http://127.0.0.1:4000/oauth/callback`).

## Configuration

```bash
cp config.example.json config.json
```

```json
{
  "host": "127.0.0.1",
  "port": 8080,
  "oauthIssuer": "https://sso.redhat.com/auth/realms/redhat-external",
  "clientId": "your-client-id",
  "clientSecret": null,
  "scopes": ["openid"],
  "defaultTarget": "https://your-mcp-server.example.com/mcp",
  "sessionTtlMinutes": 480,
  "allowedTargets": null,
  "logLevel": "info"
}
```

### All Configuration Options

| Config Key          | Env Var              | CLI Flag            | Default   | Description |
| ------------------- | -------------------- | ------------------- | --------- | ----------- |
| `host`              | `HOST`               | `--host`            | `127.0.0.1` | Bind address |
| `port`              | `PORT`               | `--port`            | `8080`    | Listen port |
| `oauthIssuer`       | `OAUTH_ISSUER`       | `--oauth-issuer`    | RH SSO staging | OIDC issuer URL |
| `clientId`          | `CLIENT_ID`          | `--client-id`       | *required* | Pre-registered SSO client ID |
| `clientSecret`      | `CLIENT_SECRET`      | `--client-secret`   | `null`    | SSO client secret (omit for PKCE) |
| `scopes`            | `SCOPES`             | `--scopes`          | `["openid"]` | OAuth scopes to request |
| `defaultTarget`     | `DEFAULT_TARGET`     | `--default-target`  | `null`    | Default MCP server URL |
| `sessionTtlMinutes` | `SESSION_TTL_MINUTES`| `--session-ttl`     | `480`     | Session idle timeout (minutes) |
| `allowedTargets`    | `ALLOWED_TARGETS`    | `--allowed-targets` | `null`    | Whitelist of MCP server URLs |
| `logLevel`          | `LOG_LEVEL`          | `--log-level`       | `info`    | debug, info, warning, error |

Config priority: **CLI flags > environment variables > config file**.

## Running

```bash
source .venv/bin/activate
PYTHONPATH=src python -m mcp_proxy --config config.json
```

Output:

```
MCP DCR Proxy running on http://127.0.0.1:8080
  MCP endpoint:  http://127.0.0.1:8080/mcp
  DCR register:  POST http://127.0.0.1:8080/oauth/register
  OAuth metadata: http://127.0.0.1:8080/.well-known/oauth-authorization-server
  Health:        http://127.0.0.1:8080/health
```

## Manual Testing with curl

### Check health

```bash
curl -s http://127.0.0.1:8080/health | python3 -m json.tool
```

### View OAuth metadata

```bash
curl -s http://127.0.0.1:8080/.well-known/oauth-authorization-server | python3 -m json.tool
```

### View protected resource metadata

```bash
curl -s http://127.0.0.1:8080/.well-known/oauth-protected-resource | python3 -m json.tool
```

### Register a client via DCR

```bash
curl -s -X POST http://127.0.0.1:8080/oauth/register \
  -H 'Content-Type: application/json' \
  -d '{
    "client_name": "my-test-client",
    "redirect_uris": ["http://127.0.0.1:9999/callback"],
    "grant_types": ["authorization_code"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none"
  }' | python3 -m json.tool
```

Returns `client_id` and `client_secret` for use in the OAuth flow.

### Test MCP via cookie-based flow (quick manual test)

For manual curl testing, the easiest approach is to authenticate via browser first, then use the gateway token. The full OAuth flow (DCR -> authorize -> token -> MCP) is handled automatically by MCP clients like Cursor and MCP Inspector.

### Initialize MCP session (after obtaining a Bearer token)

```bash
export TOKEN="your-gateway-access-token"
export TARGET="https://your-mcp-server.example.com/mcp"

curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8080/mcp?target=$TARGET" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | python3 -m json.tool
```

### List tools

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8080/mcp?target=$TARGET" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python3 -m json.tool
```

### Call a tool

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8080/mcp?target=$TARGET" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"CveByTitle","arguments":{"title":"CVE-2021-44228"}}}' | python3 -m json.tool
```

## API Reference

### Discovery Endpoints (no auth)

| Endpoint | Method | Description |
|---|---|---|
| `/.well-known/oauth-protected-resource` | GET | RFC 9728 -- resource metadata with authorization server URL |
| `/.well-known/oauth-authorization-server` | GET | RFC 8414 -- OAuth metadata with all endpoint URLs |

**Example response** (`/.well-known/oauth-authorization-server`):

```json
{
  "issuer": "http://127.0.0.1:8080",
  "authorization_endpoint": "http://127.0.0.1:8080/oauth/authorize",
  "token_endpoint": "http://127.0.0.1:8080/oauth/token",
  "registration_endpoint": "http://127.0.0.1:8080/oauth/register",
  "scopes_supported": ["openid", "api.graphql"],
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code"],
  "code_challenge_methods_supported": ["S256"]
}
```

### OAuth Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/oauth/register` | POST | DCR (RFC 7591) -- register a new client dynamically |
| `/oauth/authorize` | GET | Start Authorization Code + PKCE flow (redirects to SSO) |
| `/oauth/callback` | GET | Internal -- receives SSO callback, issues gateway auth code |
| `/oauth/token` | POST | Exchange gateway auth code for gateway access token |

**DCR request example** (`POST /oauth/register`):

```json
{
  "client_name": "Cursor",
  "redirect_uris": ["cursor://anysphere.cursor-mcp/oauth/callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```

**DCR response example** (201):

```json
{
  "client_id": "9J1pc2fJcNyZ2dwW9434PKQH5sDt-uNw",
  "client_secret": "...",
  "client_name": "Cursor",
  "redirect_uris": ["cursor://anysphere.cursor-mcp/oauth/callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "client_id_issued_at": 1776780199
}
```

**Token response example** (`POST /oauth/token`):

```json
{
  "access_token": "eyJ...",
  "refresh_token": "dGhp...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

**Refresh token request** (`POST /oauth/token`):

```bash
curl -s -X POST http://127.0.0.1:8080/oauth/token \
  -d "grant_type=refresh_token&refresh_token=YOUR_REFRESH_TOKEN&client_id=YOUR_CLIENT_ID"
```

Returns a new `access_token` + `refresh_token` pair. The old pair is revoked (token rotation).

### MCP Endpoints (Bearer token required)

| Endpoint | Method | Description |
|---|---|---|
| `/mcp` | POST | Forward JSON-RPC to default target MCP server |
| `/mcp?target=URL` | POST | Forward JSON-RPC to a specific MCP server |
| `/mcp` | GET | SSE stream passthrough from default target |
| `/mcp?target=URL` | GET | SSE stream passthrough from a specific target |
| `/health` | GET | Returns `{"status": "ok"}` (no auth) |

On `401`, the response includes a `WWW-Authenticate` header:

```
WWW-Authenticate: Bearer resource_metadata="http://127.0.0.1:8080/.well-known/oauth-protected-resource"
```

MCP clients use this to auto-discover the OAuth flow.

## Security

### Target Whitelist

By default, clients can target any MCP server URL. To restrict:

```json
{
  "allowedTargets": [
    "https://mcp-server-1.example.com/mcp",
    "https://mcp-server-2.example.com/mcp"
  ]
}
```

Requests to unlisted targets get `403 Forbidden`.

### Token Isolation

Each MCP client gets its own DCR registration, its own OAuth session, and its own gateway token. Gateway tokens map to SSO tokens internally -- clients never see the real SSO tokens.

### Token Lifetimes

| Token | TTL | Behavior |
|---|---|---|
| Gateway auth codes | 5 minutes | Single-use, expire after exchange |
| Gateway access tokens | 1 hour | Client uses refresh token to get a new one |
| Gateway refresh tokens | 24 hours | Rotation: each refresh issues a new pair, old is revoked |
| SSO access tokens | Per Keycloak config | Auto-refreshed by the proxy using refresh tokens |
| Sessions | Configurable (`sessionTtlMinutes`) | Pruned after idle timeout (default 8 hours) |

### Limitations

- All state (DCR clients, tokens, sessions) is **in-memory** and lost on restart. MCP clients will need to re-register and re-authenticate.
- Gateway refresh tokens last 24 hours. After that, clients must re-authenticate via the OAuth flow.

## Tested With

| Client | Status |
|---|---|
| **MCP Inspector** | Full flow confirmed: DCR -> SSO login -> tool discovery -> tool calls |
| **Cursor** | DCR registration + OAuth flow confirmed |
| **curl** | Manual testing of all endpoints confirmed |

## Troubleshooting

### MCP client gets 401

The client needs to go through the OAuth flow first. Most MCP clients handle this automatically via the `WWW-Authenticate` header and OAuth metadata discovery.

### SSO shows "Invalid parameter: redirect_uri"

Your Keycloak client needs `http://127.0.0.1:8080/auth/callback` as a Valid Redirect URI. Make sure it matches the proxy's host and port exactly.

### DCR registration returns 400

Check the gateway logs (`--log-level debug`) to see what the client sent. The proxy accepts registrations with or without `redirect_uris`.

### tools/list returns 422 or 404

You must call `initialize` before any other MCP method. Each new target MCP server requires its own `initialize` handshake. The proxy persists the `Mcp-Session-Id` across requests to the same target within a session.

### Gateway token expired

Gateway access tokens last 1 hour. The client should use its refresh token to get a new pair via `POST /oauth/token` with `grant_type=refresh_token`. Most MCP clients (Cursor, Inspector) handle this automatically. Refresh tokens last 24 hours.

### SSO refresh token expired

The proxy auto-refreshes SSO tokens using the refresh token. If the refresh token itself expires (Keycloak default: 30 minutes idle), the user must re-authenticate via browser SSO login.

### Debugging

```bash
PYTHONPATH=src python -m mcp_proxy --config config.json --log-level debug
```

All logs go to **stderr**.

## Project Structure

```
dcr-proxy/
  src/mcp_proxy/
    __init__.py           # Package metadata
    __main__.py           # python -m mcp_proxy entry point
    cli.py                # CLI argument parsing
    config.py             # GatewayConfig (Pydantic)
    oauth.py              # SSO-facing OAuth: PKCE, token exchange, refresh
    oauth_server.py       # Client-facing OAuth: DCR, authorize, token endpoints
    client_registry.py    # In-memory DCR client store (RFC 7591)
    token_store.py        # Gateway token <-> SSO session mapping
    session.py            # Per-user session manager with pooled MCP clients
    gateway.py            # HTTP gateway: MCP proxy + Bearer auth + health
    http_client.py        # MCP HTTP client with Bearer auth + SSE
  config.example.json     # Example configuration
  requirements.txt        # Python dependencies (aiohttp, pydantic, python-dotenv)
  .gitignore
  README.md
```

## Developer Credits

Built by **[Rigin Oommen](https://github.com/riginoommen)** with assistance from **Cursor AI (Claude)**.

Contact: [riginoommen@gmail.com](mailto:riginoommen@gmail.com)

## Contributing

Contributions are welcome! If you'd like to help improve MCP DCR Proxy, here's how you can get involved:

1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/my-feature`)
3. **Commit** your changes (`git commit -m "Add my feature"`)
4. **Push** to your branch (`git push origin feature/my-feature`)
5. **Open** a Pull Request

### Ways to Contribute

- Report bugs or suggest features via [GitHub Issues](https://github.com/riginoommen/dcr-proxy/issues)
- Improve documentation
- Add support for additional SSO providers
- Write tests
- Review open Pull Requests

For questions or discussions, reach out at [riginoommen@gmail.com](mailto:riginoommen@gmail.com).

## License

Copyright 2026
Rigin Oommen

Licensed under the [Apache License, Version 2.0](LICENSE.md). You may not use this project except in compliance with the License. See the [LICENSE.md](LICENSE.md) file for details.
