# Web Terminal

Browser-based terminal served over WebSocket. Each connection spawns an
independent PTY-backed shell session.

## Architecture

```
Browser (xterm.js)  ←—WebSocket—→  server.py  ←—PTY—→  /bin/bash
```

- **`server.py`** — aiohttp server: serves the UI, handles WebSocket
  connections, spawns/manages PTY child processes, relays I/O as binary frames.
- **`static/index.html`** — Terminal frontend using xterm.js with the FitAddon
  (auto-resize via ResizeObserver) and CanvasAddon.
- **`static/ws-client.js`** / **`static/terminal-connection.js`** — Optional
  standalone WebSocket/connection helpers (not used by the main UI).

## Quick Start

```bash
pip install aiohttp
python server.py
# → http://localhost:8888
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 8888 | Listen port |
| `--host` | 0.0.0.0 | Bind address |
| `--shell` | /bin/bash | Shell to spawn per session |
| `--token` | *(disabled)* | Generate a random access token; printed to stdout on startup |
| `--max-connections` | 4 | Maximum concurrent sessions (0 = unlimited) |
| `--cert` | *(disabled)* | Path to TLS certificate file (enables HTTPS/WSS) |
| `--key` | *(auto)* | Path to TLS private key (defaults to cert path with `.key` extension) |

## Authentication

Without `--token`, anyone who can reach the port can connect (tokenless mode).

With `--token`, the server generates a random URL-safe token and prints it at
startup. Clients must pass it as a `?token=` query parameter on the WebSocket
URL. Connections without a valid token receive HTTP 403.

The web UI has a password-masked Token field for pasting the token before
connecting.

## TLS

To enable HTTPS/WSS, pass `--cert`:

```bash
python server.py --cert cert.pem --key key.pem
# → https://localhost:8888
```

If `--key` is omitted, the server looks for a file with the same name as the
cert but with a `.key` extension (e.g., `cert.pem` → `cert.key`).

To generate a self-signed certificate for local development:

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout cert.key -out cert.pem \
  -days 365 -subj '/CN=localhost'
```

Note: browsers will show a security warning for self-signed certs.

## Connection Limit

The server allows at most 4 concurrent sessions by default. Additional
connections receive HTTP 503. Configure with `--max-connections`:

```bash
python server.py --max-connections 8    # allow 8 sessions
python server.py --max-connections 0    # unlimited
```

## Resize Handling

The frontend uses a `ResizeObserver` on the terminal container. When the
container dimensions change (window resize, iframe resize, DevTools open, etc.):

1. `fitAddon.fit()` recalculates cols/rows from pixel dimensions
2. xterm.js `onResize` fires → client sends `{"cols": N, "rows": M}` over WS
3. Server calls `TIOCSWINSZ` on the PTY fd
4. Shell/application receives `SIGWINCH` and adapts

## Wire Protocol

See [PROTOCOL.md](PROTOCOL.md) for frame-level details.

**Client → Server (JSON text frames):**
- `{"data": "..."}` — terminal input
- `{"cols": N, "rows": M}` — resize

**Server → Client (binary frames):**
- Raw PTY output bytes (UTF-8)

## Sessions

Each WebSocket connection is an independent session with its own bash process.
Disconnecting kills the shell. There is no session persistence — use tmux/screen
inside the terminal if you need to survive disconnects.
