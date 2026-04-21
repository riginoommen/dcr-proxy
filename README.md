# MCP DCR Proxy

An OAuth gateway with **Dynamic Client Registration (RFC 7591)** for the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). Enables any MCP client to connect to your MCP servers through Red Hat SSO authentication -- no DCR support needed on the SSO side.

```
┌──────────┐                    ┌────────────┐                    ┌────────────┐
│  Cursor  │                    │            │                    │            │
│  Claude  │── MCP + Bearer ──► │ DCR Proxy  │── Bearer (SSO) ──►│ MCP Server │
│Inspector │   (gateway token)  │   :8080    │   (real token)    │   (HTTP)   │
└──────────┘                    └─────┬──────┘                    └────────────┘
     │                                │
     │  DCR + OAuth PKCE              │  OAuth PKCE
     │  (auto-discovered)             │  (pre-registered client)
     │                                │
     │                          ┌─────▼──────┐
     └─── browser SSO login ──►│  Red Hat   │
                                │  SSO       │
                                └────────────┘
```

## How It Works

1. MCP client connects to `http://proxy:8080/mcp`
2. Proxy returns `401` with `WWW-Authenticate` header pointing to OAuth metadata
3. Client auto-discovers `/.well-known/oauth-authorization-server`
4. Client registers via **DCR** at `POST /oauth/register` (gets a `client_id`)
5. Client starts **Authorization Code + PKCE** flow at `/oauth/authorize`
6. Proxy redirects user's browser to **Red Hat SSO** for login
7. After SSO login, proxy issues a **gateway access token** to the client
8. Client uses the gateway token for all MCP requests (`Authorization: Bearer <token>`)
9. Proxy forwards requests to the backend MCP server using the **real SSO token**

## Client Setup

### Cursor

```json
{
  "mcpServers": {
    "security-mcp": {
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

### Claude Desktop

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

1. Run: `npx @modelcontextprotocol/inspector`
2. Select **Streamable HTTP** transport
3. Enter URL: `http://127.0.0.1:8080/mcp`
4. Click **Connect**

### Targeting a Different MCP Server

Pass `?target=` to reach any backend:

```
http://127.0.0.1:8080/mcp?target=https://other-mcp-server.example.com/mcp
```

Without `?target=`, the proxy uses the `defaultTarget` from config.

## Prerequisites

- **Python 3.10+**
- **A pre-registered OAuth client** in Red Hat SSO (see [Keycloak Client Setup](#keycloak-client-setup))
- **One or more HTTP MCP servers** that accept Bearer token authentication

## Installation

```bash
cd /path/to/dcr-proxy
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Keycloak Client Setup

Your SSO team needs to create a client with these settings:

| Setting                        | Value                                          |
| ------------------------------ | ---------------------------------------------- |
| **Client Protocol**            | `openid-connect`                               |
| **Client ID**                  | e.g. `mcp-gateway-client`                      |
| **Access Type**                | `public` (recommended with PKCE)               |
| **Standard Flow Enabled**      | `ON`                                           |
| **Direct Access Grants**       | `OFF`                                          |
| **Valid Redirect URIs**        | `http://127.0.0.1:8080/oauth/callback`         |
| **Web Origins**                | `http://127.0.0.1:8080`                        |
| **PKCE Code Challenge Method** | `S256`                                         |

## Configuration

```bash
cp config.example.json config.json
# Edit with your values
```

```json
{
  "host": "127.0.0.1",
  "port": 8080,
  "oauthIssuer": "https://sso.stage.redhat.com/auth/realms/redhat-external",
  "clientId": "your-client-id",
  "scopes": ["openid"],
  "defaultTarget": "https://your-mcp-server.example.com/mcp",
  "sessionTtlMinutes": 480,
  "allowedTargets": null,
  "logLevel": "info"
}
```

| Config Key          | Env Var              | CLI Flag            | Default   | Description |
| ------------------- | -------------------- | ------------------- | --------- | ----------- |
| `host`              | `HOST`               | `--host`            | `127.0.0.1` | Bind address |
| `port`              | `PORT`               | `--port`            | `8080`    | Listen port |
| `oauthIssuer`       | `OAUTH_ISSUER`       | `--oauth-issuer`    | RH SSO staging | OIDC issuer |
| `clientId`          | `CLIENT_ID`          | `--client-id`       | *required* | SSO client ID |
| `clientSecret`      | `CLIENT_SECRET`      | `--client-secret`   | `null`    | SSO client secret |
| `scopes`            | `SCOPES`             | `--scopes`          | `["openid"]` | OAuth scopes |
| `defaultTarget`     | `DEFAULT_TARGET`     | `--default-target`  | `null`    | Default MCP server URL |
| `sessionTtlMinutes` | `SESSION_TTL_MINUTES`| `--session-ttl`     | `480`     | Session idle timeout |
| `allowedTargets`    | `ALLOWED_TARGETS`    | `--allowed-targets` | `null`    | Target whitelist |
| `logLevel`          | `LOG_LEVEL`          | `--log-level`       | `info`    | Logging level |

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

## API Reference

### Discovery Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/.well-known/oauth-protected-resource` | GET | RFC 9728 -- resource metadata with authorization server URL |
| `/.well-known/oauth-authorization-server` | GET | RFC 8414 -- OAuth metadata with all endpoints |

### OAuth Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/oauth/register` | POST | DCR (RFC 7591) -- register a new client |
| `/oauth/authorize` | GET | Start Authorization Code + PKCE flow |
| `/oauth/callback` | GET | Internal SSO callback |
| `/oauth/token` | POST | Exchange auth code for access token |

### MCP Endpoints

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/mcp` | POST | Bearer | Forward JSON-RPC to target MCP server |
| `/mcp` | GET | Bearer | SSE stream passthrough |
| `/mcp?target=URL` | POST/GET | Bearer | Target a specific MCP server |
| `/health` | GET | None | Health check |

## Security

### Target Whitelist

Restrict reachable backends:

```json
{
  "allowedTargets": [
    "https://mcp-server-1.example.com/mcp",
    "https://mcp-server-2.example.com/mcp"
  ]
}
```

### Token Isolation

Each MCP client gets its own DCR registration, its own OAuth session, and its own gateway token. Gateway tokens map to SSO tokens internally -- clients never see the real SSO tokens.

## Troubleshooting

### MCP client gets 401

The client needs to go through the OAuth flow first. Most MCP clients handle this automatically via the `WWW-Authenticate` header and OAuth metadata discovery.

### "redirect_uri not registered"

The MCP client's redirect_uri must match one of the URIs it registered via DCR. Check your client's DCR registration.

### SSO redirect URI mismatch

Your Keycloak client needs `http://127.0.0.1:8080/oauth/callback` as a valid redirect URI.

### Debugging

```bash
PYTHONPATH=src python -m mcp_proxy --config config.json --log-level debug
```

## Project Structure

```
dcr-proxy/
  src/mcp_proxy/
    __init__.py
    __main__.py
    cli.py                # CLI entry point
    config.py             # GatewayConfig model
    oauth.py              # SSO-facing OAuth (PKCE, token exchange)
    oauth_server.py       # Client-facing OAuth server (DCR, authorize, token)
    client_registry.py    # DCR client store (RFC 7591)
    token_store.py        # Gateway token <-> SSO session mapping
    session.py            # Per-user session manager
    gateway.py            # HTTP gateway server
    http_client.py        # MCP HTTP client with Bearer auth
  config.example.json
  requirements.txt
  .gitignore
  README.md
```

## License

MIT
