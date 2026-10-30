import json
import select
import subprocess
import sys

import pytest


def _rpc(proc, payload, timeout=3):
    proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
    proc.stdin.flush()
    if payload.get("id") is None:
        return None
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        err = b""
        if proc.stderr:
            ready_err, _, _ = select.select([proc.stderr], [], [], 0.1)
            if ready_err:
                err = proc.stderr.read1(4000)
        pytest.fail(f"MCP server did not answer: {err!r}")
    line = proc.stdout.readline()
    assert line
    assert not line.lower().startswith(b"content-length")
    assert b"\n" not in line[:-1]
    return json.loads(line.decode("utf-8"))


def test_mcp_stdio_speaks_newline_jsonrpc(env):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "shuin",
            "--db",
            str(env.db),
            "--token",
            env.tokens["eng-lead"],
            "mcp",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        init = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0"},
                },
            },
        )
        assert init["id"] == 1
        assert init["result"]["protocolVersion"] == "2024-11-05"
        assert init["result"]["serverInfo"]["name"] == "shuin"
        assert "tools" in init["result"]["capabilities"]
        _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        assert "list_board" in names
        assert "tools/call" not in names
        called = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_board", "arguments": {}},
            },
        )
        assert called["id"] == 3
        assert called["result"]["isError"] is False
        text = called["result"]["content"][0]["text"]
        assert "tasks" in text
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_mcp_bad_line_is_an_error_and_the_server_stays_up(env):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "shuin",
            "--db",
            str(env.db),
            "--token",
            env.tokens["eng-lead"],
            "mcp",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc.stdin.write(b"not-json\n")
        proc.stdin.flush()
        ready, _, _ = select.select([proc.stdout], [], [], 3)
        assert ready, "bad line produced no response"
        err = json.loads(proc.stdout.readline().decode("utf-8"))
        assert err["id"] is None
        assert err["error"]["code"] == -32700
        init = _rpc(
            proc,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert init["id"] == 1
        assert init["result"]["serverInfo"]["name"] == "shuin"
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=5)
