"""The person's control channel: the pane and /realm, without the model's tools.

Claude Code routes a plugin's own ``$.mcp.call`` through the tool permission
flow, so a pane that polls status through MCP would keep asking the person to
approve their own clicks. The mod reaches this server another way: a Unix
socket (mode 0600, in a 0700 directory under ``XDG_RUNTIME_DIR``) that only
the same user can connect to, found through a small rendezvous file naming
the server's ancestor processes. The mod knows its Claude Code process ID and
picks the server that descends from it.

Protocol: one JSON request line (the ``realm`` tool's arguments), one JSON
response line ``{"texts": [...], "isError": bool}``. The model never sees
this socket; it keeps calling the MCP tools under the session's permissions.
"""

import json
import os
from pathlib import Path
import socket
import struct
import sys

import anyio

from realms_core.lifecycle import atomic_json, identity


def runtime_directory():
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base or not Path(base).is_dir():
        base = "/tmp"
    path = Path(base) / ("claude-realms-%d" % os.getuid())
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if info.st_uid != os.getuid() or path.is_symlink() or info.st_mode & 0o077:
        raise PermissionError("control directory has unsafe ownership: " + str(path))
    return path


def ancestors(pid=None):
    """This process and every ancestor, each with its kernel start time."""
    chain = []
    pid = pid or os.getpid()
    while pid and pid > 1:
        process = identity(pid)
        if process is None:
            break
        chain.append(process)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            pid = int(stat[1])
        except (OSError, ValueError, IndexError):
            break
    return chain


def _peer_uid(sock):
    try:
        raw = sock.extra(anyio.abc.SocketAttribute.raw_socket)
        _, uid, _ = struct.unpack("3i", raw.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid
    except Exception:  # noqa: BLE001
        return None


async def serve(handler, *, session_id, task_status=anyio.TASK_STATUS_IGNORED):
    """Serve ``handler(args) -> (texts, is_error)`` until cancelled."""
    directory = runtime_directory()
    pid = os.getpid()
    path = directory / f"{pid}.sock"
    path.unlink(missing_ok=True)
    listener = await anyio.create_unix_listener(path)
    os.chmod(path, 0o600)
    receipt = directory / f"{pid}.json"
    atomic_json(receipt, {
        "pid": pid,
        "ancestors": ancestors(),
        "session_id": session_id,
        "socket": str(path),
        "python": sys.executable,
        "client": str(Path(__file__).with_name("ctl.py")),
    })
    os.chmod(receipt, 0o600)

    async def connection(stream):
        async with stream:
            try:
                if _peer_uid(stream) not in (None, os.getuid()):
                    return
                data = b""
                with anyio.fail_after(10):
                    while not data.endswith(b"\n"):
                        chunk = await stream.receive(65536)
                        if not chunk:
                            return
                        data += chunk
                        if len(data) > 1 << 20:
                            return
                texts, is_error = await handler(json.loads(data))
                reply = {"texts": texts, "isError": is_error}
            except Exception as exc:  # noqa: BLE001 - reported to the person
                reply = {"texts": [type(exc).__name__ + ": " + str(exc)], "isError": True}
            try:
                await stream.send(json.dumps(reply, default=str).encode() + b"\n")
            except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                pass  # the client gave up waiting; nobody is left to tell

    try:
        task_status.started()
        async with listener:
            await listener.serve(connection)
    finally:
        path.unlink(missing_ok=True)
        receipt.unlink(missing_ok=True)
