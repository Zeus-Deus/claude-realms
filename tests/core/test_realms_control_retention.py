"""Interrupted human control is durable; transport TTL never grants agent access."""
from pathlib import Path
import os
import contextlib
import socket
import tempfile
import threading
import time
import runpy
import subprocess
import sys

import pytest
from realms_test_paths import PLUGIN_ROOT

PLUGIN = PLUGIN_ROOT
load = runpy.run_path(str(PLUGIN / "realms_core/_binding.py"))["load_runtime"]
pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize("interruption", ["expired", "dead-owner", "disconnected"])
def test_hold_survives_transport_loss_and_epoch_fences_handback(tmp_path, interruption):
    authority = load("viewer_state").ControlAuthority(tmp_path / "state")
    realm, generation = "fixture", "generation:0"
    before = authority.agent_epoch(realm)
    ticket_epoch = authority.epoch(realm)
    assert authority.acquire(realm, generation, "first")
    assert not authority.acquire(realm, generation, "foreign")
    assert authority.status(realm) == {"controlled": True, "connected": True}
    if interruption == "expired":
        with authority._connection() as db:
            db.execute("UPDATE leases SET expires=0 WHERE realm=?", (realm,))
    elif interruption == "dead-owner":
        # An actually exited process identity, not a guessed PID or mocked alive().
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        try:
            own = load("lifecycle").identity(child.pid)
            assert own
            with authority._connection() as db:
                db.execute("UPDATE leases SET pid=?,started=? WHERE realm=?", (own["pid"], own["start_time"], realm))
        finally:
            child.communicate(timeout=5)
    else:
        authority.disconnect(realm, "first")
    reopened = load("viewer_state").ControlAuthority(tmp_path / "state")
    assert reopened.controlled(realm)
    assert reopened.status(realm) == {"controlled": True, "connected": False}
    with pytest.raises(PermissionError):
        reopened.agent_epoch(realm)
    with reopened.forwarding(realm, generation, "first") as allowed:
        assert not allowed
    assert reopened.acquire(realm, generation, "recovery")
    reopened.release(realm, "first")
    reopened.disconnect(realm, "first")
    assert reopened.status(realm) == {"controlled": True, "connected": True}
    with reopened.forwarding(realm, generation, "recovery") as allowed:
        assert allowed
    reopened.release(realm, "recovery")
    after = reopened.agent_epoch(realm)
    assert after > before
    assert reopened.epoch(realm) == ticket_epoch
    reopened.release(realm, "first")
    assert reopened.agent_epoch(realm) == after
    reopened.revoke(realm)
    assert reopened.agent_epoch(realm) > after
    assert reopened.epoch(realm) > ticket_epoch


@contextlib.contextmanager
def viewer_fixture(tmp_path, before_frame=None):
    # Same authenticated Unix byte-source route as the dependency-floor fixture;
    # no compositor, desktop, VM or input device is opened.
    with tempfile.TemporaryDirectory(prefix="realm-control-") as runtime:
        endpoint = Path(runtime) / "vnc"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(endpoint))
        endpoint.chmod(0o600)
        listener.listen()
        listener.settimeout(0.1)
        stopping = threading.Event()
        peers, threads, received = [], [], []

        def source(peer):
            with peer:
                peer.settimeout(0.1)
                peer.sendall(b"RFB 003.008\n")
                while not stopping.is_set():
                    try:
                        data = peer.recv(1024)
                    except socket.timeout:
                        continue
                    except OSError:
                        return
                    if not data:
                        return
                    received.append(data)

        def accept():
            while not stopping.is_set():
                try:
                    peer, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                peers.append(peer)
                worker = threading.Thread(target=source, args=(peer,))
                threads.append(worker)
                worker.start()

        worker = threading.Thread(target=accept)
        worker.start()
        record = {"id": "fixture", "generation": "first", "runtime_dir": runtime,
                  "vnc_socket": str(endpoint), "processes": {"worker": load("lifecycle").identity(os.getpid())}}
        server = load("bridge").ViewerServer(lambda rid: record if rid == "fixture" else None,
                                             state_dir=tmp_path / "authority")
        if before_frame:
            class ReceiveBoundary:
                def __init__(self, app):
                    self.app = app

                async def __call__(self, scope, receive, send):
                    async def intercepted():
                        message = await receive()
                        if message.get("bytes"):
                            before_frame(server, message["bytes"])
                        return message
                    await self.app(scope, intercepted, send)

            server.app.add_middleware(ReceiveBoundary)
        server.start()
        try:
            yield server, listener, peers, received
        finally:
            server.stop()
            stopping.set()
            listener.close()
            worker.join(5)
            for thread in threads:
                thread.join(5)
            assert not worker.is_alive()
            assert not any(thread.is_alive() for thread in threads)


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "viewer cleanup did not converge"
        time.sleep(0.02)


def test_revoked_live_viewer_closes_with_non_retryable_policy_code(tmp_path):
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed
    from websockets.typing import Subprotocol

    with viewer_fixture(tmp_path) as (server, listener, peers, received):
        token = server.issue("fixture", can_control=True)
        url = server.origin.replace("http:", "ws:") + "/api/realms/fixture/vnc"
        with connect(url, origin=server.origin, subprotocols=[Subprotocol("binary"), Subprotocol("realm." + token)], close_timeout=2) as ws:
            assert ws.recv(timeout=5) == b"RFB 003.008\n"
            server.revoke("fixture")
            with pytest.raises(ConnectionClosed) as closed:
                ws.recv(timeout=5)
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == 1008


@pytest.mark.parametrize("ending", [1000, 1001, 1005, 1006, "malformed", "expired", "proxy-failure", "initial-failure", "recovery-failure"])
def test_only_intentional_client_close_hands_back(tmp_path, ending):
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    with viewer_fixture(tmp_path) as (server, listener, peers, received):
        authority = server.authority
        before = authority.agent_epoch("fixture")
        token = server.issue("fixture", can_control=True)
        generation = server._ticket_generation(server.resolve_realm("fixture"))
        if ending == "recovery-failure":
            assert authority.acquire("fixture", generation, "previous")
            authority.disconnect("fixture", "previous")
        url = server.origin.replace("http:", "ws:") + "/api/realms/fixture/vnc"
        options = {"origin": server.origin, "subprotocols": ["binary", "realm." + token], "close_timeout": 2}
        if ending in ("initial-failure", "recovery-failure"):
            listener.shutdown(socket.SHUT_RDWR)
            listener.close()
            with pytest.raises(InvalidStatus):
                connect(url + "?control=1", **options)
        else:
            ws = connect(url + "?control=1", **options)
            try:
                assert ws.recv(timeout=5) == b"RFB 003.008\n"
                ws.send(b"RFB 003.008\n")
                wait_until(lambda: bool(received))
                assert authority.controlled("fixture")
                with pytest.raises(PermissionError):
                    authority.agent_epoch("fixture")
                # Normal takeover must not invalidate this ticket.
                assert server.tickets.check(token, "fixture", generation, control=True)
                if ending in (1000, 1001):
                    ws.close(code=ending)
                elif ending == 1005:
                    from websockets.frames import Frame, Opcode
                    with ws.send_context():
                        ws.protocol.send_frame(Frame(Opcode.CLOSE, b""))
                elif ending == 1006:
                    ws.socket.shutdown(socket.SHUT_RDWR)
                    ws.socket.close()
                elif ending == "malformed":
                    ws.send("not binary")
                elif ending == "expired":
                    server.tickets._clock = lambda: time.monotonic() + 10000
                else:
                    peers[0].shutdown(socket.SHUT_RDWR)
                if not isinstance(ending, int) or ending == 1005:
                    with pytest.raises(ConnectionClosed):
                        ws.recv(timeout=5)
            except ConnectionClosed:
                assert ending == 1005
            finally:
                ws.close()
        wait_until(lambda: not authority.status("fixture")["connected"])
        retained = ending not in (1000, 1001, "initial-failure")
        assert authority.controlled("fixture") is retained
        if retained:
            with pytest.raises(PermissionError):
                authority.agent_epoch("fixture")
            if ending != "recovery-failure":
                # A view-only reconnect neither reacquires input nor releases the hold.
                fresh = server.issue("fixture")
                with connect(url, **{**options, "subprotocols": ["binary", "realm." + fresh]}) as watch:
                    assert watch.recv(timeout=5) == b"RFB 003.008\n"
                assert authority.controlled("fixture")
            assert authority.acquire("fixture", generation, "manual-recovery")
            authority.release("fixture", "manual-recovery")
        assert authority.agent_epoch("fixture") > before


@pytest.mark.parametrize("invalidation", ["ticket-expiry", "revocation", "generation", "endpoint", "lease"])
def test_retained_hold_does_not_weaken_receive_boundary(tmp_path, invalidation):
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed

    handshake = b"RFB 003.008\n\x01\x01"
    key = b"\x04\x01\x00\x00\x00\x00\x00A"
    intercepted = threading.Event()

    def invalidate(server, data):
        if data != key:
            return
        record = server.resolve_realm("fixture")
        if invalidation == "ticket-expiry":
            server.tickets._clock = lambda: time.monotonic() + 10000
        elif invalidation == "revocation":
            server.revoke("fixture")
        elif invalidation == "generation":
            record["generation"] = "replaced"
        elif invalidation == "endpoint":
            Path(record["vnc_socket"]).chmod(0o644)
        else:
            authority = server.authority
            with authority._connection() as db:
                lease = db.execute("SELECT lease FROM leases WHERE realm='fixture'").fetchone()[0]
            authority.disconnect("fixture", lease)
            assert authority.acquire("fixture", server._ticket_generation(record), "new-controller")
        intercepted.set()

    with viewer_fixture(tmp_path, invalidate) as (server, _, __, received):
        token = server.issue("fixture", can_control=True)
        url = server.origin.replace("http:", "ws:") + "/api/realms/fixture/vnc?control=1"
        with connect(url, origin=server.origin, subprotocols=["binary", "realm." + token]) as ws:
            assert ws.recv(timeout=5) == b"RFB 003.008\n"
            ws.send(handshake)
            wait_until(lambda: b"".join(received) == handshake)
            ws.send(key)
            with pytest.raises(ConnectionClosed):
                ws.recv(timeout=5)
        assert intercepted.is_set()
        assert b"".join(received) == handshake
        assert server.authority.controlled("fixture")
        with pytest.raises(PermissionError):
            server.authority.agent_epoch("fixture")
        if invalidation == "lease":
            assert server.authority.status("fixture")["connected"]
