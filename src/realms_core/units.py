"""Process-tree ownership backends for a realm's guardian and desktop scope.

A realm owns its whole process tree. Two backends provide that ownership:

``systemd``
    The original design: the guardian is a transient ``--user`` service and
    the desktop runs in a transient scope bound to it. Membership is the
    scope's cgroup, so even double-forked daemons are owned and stopping the
    scope kills everything. Used whenever a systemd user manager is reachable.

``direct``
    For hosts without a user manager (containers, minimal installs, WSL-like
    environments). The guardian and the desktop worker are ordinary session
    leaders that mark themselves *child subreapers*, so orphaned descendants
    reparent to them instead of to init. Membership is the process tree below
    the recorded root (PID plus kernel start time, never a bare PID), and
    stopping signals that tree. A process that deliberately reparents itself
    outside the tree is not contained; neither backend is a hostile-code
    sandbox.

A realm record stores the backend that started it, so every later check uses
the same rules no matter what the host offers today.
"""

import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

BACKENDS = ("systemd", "direct")
_PR_SET_CHILD_SUBREAPER = 36
_UNIT = re.compile(r"[A-Za-z0-9@_.:-]+\.(scope|service)")


class UnitError(RuntimeError):
    pass


def host_control_env():
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
    }


def systemd_available():
    """True when a systemd user manager answers on this host."""
    if not shutil.which("systemctl") or not shutil.which("systemd-run"):
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "basic.target", "--property=ActiveState"],
            env=host_control_env(), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "ActiveState=active"


def select(preference=None):
    """The backend name for a new realm: explicit preference, else auto."""
    preference = preference or os.environ.get("REALMS_PROCESS_BACKEND") or "auto"
    if preference not in ("auto", *BACKENDS):
        raise UnitError("process backend must be auto, systemd or direct")
    if preference == "auto":
        return "systemd" if systemd_available() else "direct"
    if preference == "systemd" and not systemd_available():
        raise UnitError("the systemd process backend needs a running systemd user manager")
    return preference


def backend_of(record):
    name = record.get("backend", "systemd")
    if name not in BACKENDS:
        raise UnitError("unknown process backend " + repr(name))
    return name


# --------------------------------------------------------------------------
# Kernel process identity helpers (shared by both backends)


def _stat(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    fields = raw.rsplit(")", 1)[1].split()
    return fields


def identity(pid):
    fields = _stat(pid)
    if fields is None or fields[0] == "Z":
        return None
    return {"pid": int(pid), "start_time": int(fields[19])}


def alive(process):
    return bool(process) and identity(process["pid"]) == process


def become_subreaper():
    """Keep orphaned descendants in this process's tree (Linux >= 3.4)."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise UnitError("could not become a child subreaper: errno " + str(ctypes.get_errno()))


def descendants(root):
    """PIDs of ``root`` (an identity) and every live descendant, or empty."""
    if not alive(root):
        return []
    children = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        fields = _stat(entry.name)
        if fields is None or fields[0] == "Z":
            continue
        children.setdefault(int(fields[1]), []).append(int(entry.name))
    found, pending = [], [root["pid"]]
    while pending:
        pid = pending.pop()
        found.append(pid)
        pending.extend(children.get(pid, ()))
    return found


# --------------------------------------------------------------------------
# systemd backend


def _systemd_info(unit):
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "--property=LoadState,InvocationID,ControlGroup,ActiveState,Description"],
            env=host_control_env(), capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnitError("could not inspect systemd unit " + unit) from exc
    info = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not info.get("ActiveState"):
        raise UnitError("could not inspect systemd unit state " + unit)
    return info


def _systemd_pids(record, key):
    cgroup = _systemd_info(record[key]).get("ControlGroup", "")
    if not cgroup:
        return []
    path = Path("/sys/fs/cgroup") / cgroup.lstrip("/") / "cgroup.procs"
    try:
        return [int(pid) for pid in path.read_text(encoding="utf-8").split()]
    except FileNotFoundError:
        return []


def _systemd_stop(unit):
    try:
        subprocess.run(["systemctl", "--user", "stop", unit], env=host_control_env(),
                       capture_output=True, timeout=12, check=True)
    except subprocess.CalledProcessError:
        # BindsTo/--collect can retire the unit between inspection and stop.
        if _systemd_info(unit).get("ActiveState") not in ("inactive", "failed"):
            raise
        return
    if _systemd_info(unit).get("ActiveState") not in ("inactive", "failed"):
        raise UnitError("unit " + unit + " has not stopped")


# --------------------------------------------------------------------------
# direct backend: unit receipts live beside the registry


def _units_dir(home):
    path = Path(home) / "realms" / "units"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _receipt(home, unit):
    if not _UNIT.fullmatch(unit):
        raise UnitError("invalid unit name " + repr(unit))
    return _units_dir(home) / (unit + ".json")


def publish_direct(home, unit, process, description):
    """Record ``process`` (an identity) as the root of ``unit``."""
    from .lifecycle import atomic_json

    if identity(process["pid"]) != process:
        raise UnitError("unit root exited before publication")
    atomic_json(_receipt(home, unit), {
        "unit": unit,
        "root": process,
        "description": description,
        "invocation_id": "direct-%d-%d" % (process["pid"], process["start_time"]),
    })


def _direct_info(home, unit):
    path = _receipt(home, unit)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"LoadState": "not-found", "ActiveState": "inactive", "InvocationID": "",
                "ControlGroup": "", "Description": ""}
    root = data["root"]
    active = alive(root)
    return {
        "LoadState": "loaded",
        "ActiveState": "active" if active else "inactive",
        "InvocationID": data["invocation_id"] if active else "",
        "ControlGroup": "direct:%d:%d" % (root["pid"], root["start_time"]),
        "Description": data["description"],
        "_root": root,
    }


def _direct_stop(home, unit, timeout=3.0):
    info = _direct_info(home, unit)
    root = info.get("_root")
    if root is not None:
        for number in (signal.SIGTERM, signal.SIGKILL):
            for pid in reversed(descendants(root)):
                try:
                    os.kill(pid, number)
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.monotonic() + timeout
            while descendants(root) and time.monotonic() < deadline:
                time.sleep(0.05)
            if not descendants(root):
                break
        if descendants(root):
            raise UnitError("unit " + unit + " has not stopped")
    _receipt(home, unit).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Backend-neutral API used by the lifecycle code


def info(record, key):
    """``systemctl show``-shaped state of ``record[key]`` (scope or guardian)."""
    if backend_of(record) == "systemd":
        return _systemd_info(record[key])
    return _direct_info(record["home"], record[key])


def pids(record, key="scope"):
    if backend_of(record) == "systemd":
        return _systemd_pids(record, key)
    root = _direct_info(record["home"], record[key]).get("_root")
    return descendants(root) if root else []


def member(record, pid, key="scope"):
    """Whether ``pid`` belongs to the unit the record names under ``key``."""
    if backend_of(record) == "systemd":
        try:
            return (Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").strip()
                    == "0::" + record["cgroup"])
        except FileNotFoundError:
            return False
    return int(pid) in pids(record, key)


def stop(record, key="scope"):
    if backend_of(record) == "systemd":
        return _systemd_stop(record[key])
    return _direct_stop(record["home"], record[key])


def guardian_command(record, argv, *, env, cleanup_argv):
    """argv that starts the realm's guardian (the supervisor) for ``record``."""
    if backend_of(record) == "systemd":
        import shlex

        cleanup = shlex.join(cleanup_argv).replace("%", "%%")
        return [
            "systemd-run", "--user", "--quiet", "--collect", "--pipe",
            "--service-type=exec", "--expand-environment=no",
            "--unit=" + record["guardian_unit"],
            "--setenv=PYTHONPATH=" + env["PYTHONPATH"],
            "--setenv=PATH=" + env["PATH"],
            "--property=ExecStopPost=:" + cleanup,
            *argv,
        ]
    # The supervisor publishes its own receipt, becomes a subreaper and runs
    # the cleanup itself on exit; start_new_session detaches it from callers.
    return list(argv)


def scope_command(record, argv, *, description, inhibit_who, inhibit_why):
    """argv that starts the desktop worker inside the realm's scope."""
    if backend_of(record) == "systemd":
        return [
            "systemd-run", "--user", "--scope", "--quiet", "--collect",
            "--unit=" + record["scope"],
            "--property=BindsTo=" + record["guardian_unit"],
            "--property=After=" + record["guardian_unit"],
            "--description=" + description,
            "--property=TimeoutStopSec=3s",
            "--",
            "systemd-inhibit", "--what=sleep", "--mode=block",
            "--who=" + inhibit_who, "--why=" + inhibit_why,
            *argv,
        ]
    return list(argv)


def capabilities(record_or_name):
    """What the backend can promise, for doctor output."""
    name = record_or_name if isinstance(record_or_name, str) else backend_of(record_or_name)
    if name == "systemd":
        return {"backend": "systemd", "containment": "cgroup scope", "sleep_inhibitor": True}
    return {"backend": "direct", "containment": "subreaper process tree", "sleep_inhibitor": False}
