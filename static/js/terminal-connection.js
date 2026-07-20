/**
 * Terminal connection manager.
 * Wires a WebSocketClient to an xterm.js Terminal instance.
 * Adapted from the connectSocket pattern in raw/terminal-component.js
 * and TerminalConnectionManager in raw/connection-factories.js.
 */

import { WebSocketClient } from '/static/js/websocket-client.js';

class TerminalConnection {
  constructor() {
    this.client = null;
    this.terminal = null;
    this.dataDisposable = null;
    this.resizeDisposable = null;
    this.statusCallback = null;
  }

  attachTerminal(terminal) {
    this.terminal = terminal;
  }

  onStatus(callback) {
    this.statusCallback = callback;
  }

  connect(url, token = null) {
    this.disconnect();

    const client = new WebSocketClient(url, token);
    this.client = client;

    client.onOpen = () => {
      if (this.client !== client) return;
      this._setStatus("connected");
      this.terminal.reset();
      client.sendJSON({
        cols: this.terminal.cols,
        rows: this.terminal.rows,
      });
    };

    client.onMessage = (data) => {
      if (this.client !== client || !this.terminal) return;
      if (data instanceof ArrayBuffer) {
        this.terminal.write(new Uint8Array(data));
      } else if (typeof data === "string") {
        this.terminal.write(data);
      }
    };

    client.onClose = () => {
      // Ignore close events from a superseded connection so a stale
      // close doesn't detach the new connection's terminal handlers.
      if (this.client !== client) return;
      this._setStatus("disconnected");
      this._detachTerminalHandlers();
    };

    client.onError = () => {
      if (this.client !== client) return;
      this._setStatus("disconnected");
    };

    this._attachTerminalHandlers();

    return this.client.connect().catch(() => {});
  }

  disconnect() {
    this._detachTerminalHandlers();
    if (this.client) {
      this.client.close();
      this.client = null;
    }
    this._setStatus("disconnected");
  }

  reconnect(url, token = null) {
    return this.connect(url, token);
  }

  _attachTerminalHandlers() {
    if (!this.terminal) return;

    this.dataDisposable = this.terminal.onData((data) => {
      if (this.client && this.client.isOpen()) {
        this.client.sendJSON({ data });
      }
    });

    this.resizeDisposable = this.terminal.onResize(({ cols, rows }) => {
      if (this.client && this.client.isOpen()) {
        this.client.sendJSON({ cols, rows });
      }
    });
  }

  _detachTerminalHandlers() {
    if (this.dataDisposable) {
      this.dataDisposable.dispose();
      this.dataDisposable = null;
    }
    if (this.resizeDisposable) {
      this.resizeDisposable.dispose();
      this.resizeDisposable = null;
    }
  }

  _setStatus(status) {
    if (this.statusCallback) this.statusCallback(status);
  }
}

export { TerminalConnection };
