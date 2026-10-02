"""One Claude Code session's realms: which kind, whether on, and their actions.

An MCP server process lives exactly as long as its Claude Code session, so the
service is per process. Subagents of the session call the same server and
share its realm. Everything here is synchronous; the server runs it in worker
threads.
"""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import struct
import sys
import tempfile
import threading
import time

from realms_core.config import Config, KINDS, effective_home
from realms_core.lifecycle import RealmError

KIND_ALIASES = {"omarchy": "omarchy-vm", "vm": "omarchy-vm", "omarchy-vm": "omarchy-vm",
                "realm": "realm", "labwc": "realm", "desktop": "realm"}


class SetupRequired(RealmError):
    """Prerequisites are missing; the message says how to fix them."""


class RealmOff(RealmError):
    """The person turned realm use off for this session."""


def _png_size(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RealmError("realm capture did not return a PNG")
    return struct.unpack(">II", data[16:24])


class RealmService:
    def __init__(self, home, owner, settings):
        from realms_core.manager import Manager

        self.home = effective_home(home)
        self.owner = owner
        self.settings = settings
        self.manager = Manager(self.home)
        self.config = self.manager.config
        self._vm = None
        self.kind = self.config.default_kind
        self.enabled = self.config.default_mode != "host"
        self.lock = threading.RLock()
        self.events = []  # (time, text) for the UI: started, stopped, errors

    # ------------------------------------------------------------------ kinds

    @property
    def vm(self):
        if self._vm is None:
            from realms_core.vm_manager import VmManager

            self._vm = VmManager(self.home)
        return self._vm

    def _note(self, text):
        self.events.append((time.time(), text))
        del self.events[:-20]

    def records(self, kind=None):
        kind = kind or self.kind
        try:
            if kind == "omarchy-vm":
                return [r for r in self.vm.list() if r["session_id"] == self.owner]
            return [r for r in self.manager.list() if r["session_id"] == self.owner]
        except (RealmError, OSError, ValueError):
            return []

    def running(self, kind=None):
        return next((r for r in self.records(kind) if r.get("status") == "running"), None)

    # ------------------------------------------------------------------ setup

    def setup_status(self, kind=None):
        from .setup import readiness

        return readiness(self.home, kind or self.kind)

    def require_ready(self, kind):
        status = self.setup_status(kind)
        if not status["ready"]:
            raise SetupRequired(status["message"])

    # ------------------------------------------------------------- lifecycle

    def select(self, kind):
        kind = KIND_ALIASES.get(kind, kind)
        if kind not in KINDS:
            raise ValueError("realm kind must be realm or omarchy")
        with self.lock:
            if kind != self.kind:
                self._stop_kind(self.kind)
                self.kind = kind
        return kind

    def ensure(self, *, agent=False):
        """The running realm of the selected kind, starting it if needed."""
        with self.lock:
            if not self.enabled:
                if agent:
                    raise RealmOff(
                        "Realm use is off for this session. The person can turn it back on "
                        "with /realm on; ordinary tools remain available.")
                self.enabled = True
            record = self.running()
            if record is not None:
                if self.kind == "omarchy-vm":
                    return self.vm.start(self.owner)  # renews the lease, re-checks the guest
                self.manager.env(record["id"])  # validates and renews the idle lease
                return record
            if agent and not self.settings.auto_start:
                raise RealmError("No realm is running. Start it with the realm tool (action: on) "
                                 "or ask the person to run /realm on.")
            self.require_ready(self.kind)
            if self.kind == "omarchy-vm":
                record = self.vm.start(self.owner)
            else:
                from realms_core.units import identity

                record = self.manager.start(self.owner, owner_process=identity(os.getpid()))
            self._note("started " + record["id"])
            return record

    def _stop_kind(self, kind):
        for record in self.records(kind):
            if record.get("status") in ("running", "starting"):
                if kind == "omarchy-vm":
                    self.vm.stop(record["id"])
                else:
                    self.manager.stop(record["id"])
                self._note("stopped " + record["id"])

    def stop(self):
        with self.lock:
            self._stop_kind(self.kind)
            if self._vm is not None and self.kind != "omarchy-vm":
                self._stop_kind("omarchy-vm")

    def off(self):
        """Disable agent use for this session and stop its desktop."""
        with self.lock:
            self.enabled = False
            self.stop()

    def shutdown(self):
        """The session ended: stop compute, keep durable workspaces."""
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - best effort at exit; idle TTL is the backstop
            pass

    # --------------------------------------------------------------- status

    def status(self):
        rows = []
        for kind in KINDS:
            if kind == "omarchy-vm" and self._vm is None and self.kind != "omarchy-vm":
                continue
            for record in self.records(kind):
                rows.append({
                    "id": record["id"],
                    "kind": kind,
                    "state": "live" if record.get("status") == "running" else record.get("status"),
                    "size": record.get("size"),
                    "memory_mb": record.get("memory"),
                    "vnc_socket": record.get("vnc_socket") if record.get("status") == "running" else None,
                    "backend": record.get("backend", "systemd"),
                    "idle_ttl": record.get("idle_ttl", self.config.idle_ttl),
                    "last_activity": record.get("last_activity"),
                })
        live = next((row for row in rows if row["state"] == "live" and row["kind"] == self.kind), None)
        return {
            "owner": self.owner,
            "enabled": self.enabled,
            "kind": self.kind,
            "live": live,
            "realms": rows,
            "setup": self.setup_status(),
            "frames_argv": [sys.executable, "-P", "-m", "claude_realms.frames"],
            "input_path": str(self.input_path()),
            "events": [{"at": at, "text": text} for at, text in self.events[-5:]],
        }

    def input_path(self):
        """Where the pane writes a person's keys and clicks while they hold control."""
        directory = self.manager.registry.root / "input"
        directory.mkdir(mode=0o700, exist_ok=True)
        name = hashlib.sha256(self.owner.encode()).hexdigest()[:16] + ".json"
        return directory / name

    # -------------------------------------------------------------- actions

    def shot(self):
        """PNG bytes of the realm's screen, plus its size."""
        record = self.ensure()
        directory = Path(tempfile.mkdtemp(prefix="shot-", dir=self.manager.registry.root))
        try:
            source = self.vm if self.kind == "omarchy-vm" else self.manager
            target = Path(source.shot(record["id"], directory / "screen.png"))
            data = target.read_bytes()
            width, height = _png_size(data)
            return {"realm_id": record["id"], "png": data, "width": width, "height": height,
                    "sha256": hashlib.sha256(data).hexdigest()}
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def resize(self, size):
        if self.kind != "realm":
            raise ValueError("resize is available for the regular realm only")
        record = self.ensure()
        return self.manager.resize(record["id"], size)

    def exec(self, command, *, cwd=None, timeout=60):
        """Run a shell command inside the realm and wait for its result."""
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a nonempty string")
        timeout = max(1, min(float(timeout or 60), 600))
        record = self.ensure(agent=True)
        if self.kind == "omarchy-vm":
            return self.vm.guest_run(record["id"], self._as_desktop_user(record, command), timeout=timeout)
        env = self.manager.env(record["id"])
        workdir = cwd or env.get("HOME")
        job = self.manager.exec(record["id"], ["/bin/bash", "-lc", command], cwd=workdir,
                                wait=True, timeout=timeout)
        return {key: job.get(key) for key in (
            "returncode", "stdout", "stderr", "stdout_truncated", "stderr_truncated")}

    def launch(self, command, *, cwd=None):
        """Start a GUI program in the realm without waiting for it."""
        if isinstance(command, str):
            command = shlex.split(command)
        record = self.ensure(agent=True)
        if self.kind == "omarchy-vm":
            line = "setsid -f " + shlex.join(command) + " >/dev/null 2>&1 </dev/null"
            result = self.vm.guest_run(record["id"], self._as_desktop_user(record, line), timeout=30)
            if result["returncode"] != 0:
                raise RealmError("could not start the program in the guest: " + result["stderr"][-500:])
            return {"started": command}
        env = self.manager.env(record["id"])
        job = self.manager.exec(record["id"], list(command), cwd=cwd or env.get("HOME"), wait=False)
        return {"started": command, "job_id": job["job_id"], "pid": job["pid"]}

    def _as_desktop_user(self, record, line):
        """argv running a shell line as the guest's desktop user, on its session."""
        from realms_core.vm_launch import guest_shell_init

        user = self.vm.guest_user(record["id"])
        return ["runuser", "-u", user, "--", "bash", "-lc", guest_shell_init() + line]

    def push(self, source, destination=None):
        if self.kind != "omarchy-vm":
            raise ValueError("push copies into an Omarchy VM; the regular realm shares this filesystem")
        record = self.ensure()
        return self.vm.push(record["id"], source, *([destination] if destination else []))

    def pull(self, source, destination):
        if self.kind != "omarchy-vm":
            raise ValueError("pull copies out of an Omarchy VM; the regular realm shares this filesystem")
        record = self.ensure()
        return self.vm.pull(record["id"], source, destination)

    def watch(self, *, control=False, ttl=300):
        """A loopback noVNC link for a browser, with a short-lived ticket."""
        from realms_core.bridge import get_profile_viewer

        record = self.running()
        if record is None:
            raise RealmError("no realm is running for this session")
        viewer = get_profile_viewer(self.home).start()
        ttl = max(30, min(int(ttl), 3600))
        token = viewer.issue(record["id"], can_control=control, ttl=ttl)
        return {"url": viewer.origin + "/realms/" + record["id"] + "/view#ticket=" + token,
                "control": control, "expires_in": ttl}

    def doctor(self):
        report = {"realm": self.manager.doctor((self.running("realm") or {}).get("id"))}
        if self.kind == "omarchy-vm" or self._vm is not None:
            try:
                report["omarchy-vm"] = self.vm.doctor((self.running("omarchy-vm") or {}).get("id"))
            except (RealmError, OSError, ValueError) as exc:
                report["omarchy-vm"] = {"ok": False, "error": str(exc)}
        return report

    def env(self):
        record = self.ensure()
        return self.manager.env(record["id"])

    def driver_launcher(self, record):
        """An executable with the driver CLI's shape, bound to this realm."""
        from realms_core.install_driver import driver_executable

        binary = driver_executable(self.home)
        if self.kind == "omarchy-vm":
            from realms_core.vm_cua import create_vm_driver_launcher

            return create_vm_driver_launcher(self.vm, record["id"], binary)
        from realms_core.driver import create_driver_launcher

        return create_driver_launcher(self.manager, record["id"], binary)


def describe(result):
    """A short human line for a status payload."""
    live = result.get("live")
    if not result.get("enabled"):
        return "Realm use is off for this session (/realm on to enable)."
    if live:
        size = live.get("size") or (str(live.get("memory_mb")) + " MB" if live.get("memory_mb") else "")
        return f"{live['kind']} {live['id']} is live {size}".strip()
    if not result["setup"]["ready"]:
        return result["setup"]["message"]
    return f"No {result['kind']} running; it starts when the agent first uses a desktop tool."


def dumps(value):
    return json.dumps(value, indent=2, default=str)



# --------------------------------------------------------------------------
# Managing what realms leave on disk


def _disk_usage(path):
    total = 0
    try:
        for entry in Path(path).rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_blocks * 512
            except OSError:
                pass
    except OSError:
        pass
    return total


def inventory(service):
    """Every realm and VM workspace in this data home, newest first."""
    rows = []
    try:
        records = [("realm", r) for r in service.manager.list()]
    except (RealmError, OSError, ValueError):
        records = []
    try:
        if (service.home / "realms" / "vm").exists() or service._vm is not None:
            records += [("omarchy-vm", r) for r in service.vm.list()]
    except (RealmError, OSError, ValueError):
        pass
    for kind, record in records:
        place = record.get("workspace_dir") or record.get("session_dir")
        rows.append({
            "id": record["id"],
            "kind": kind,
            "state": "live" if record.get("status") == "running" else record.get("status"),
            "this_session": record.get("session_id") == service.owner,
            "last_used": record.get("last_activity") or record.get("stopped_at") or record.get("created_at"),
            "bytes": _disk_usage(place) if place else 0,
        })
    rows.sort(key=lambda row: row["last_used"] or 0, reverse=True)
    return rows


def delete(service, realm_id):
    """Delete one stopped workspace (its HOME or VM disk). Never a live one."""
    if realm_id.startswith("v-"):
        removed = service.vm.delete(realm_id)
    else:
        removed = service.manager.delete(realm_id)
    if not removed:
        raise RealmError("no realm " + realm_id)
    return realm_id


def clean(service, *, older_than_days=0, keep_session=True):
    """Delete stopped workspaces unused for ``older_than_days`` (0: all stopped)."""
    cutoff = time.time() - older_than_days * 86400
    removed, kept = [], []
    for row in inventory(service):
        if row["state"] != "stopped" or (keep_session and row["this_session"]):
            continue
        if older_than_days and (row["last_used"] or 0) > cutoff:
            continue
        try:
            delete(service, row["id"])
            removed.append(row)
        except (RealmError, OSError, ValueError) as exc:
            kept.append({"id": row["id"], "reason": str(exc)})
    return {"removed": removed, "freed_bytes": sum(r["bytes"] for r in removed), "kept": kept}


def human_bytes(count):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return str(count)


# --------------------------------------------------------------------------
# Reusing an Omarchy base image another realms host already built


def hermes_bases():
    """Omarchy VM bases built by hermes-realms profiles on this machine."""
    roots = [Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")]
    roots += sorted((roots[0] / "profiles").glob("*")) if (roots[0] / "profiles").is_dir() else []
    found = []
    for root in roots:
        data = root / "plugin-data" / "hermes-realms" / "vm"
        candidates = [data / "base"]
        try:
            pointer = json.loads((data / "current-base.json").read_text(encoding="utf-8"))
            candidates.insert(0, data / "bases" / pointer["generation"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        for base in candidates:
            disk, meta = base / "disk.qcow2", base / "base.json"
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
                if disk.is_file() and disk.stat().st_size > 0 and disk.stat().st_uid == os.getuid() \
                        and isinstance(info.get("built_at"), (int, float)):
                    found.append({"path": str(base), "iso": info.get("iso"), "built_at": info["built_at"],
                                  "bytes": disk.stat().st_size})
                    break
            except (OSError, ValueError):
                continue
    found.sort(key=lambda base: base["built_at"], reverse=True)
    return found


def import_base(service, source, progress=lambda message: None):
    """Copy a built base into this home (a reflink: instant on btrfs/xfs)."""
    import subprocess

    from realms_core.config import vm_data_path
    from realms_core.setup_plan import base_present

    if base_present(service.home):
        return {"imported": False, "reason": "an Omarchy base image is already present"}
    source = Path(source)
    data = vm_data_path(service.home)
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = data / (".import-" + str(os.getpid()))
    if staging.exists():
        shutil.rmtree(staging)
    progress("copying the Omarchy base from " + str(source))
    subprocess.run(["cp", "-a", "--reflink=auto", str(source), str(staging)], check=True, timeout=3600)
    os.chmod(staging, 0o700)
    target = data / "base"
    if target.exists():
        shutil.rmtree(staging)
        raise RealmError("a base directory appeared during the import")
    staging.rename(target)
    if not base_present(service.home):
        raise RealmError("the imported base is incomplete")
    progress("imported")
    return {"imported": True, "from": str(source)}
