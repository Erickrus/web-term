/**
 * WebSocket client for terminal connections.
 * Adapted from the deobfuscated Closure Compiler output (raw/websocket-class.js).
 */

class WebSocketClient {
  constructor(url, token = null) {
    this.url = url;
    this.token = token;
    this.webSocket = null;
    this.onOpen = null;
    this.onClose = null;
    this.onMessage = null;
    this.onError = null;
  }

  connect() {
    return new Promise((resolve, reject) => {
      const wsUrl = new URL(this.url);

      if (this.token) {
        wsUrl.searchParams.set("token", this.token);
      }

      const socket = new WebSocket(wsUrl.toString());
      socket.binaryType = "arraybuffer";

      socket.addEventListener("open", () => {
        this.webSocket = socket;
        if (this.onOpen) this.onOpen();
        resolve();
      });

      socket.addEventListener("close", (event) => {
        this.webSocket = null;
        if (this.onClose) this.onClose(event.code, event.reason);
        reject(event);
      });

      socket.addEventListener("message", (event) => {
        if (this.onMessage) this.onMessage(event.data);
      });

      socket.addEventListener("error", (event) => {
        if (this.onError) this.onError(event);
      });
    });
  }

  isOpen() {
    return !!this.webSocket && this.webSocket.readyState === WebSocket.OPEN;
  }

  send(data) {
    if (!this.isOpen()) throw new Error("WebSocket is not open");
    this.webSocket.send(data);
  }

  sendJSON(obj) {
    this.send(JSON.stringify(obj));
  }

  close() {
    if (this.webSocket) {
      const socket = this.webSocket;
      this.webSocket = null;
      if (socket.readyState !== WebSocket.CLOSED) {
        socket.close();
      }
    }
  }
}

export { WebSocketClient };
