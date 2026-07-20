"""
Web Terminal Server
Serves a browser-based terminal on port 8888.

- GET /          → terminal UI (static/index.html)
- GET /static/*  → static assets
- WS  /ws        → PTY-backed terminal WebSocket (binary frames)

Usage:
    python server.py [--port 8888] [--shell /bin/bash]
"""

import asyncio
import json
import os
import secrets
import signal
import socket
import ssl
import struct
import sys
import argparse
import warnings
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

if not IS_WINDOWS:
    import pty
    import select
    import fcntl
    import termios

try:
    from aiohttp import web
    from aiohttp.web_runner import GracefulExit
except ImportError:
    raise SystemExit("Install aiohttp: pip install aiohttp")

if IS_WINDOWS:
    try:
        import winpty
    except ImportError:
        winpty = None

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

OUTPUT_FLUSH_MS = 8
READ_BUFSIZE = 65536


def set_pty_size(fd, cols, rows):
    if IS_WINDOWS:
        return
    size = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


_active_processes: list["PtyProcess"] = []


class PtyProcess:
    def __init__(self, shell="/bin/bash", cols=80, rows=24):
        self.shell = shell
        self.cols = cols
        self.rows = rows
        self.pid = None
        self.fd = None

    def spawn(self):
        pid, fd = pty.openpty()
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(self.cols)
        env["LINES"] = str(self.rows)

        # fork() in a multi-threaded process warns on Python 3.12+, but it's
        # safe here: the child calls execvpe() immediately, never running
        # Python code that could deadlock on inherited locks.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            child_pid = os.fork()
        if child_pid == 0:
            os.close(pid)
            os.setsid()
            fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
            os.dup2(fd, 0)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            if fd > 2:
                os.close(fd)
            os.execvpe(self.shell, [self.shell], env)
        else:
            os.close(fd)
            self.pid = child_pid
            self.fd = pid
            set_pty_size(self.fd, self.cols, self.rows)
            _active_processes.append(self)
            return self

    def resize(self, cols, rows):
        self.cols = cols
        self.rows = rows
        if self.fd is not None:
            set_pty_size(self.fd, cols, rows)

    def read_nonblock(self):
        """Read all available data from PTY without blocking.
        Returns raw bytes, preserving UTF-8 boundaries."""
        if self.fd is None:
            return b""
        chunks = []
        while True:
            r, _, _ = select.select([self.fd], [], [], 0)
            if not r:
                break
            try:
                chunk = os.read(self.fd, READ_BUFSIZE)
                if chunk:
                    chunks.append(chunk)
                else:
                    break
            except OSError:
                break
        return b"".join(chunks)

    def read_wait(self, timeout=0.01):
        """Wait up to timeout for data, then read all available."""
        if self.fd is None:
            return b""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        return self.read_nonblock()

    def write(self, data):
        if self.fd is not None:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            os.write(self.fd, raw)

    def kill(self):
        if self in _active_processes:
            _active_processes.remove(self)
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except (OSError, ChildProcessError):
                pass
            self.pid = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def is_alive(self):
        if self.pid is None:
            return False
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid != 0:
                self.pid = None
                return False
            return True
        except ChildProcessError:
            self.pid = None
            return False


class WindowsPtyProcess:
    """PTY process for Windows using pywinpty."""

    def __init__(self, shell="cmd.exe", cols=80, rows=24):
        self.shell = shell
        self.cols = cols
        self.rows = rows
        self.process = None

    def spawn(self):
        if winpty is None:
            raise RuntimeError("pywinpty is required on Windows: pip install pywinpty")
        self.process = winpty.PtyProcess.spawn(
            self.shell, dimensions=(self.rows, self.cols)
        )
        _active_processes.append(self)
        return self

    def resize(self, cols, rows):
        self.cols = cols
        self.rows = rows
        if self.process:
            self.process.setwinsize(rows, cols)

    def read_nonblock(self):
        if not self.process:
            return b""
        try:
            if not self.process.isalive():
                return b""
            data = self.process.read(READ_BUFSIZE)
            return data.encode("utf-8") if isinstance(data, str) else data
        except (EOFError, OSError, Exception):
            return b""

    def read_wait(self, timeout=0.01):
        if not self.process:
            return b""
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if not self.process.isalive():
                    return b""
                data = self.process.read(READ_BUFSIZE)
                if data:
                    return data.encode("utf-8") if isinstance(data, str) else data
            except (EOFError, OSError, Exception):
                return b""
            time.sleep(0.005)
        return b""

    def write(self, data):
        if not self.process:
            return
        try:
            text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
            self.process.write(text)
        except (EOFError, OSError, Exception):
            pass

    def kill(self):
        if self in _active_processes:
            _active_processes.remove(self)
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
            self.process = None

    def is_alive(self):
        if self.process is None:
            return False
        try:
            return self.process.isalive()
        except (EOFError, OSError, Exception):
            return False


def _safe_utf8_split(buf: bytes) -> tuple[bytes, bytes]:
    """Split buf into (complete_utf8, leftover_incomplete_tail).
    Ensures we never send a partial multi-byte UTF-8 character."""
    if not buf:
        return b"", b""
    # Check if the last few bytes are an incomplete UTF-8 sequence.
    # UTF-8 continuation bytes start with 10xxxxxx (0x80-0xBF).
    # A leading byte tells us how many bytes the character needs.
    tail = 0
    for i in range(min(4, len(buf)), 0, -1):
        b = buf[-i]
        if b < 0x80:
            break
        elif b >= 0xC0:
            # This is a leading byte — figure out expected length
            if b < 0xE0:
                expected = 2
            elif b < 0xF0:
                expected = 3
            else:
                expected = 4
            available = i
            if available < expected:
                tail = i
            break
    if tail:
        return buf[:-tail], buf[-tail:]
    return buf, b""


async def websocket_handler(request):
    token = request.app.get("token")
    if token:
        client_token = request.query.get("token", "")
        if client_token != token:
            return web.Response(status=403, text="Invalid token")

    max_conn = request.app.get("max_connections")
    if max_conn and len(_active_processes) >= max_conn:
        return web.Response(status=503, text="Connection limit reached")

    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)

    shell = request.app["shell"]
    if IS_WINDOWS:
        proc = WindowsPtyProcess(shell=shell)
    else:
        proc = PtyProcess(shell=shell)
    proc.spawn()

    # Accumulator for incomplete UTF-8 at read boundaries
    utf8_remainder = bytearray()

    async def read_pty():
        nonlocal utf8_remainder
        loop = asyncio.get_event_loop()
        try:
            while proc.is_alive() and not ws.closed:
                data = await loop.run_in_executor(None, proc.read_wait)
                if not data:
                    continue

                # Prepend any leftover bytes from previous read
                if utf8_remainder:
                    data = bytes(utf8_remainder) + data
                    utf8_remainder.clear()

                # Split at UTF-8 boundary
                complete, remainder = _safe_utf8_split(data)
                if remainder:
                    utf8_remainder.extend(remainder)

                if complete:
                    # Send as binary WebSocket frame — xterm.js handles raw bytes
                    await ws.send_bytes(complete)
        except (asyncio.CancelledError, ConnectionResetError):
            pass

    reader_task = asyncio.create_task(read_pty())

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                if "data" in payload:
                    proc.write(payload["data"])
                elif "cols" in payload and "rows" in payload:
                    proc.resize(payload["cols"], payload["rows"])
            elif msg.type == web.WSMsgType.BINARY:
                # Raw input bytes from client
                proc.write(msg.data)
            elif msg.type == web.WSMsgType.ERROR:
                break
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass
        proc.kill()

    return ws


async def index_handler(request):
    return web.FileResponse(STATIC_DIR / "index.html")


def _cleanup_all():
    """Kill all active PTY child processes."""
    for proc in list(_active_processes):
        proc.kill()


async def _on_shutdown(app):
    _cleanup_all()


def create_app(shell="/bin/bash", token=None, max_connections=None):
    app = web.Application()
    app["shell"] = shell
    app["token"] = token
    app["max_connections"] = max_connections
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_static("/static", STATIC_DIR, show_index=False)
    app.on_shutdown.append(_on_shutdown)
    return app


def _get_interface_ips():
    """Return a list of non-loopback IPv4 addresses from network interfaces."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr and not addr.startswith("127."):
                ips.append(addr)
    except (socket.gaierror, OSError):
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            addr = s.getsockname()[0]
            s.close()
            if not addr.startswith("127."):
                ips.append(addr)
        except (OSError, socket.error):
            pass
    return list(dict.fromkeys(ips))


def main():
    default_shell = "cmd.exe" if IS_WINDOWS else "/bin/bash"
    parser = argparse.ArgumentParser(description="Web Terminal Server")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--shell", default=default_shell)
    parser.add_argument("--token", action="store_true",
                        help="Generate a random access token (required for WebSocket connections)")
    parser.add_argument("--max-connections", type=int, default=4,
                        help="Maximum concurrent sessions (default: 4, 0 = unlimited)")
    parser.add_argument("--cert", type=str, default=None,
                        help="Path to TLS certificate file (enables HTTPS)")
    parser.add_argument("--key", type=str, default=None,
                        help="Path to TLS private key file (defaults to cert path with .key extension)")
    args = parser.parse_args()

    token = None
    if args.token:
        token = secrets.token_urlsafe(16)

    max_connections = args.max_connections if args.max_connections > 0 else None

    ssl_ctx = None
    if args.cert:
        cert_path = Path(args.cert)
        key_path = Path(args.key) if args.key else cert_path.with_suffix(".key")
        if not cert_path.exists():
            raise SystemExit(f"Certificate not found: {cert_path}")
        if not key_path.exists():
            raise SystemExit(f"Key not found: {key_path}")
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(str(cert_path), str(key_path))

    app = create_app(shell=args.shell, token=token, max_connections=max_connections)
    proto = "https" if ssl_ctx else "http"

    token_qs = f"?token={token}" if token else ""

    print(f"Web Terminal Server")
    print(f"  Shell: {args.shell}")
    print(f"  Max connections: {max_connections or 'unlimited'}")
    print()
    print("Access URLs:")
    print(f"    {proto}://localhost:{args.port}/{token_qs}")
    if args.host in ("0.0.0.0", "::"):
        for ip in _get_interface_ips():
            print(f"    {proto}://{ip}:{args.port}/{token_qs}")
    else:
        print(f"    {proto}://{args.host}:{args.port}/{token_qs}")
    print()
    if token:
        print(f"Token: {token}")
    print("Press Ctrl+C to stop.")

    try:
        web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_ctx,
                    print=None, handle_signals=True, shutdown_timeout=2.0)
    except (KeyboardInterrupt, SystemExit, GracefulExit):
        pass
    finally:
        _cleanup_all()


if __name__ == "__main__":
    main()
