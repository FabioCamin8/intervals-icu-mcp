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

## Container liveness check

The image healthcheck runs `python -m intervals_icu_mcp.healthcheck` every
30 seconds, with a 3-second hard timeout, a 5-second start period, and 3
retries. The probe has a 2-second timeout by default and uses only Python's
standard library. It reads Linux `/proc/1/cmdline` and recognizes the image's
Python module entrypoint, `python -m intervals_icu_mcp.server` (including an
absolute Python executable path). An init wrapper such as Docker `--init`, a
different entrypoint, unreadable process information, or an unknown process
layout fails the check; the probe does not guess.

For a recognized stdio server, the probe performs an import-only check and
makes no network connection. For `http`, `streamable-http`, and `sse`, it
uses the server's configured host and port, mapping wildcard binds `0.0.0.0`
and `::` to loopback addresses `127.0.0.1` and `::1`, then sends exactly
`GET /health`. The root health route is independent of MCP's configured
`--path`. The probe requires HTTP 200 and the exact JSON object
`{"status":"ok"}`, with a maximum response size of 4096 bytes. It does not
follow redirects or use HTTPS, proxies, or a configurable URL.

This is a liveness check: it confirms that the process answers its static
health route. It does not verify the MCP handshake or tool catalog, Intervals.icu
credentials, or upstream API availability.

The image default already checks HTTP transports. A Compose healthcheck
override is optional; use one when production needs a different probe timeout,
cadence, or startup period. Keep the existing server command and environment
configuration:

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

The production settings keep a 30-second interval, a 12-second hard timeout,
a 30-second start period, and 3 retries. The probe's `--timeout 10` bounds the
individual connect/read work through a cooperative deadline and socket
timeouts. Docker's 12-second hard timeout is the ultimate wall-time bound for
the whole check, including DNS resolution and slow HTTP headers. Do not
configure a separate probe endpoint or duplicate the MCP path in the
healthcheck: it always requests the root `/health` route.
For HTTP transports, the server CLI rejects MCP paths `/health` and `/health/`
because they collide with that route.
