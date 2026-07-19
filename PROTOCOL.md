# Web Terminal WebSocket Protocol

## Endpoint

```
wss://<host>/ws
```

## Authentication

Query parameters on the WebSocket URL:
- `token` — access token (optional, required when server started with `--token`)

## Message Format (JSON over WebSocket)

### Client → Server

**Initial handshake (sent immediately on connect):**
```json
{"cols": 80, "rows": 24}
```

**User input:**
```json
{"data": "ls -la\r"}
```

**Terminal resize:**
```json
{"cols": 120, "rows": 40}
```

**Acknowledgement (when server requests it):**
```json
{"ack": true}
```

### Server → Client

**Terminal output:**
```json
{"data": "some output text", "ack": false}
```

- `data` — terminal output (string or binary as ArrayBuffer)
- `ack` — if true, client must send `{"ack": true}` back (flow control)

## Connection Types

### Direct
URL-based connection with optional token:
```
wss://<host>/ws?token=<token>
```

### Proxy
Proxy-based connection with token:
```
wss://<proxy-url>/ws?token=<token>
```

## Terminal Options (xterm.js)

```javascript
{
  macOptionIsMeta: true,
  scrollback: 1000,
  screenReaderMode: true
}
```
