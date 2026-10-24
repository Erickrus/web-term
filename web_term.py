#!/usr/bin/env python3
"""
Browser terminal (xterm.js) over a WebSocket, exposed via Cloudflare Tunnel.

Serves:
  GET  /            — xterm.js web terminal page
  GET  /ws          — WebSocket endpoint bridged to a real PTY (a shell)

The shell is run inside `tmux` when available so a dropped/reconnected
WebSocket re-attaches to the SAME live session (scrollback + running
processes survive). Falls back to a plain shell otherwise.

Only the Python standard library is used (Linux/macOS). Point this at a
Cloudflare quick tunnel and open the URL from any browser, incl. an iPhone.

Auth:  visitors sign in at /login with the OS account password. The
username defaults to the current OS user; override with WEBTERM_USER.
"""

import base64
import hashlib
import http.server
import json
import os
import platform
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

try:
    import fcntl
    import termios
except ImportError:  # non-unix
    fcntl = None
    termios = None

PORT = 8788
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLOUDFLARED = os.path.join(SCRIPT_DIR, "cloudflared")
SHELL = os.environ.get("SHELL", "/bin/bash")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# --- Login credentials -------------------------------------------------------
# Login uses the OS account password: the entered password is verified against
# the real system credentials (macOS: dscl -authonly, Linux: PAM/su). The
# username defaults to the current OS user; override with WEBTERM_USER.
import getpass
USERNAME = os.environ.get("WEBTERM_USER", getpass.getuser())
# Secret used to sign session cookies; rotates each startup (restart = logout).
import hmac
import secrets
SESSION_SECRET = secrets.token_bytes(32)
SESSION_COOKIE = "webterm_session"


def make_session():
    """Return a signed opaque session token proving a valid login."""
    payload = secrets.token_urlsafe(16).encode()
    sig = hmac.new(SESSION_SECRET, payload, hashlib.sha256).hexdigest()[:32]
    return payload.decode() + "." + sig


def valid_session(token):
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    expected = hmac.new(SESSION_SECRET, payload.encode(),
                        hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def _os_authenticate(user, pw):
    """Verify (user, pw) against the OS account. Returns True/False."""
    system = platform.system().lower()
    if system == "darwin":
        # dscl . -authonly <user> <pass> exits 0 on success. Pass args as a
        # list (no shell) so the password is never interpolated into a string.
        try:
            r = subprocess.run(
                ["dscl", ".", "-authonly", user, pw],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False
    # Linux/other: try PAM if the python-pam module is available.
    try:
        import pam  # type: ignore
        return bool(pam.pam().authenticate(user, pw))
    except Exception:
        return False


def check_login(user, pw):
    # Username must match the allowed account, then verify against the OS.
    if not hmac.compare_digest(user or "", USERNAME):
        return False
    if not pw:
        return False
    return _os_authenticate(user, pw)
TMUX_SESSION = "web"

def get_cloudflared_url():
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
        return (
            "https://github.com/cloudflare/cloudflared/releases/latest/"
            f"download/cloudflared-linux-{arch}"
        )
    elif system == "darwin":
        return (
            "https://github.com/cloudflare/cloudflared/releases/latest/"
            "download/cloudflared-darwin-amd64.tgz"
        )
    else:
        sys.exit(f"Unsupported platform: {system}/{machine}")


def ensure_cloudflared():
    if os.path.isfile(CLOUDFLARED) and os.access(CLOUDFLARED, os.X_OK):
        return
    url = get_cloudflared_url()
    print(f"cloudflared not found, downloading from:\n  {url}")
    if url.endswith(".tgz"):
        import io
        import tarfile
        data = urllib.request.urlopen(url).read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extract("cloudflared", path=SCRIPT_DIR)
    else:
        urllib.request.urlretrieve(url, CLOUDFLARED)
    os.chmod(CLOUDFLARED, 0o755)
    print("cloudflared downloaded successfully.")


HTML_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0,
  maximum-scale=1.0, user-scalable=no" />
<title>web term</title>
<link rel="stylesheet"
  href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css" />
<style>
  html, body { margin: 0; height: 100%; background: #fff; overflow: hidden;
    font-family: -apple-system, monospace; -webkit-text-size-adjust: 100%; }
  /* Whole app is a fixed-height column pinned to the top of the visual
     viewport: terminal on top, then key bar, then status. --vh is set from
     JS to the visible height so nothing hides behind the keyboard. */
  #app { position: fixed; top: 0; left: 0; right: 0;
    height: var(--vh, 100vh); display: flex; flex-direction: column; }
  #term { flex: 1 1 auto; min-height: 0; padding: 2px;
    box-sizing: border-box; background: #fff; }
  #keys { flex: 0 0 auto; display: flex; flex-wrap: wrap; gap: 3px;
    padding: 3px; background: #f0f0f0; border-top: 1px solid #ccc; }
  #keys button { flex: 1 0 auto; min-width: 40px; min-height: 28px;
    font: 11px monospace; padding: 2px 4px; color: #111; background: #fff;
    border: 1px solid #bbb; border-radius: 4px; -webkit-user-select: none;
    user-select: none; touch-action: manipulation; }
  #keys button:active { background: #ddd; }
  #keys button.on { background: #0c67d6; color: #fff;
    border-color: #0c67d6; }
  #bar { flex: 0 0 auto; font: 11px monospace; color: #666;
    padding: 2px 8px; background: #f0f0f0; }
  /* File browser overlay: covers the whole fixed #app (terminal, keys, bar)
     without touching the flex layout, so the terminal is never resized and the
     PTY is never resized when toggling. */
  #files { position: absolute; inset: 0; z-index: 10; background: #fff;
    display: none; flex-direction: column; }
  #files.open { display: flex; }
  #fhead { flex: 0 0 auto; display: flex; align-items: center; gap: 6px;
    padding: 6px; background: #f0f0f0; border-bottom: 1px solid #ccc; }
  #fhead button, #fupload { font: 12px monospace; color: #111; background: #fff;
    border: 1px solid #bbb; border-radius: 4px; padding: 6px 8px;
    -webkit-user-select: none; user-select: none; touch-action: manipulation;
    cursor: pointer; }
  #fpath { flex: 1 1 auto; min-width: 0; font: 11px monospace; color: #444;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    direction: rtl; text-align: left; }
  #flist { flex: 1 1 auto; min-height: 0; overflow: auto;
    -webkit-overflow-scrolling: touch; }
  .frow { display: flex; align-items: center; gap: 8px; min-height: 40px;
    padding: 6px 10px; border-bottom: 1px solid #eee; font: 13px monospace;
    color: #111; -webkit-user-select: none; user-select: none;
    touch-action: manipulation; }
  .frow:active { background: #e8f0fe; }
  .frow .fname { flex: 1 1 auto; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .frow.dir .fname { color: #0c67d6; }
  .frow .fsize { flex: 0 0 auto; font-size: 11px; color: #999; }
</style>
</head>
<body>
<div id="app">
  <div id="term"></div>
  <div id="keys">
    <button data-seq="\x1b">esc</button>
    <button data-seq="\t">tab</button>
    <button data-seq="\x03">^C</button>
    <button data-seq="\x1a">^Z</button>
    <button data-seq="\x04">^D</button>
    <button data-seq="\x0c">^L</button>
    <button data-seq="\x1b[A">↑</button>
    <button data-seq="\x1b[B">↓</button>
    <button data-seq="\x1b[D">←</button>
    <button data-seq="\x1b[C">→</button>
    <button data-scroll="-1">pgUp</button>
    <button data-scroll="1">pgDn</button>
    <button data-act="sel" id="selbtn">sel</button>
    <button data-act="copy">copy</button>
    <button data-act="paste">paste</button>
    <button data-act="copyall">all</button>
    <button data-act="files">files</button>
  </div>
  <div id="bar"><span id="status">connecting…</span></div>
  <div id="files">
    <div id="fhead">
      <button id="fclose">‹ term</button>
      <button id="fup">up</button>
      <button id="frefresh">⟳</button>
      <label id="fupload">upload<input id="ffile" type="file" multiple
        hidden /></label>
      <span id="fpath"></span>
    </div>
    <div id="flist"></div>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<script>
const term = new Terminal({ cursorBlink: true, fontSize: 9,
  fontFamily: "Menlo, Monaco, 'SF Mono', monospace",
  theme: {
    background: "#ffffff", foreground: "#1a1a1a",
    cursor: "#1a1a1a", cursorAccent: "#ffffff",
    selectionBackground: "#b3d7ff", selectionForeground: "#000000",
    black: "#000000", red: "#c33720", green: "#26a439",
    yellow: "#a89c14", blue: "#0c67d6", magenta: "#b73ec0",
    cyan: "#1fa6 b0".replace(" ", ""), white: "#b3b3b3",
    brightBlack: "#767676", brightRed: "#e04030", brightGreen: "#30c040",
    brightYellow: "#c0b020", brightBlue: "#2080f0", brightMagenta: "#d050e0",
    brightCyan: "#30c0d0", brightWhite: "#1a1a1a"
  } });
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
const termEl = document.getElementById("term");
term.open(termEl);
const statusEl = document.getElementById("status");
// PTY output is raw UTF-8; decode as a stream so a multi-byte character
// split across two binary frames (os.read boundaries) is not corrupted.
const utf8 = new TextDecoder("utf-8");

// Keep the terminal in the space actually visible above the keyboard.
// visualViewport shrinks when the iOS keyboard opens; when it's closed we
// cap at half the height so the input line stays comfortably in view.
function sizeTerm() {
  const vv = window.visualViewport;
  let h;
  const keyboardOpen = vv && vv.height < window.innerHeight - 40;
  if (keyboardOpen) {
    // Keyboard open: size the whole app to the visible strip above it so
    // the terminal AND the key bar stay on screen.
    h = vv.height;
  } else {
    // Keyboard closed: only take half the screen so it's not too long.
    h = Math.round(window.innerHeight * 0.5);
  }
  // Must match the CSS var used by #app.
  document.documentElement.style.setProperty("--vh", h + "px");
  // On iOS a fixed element is anchored to the layout viewport, so when the
  // keyboard opens it can hide behind it. Nudge #app to the visible top.
  const app = document.getElementById("app");
  if (app && vv) app.style.transform = `translateY(${vv.offsetTop}px)`;
  fit.fit();
  // Keyboard closed: grow the terminal by a couple more rows so more
  // scrollback is visible without opening the keyboard.
  if (!keyboardOpen) {
    const rh = term.rows > 0 ? termEl.clientHeight / term.rows : 12;
    h += Math.round(2 * rh);
    document.documentElement.style.setProperty("--vh", h + "px");
    fit.fit();
  }
  sendResize();
}
// We deliberately do NOT re-fit on every visualViewport resize/scroll.
// Those fire in rapid bursts on iOS and each re-fit resizes the PTY, which
// makes tmux / full-screen TUIs (vim, htop, claude, opencode) redraw and
// pile up repeated lines. We size once, and once more after the keyboard
// first opens — then leave the dimensions fixed.

const wsProto = location.protocol === "https:" ? "wss" : "ws";
let ws, reconnectTimer, alive = false;

let lastCols = 0, lastRows = 0;
function sendResize(force) {
  if (!ws || ws.readyState !== 1) return;
  // Skip no-op resizes: re-sending the same size still triggers a redraw
  // in tmux / vim / htop, which is what caused the repeated prompt lines.
  if (!force && term.cols === lastCols && term.rows === lastRows) return;
  lastCols = term.cols;
  lastRows = term.rows;
  ws.send(JSON.stringify({ type: "resize",
    cols: term.cols, rows: term.rows }));
}

function connect() {
  statusEl.textContent = "connecting…";
  const url = `${wsProto}://${location.host}/ws`;
  ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    alive = true;
    statusEl.textContent = "● connected";
    lastCols = 0; lastRows = 0;   // new socket: server needs our real size
    utf8.decode();                // reset any partial UTF-8 from the old socket
    sendResize(true);
    term.focus();
  };
  ws.onmessage = (e) => {
    const data = typeof e.data === "string"
      ? e.data : utf8.decode(e.data, { stream: true });
    term.write(data);
  };
  ws.onclose = () => {
    alive = false;
    statusEl.textContent = "○ reconnecting…";
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 1000);
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}

term.onData((d) => { if (ws && ws.readyState === 1) ws.send(d); });
// One-shot: when the keyboard first opens (viewport shrinks), re-fit exactly
// once, then stop listening so we never resize the PTY again.
if (window.visualViewport) {
  const once = () => {
    window.visualViewport.removeEventListener("resize", once);
    sizeTerm();
  };
  window.visualViewport.addEventListener("resize", once);
}

// Wire the shortcut bar. data-seq holds the literal escape bytes; we send
// them straight to the PTY. Prevent the tap from stealing focus/keyboard.
function sendSeq(seq) { if (ws && ws.readyState === 1) ws.send(seq); }
// HTML attributes store escapes as literal text (e.g. "\x1b" is 4 chars),
// so decode \xNN and \t into the real control bytes before sending.
function decodeSeq(s) {
  return s.replace(/\\x([0-9a-fA-F]{2})/g,
                   (_, h) => String.fromCharCode(parseInt(h, 16)))
          .replace(/\\t/g, "\t");
}
// pgUp/pgDn: on the normal screen, scroll xterm.js's own scrollback (a
// client-side viewport action). When a fullscreen app is on the alternate
// screen (vim, less, htop, tmux copy-mode) there's no local scrollback, so
// forward the real PageUp/PageDown escape to the PTY instead.
function pageScroll(dir) {
  if (term.buffer.active.type === "alternate") {
    sendSeq(dir < 0 ? "\x1b[5~" : "\x1b[6~");
  } else {
    term.scrollPages(dir);
  }
}
// ---- selection / copy / paste ------------------------------------------
// xterm.js draws to a <canvas>, so the browser has no real text to select
// and nothing to copy. We add:
//   * a "sel" mode that forwards a finger drag as synthetic mouse events so
//     xterm's built-in (mouse) selection works on touch screens,
//   * copy / paste / copy-all buttons using the async Clipboard API, with a
//     hidden <textarea> fallback for browsers where that API is unavailable.

const clipEl = document.createElement("textarea");
clipEl.id = "clip";
clipEl.setAttribute("autocomplete", "off");
clipEl.setAttribute("autocorrect", "off");
clipEl.setAttribute("autocapitalize", "off");
clipEl.setAttribute("spellcheck", "false");
clipEl.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;" +
  "opacity:0.01;border:0;padding:0;z-index:-1;";
document.body.appendChild(clipEl);
clipEl.addEventListener("input", () => {
  const v = clipEl.value;
  if (v) { term.paste(v); flashStatus("pasted"); }
  clipEl.value = "";
});

function flashStatus(msg) {
  statusEl.textContent = msg;
  clearTimeout(flashStatus._t);
  flashStatus._t = setTimeout(() => {
    statusEl.textContent = (ws && ws.readyState === 1)
      ? "● connected" : "○ reconnecting…";
  }, 1600);
}

async function copyText(text) {
  if (!text) return false;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) { /* fall through to execCommand */ }
  }
  try {
    clipEl.readOnly = true;      // readOnly keeps the soft keyboard closed
    clipEl.value = text;
    clipEl.focus();
    clipEl.select();
    const ok = document.execCommand("copy");
    clipEl.value = "";
    clipEl.readOnly = false;
    return ok;
  } catch (e2) {
    clipEl.readOnly = false;
    return false;
  }
}

async function copySelection() {
  const sel = term.getSelection();
  if (!sel) { flashStatus("nothing selected"); return; }
  const ok = await copyText(sel);
  flashStatus(ok ? "copied" : "copy failed");
}

async function copyAll() {
  term.selectAll();
  const sel = term.getSelection();
  if (!sel) { flashStatus("nothing to copy"); return; }
  const ok = await copyText(sel);
  flashStatus(ok ? "copied" : "copy failed");
}

async function pasteClipboard() {
  if (navigator.clipboard && navigator.clipboard.readText) {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { term.paste(text); flashStatus("pasted"); }
      else flashStatus("clipboard empty");
      return;
    } catch (e) { /* fall through to manual paste */ }
  }
  clipEl.value = "";
  clipEl.focus();
  flashStatus("long-press → paste");
}

let selectMode = false;
function setSelectMode(on) {
  selectMode = on;
  const btn = document.getElementById("selbtn");
  if (btn) btn.classList.toggle("on", on);
  flashStatus(on ? "select mode" : "select off");
}

// Forward touch events as mouse events ONLY while select mode is on. We
// listen in the capture phase on #app so we can stop the event before
// xterm's own touch handlers (viewport scrolling) see it.
const appEl = document.getElementById("app");
let selDrag = false;

function selMouse(type, touch) {
  // xterm.js attaches its mouse listeners to the inner .xterm element
  // (a child of #term), NOT to the #term container itself. Dispatching on
  // #term would bubble up and never reach them, so target .xterm instead.
  const target = termEl.querySelector(".xterm") || termEl;
  target.dispatchEvent(new MouseEvent(type, {
    bubbles: true, cancelable: true, view: window,
    clientX: touch.clientX, clientY: touch.clientY,
    screenX: touch.screenX, screenY: touch.screenY,
    button: 0,
    buttons: type === "mouseup" ? 0 : 1,
    // xterm only starts a selection for detail 1/2/3; a synthetic event
    // defaults to detail 0, so no selection would ever begin.
    detail: type === "mousedown" ? 1 : 0
  }));
}

appEl.addEventListener("touchstart", (e) => {
  if (!selectMode || !termEl.contains(e.target)) return;
  e.preventDefault();
  e.stopPropagation();
  const t = e.touches[0];
  if (!t) return;
  selDrag = true;
  term.clearSelection();
  selMouse("mousedown", t);
}, { capture: true, passive: false });

appEl.addEventListener("touchmove", (e) => {
  if (!selectMode || !selDrag) return;
  e.preventDefault();
  e.stopPropagation();
  const t = e.touches[0];
  if (t) selMouse("mousemove", t);
}, { capture: true, passive: false });

appEl.addEventListener("touchend", (e) => {
  if (!selectMode || !selDrag) return;
  e.preventDefault();
  e.stopPropagation();
  selDrag = false;
  const t = e.changedTouches[0];
  if (t) selMouse("mouseup", t);
  const sel = term.getSelection();
  if (sel) {
    copyText(sel).then((ok) => flashStatus(ok ? "copied" : "copy failed"));
  } else {
    flashStatus("drag to select");
  }
}, { capture: true, passive: false });

appEl.addEventListener("touchcancel", () => { selDrag = false; },
  { capture: true });

document.querySelectorAll("#keys button").forEach((btn) => {
  const act = btn.getAttribute("data-act");
  const scroll = btn.getAttribute("data-scroll");
  const fire = act === "sel"
      ? () => setSelectMode(!selectMode)
      : act === "copy"
        ? () => copySelection()
        : act === "paste"
          ? () => pasteClipboard()
          : act === "copyall"
            ? () => copyAll()
            : act === "files"
              ? () => toggleFiles()
              : scroll !== null
                ? () => pageScroll(parseInt(scroll, 10))
                : (() => { const seq = decodeSeq(btn.getAttribute("data-seq"));
                           return () => sendSeq(seq); })();
  btn.addEventListener("touchstart", (e) => {
    e.preventDefault();          // keep the soft keyboard up
    fire();
  }, { passive: false });
  btn.addEventListener("mousedown", (e) => { e.preventDefault(); fire(); });
});

// ---- file browser ------------------------------------------------------
// A full-screen overlay (#files) sits above the terminal. Browsing never
// navigates the page, and downloads are fetched as a Blob + saved via a
// synthetic <a download> so iPhone Chrome can't render a file inline and
// kill the terminal's WebSocket. All endpoints are behind the login cookie
// (sent automatically on same-origin fetch).
const filesEl = document.getElementById("files");
const flistEl = document.getElementById("flist");
const fpathEl = document.getElementById("fpath");
let filesOpen = false, curDir = "", curParent = "", dirLoaded = false;

function toggleFiles() {
  filesOpen = !filesOpen;
  filesEl.classList.toggle("open", filesOpen);
  if (filesOpen && !dirLoaded) { dirLoaded = true; loadDir(""); }
}

function fmtSize(n) {
  if (n < 1024) return n + "B";
  if (n < 1048576) return (n / 1024).toFixed(1) + "K";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + "M";
  return (n / 1073741824).toFixed(1) + "G";
}

async function loadDir(path) {
  try {
    // _=<ts> busts any HTTP cache so a re-list always reflects the real
    // directory; {cache:"no-store"} is the belt to that suspenders.
    const r = await fetch(
      "/api/ls?path=" + encodeURIComponent(path || "") + "&_=" + Date.now(),
      { cache: "no-store" });
    if (r.status === 401) { location.href = "/login"; return; }
    const d = await r.json();
    if (!r.ok) { flashStatus(d.error || "ls failed"); return; }
    curDir = d.path;
    curParent = d.parent;
    fpathEl.textContent = d.path;
    flistEl.innerHTML = "";
    if (d.parent && d.parent !== d.path) {
      flistEl.appendChild(makeRow("..", true, 0, () => loadDir(d.parent)));
    }
    for (const e of d.entries) {
      flistEl.appendChild(makeRow(
        e.name, e.is_dir, e.size,
        e.is_dir ? () => loadDir(e.path)
                 : () => downloadFile(e.path, e.name)));
    }
    flistEl.scrollTop = 0;
  } catch (err) {
    flashStatus("ls error");
  }
}

function makeRow(name, isDir, size, onTap) {
  const row = document.createElement("div");
  row.className = "frow" + (isDir ? " dir" : "");
  const nm = document.createElement("span");
  nm.className = "fname";
  nm.textContent = (isDir ? "📁 " : "📄 ") + name;
  row.appendChild(nm);
  if (!isDir) {
    const sz = document.createElement("span");
    sz.className = "fsize";
    sz.textContent = fmtSize(size);
    row.appendChild(sz);
  }
  // Distinguish a tap from a scroll: record where the finger landed and only
  // fire onTap if it barely moved. We must NOT preventDefault on touchstart —
  // that would cancel the list's scroll gesture (the original bug).
  let sx = 0, sy = 0, moved = false;
  row.addEventListener("touchstart", (e) => {
    const t = e.touches[0];
    sx = t ? t.clientX : 0; sy = t ? t.clientY : 0; moved = false;
  }, { passive: true });
  row.addEventListener("touchmove", (e) => {
    const t = e.touches[0];
    if (t && (Math.abs(t.clientX - sx) > 10 || Math.abs(t.clientY - sy) > 10)) {
      moved = true;   // it's a scroll, not a tap
    }
  }, { passive: true });
  row.addEventListener("touchend", (e) => {
    if (moved) return;        // scrolled — ignore
    e.preventDefault();       // stop the synthesized click (avoid double-fire)
    onTap();
  });
  // Desktop / non-touch fallback.
  row.addEventListener("click", () => onTap());
  return row;
}

async function downloadFile(path, name) {
  flashStatus("downloading…");
  try {
    const r = await fetch("/api/download?path=" + encodeURIComponent(path));
    if (r.status === 401) { location.href = "/login"; return; }
    if (!r.ok) { flashStatus("download failed"); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    // If an engine insists on navigating a blob: URL, send it to a throwaway
    // tab so the terminal tab is never replaced.
    a.target = "_blank";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    flashStatus("saved " + name);
  } catch (err) {
    flashStatus("download error");
  }
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList);
  let done = 0;
  for (const f of files) {
    flashStatus("uploading " + f.name + "…");
    try {
      const r = await fetch(
        "/api/upload?dir=" + encodeURIComponent(curDir) +
        "&name=" + encodeURIComponent(f.name),
        { method: "POST", body: f });
      if (r.status === 401) { location.href = "/login"; return; }
      if (r.ok) done++;
      else flashStatus("upload failed: " + f.name);
    } catch (err) {
      flashStatus("upload error: " + f.name);
    }
  }
  flashStatus("uploaded " + done + "/" + files.length);
  loadDir(curDir);
}

document.getElementById("fclose").addEventListener("click", toggleFiles);
document.getElementById("fup").addEventListener("click", () => {
  if (curParent && curParent !== curDir) loadDir(curParent);
});
document.getElementById("frefresh").addEventListener("click", () => {
  loadDir(curDir);
  flashStatus("refreshed");
});
document.getElementById("ffile").addEventListener("change", (e) => {
  if (e.target.files && e.target.files.length) uploadFiles(e.target.files);
  e.target.value = "";   // reset so the same file can be re-picked
});

connect();
sizeTerm();
</script>
</body>
</html>
"""

LOGIN_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>web term · login</title>
<style>
  html, body { margin: 0; height: 100%; background: #fff;
    font-family: -apple-system, monospace; -webkit-text-size-adjust: 100%; }
  form { max-width: 320px; margin: 0 auto; padding: 40px 16px; }
  h1 { font-size: 16px; color: #111; }
  label { display: block; font-size: 12px; color: #666; margin: 12px 0 4px; }
  input { width: 100%; box-sizing: border-box; font: 15px monospace;
    padding: 8px; border: 1px solid #bbb; border-radius: 4px; }
  button { width: 100%; margin-top: 16px; padding: 10px; font: 14px monospace;
    color: #fff; background: #0c67d6; border: 0; border-radius: 4px; }
  #error { color: #c33720; font-size: 12px; margin-top: 12px; }
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>Sign in to web term</h1>
  <label for="username">Username</label>
  <input id="username" name="username" value="__USER__" autocomplete="username" />
  <label for="password">Password</label>
  <input id="password" name="password" type="password"
    autocomplete="current-password" autofocus />
  <div id="error">__ERROR__</div>
  <button type="submit">Sign in</button>
</form>
</body>
</html>
"""


def ws_accept_key(client_key):
    digest = hashlib.sha1((client_key + WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def ws_encode(payload, opcode=0x1):
    """Server->client frame (unmasked). opcode 0x1=text, 0x8=close."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "replace")
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


def ws_read_frame(rfile):
    """Read one client frame. Returns (opcode, payload_bytes) or None on EOF.

    A peer that drops the connection (reset, broken pipe, tab close) is a
    normal disconnect, not an error — treat it as EOF.
    """
    try:
        return _ws_read_frame(rfile)
    except (OSError, ValueError):
        return None


def _ws_read_frame(rfile):
    hdr = rfile.read(2)
    if len(hdr) < 2:
        return None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        ext = rfile.read(2)
        if len(ext) < 2:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = rfile.read(8)
        if len(ext) < 8:
            return None
        length = struct.unpack(">Q", ext)[0]
    mask = rfile.read(4) if masked else b"\x00\x00\x00\x00"
    if masked and len(mask) < 4:
        return None
    data = bytearray()
    remaining = length
    while remaining > 0:
        chunk = rfile.read(remaining)
        if not chunk:
            return None
        data += chunk
        remaining -= len(chunk)
    if masked:
        for i in range(len(data)):
            data[i] ^= mask[i & 3]
    return opcode, bytes(data)


def spawn_pty():
    """Fork a shell attached to a new PTY. Returns (pid, master_fd).

    Uses `tmux new-session -A -s web` when tmux is present so reconnecting
    re-attaches to the same live session.
    """
    if shutil.which("tmux"):
        argv = ["tmux", "new-session", "-A", "-s", TMUX_SESSION]
    else:
        argv = [SHELL, "-l"]

    pid, master_fd = pty.fork()
    if pid == 0:  # child
        os.environ.setdefault("TERM", "xterm-256color")
        try:
            os.execvp(argv[0], argv)
        except Exception:
            os.execvp(SHELL, [SHELL])
        os._exit(1)
    return pid, master_fd


def set_winsize(fd, rows, cols):
    if fcntl is None or termios is None:
        return
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass


class TermHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        # A client that drops the connection before/mid request (tab close,
        # network blip, cloudflared health probe) is normal — don't let the
        # reset bubble up as a scary traceback.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
                TimeoutError):
            self.close_connection = True

    def _authed(self):
        # Authenticated only if a valid signed session cookie is present.
        import http.cookies
        raw = self.headers.get("Cookie", "")
        if not raw:
            return False
        try:
            jar = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return False
        morsel = jar.get(SESSION_COOKIE)
        return bool(morsel) and valid_session(morsel.value)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/ws":
            self._handle_ws()
        elif path == "/":
            self._serve_page()
        elif path == "/login":
            self._serve_login()
        elif path == "/logout":
            self._do_logout()
        elif path == "/api/ls":
            self._api_ls()
        elif path == "/api/download":
            self._api_download()
        else:
            self.send_error(404, "not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            self._handle_login()
        elif path == "/api/upload":
            self._api_upload()
        else:
            self.send_error(404, "not found")

    def _serve_page(self):
        if not self._authed():
            # Not logged in → send them to the login page.
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_html(HTML_PAGE)

    def _send_html(self, html, extra_headers=None):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- File browser API ----------------------------------------------------
    # All three endpoints sit behind the same login as /ws. They add no new
    # privilege: the terminal tab already grants full shell access to this
    # account, so the file API simply gives the same reach a nicer UI. Every
    # download forces Content-Disposition: attachment so a browser (notably
    # iPhone Chrome) can never render a file inline and navigate away from the
    # single-page terminal.

    def _api_ls(self):
        if not self._authed():
            self._send_json({"error": "unauthorized"}, 401)
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw = qs.get("path", [""])[0]
        d = os.path.realpath(raw) if raw else os.getcwd()
        if not os.path.isdir(d):
            self._send_json({"error": "not a directory", "path": d}, 404)
            return
        try:
            names = os.listdir(d)
        except PermissionError:
            self._send_json({"error": "permission denied", "path": d}, 403)
            return
        except OSError as e:
            self._send_json({"error": str(e), "path": d}, 500)
            return
        entries = []
        for name in names:
            full = os.path.join(d, name)
            try:
                is_dir = os.path.isdir(full)
                size = 0 if is_dir else os.path.getsize(full)
            except OSError:
                continue  # dangling symlink / vanished / unreadable — skip it
            entries.append({"name": name, "path": full,
                            "is_dir": is_dir, "size": size})
        # Directories first, then files; each group sorted case-insensitively.
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        self._send_json({"path": d, "parent": os.path.dirname(d),
                         "entries": entries})

    def _api_download(self):
        if not self._authed():
            self._send_json({"error": "unauthorized"}, 401)
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        p = os.path.realpath(qs.get("path", [""])[0])
        if not os.path.isfile(p):
            self._send_json({"error": "not a file", "path": p}, 404)
            return
        name = os.path.basename(p)
        # RFC 5987: ascii-safe fallback plus a UTF-8 encoded name for spaces /
        # unicode. application/octet-stream + attachment => never render inline.
        ascii_name = name.encode("ascii", "replace").decode("ascii") \
            .replace('"', "'")
        enc_name = urllib.parse.quote(name, safe="")
        try:
            size = os.path.getsize(p)
            f = open(p, "rb")
        except OSError as e:
            self._send_json({"error": str(e), "path": p}, 500)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{enc_name}")
            self.end_headers()
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            f.close()

    def _api_upload(self):
        if not self._authed():
            self._send_json({"error": "unauthorized"}, 401)
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        d = os.path.realpath(qs.get("dir", [""])[0] or os.getcwd())
        name = os.path.basename(qs.get("name", [""])[0])
        if not os.path.isdir(d):
            self._send_json({"error": "not a directory", "dir": d}, 404)
            return
        if not name:
            self._send_json({"error": "missing name"}, 400)
            return
        length = int(self.headers.get("Content-Length", 0))
        dest = os.path.join(d, name)
        try:
            # Raw request body IS the file bytes (client sends the File object
            # directly) — no multipart parsing needed. Read exactly
            # Content-Length so we never block waiting on a short body.
            with open(dest, "wb") as out:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            self._send_json({"error": str(e), "dir": d}, 500)
            return
        self._send_json({"ok": True, "name": name, "path": dest})

    def _serve_login(self, error=""):
        self._send_html(LOGIN_PAGE.replace("__ERROR__", error)
                        .replace("__USER__", USERNAME))

    def _handle_login(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(body)
        user = form.get("username", [""])[0]
        pw = form.get("password", [""])[0]
        if check_login(user, pw):
            token = make_session()
            # Only flag the cookie Secure when the client reached us over
            # HTTPS (i.e. via the Cloudflare tunnel). Localhost testing is
            # plain HTTP, where a Secure cookie would never be stored.
            proto = self.headers.get("X-Forwarded-Proto", "").lower()
            secure = "; Secure" if proto == "https" else ""
            cookie = (f"{SESSION_COOKIE}={token}; HttpOnly; Path=/; "
                      f"SameSite=Strict{secure}")
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._serve_login(error="Invalid username or password")

    def _do_logout(self):
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}=; Path=/; Max-Age=0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass

    def _handle_ws(self):
        if not self._authed():
            self.send_error(401, "unauthorized")
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            self.send_error(400, "expected websocket upgrade")
            return

        # Complete the WebSocket handshake by hand.
        accept = ws_accept_key(key)
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
        )
        self.wfile.flush()

        pid, master_fd = spawn_pty()
        sock = self.connection
        stop = threading.Event()

        def pty_to_ws():
            try:
                while not stop.is_set():
                    r, _, _ = select.select([master_fd], [], [], 0.2)
                    if master_fd in r:
                        try:
                            data = os.read(master_fd, 65536)
                        except OSError:
                            break
                        if not data:
                            break
                        try:
                            # Send PTY output as a BINARY frame: the bytes are
                            # raw UTF-8 and a multibyte char may be split
                            # across two os.read() calls, so the browser must
                            # decode them as one continuous stream.
                            sock.sendall(ws_encode(data, opcode=0x2))
                        except OSError:
                            break
            finally:
                stop.set()

        reader = threading.Thread(target=pty_to_ws, daemon=True)
        reader.start()

        try:
            while not stop.is_set():
                frame = ws_read_frame(self.rfile)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:  # close
                    break
                if opcode == 0x9:  # ping -> pong
                    try:
                        sock.sendall(ws_encode(payload, opcode=0xA))
                    except OSError:
                        break
                    continue
                if opcode not in (0x1, 0x2):
                    continue
                # Control messages are JSON {type:"resize",cols,rows}.
                if payload[:1] == b"{":
                    try:
                        msg = json.loads(payload.decode("utf-8", "replace"))
                        if msg.get("type") == "resize":
                            set_winsize(master_fd,
                                        int(msg["rows"]), int(msg["cols"]))
                            continue
                    except (ValueError, KeyError):
                        pass
                try:
                    os.write(master_fd, payload)
                except OSError:
                    break
        finally:
            stop.set()
            reader.join(timeout=1)
            try:
                os.close(master_fd)
            except OSError:
                pass
            # Do not kill the tmux server; detaching keeps the session alive
            # for the next reconnect. Only reap a non-tmux child.
            if not shutil.which("tmux"):
                try:
                    os.kill(pid, signal.SIGHUP)
                except OSError:
                    pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                pass


def print_qr(url):
    """Render a QR to stdout (ASCII) and save qrcode.png next to the script."""
    try:
        import qrcode
    except ImportError:
        print("(install qrcode for a scannable code:  pip install 'qrcode[pil]')")
        return
    qr = qrcode.QRCode(border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    try:
        img = qr.make_image(fill_color="black", back_color="white")
        png_path = os.path.join(SCRIPT_DIR, "qrcode.png")
        img.save(png_path)
        print(f"  QR image saved: {png_path}")
    except Exception as e:
        print(f"  (could not write qrcode.png: {e})")


def announce(url):
    print(f"\n{'=' * 60}")
    print(f"  WEB TERMINAL: {url}")
    print(f"{'=' * 60}")
    print("  Scan the QR below (or open the URL) on your phone:\n")
    print_qr(url)
    print("\n  The phone remembers this URL in local storage.")
    print("  Rescan a new QR or tap 'change' on the page to switch.\n")


def start_tunnel():
    proc = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--url", f"http://localhost:{PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url_pattern = re.compile(r"(https://[a-z0-9-]+\.trycloudflare\.com)")
    announced = threading.Event()

    def read_stream(stream):
        for line in stream:
            m = url_pattern.search(line)
            if m and not announced.is_set():
                announced.set()
                announce(m.group(1))

    threading.Thread(target=read_stream, args=(proc.stdout,),
                     daemon=True).start()
    return proc


def main():
    ensure_cloudflared()
    print(f"Starting web-terminal server on port {PORT}...")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), TermHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Server listening on http://localhost:{PORT}")

    print("Starting cloudflare tunnel...")
    tunnel_proc = start_tunnel()

    def kill_tmux_session():
        # Tear down the detached tmux server so the shell and every
        # subprocess it spawned die with this program (clean shutdown).
        if not shutil.which("tmux"):
            return
        try:
            subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass

    _shutting_down = threading.Event()

    def shutdown(sig, frame):
        if _shutting_down.is_set():
            return
        _shutting_down.set()
        print("\nShutting down...")
        kill_tmux_session()
        tunnel_proc.terminate()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
