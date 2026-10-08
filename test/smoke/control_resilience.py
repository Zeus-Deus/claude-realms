"""Smoke: the real server outlives pane requests whose client gave up waiting.

The Realm pane runs ctl.py with a deadline; a reply that came after it used to
crash the whole server (and stop the realm with it). This starts the server
over stdio, starts a realm, abandons control requests mid-flight, then checks
that the server, its tools and the control socket still answer.

    test/docker/run.sh -- bash -lc 'cd /plugin && uv sync --frozen -q &&
        /home/tester/venv/bin/python test/smoke/control_resilience.py'
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from claude_realms import control


def text(result):
    return "\n".join(block.text for block in result.content if block.type == "text")


def ask(socket, arguments, *, abandon_after=None):
    """Run ctl.py like the pane does; optionally kill it before the reply."""
    client = Path(control.__file__).with_name("ctl.py")
    process = subprocess.Popen([sys.executable, "-I", "-S", str(client), str(socket), json.dumps(arguments)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if abandon_after is not None:
        time.sleep(abandon_after)
        process.kill()
        process.wait()
        return None
    out, _ = process.communicate(timeout=120)
    return json.loads(out)


async def main():
    errlog = open("/tmp/realms-server.stderr", "w+")
    params = StdioServerParameters(command=sys.executable, args=["-m", "claude_realms.server"],
                                   env=dict(os.environ, CLAUDE_CODE_SESSION_ID="resilience"))
    async with stdio_client(params, errlog=errlog) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            started = await session.call_tool("realm", {"action": "on"})
            print("on:", text(started).splitlines()[0], flush=True)
            assert not started.isError, text(started)

            sockets = sorted(control.runtime_directory().glob("*.sock"), key=lambda p: p.stat().st_mtime)
            socket = sockets[-1]
            # Requests that take a while, abandoned before their reply.
            abandoned = [("doctor", 0.05), ("shot", 0.02), ("status", 0.0), ("doctor", 0.2), ("shot", 0.1)]
            for action, delay in abandoned:
                await asyncio.to_thread(ask, socket, {"action": action}, abandon_after=delay)
            await asyncio.sleep(8)  # let every late reply hit its closed connection

            status = await session.call_tool("realm", {"action": "status"})
            data = json.loads(status.content[1].text)
            print("status after abandoned requests: live =", bool(data["live"]), flush=True)
            assert not status.isError and data["live"], text(status)
            exec_result = await session.call_tool("realm_exec", {"command": "echo still-here"})
            assert "still-here" in text(exec_result), text(exec_result)
            reply = await asyncio.to_thread(ask, socket, {"action": "status"})
            assert not reply["isError"], reply
            print("control socket still answers:", reply["texts"][0].splitlines()[0], flush=True)
            await session.call_tool("realm", {"action": "stop"})
    errlog.seek(0)
    stderr = errlog.read()
    assert "Traceback" not in stderr, stderr
    print("PASS: server survived", len(abandoned), "abandoned control requests", flush=True)


asyncio.run(main())
