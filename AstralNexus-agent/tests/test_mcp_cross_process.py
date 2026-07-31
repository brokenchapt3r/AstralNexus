"""Process-level MCP service and session recovery acceptance test."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


SERVER_CODE = r'''
import asyncio, json, sys
from pathlib import Path
import websockets

port_file = Path(sys.argv[1])

async def handler(connection):
    async for raw in connection:
        message = json.loads(raw)
        method = message.get("method")
        result = {"ok": True}
        if method == "tools/list":
            result = {"tools": [{"name": "echo"}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "echo:ok"}]}
        await connection.send(json.dumps({
            "jsonrpc": "2.0", "id": message.get("id"), "result": result
        }))

async def main():
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port_file.write_text(str(server.sockets[0].getsockname()[1]), encoding="utf-8")
    await asyncio.Future()

asyncio.run(main())
'''

CLIENT_CODE = r'''
import json, sys
from AstralNexus.tools.mcp_client import MCPClient, WebSocketTransport
from AstralNexus.tools.mcp_sessions import MCPSessionManager

mode, storage, url, *extra = sys.argv[1:]
manager = MCPSessionManager(storage)
if mode == "open":
    session = manager.open_session("acceptance-server", owner="acceptance", capabilities={"mcp"})
else:
    session_id = extra[0]
    session = manager.get(session_id, owner="acceptance")
transport = WebSocketTransport(url, timeout=5, reconnect_attempts=2)
client = MCPClient(session, transport, manager)
client.initialize({"name": "AstralNexus-cross-process"})
tools = client.list_tools()
call = client.call_tool("echo", {"text": "ok"})
print(json.dumps({"session_id": session.session_id, "tools": tools, "call": call}, ensure_ascii=False))
if mode != "open":
    client.close()
transport.close()
'''


def _run_client(source_root: Path, args: list[str]) -> dict:
    env = os.environ.copy()
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    result = subprocess.run(
        [sys.executable, "-c", CLIENT_CODE, *args],
        cwd=source_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"MCP client process failed ({result.returncode}): {result.stderr.strip()}"
        )
    return json.loads(result.stdout.strip())


def test_real_mcp_service_and_cross_process_session_recovery(tmp_path: Path):
    source_root = Path(__file__).resolve().parents[2]
    port_file = tmp_path / "mcp_port.txt"
    storage = tmp_path / "mcp_sessions.json"
    env = os.environ.copy()
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    server = subprocess.Popen(
        [sys.executable, "-c", SERVER_CODE, str(port_file)],
        cwd=source_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 10
        while not port_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert port_file.exists(), "MCP service did not publish a listening port"
        url = f"ws://127.0.0.1:{port_file.read_text(encoding='utf-8').strip()}"

        first = _run_client(source_root, ["open", str(storage), url])
        assert first["tools"]["tools"][0]["name"] == "echo"
        assert storage.exists()

        second = _run_client(
            source_root,
            ["resume", str(storage), url, first["session_id"]],
        )
        assert second["session_id"] == first["session_id"]
        assert second["call"]["content"][0]["text"] == "echo:ok"
    finally:
        server.terminate()
        server.wait(timeout=10)
