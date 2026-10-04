# Remote Deployment (HTTP / SSE)

How to run the server over HTTP/SSE for remote or hosted use, the available transport flags, and the security model you must apply before exposing it.

By default the server runs over **stdio** — the right transport for local clients like Claude Desktop, Claude Code, and Cursor. For remote deployment (hosted MCP, reverse proxy, Docker-on-a-server, ChatGPT connector), pass `--transport`:

```bash
# Streamable HTTP (recommended — used by ChatGPT and modern remote clients)
intervals-icu-mcp --transport http --host 127.0.0.1 --port 8000

# Legacy SSE (for clients that haven't moved to streamable HTTP yet)
intervals-icu-mcp --transport sse --host 127.0.0.1 --port 8000
```

| Flag | Default | Description |
|---|---|---|
| `--transport` | `stdio` | One of `stdio`, `http`, `sse`, `streamable-http` |
| `--host` | `127.0.0.1` | Interface to bind. Use `0.0.0.0` only inside a container where Docker controls the exposure. |
| `--port` | `8000` | TCP port |
| `--path` | (framework default) | URL path to mount the server under |

> ⚠️ **Security: do not expose an HTTP-mode server to untrusted networks.**
>
> The MCP protocol has **no built-in authentication**. Anyone who can reach the URL can exercise every tool with your credentials — read every activity, delete activities, modify your FTP, create calendar events, etc. Binding to `0.0.0.0` on a direct-exposed host (VPS, LAN with open port) is equivalent to publishing your Intervals.icu API key.
>
> For remote access, prefer one of the following:
> - **Tailscale / Cloudflare Tunnel / ZeroTier** — only your authenticated devices can reach the endpoint. Zero code changes, simplest option.
> - **Reverse proxy with auth** (nginx + basic auth, Cloudflare Access, etc.) — terminates TLS and gates access.
> - **SSH tunnel** — `ssh -L 8000:localhost:8000 host` if you just need occasional access from one machine.
>
> Credentials are always read from `INTERVALS_ICU_API_KEY` and `INTERVALS_ICU_ATHLETE_ID` — use env vars (not a committed `.env`) when deploying to a shared host.

## Lightweight readiness check for Streamable HTTP

For HTTP containers, override the image's import-only healthcheck with the
optional dependency-free MCP probe:

```yaml
services:
  intervals-icu-mcp:
    image: ghcr.io/hhopke/intervals-icu-mcp:latest
    command: ["--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
    # Supply credentials using your existing environment/secret configuration.
    healthcheck:
      test: ["CMD", "python", "-m", "intervals_icu_mcp.healthcheck", "--timeout", "10"]
      interval: 30s
      timeout: 12s
      start_period: 30s
      retries: 3
```

The probe initializes MCP, sends the initialized notification, and reads the complete tool
catalog, requiring `icu_get_athlete_profile`. It never calls a tool or the Intervals.icu
API. Unlike launching a FastMCP client for every check, it imports only Python's
standard library. JSON and SSE responses, optional sessions, and catalog
pagination are supported. Sessions are terminated on a best-effort basis.

Use `--url http://127.0.0.1:8000/custom-path` when overriding the MCP path or port.
The default is `http://127.0.0.1:8000/mcp`. Use a direct container-local endpoint;
the probe does not support authenticated proxies, URL credentials, redirects,
or the legacy SSE transport. HTTPS uses the system certificate trust store.
Failure exits nonzero without printing endpoint details or response content.

The probe's deadline covers the handshake and catalog traversal. Keep Docker's
hard timeout longer than `--timeout`; the hard timeout also bounds DNS resolution
and slow HTTP headers. Each response is limited to 1 MiB. Readiness validates the JSON-RPC envelope, initialization fields, and known MCP
Tool fields, including optional titles/descriptions, annotations, icons, output
schemas, `_meta`, and execution properties. Optional fields may be omitted or
null; unknown extension fields are allowed. Values must use the protocol's JSON
types: strings/numbers are not coerced into booleans. Input/output schemas and
`_meta` are checked as objects, as in the SDK; their contents are not evaluated
as JSON Schema. Readiness does not demonstrate upstream API credential validity.
Preserve your existing cadence, retry threshold, and recovery policy when
replacing a probe. The default image healthcheck is unchanged for stdio users.
