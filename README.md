# MCP HTTP Gateway with OAuth

A multi-client HTTP gateway for the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), with per-user OAuth Authorization Code + PKCE authentication against Red Hat SSO (Keycloak).

Multiple clients authenticate independently and can target **any** backend MCP server dynamically via a query parameter.

```
┌──────────┐                          ┌────────────────┐
│ Client A │──┐                  ┌──► │ MCP Server 1   │
└──────────┘  │  POST /mcp       │    └────────────────┘
              ├─ ?target=... ──► │
┌──────────┐  │                  │    ┌────────────────┐
│ Client B │──┘  ┌───────────┐  └──► │ MCP Server 2   │
└──────────┘     │  Gateway  │        └────────────────┘
                 │  :8080    │
                 └─────┬─────┘
                       │
              OAuth PKCE per user
                       │
                 ┌─────▼──────┐
                 │  Red Hat   │
                 │  SSO       │
                 └────────────┘
```

## Prerequisites

- **Python 3.10+**
- **A pre-registered OAuth client** in your Keycloak / Red Hat SSO realm (see [Keycloak Client Setup](#keycloak-client-setup))
- **One or more HTTP streamable MCP servers** that accept Bearer token authentication

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
| **Access Type**                | `public` (recommended with PKCE) or `confidential` |
| **Standard Flow Enabled**      | `ON` (Authorization Code)                      |
| **Direct Access Grants**       | `OFF`                                          |
| **Valid Redirect URIs**        | `http://127.0.0.1:8080/auth/callback`          |
| **Web Origins**                | `http://127.0.0.1:8080`                        |
| **PKCE Code Challenge Method** | `S256`                                         |
| **Scopes**                     | `openid` (plus any your MCP servers require)   |

Note: the redirect URI is now a **fixed path** (`/auth/callback`) on your gateway's host and port -- no more ephemeral ports.

## Configuration

The gateway loads configuration with priority: **CLI flags > environment variables > config file**.

### Config File

```bash
cp config.example.json config.json
# Edit config.json with your values
```

```json
{
  "host": "127.0.0.1",
  "port": 8080,
  "oauthIssuer": "https://sso.stage.redhat.com/auth/realms/redhat-external",
  "clientId": "your-client-id",
  "clientSecret": null,
  "scopes": ["openid"],
  "sessionTtlMinutes": 480,
  "allowedTargets": null,
  "logLevel": "info"
}
```

### Environment Variables

```bash
export HOST=127.0.0.1
export PORT=8080
export OAUTH_ISSUER=https://sso.stage.redhat.com/auth/realms/redhat-external
export CLIENT_ID=your-client-id
export SCOPES=openid
export SESSION_TTL_MINUTES=480
export LOG_LEVEL=info
```

### CLI Flags

```bash
python -m mcp_proxy \
  --host 127.0.0.1 \
  --port 8080 \
  --client-id your-client-id \
  --log-level info
```

### All Configuration Options

| Config Key          | Env Var              | CLI Flag            | Default | Description |
| ------------------- | -------------------- | ------------------- | ------- | ----------- |
| `host`              | `HOST`               | `--host`            | `127.0.0.1` | Bind address |
| `port`              | `PORT`               | `--port`            | `8080` | Listen port |
| `oauthIssuer`       | `OAUTH_ISSUER`       | `--oauth-issuer`    | `https://sso.stage.redhat.com/auth/realms/redhat-external` | OIDC issuer |
| `clientId`          | `CLIENT_ID`          | `--client-id`       | *required* | OAuth client ID |
| `clientSecret`      | `CLIENT_SECRET`      | `--client-secret`   | `null` | Client secret (omit for PKCE) |
| `scopes`            | `SCOPES`             | `--scopes`          | `["openid"]` | OAuth scopes |
| `sessionTtlMinutes` | `SESSION_TTL_MINUTES`| `--session-ttl`     | `480` | Session idle timeout (minutes) |
| `allowedTargets`    | `ALLOWED_TARGETS`    | `--allowed-targets` | `null` | Whitelist of MCP server URLs |
| `logLevel`          | `LOG_LEVEL`          | `--log-level`       | `info` | Logging level |

## Running

```bash
source .venv/bin/activate
PYTHONPATH=src python -m mcp_proxy --config config.json
```

Output:

```
MCP Gateway running on http://127.0.0.1:8080
  Login:  http://127.0.0.1:8080/auth/login
  Health: http://127.0.0.1:8080/health
  MCP:    POST http://127.0.0.1:8080/mcp?target=<mcp-server-url>
```

## Usage

### Step 1: Authenticate

Open your browser and visit:

```
http://127.0.0.1:8080/auth/login
```

This redirects you to Red Hat SSO for login. After authenticating, you get a session cookie (`mcp_session`) and see "Authentication successful!".

### Step 2: Make MCP Requests

Send JSON-RPC to any backend MCP server through the gateway:

```bash
curl -b cookies.txt -c cookies.txt \
  'http://127.0.0.1:8080/mcp?target=https://mcp-server-1.example.com/mcp' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

The `target` query parameter specifies which MCP server to forward to. The gateway attaches your OAuth Bearer token automatically.

### Step 3: Hit a Different MCP Server (Same Session)

```bash
curl -b cookies.txt \
  'http://127.0.0.1:8080/mcp?target=https://mcp-server-2.example.com/mcp' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

One login, any number of backend MCP servers.

### SSE Stream (Server-Initiated Messages)

```bash
curl -b cookies.txt -N \
  'http://127.0.0.1:8080/mcp?target=https://mcp-server.example.com/mcp'
```

GET requests are passed through as SSE streams.

## API Reference

| Endpoint          | Method | Auth Required | Description |
| ----------------- | ------ | ------------- | ----------- |
| `/auth/login`     | GET    | No            | Initiates SSO login, redirects to Keycloak |
| `/auth/callback`  | GET    | Cookie        | OAuth callback from SSO, sets session |
| `/mcp?target=URL` | POST   | Cookie        | Forward JSON-RPC to target MCP server |
| `/mcp?target=URL` | GET    | Cookie        | SSE stream passthrough from target |
| `/health`         | GET    | No            | Returns `{"status": "ok"}` |

## Security

### Target Whitelist

By default, clients can target any URL. To restrict which MCP servers are reachable, set `allowedTargets`:

```json
{
  "allowedTargets": [
    "https://mcp-server-1.example.com/mcp",
    "https://mcp-server-2.example.com/mcp"
  ]
}
```

Requests to unlisted targets will get a `403 Forbidden`.

### Session Isolation

Each client gets an independent OAuth session. Client A cannot use Client B's tokens. Sessions are automatically pruned after the configured idle timeout.

## Troubleshooting

### "unauthorized" response on /mcp

You need to authenticate first. Visit `http://127.0.0.1:8080/auth/login` in your browser.

### Session expired

If your session was idle longer than `sessionTtlMinutes` (default 8 hours) or the SSO refresh token expired, re-authenticate via `/auth/login`.

### "target not in allowed list"

The `allowedTargets` whitelist is blocking the URL. Either add the target URL to the whitelist or set `allowedTargets` to `null` to allow any target.

### Keycloak redirect URI mismatch

Make sure your Keycloak client's Valid Redirect URIs includes `http://127.0.0.1:8080/auth/callback` (matching your gateway's host and port exactly).

### Debugging

```bash
PYTHONPATH=src python -m mcp_proxy --config config.json --log-level debug
```

All logs go to **stderr**.

## Project Structure

```
dcr-proxy/
  src/mcp_proxy/
    __init__.py          # Package metadata
    __main__.py          # python -m entry point
    cli.py               # Argument parsing, config loading, main()
    config.py            # Pydantic GatewayConfig model
    oauth.py             # OAuth Auth Code + PKCE flow manager
    session.py           # Per-user session manager
    gateway.py           # HTTP gateway server (aiohttp)
    http_client.py       # HTTP MCP client with Bearer auth & SSE
  config.example.json    # Example configuration
  requirements.txt       # Python dependencies
  .gitignore
  README.md
```

## License

MIT
