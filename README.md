# MCP Proxy with OAuth (Pre-Registered Client)

A stdio-to-HTTP protocol translation proxy for the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), with OAuth Authorization Code + PKCE authentication against Red Hat SSO (Keycloak).

This proxy sits between an MCP client that speaks **stdio** (e.g. Cursor) and an MCP server that speaks **HTTP streamable transport**, handling authentication transparently.

```
┌──────────┐  stdio   ┌───────────┐  HTTPS + Bearer  ┌────────────┐
│  Cursor  │ ──────── │ dcr-proxy │ ────────────────► │ MCP Server │
│  (IDE)   │  JSON-RPC│           │    JSON-RPC       │   (HTTP)   │
└──────────┘          └─────┬─────┘                   └────────────┘
                            │
                   OAuth PKCE flow
                            │
                      ┌─────▼──────┐
                      │  Red Hat   │
                      │  SSO       │
                      │  (staging) │
                      └────────────┘
```

## Prerequisites

- **Python 3.10+**
- **A pre-registered OAuth client** in your Keycloak / Red Hat SSO realm (see [Keycloak Client Setup](#keycloak-client-setup) below)
- **An HTTP streamable MCP server** that accepts Bearer token authentication
- **Cursor** (or any MCP client that launches stdio subprocesses)

## Installation

```bash
# Clone and install
cd /path/to/dcr-proxy
python -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Keycloak Client Setup

Since Dynamic Client Registration (DCR) is not enabled, you need your SSO team to create a client manually. Provide them with the following specification:

| Setting                        | Value                                          |
| ------------------------------ | ---------------------------------------------- |
| **Client Protocol**            | `openid-connect`                               |
| **Client ID**                  | e.g. `mcp-proxy-client`                        |
| **Access Type**                | `public` (recommended with PKCE) or `confidential` |
| **Standard Flow Enabled**      | `ON` (Authorization Code)                      |
| **Direct Access Grants**       | `OFF`                                          |
| **Valid Redirect URIs**        | `http://127.0.0.1:*`                           |
| **Web Origins**                | `http://127.0.0.1`                             |
| **PKCE Code Challenge Method** | `S256`                                         |
| **Scopes**                     | `openid` (plus any scopes your MCP server requires) |

### Why These Settings?

- **Public client + PKCE**: No client secret is stored on disk. The PKCE S256 challenge secures the flow without needing a shared secret.
- **Redirect to 127.0.0.1**: The proxy starts a temporary local HTTP server to catch the OAuth callback. The wildcard port (`*`) allows any ephemeral port.
- **Standard Flow only**: This is the Authorization Code grant, which is the most secure browser-based flow.

## Configuration

The proxy loads configuration with this priority: **CLI flags > environment variables > config file**.

### Option A: Config File

Copy and edit the example:

```bash
cp config.example.json config.json
```

```json
{
  "mcpServerUrl": "https://your-mcp-server.example.com/mcp",
  "oauthIssuer": "https://sso.stage.redhat.com/auth/realms/redhat-external",
  "clientId": "mcp-proxy-client",
  "clientSecret": null,
  "scopes": ["openid"],
  "redirectPort": 0,
  "tokenCachePath": ".tokens.json",
  "logLevel": "info"
}
```

### Option B: Environment Variables

```bash
export MCP_SERVER_URL=https://your-mcp-server.example.com/mcp
export OAUTH_ISSUER=https://sso.stage.redhat.com/auth/realms/redhat-external
export CLIENT_ID=mcp-proxy-client
export SCOPES=openid
export LOG_LEVEL=info
```

### Option C: CLI Flags

```bash
python -m mcp_proxy \
  --mcp-server-url https://your-mcp-server.example.com/mcp \
  --oauth-issuer https://sso.stage.redhat.com/auth/realms/redhat-external \
  --client-id mcp-proxy-client \
  --scopes openid \
  --log-level info
```

### All Configuration Options

| Config Key       | Env Var            | CLI Flag             | Default | Description |
| ---------------- | ------------------ | -------------------- | ------- | ----------- |
| `mcpServerUrl`   | `MCP_SERVER_URL`   | `--mcp-server-url`   | *required* | Target MCP server URL |
| `oauthIssuer`    | `OAUTH_ISSUER`     | `--oauth-issuer`     | `https://sso.stage.redhat.com/auth/realms/redhat-external` | OIDC issuer URL |
| `clientId`       | `CLIENT_ID`        | `--client-id`        | *required* | Pre-registered OAuth client ID |
| `clientSecret`   | `CLIENT_SECRET`    | `--client-secret`    | `null` | Client secret (omit for public PKCE clients) |
| `scopes`         | `SCOPES`           | `--scopes`           | `["openid"]` | OAuth scopes (comma-separated in env) |
| `redirectPort`   | `REDIRECT_PORT`    | `--redirect-port`    | `0` | Local callback port (`0` = auto-select) |
| `tokenCachePath` | `TOKEN_CACHE_PATH` | `--token-cache-path` | `null` | Path to persist tokens to disk |
| `logLevel`       | `LOG_LEVEL`        | `--log-level`        | `info` | `debug`, `info`, `warning`, `error` |

## Running

### Standalone

```bash
# Activate your virtualenv first
source .venv/bin/activate

# With config file
PYTHONPATH=src python -m mcp_proxy --config config.json

# With CLI flags
PYTHONPATH=src python -m mcp_proxy \
  --mcp-server-url https://your-server.com/mcp \
  --client-id mcp-proxy-client
```

On the first run, a browser window will open for Red Hat SSO login. After authenticating, the proxy is ready and will forward MCP traffic.

### In Cursor

Add the proxy to your Cursor MCP configuration. Edit `.cursor/mcp.json` in your project (or the global Cursor MCP config):

```json
{
  "mcpServers": {
    "my-mcp-server": {
      "command": "python",
      "args": [
        "-m",
        "mcp_proxy",
        "--config",
        "/absolute/path/to/dcr-proxy/config.json"
      ],
      "env": {
        "PYTHONPATH": "/absolute/path/to/dcr-proxy/src"
      }
    }
  }
}
```

Alternatively, if you have a virtualenv:

```json
{
  "mcpServers": {
    "my-mcp-server": {
      "command": "/absolute/path/to/dcr-proxy/.venv/bin/python",
      "args": [
        "-m",
        "mcp_proxy",
        "--config",
        "/absolute/path/to/dcr-proxy/config.json"
      ],
      "env": {
        "PYTHONPATH": "/absolute/path/to/dcr-proxy/src"
      }
    }
  }
}
```

When Cursor starts the MCP server, a browser window will open for SSO login. After authenticating, the connection is established and Cursor can use the MCP tools.

If you set `tokenCachePath` in your config, subsequent restarts will use the cached token (or refresh it silently) without opening the browser again.

## How It Works

1. **Cursor** launches the proxy as a stdio subprocess.
2. **Proxy** starts and performs OIDC discovery against the SSO issuer to learn endpoints.
3. On the first MCP request (or at startup), the proxy initiates the **Authorization Code + PKCE** flow:
   - Generates a cryptographic `code_verifier` and its S256 `code_challenge`.
   - Starts a temporary HTTP server on `127.0.0.1` for the callback.
   - Opens your browser to the SSO login page.
4. After you authenticate, SSO redirects to the local callback with an **authorization code**.
5. The proxy exchanges the code (with the PKCE verifier) for an **access token** and **refresh token**.
6. All subsequent MCP JSON-RPC messages from stdin are forwarded to the HTTP MCP server with an `Authorization: Bearer` header.
7. Responses (including SSE-streamed responses) are forwarded back to stdout.
8. When the access token expires, the proxy silently uses the refresh token to obtain a new one.

## Troubleshooting

### Browser doesn't open

If the browser doesn't open automatically, the proxy prints the authorization URL to stderr. Copy and paste it into your browser manually.

### "Configuration error: ... mcp_server_url"

The `mcpServerUrl` / `MCP_SERVER_URL` / `--mcp-server-url` is required. Make sure it's set in your config file, environment, or CLI flags.

### "Configuration error: ... client_id"

The `clientId` / `CLIENT_ID` / `--client-id` is required. This is the OAuth client ID your SSO team registered for you.

### 401 from MCP server after token refresh

The refresh token may have expired (Keycloak default is 30 minutes for SSO session idle timeout). The proxy will fall back to a full re-authentication flow. If this happens frequently, ask your SSO team to increase the session timeout for your client.

### Port conflict on callback server

By default, the proxy auto-selects an available port (`redirectPort: 0`). If you need a fixed port, set `redirectPort` to a specific value and ensure the Keycloak client's redirect URIs allow it.

### Token cache file permissions

If using `tokenCachePath`, ensure the file is only readable by your user:

```bash
chmod 600 .tokens.json
```

The token cache contains sensitive credentials. The file is listed in `.gitignore` to prevent accidental commits.

### Debugging

Set `logLevel` to `debug` for verbose output:

```bash
PYTHONPATH=src python -m mcp_proxy --config config.json --log-level debug
```

All logs go to **stderr** so they never interfere with the MCP JSON-RPC stream on stdout.

## Project Structure

```
dcr-proxy/
  src/mcp_proxy/
    __init__.py          # Package metadata
    __main__.py          # python -m entry point
    cli.py               # Argument parsing, config loading, main()
    config.py            # Pydantic config model, env/file/CLI loading
    oauth.py             # OAuth Auth Code + PKCE flow manager
    stdio_handler.py     # Async stdin/stdout JSON-RPC handler
    http_client.py       # HTTP MCP client with Bearer auth & SSE
    proxy.py             # Main orchestrator wiring everything together
  config.example.json    # Example configuration
  mcp.example.json       # Example Cursor MCP server config
  requirements.txt       # Python dependencies
  .gitignore
  README.md
```

## License

MIT
