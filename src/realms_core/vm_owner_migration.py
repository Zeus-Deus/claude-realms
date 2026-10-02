"""One-way migration of the historical shell watcher, without guest restart.

Only long-lived adoption calls migrate(), under the VM registry lock. Routing
validation is read-only. A receipt/lock/protocol marker is never downgraded to
legacy, including incomplete migration. The old watcher is not suspended until
the new pidfd-ready guardian is an observed live dependency of this invocation.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import time

from .lifecycle import OwnershipError, host_control_env, identity, scope_info
from . import vm_owner_lifetime as lifetime


def is_legacy(record):
    lifetime._binding(record)
    if any(key in record for key in ("owner_protocol", "owner_invocation_id", "owner_migration")):
        return False
    paths = (lifetime.receipt_path(record), Path(record["runtime_dir"]) / "owner.lock",
             lifetime.dropin_directory(record))
    return not any(path.exists() or path.is_symlink() for path in paths)


def require_guest(record):
    lifetime._binding(record)
    info = scope_info(record["unit"])
    if (info["ActiveState"] != "active" or not record.get("invocation_id")
            or info.get("InvocationID") != record["invocation_id"]
            or not record.get("cgroup") or info.get("ControlGroup") != record["cgroup"]):
        raise OwnershipError("VM is not the owned live invocation and cgroup")
    return info


def _properties(unit, names):
    result = lifetime._control(["show", unit, "--all", "--property=" + ",".join(names)])
    info = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if set(info) != set(names):
        raise OwnershipError("incomplete VM migration unit observation")
    return info


def _legacy_unit(record):
    return record["unit"].removesuffix(".service") + "-owner.service"


def _require_no_stop_commands(unit):
    # systemctl show omits empty Exec arrays even with --all. Read the typed
    # properties instead of turning a missing observation into an empty one.
    def bus(arguments):
        result = subprocess.run(
            ["/usr/bin/busctl", "--user", "--json=short", *arguments],
            env=host_control_env(), capture_output=True, text=True, timeout=30, check=True,
        )
        return json.loads(result.stdout)

    address = bus(["call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                   "org.freedesktop.systemd1.Manager", "GetUnit", "s", unit])
    paths = address.get("data")
    if (address.get("type") != "o" or not isinstance(paths, list) or len(paths) != 1
            or not isinstance(paths[0], str) or not paths[0].startswith("/org/freedesktop/systemd1/unit/")):
        raise OwnershipError("legacy VM watcher object is unavailable")
    for name in ("ExecStop", "ExecStopPost"):
        value = bus(["get-property", "org.freedesktop.systemd1", paths[0],
                     "org.freedesktop.systemd1.Service", name])
        if value.get("type") != "a(sasbttttuii)" or value.get("data") != []:
            raise OwnershipError("legacy VM watcher has unknown or configured stop commands")


def _watcher(record):
    unit = _legacy_unit(record)
    names = ("ActiveState", "InvocationID", "ControlGroup", "MainPID", "BindsTo", "After",
             "Transient", "Restart", "Type", "KillMode", "FailureAction", "SuccessAction",
             "OnFailure", "OnSuccess", "PartOf", "PropagatesStopTo")
    info = _properties(unit, names)
    if (info["ActiveState"] != "active" or not re.fullmatch(r"[0-9a-f]{32}", info["InvocationID"])
            or not re.fullmatch(r"[1-9][0-9]*", info["MainPID"])
            or not info["ControlGroup"].endswith("/" + unit)
            or set(info["BindsTo"].split()) != {record["unit"]}
            or record["unit"] not in info["After"].split()
            or info["Transient"] != "yes" or info["Restart"] != "no"
            or info["Type"] not in {"simple", "exec"} or info["KillMode"] != "control-group"
            or info["FailureAction"] != "none" or info["SuccessAction"] != "none"
            or any(info[key] for key in ("OnFailure", "OnSuccess",
                                        "PartOf", "PropagatesStopTo"))):
        raise OwnershipError("legacy VM watcher is not the canonical live unit")
    _require_no_stop_commands(unit)
    # systemd can reorder the same dependency set during daemon-reload.
    # Compare its meaning, not the unstable presentation order.
    info["After"] = " ".join(sorted(info["After"].split()))
    return info


def _command(record, info, *, stopped=False):
    pid = int(info["MainPID"])
    root = Path(f"/proc/{pid}")
    try:
        argv = (root / "cmdline").read_bytes().split(b"\0")
        expected = rb"while kill -0 ([1-9][0-9]*) 2>/dev/null; do sleep 5; done; exec systemctl --user stop "
        match = re.fullmatch(expected + re.escape(record["unit"].encode()), argv[2]) if len(argv) == 4 else None
        state = (root / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
        if (argv[:2] != [b"/bin/sh", b"-c"] or argv[-1] != b"" or not match
                or (root / "exe").resolve() != Path("/bin/sh").resolve()
                or (root / "cgroup").read_text(encoding="utf-8").strip() != "0::" + info["ControlGroup"]
                or (state != "T" if stopped else state in {"T", "t", "Z", "X"})):
            raise OwnershipError("legacy VM watcher command or process identity changed")
        return int(match.group(1))
    except (OSError, ValueError, IndexError) as exc:
        raise OwnershipError("legacy VM watcher process is unavailable") from exc


@contextmanager
def _captured_watcher(record):
    require_guest(record)
    # Retiring this follower must not stop the VM through a reverse dependency.
    dependencies = _properties(record["unit"], ("BindsTo", "Requires", "Requisite"))
    if any(_legacy_unit(record) in value.split() for value in dependencies.values()):
        raise OwnershipError("legacy VM unexpectedly depends on its old watcher")
    info = _watcher(record)
    process = identity(int(info["MainPID"]))
    if process is None:
        raise OwnershipError("legacy VM watcher exited")
    with lifetime.live_owner_fd(process) as fd:
        owner = identity(_command(record, info))
        if owner is None:
            raise OwnershipError("legacy VM owner exited")
        with lifetime.live_owner_fd(owner):
            if _watcher(record) != info:
                raise OwnershipError("legacy VM watcher invocation changed")
            yield info, process, owner, fd


def validate_legacy(record):
    """Keep the existing owner, never the short-lived routing helper's PID."""
    with _captured_watcher(record):
        return


def _signal(fd, number):
    # Standalone Python builds can omit signal.pidfd_send_signal as well as
    # os.pidfd_open. Never fall back to signalling a reusable numerical PID.
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, number)
        return
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    send = libc.pidfd_send_signal
    send.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    send.restype = ctypes.c_int
    if send(fd, number, None, 0) < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _require_dependency(record):
    require_guest(record)
    lifetime._require_guard(record, lifetime.read_receipt(record))
    dependencies = _properties(record["unit"], ("BindsTo", "After"))
    if not all(lifetime.owner_unit(record) in value.split() for value in dependencies.values()):
        raise OwnershipError("VM lifetime dependency was not installed")


def migrate(record, registry):
    """Caller holds registry lock; failure retains a non-routable receipt.

    Do not roll back a new dependency by stopping its guardian: that would
    stop the guest. Before suspension the original watcher remains intact;
    after suspension the new guardian covers adopter death even on SIGKILL.
    """
    if not is_legacy(record) or record.get("status") != "running":
        raise OwnershipError("VM is not an unmigrated running legacy record")
    with _captured_watcher(record) as (info, process, old_owner, fd):
        record.update(owner_protocol="pidfd-v2", owner_migration="preparing",
                      legacy_owner={"unit": _legacy_unit(record),
                                    "invocation_id": info["InvocationID"], "process": process})
        # Persist before creating ANY new policy. A crash/missing modern
        # receipt must never masquerade as another eligible legacy migration.
        registry.put(record)
        lifetime.install(record, live_guest=True)
        registry.put(record)
        _require_dependency(record)
        if _watcher(record) != info or _command(record, info) != old_owner["pid"]:
            raise OwnershipError("legacy VM watcher changed during migration")
        with lifetime.live_owner_fd(old_owner):
            _signal(fd, signal.SIGSTOP)
        killed = False
        try:
            deadline = time.monotonic() + 5
            while True:
                state = Path(f"/proc/{process['pid']}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
                if state == "T":
                    break
                if time.monotonic() >= deadline:
                    raise OwnershipError("legacy VM watcher did not suspend")
                time.sleep(.01)
            if (_watcher(record) != info or identity(process["pid"]) != process
                    or _command(record, info, stopped=True) != old_owner["pid"]):
                raise OwnershipError("suspended legacy VM watcher identity changed")
            _require_dependency(record)
            # Kill the captured shell, never StopUnit on an unrecorded/reused
            # name. Its sleeping child cannot execute the shutdown command.
            _signal(fd, signal.SIGKILL)  # windows-footgun: ok — runtime package rejects non-Linux hosts
            killed = True
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            if not poller.poll(5000):
                raise OwnershipError("legacy VM watcher has not exited")
            deadline = time.monotonic() + 10
            while scope_info(_legacy_unit(record))["ActiveState"] not in {"inactive", "failed"}:
                current = scope_info(_legacy_unit(record))
                if current.get("InvocationID") != info["InvocationID"] or time.monotonic() >= deadline:
                    raise OwnershipError("legacy VM watcher has not retired")
                time.sleep(.05)
            _require_dependency(record)
            record.pop("owner_migration")
            registry.put(record)
        finally:
            if not killed:
                try:
                    _signal(fd, signal.SIGCONT)
                except ProcessLookupError:
                    # The exact captured process already exited, never retry
                    # with a numerical PID or a guessed replacement unit.
                    pass
