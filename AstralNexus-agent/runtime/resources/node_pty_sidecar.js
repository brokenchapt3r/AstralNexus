// Controlled node-pty sidecar for the AstralNexus subprocess-local port.
//
// One terminal per sidecar process; JSON-lines protocol over 127.0.0.1
// (the host sandbox refuses anonymous pipes for spawned processes, so
// localhost TCP is the carrier; the port is handed off through a private
// locator file written with mode 0600 and flag wx).
//
// Usage: <electron-as-node|node> node_pty_sidecar.js <locatorPath> <nodePtyRequirePath>
//
// Commands (client -> sidecar):
//   {"type":"spawn","file":...,"args":[...],"options":{name,rows,cols,cwd,env}}
//   {"type":"write","data":...}
//   {"type":"resize","cols":N,"rows":N}
//   {"type":"kill","signal":"SIGTERM"|"SIGKILL"|...}   (signal optional)
//   {"type":"pause"} / {"type":"resume"}
//   {"type":"shutdown"}
// Events (sidecar -> client):
//   {"type":"spawned","pid":N}
//   {"type":"data","data":"..."}
//   {"type":"exit","exitCode":N,"signal":N|undefined}
//   {"type":"error","message":"..."}
"use strict";
const net = require("net");
const fs = require("fs");
const readline = require("readline");

const locatorPath = process.argv[2];
const ptyPath = process.argv[3];
if (!locatorPath || !ptyPath) {
  process.stderr.write("usage: node_pty_sidecar.js <locatorPath> <nodePtyRequirePath>\n");
  process.exit(2);
}
const pty = require(ptyPath);

let socket = null;
let term = null;
let shuttingDown = false;

function send(message) {
  if (socket && !socket.destroyed) {
    socket.write(JSON.stringify(message) + "\n");
  }
}

function shutdown(code) {
  if (shuttingDown) return;
  shuttingDown = true;
  try { if (term) term.kill(); } catch (_alreadyGone) {}
  try { if (socket) socket.destroy(); } catch (_closedSocket) {}
  try { server.close(); } catch (_closedServer) {}
  process.exit(code);
}

const server = net.createServer((connection) => {
  if (socket) { connection.destroy(); return; }
  socket = connection;
  const lines = readline.createInterface({ input: connection });
  lines.on("line", (line) => {
    if (!line.trim()) return;
    let message;
    try { message = JSON.parse(line); } catch (_badLine) { return; }
    try {
      if (message.type === "spawn") {
        if (term) throw new Error("sidecar already owns a terminal");
        term = pty.spawn(message.file, message.args, message.options);
        term.onData((data) => send({ type: "data", data }));
        term.onExit((event) => send({
          type: "exit",
          exitCode: event.exitCode,
          signal: event.signal,
        }));
        send({ type: "spawned", pid: term.pid });
      } else if (message.type === "write") {
        term.write(message.data);
      } else if (message.type === "resize") {
        term.resize(message.cols, message.rows);
      } else if (message.type === "kill") {
        term.kill(message.signal);
      } else if (message.type === "pause") {
        term.pause();
      } else if (message.type === "resume") {
        term.resume();
      } else if (message.type === "shutdown") {
        shutdown(0);
      } else {
        send({ type: "error", message: `unknown command: ${message.type}` });
      }
    } catch (error) {
      send({ type: "error", message: String((error && error.message) || error) });
    }
  });
  connection.on("close", () => shutdown(0));
});

server.on("error", (error) => {
  try {
    fs.writeFileSync(locatorPath + ".error", String((error && error.message) || error), { mode: 0o600 });
  } catch (_unwritable) {}
  process.exit(1);
});

server.listen(0, "127.0.0.1", () => {
  const payload = JSON.stringify({ port: server.address().port, pid: process.pid });
  fs.writeFileSync(locatorPath, payload, { mode: 0o600, flag: "wx" });
});
