"""The server outlives the failures that used to end a session's realms."""
import json
import socket

import anyio
import pytest

from claude_realms import control
from claude_realms.drivers import guarded


def test_a_client_that_gave_up_does_not_end_the_control_socket(tmp_path, monkeypatch):
    # The pane waits 30 s for a reply; a slower one used to crash the server
    # when it was finally sent to the closed connection.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    calls = []

    async def handler(arguments):
        calls.append(arguments)
        if arguments.get("slow"):
            await anyio.sleep(0.5)
        return ["ok"], False

    def ask(path, arguments, *, wait):
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
            client.sendall(json.dumps(arguments).encode() + b"\n")
            if not wait:
                return None  # leave before the reply, like a killed ctl.py
            return json.loads(client.makefile().readline())

    async def scenario():
        async with anyio.create_task_group() as group:
            await group.start(lambda task_status: control.serve(handler, session_id="t", task_status=task_status))
            path = control.runtime_directory() / (str(__import__("os").getpid()) + ".sock")
            await anyio.to_thread.run_sync(lambda: ask(path, {"slow": True}, wait=False))
            await anyio.sleep(1)  # the late reply hits the closed connection
            reply = await anyio.to_thread.run_sync(lambda: ask(path, {"action": "status"}, wait=True))
            group.cancel_scope.cancel()
        return reply

    assert anyio.run(scenario) == {"texts": ["ok"], "isError": False}
    assert len(calls) == 2


def test_a_failing_background_task_leaves_the_others_running():
    async def broken():
        raise ProcessLookupError("daemon already gone")

    async def scenario():
        survived = anyio.Event()
        async with anyio.create_task_group() as group:
            group.start_soon(guarded, "driver session", broken)

            async def other():
                await anyio.sleep(0.1)
                survived.set()

            group.start_soon(other)
        return survived.is_set()

    assert anyio.run(scenario)


def test_a_call_cancelled_by_the_client_ends_cancelled(tmp_path, monkeypatch):
    # The MCP library answers a cancelled request itself; returning a result
    # afterwards would answer twice and take the server down.
    from claude_realms import server as server_module

    monkeypatch.setenv("REALMS_HOME", str(tmp_path))
    realm_server = server_module.RealmServer()

    async def slow_call(name, arguments, *, person=False):
        await anyio.to_thread.run_sync(lambda: __import__("time").sleep(0.3))  # not cancellable
        return ["finished"]

    monkeypatch.setattr(realm_server, "_call_tool", slow_call)

    async def scenario():
        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as group:
                async def call():
                    await realm_server.call_tool("realm", {})
                    pytest.fail("a cancelled call returned a result")

                group.start_soon(call)
                await anyio.sleep(0.05)
                scope.cancel()
        return scope.cancelled_caught

    assert anyio.run(scenario)
