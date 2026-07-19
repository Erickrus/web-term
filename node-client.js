/**
 * Web Terminal - Node.js WebSocket Client
 * Connect to a web terminal server programmatically.
 *
 * Usage:
 *   node node-client.js <wss-url> [token]
 *
 * Example:
 *   node node-client.js ws://localhost:8888/ws mytoken123
 */

const WebSocket = require('ws');
const readline = require('readline');

const url = process.argv[2];
const token = process.argv[3];

if (!url) {
  console.error('Usage: node node-client.js <wss-url> [token]');
  process.exit(1);
}

const wsUrl = new URL(url);
if (token) {
  wsUrl.searchParams.set('token', token);
}

const cols = process.stdout.columns || 80;
const rows = process.stdout.rows || 24;

console.error(`Connecting to ${wsUrl.hostname}...`);

const ws = new WebSocket(wsUrl.toString());

ws.on('open', () => {
  console.error('Connected. Press Ctrl+C to exit.\n');
  ws.send(JSON.stringify({ cols, rows }));

  process.stdin.setRawMode(true);
  process.stdin.resume();
  process.stdin.on('data', (data) => {
    ws.send(JSON.stringify({ data: data.toString() }));
  });

  process.stdout.on('resize', () => {
    const newCols = process.stdout.columns;
    const newRows = process.stdout.rows;
    ws.send(JSON.stringify({ cols: newCols, rows: newRows }));
  });
});

ws.on('message', (raw) => {
  let payload = raw;
  let needsAck = false;

  if (typeof raw === 'string' || raw instanceof Buffer) {
    try {
      const parsed = JSON.parse(raw.toString());
      payload = parsed.data;
      needsAck = parsed.ack || false;
    } catch (e) {
      payload = raw;
    }
  }

  if (typeof payload === 'string') {
    process.stdout.write(payload);
  } else if (Buffer.isBuffer(payload)) {
    process.stdout.write(payload);
  }

  if (needsAck) {
    ws.send(JSON.stringify({ ack: true }));
  }
});

ws.on('close', (code, reason) => {
  console.error(`\nDisconnected (code=${code}, reason=${reason})`);
  process.exit(0);
});

ws.on('error', (err) => {
  console.error(`WebSocket error: ${err.message}`);
  process.exit(1);
});

process.on('SIGINT', () => {
  ws.close();
  process.exit(0);
});
