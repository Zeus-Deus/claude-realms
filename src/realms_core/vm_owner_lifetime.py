"""Pre-launch systemd ownership and serialized VM owner handoff.

The guest depends on a stable guardian, never the reverse. Handoff replaces a
private receipt under the same flock used to commit retirement; it must not
stop/restart that guardian (which would also stop the guest).
"""
from contextlib import contextmanager
import fcntl  # windows-footgun: ok — runtime package rejects non-Linux hosts
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys

from .lifecycle import (
    OwnershipError, RealmError, atomic_json, host_control_env, identity, scope_info,
)
from .setup_process import open_pidfd

_BINDING_FIELDS = (
    "id", "generation", "uid", "home", "runtime_dir", "session_dir", "unit", "guardian_unit",
)
_TERMINAL = {"inactive", "failed"}


def _binding(record):
    from .vm_manager import validate_vm_record
    validate_vm_record(record)
    binding = {key: record[key] for key in _BINDING_FIELDS}
    if "owner_protocol" in record:
        if record["owner_protocol"] != "pidfd-v2":
            raise OwnershipError("unsupported VM owner protocol")
        binding["owner_protocol"] = record["owner_protocol"]
    return binding


def owner_unit(record):
    _binding(record)
    suffix = "-lifetime.service" if "owner_protocol" in record else "-owner.service"
    return record["unit"].removesuffix(".service") + suffix


def receipt_path(record):
    _binding(record)
    return Path(record["runtime_dir"]) / "owner.json"


def _private_fd(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:  # windows-footgun: ok — runtime package rejects non-Linux hosts
        os.close(fd)
        raise OwnershipError("VM owner receipt permissions changed")
    return fd


@contextmanager
def owner_lock(record, *, create=False):
    _binding(record)
    flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0)
    try:
        fd = _private_fd(Path(record["runtime_dir"]) / "owner.lock", flags)
    except OSError as exc:
        raise OwnershipError("VM owner lock is unavailable") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        opened = os.fstat(fd)
        current = (Path(record["runtime_dir"]) / "owner.lock").lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OwnershipError("VM owner lock identity changed")
        yield
    finally:
        os.close(fd)


def read_receipt(record):
    try:
        fd = _private_fd(receipt_path(record), os.O_RDONLY)
        with os.fdopen(fd, encoding="utf-8") as stream:
            receipt = json.load(stream)
    except (OSError, ValueError) as exc:
        raise OwnershipError("VM owner receipt is unavailable") from exc
    if (not isinstance(receipt, dict) or receipt.get("binding") != _binding(record)
            or receipt.get("state") not in {"live", "retiring"}):
        raise OwnershipError("VM owner receipt binding changed")
    return receipt


@contextmanager
def live_owner_fd(owner):
    if (not isinstance(owner, dict) or set(owner) != {"pid", "start_time"}
            or any(type(value) is not int or value <= 0 for value in owner.values())
            or owner["pid"] <= 1):
        raise OwnershipError("invalid VM owner process identity")
    fd = None
    try:
        try:
            fd = open_pidfd(owner["pid"])
            watcher = select.poll()
            watcher.register(fd, select.POLLIN)
            if (identity(owner["pid"]) != owner or watcher.poll(0)
                    or Path(f"/proc/{owner['pid']}").stat().st_uid != os.getuid()):  # windows-footgun: ok — runtime package rejects non-Linux hosts
                raise OwnershipError("VM owner process exited or identity changed")
        except OSError as exc:
            raise OwnershipError("VM owner pidfd is unavailable") from exc
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _control(arguments):
    return subprocess.run(
        ["/usr/bin/systemctl", "--user", *arguments], env=host_control_env(),
        capture_output=True, text=True, timeout=30, check=True,
    )


def dropin_directory(record):
    _binding(record)
    return Path(f"/run/user/{os.getuid()}/systemd/user/{record['unit']}.d")  # windows-footgun: ok — runtime package rejects non-Linux hosts


def _dropin(record):
    return (f"[Unit]\nBindsTo={owner_unit(record)}\nAfter={owner_unit(record)}\n"
            "[Service]\nTimeoutStopSec=10\nKillMode=control-group\n")


def install(record, *, live_guest=False, before_start=None):
    """Arm before launch, or add a distinct guardian to a witnessed legacy VM."""
    unit = owner_unit(record)
    if live_guest:
        from .vm_owner_migration import require_guest
        require_guest(record)
    for name in ((unit,) if live_guest else (record["unit"], unit)):
        info = scope_info(name)
        if info.get("LoadState") != "not-found" or info["ActiveState"] not in _TERMINAL:
            raise OwnershipError("VM startup unit already exists: " + name)
    owner = identity(os.getpid())
    with live_owner_fd(owner), owner_lock(record, create=True):
        if receipt_path(record).exists():
            raise OwnershipError("VM owner receipt already exists")
        atomic_json(receipt_path(record), {
            "binding": _binding(record), "owner": owner, "state": "live",
            "guard_invocation_id": None,
        })
    # Start/pidfd-ready FIRST. A live legacy VM must never depend on an
    # inactive guardian, even momentarily during daemon-reload.
    command = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--collect",
        "--service-type=notify", "--unit=" + unit,
        "--setenv=HOME=" + record["home"], "--setenv=HERMES_HOME=" + record["home"],
        "--property=NotifyAccess=main", "--property=TimeoutStartSec=15",
        "--property=TimeoutStopSec=5", "--property=Restart=no",
        sys.executable, "-I", str(Path(__file__).with_name("vm_owner_guard.py").resolve()),
        json.dumps(_binding(record)),
    ]
    env = host_control_env()
    if before_start is not None:
        before_start()
    subprocess.run(command, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                   timeout=30, check=True)
    receipt = read_receipt(record)
    invocation = receipt.get("guard_invocation_id")
    info = scope_info(unit)
    if (receipt["state"] != "live" or not invocation
            or info["ActiveState"] != "active" or info.get("InvocationID") != invocation):
        raise OwnershipError("VM owner guardian did not acknowledge startup")
    record["owner_invocation_id"] = invocation
    directory = dropin_directory(record)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    with (directory / "50-owner-lifetime.conf").open("x", encoding="utf-8") as stream:
        stream.write(_dropin(record))
    _control(["daemon-reload"])


def _require_guard(record, receipt):
    if receipt["state"] != "live":
        raise OwnershipError("VM owner guardian is retiring")
    info = scope_info(owner_unit(record))
    if (not record.get("owner_invocation_id")
            or receipt.get("guard_invocation_id") != record["owner_invocation_id"]
            or info.get("InvocationID") != record["owner_invocation_id"]
            or info["ActiveState"] != "active"):
        raise OwnershipError("VM owner guardian is not the owned live invocation")


def validate_owner(record):
    """Retiring/dead ownership is not a routable guest, even before QEMU exits."""
    from .vm_owner_migration import is_legacy, validate_legacy
    if "owner_migration" in record:
        raise OwnershipError("VM owner migration is incomplete")
    if is_legacy(record):
        validate_legacy(record)
        return
    with owner_lock(record):
        receipt = read_receipt(record)
        _require_guard(record, receipt)
        with live_owner_fd(receipt.get("owner")):
            return


def handoff(record):
    """A live guardian changes pidfds without a systemd stop/start gap."""
    if "owner_migration" in record:
        raise OwnershipError("VM owner migration is incomplete")
    with owner_lock(record):
        receipt = read_receipt(record)
        _require_guard(record, receipt)
        guest = scope_info(record["unit"])
        if (guest["ActiveState"] != "active" or not record.get("invocation_id")
                or guest.get("InvocationID") != record["invocation_id"]):
            raise OwnershipError("VM is not the owned live invocation")
        owner = identity(os.getpid())
        with live_owner_fd(receipt.get("owner")), live_owner_fd(owner):
            receipt["owner"] = owner
            atomic_json(receipt_path(record), receipt)


def stop_invocation(unit, invocation):
    """Unknown observations retain ownership; no bare-name/PID fallback."""
    info = scope_info(unit)
    if info["ActiveState"] in _TERMINAL:
        return
    if not invocation or info.get("InvocationID") != invocation:
        raise OwnershipError("VM unit is not the owned invocation: " + unit)
    try:
        _control(["stop", unit])
    except subprocess.CalledProcessError:
        if scope_info(unit)["ActiveState"] not in _TERMINAL:
            raise
        return
    if scope_info(unit)["ActiveState"] not in _TERMINAL:
        raise RealmError("VM unit has not stopped: " + unit)


def retire(record):
    """Stop the stable guardian first; BindsTo also covers an unready guest."""
    invocation = record.get("owner_invocation_id")
    path = receipt_path(record)
    guest = scope_info(record["unit"])
    if (guest["ActiveState"] not in _TERMINAL and record.get("invocation_id")
            and guest.get("InvocationID") != record["invocation_id"]):
        # Stopping a dependency would also stop a substituted guest. Check it
        # before any guardian mutation, not only before direct StopUnit.
        raise OwnershipError("VM unit is not the owned invocation")
    if not invocation and not path.exists() and not path.is_symlink():
        # Legacy records cannot prove their polling watcher's invocation.
        # Stop only the witnessed guest; its old BindsTo followers retire with
        # it. Do not make a legacy stop impossible by signalling an unknown guard.
        stop_invocation(record["unit"], record.get("invocation_id"))
        for name in (owner_unit(record), record["guardian_unit"]):
            if scope_info(name)["ActiveState"] not in _TERMINAL:
                raise OwnershipError("legacy VM follower has not retired: " + name)
        return
    if path.exists() or path.is_symlink():
        with owner_lock(record):
            receipt = read_receipt(record)
            captured = receipt.get("guard_invocation_id")
            if invocation and captured != invocation:
                raise OwnershipError("VM owner guardian invocation changed")
            invocation = captured
            receipt["state"] = "retiring"
            atomic_json(path, receipt)
    stop_invocation(owner_unit(record), invocation)
    # Startup can die before SSH returns its invocation. The installed BindsTo
    # is then responsible for teardown; an unexplained live unit is NOT ours
    # to kill. Keep the registry and receipt rather than forgetting it.
    stop_invocation(record["unit"], record.get("invocation_id"))
    stop_invocation(record["guardian_unit"], record.get("guardian_invocation_id"))


def remove_dropin(record):
    """Remove only this receipt's exact policy after every unit is stopped."""
    for name in (record["unit"], owner_unit(record), record["guardian_unit"]):
        if scope_info(name)["ActiveState"] not in _TERMINAL:
            raise OwnershipError("cannot remove a live VM owner policy")
    directory = dropin_directory(record)
    if not directory.exists() and not directory.is_symlink():
        # A previous removal may have completed on disk but failed its reload.
        # Retry that observation before allowing the registry to disappear.
        _control(["daemon-reload"])
        return
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():  # windows-footgun: ok — runtime package rejects non-Linux hosts
        raise OwnershipError("VM owner policy directory changed")
    path = directory / "50-owner-lifetime.conf"
    if path.is_symlink() or path.read_text(encoding="utf-8") != _dropin(record):
        raise OwnershipError("VM owner policy changed")
    path.unlink()
    directory.rmdir()
    _control(["daemon-reload"])
