"""Profile-scoped headless desktops. GUI separation, not a hostile-code sandbox."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import struct
import subprocess
import sys
import time
import uuid

from .config import Config, effective_home
from .lifecycle import (
    Registry,
    RealmError,
    OwnershipError,
    LegacyExportRequired,
    RPC_RESPONSE_LIMIT_BYTES,
    alive,
    atomic_json,
    identity,
    remove_runtime,
    require_retained_workspace,
    stop_scope,
    unit_info,
    validate_live,
    validate_environment,
)


class Manager:
    def __init__(self, home=None, *, realm_id=None):
        self.home = effective_home(home)
        if realm_id is None:
            self.config = Config.load(self.home)
        self.registry = Registry(self.home)
        if realm_id is not None:
            # Payload-only launchers attach to an already owned generation.
            # Its validated launch spec, not the host's current config, governs
            # that desktop; importing the host core here would break -P routing.
            with self.registry.lock():
                record = self.registry.get(realm_id)
                validate_live(record)
                spec = Path(record["runtime_dir"]) / "spec.json"
                self.config = Config(**json.loads(spec.read_text(encoding="utf-8")))

    def list(self):
        with self.registry.lock():
            self._reconcile_locked()
            return [
                dict(record, cleanup_note=str(LegacyExportRequired(record)))
                if "workspace_dir" not in record else record
                for record in self.registry.records()
            ]

    def _reconcile_locked(self):
        for record in self.registry.records():
            # Old loaded cleanup can erase both HOME and registry after our
            # lock is released. Do not trigger it, even for apparently dead or
            # stopped compute. Preserve recovery inventory without rewriting it.
            if "workspace_dir" not in record:
                continue
            if record["status"] == "starting":
                owner = Path(record["runtime_dir"]) / "owner.json"
                if owner.exists():
                    candidate = json.loads(owner.read_text(encoding="utf-8"))
                    if any(
                        candidate.get(key) != record.get(key)
                        for key in (
                            "id",
                            "generation",
                            "uid",
                            "home",
                            "session_id",
                            "runtime_dir",
                            "scope",
                            "guardian_unit",
                        )
                    ):
                        raise OwnershipError("startup ownership receipt changed")
                    if alive(candidate.get("supervisor")):
                        from .units import member

                        validate_live(candidate)
                        guardian = unit_info(record, "guardian_unit")
                        pid = candidate["supervisor"]["pid"]
                        if record.get("backend", "systemd") == "systemd":
                            in_guardian = (
                                Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").strip()
                                == "0::" + guardian.get("ControlGroup", "")
                            )
                        else:
                            in_guardian = member(candidate, pid, "guardian_unit")
                        if (
                            guardian["ActiveState"] != "active"
                            or guardian.get("InvocationID")
                            != candidate.get("guardian_invocation_id")
                            or not in_guardian
                        ):
                            raise OwnershipError("startup guardian invocation changed")
                        self.registry.put(candidate)
                        continue
                # A launcher can outlive the caller. Reserve its owner through
                # the startup deadline; absence must be observed on both units.
                if time.time() - record["created_at"] < 35:
                    continue
                guardian = unit_info(record, "guardian_unit")
                scope = unit_info(record, "scope")
                if any(
                    info["ActiveState"] not in ("inactive", "failed")
                    for info in (guardian, scope)
                ):
                    continue
                stop_scope(record)
                remove_runtime(self.registry, record)
            elif record["status"] == "cleanup_failed" or (
                record["status"] in ("running", "stopping")
                and (
                    not alive(record.get("supervisor"))
                    or not Path(record["runtime_dir"]).exists()
                )
            ):
                stop_scope(record)
                remove_runtime(self.registry, record)

    def start(self, session_id, *, before_start=None, owner_process=None):
        """Start or reuse this session's realm.

        ``owner_process`` (an identity, PID plus start time) ties the desktop's
        compute to a host process: when that process is gone, however it ended,
        the supervisor stops the desktop and keeps its workspace.
        """
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError("session_id must contain 1 to 256 characters")
        with self.registry.lock():
            for existing in self.registry.records():
                if existing["session_id"] == session_id:
                    require_retained_workspace(existing)
            self._reconcile_locked()
            retained = None
            for existing in self.registry.records():
                if existing["session_id"] == session_id and existing["status"] == "stopped":
                    if "workspace_dir" not in existing:
                        raise RealmError("legacy workspace must be recovered before restart")
                    if unit_info(existing, "guardian_unit")["ActiveState"] not in ("inactive", "failed"):
                        raise RealmError("realm guardian is still stopping; retry after reconciliation")
                    retained = existing
                    continue
                if (
                    existing["session_id"] == session_id
                    and existing["status"] != "running"
                ):
                    raise RealmError(
                        "realm is "
                        + existing["status"]
                        + "; retry after reconciliation"
                    )
                if (
                    existing["session_id"] == session_id
                    and existing["status"] == "running"
                ):
                    validate_live(existing)
                    existing["last_activity"] = time.time()
                    if owner_process is not None:
                        existing["owner_process"] = owner_process
                    self.registry.put(existing)
                    return existing
            if before_start is not None:
                before_start()
            from .units import select

            backend = select(self.config.process_backend)
            from dataclasses import replace

            launch_config = replace(self.config, renderer=self.config.resolved_renderer())
            generation = uuid.uuid4().hex
            realm_id = retained["id"] if retained else "r-" + generation[:24]
            runtime = Path(f"/run/user/{os.getuid()}/cr-{generation[:16]}")  # windows-footgun: ok — runtime package rejects non-Linux hosts
            runtime.mkdir(mode=0o700)
            logs = self.registry.root / "logs"
            logs.mkdir(mode=0o700, exist_ok=True)
            logfile = logs / (realm_id + ".log")
            record = {
                "id": realm_id,
                "session_id": session_id,
                "generation": generation,
                "uid": os.getuid(),  # windows-footgun: ok — runtime package rejects non-Linux hosts
                "home": str(self.home),
                "runtime_dir": str(runtime),
                "scope": "claude-realm-" + generation + ".scope",
                "status": "starting",
                "backend": backend,
                "guardian_unit": "claude-realm-" + generation + "-guard.service",
                "created_at": time.time(),
                "last_activity": time.time(),
                "size": self.config.size,
                "renderer": launch_config.renderer,
                "overlay": self.config.overlay,
                "cursor_theme": self.config.cursor_theme,
                "idle_ttl": self.config.idle_ttl,
                "log_path": str(logfile),
            }
            if owner_process is not None:
                record["owner_process"] = owner_process
            from . import workspace

            if retained:
                workspace.validate(retained)
                record["workspace_dir"] = retained["workspace_dir"]
            else:
                workspace.create(record)
            atomic_json(runtime / "workspace.json", record)
            atomic_json(runtime / "spec.json", asdict(launch_config))
            self.registry.put(record)
            env = dict(os.environ)
            # Fresh -P children resolve the CLI package only from this payload,
            # never the systemd working directory or caller PYTHONPATH.
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"  # windows-footgun: ok — runtime package rejects non-Linux hosts
            env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"  # windows-footgun: ok — runtime package rejects non-Linux hosts
            with logfile.open("w") as log:
                from .units import guardian_command

                os.chmod(logfile, 0o600)
                supervisor = [
                    sys.executable, "-P", "-m", "realms_core.supervisor",
                    str(self.home), realm_id,
                ]
                cleanup = [
                    sys.executable, "-P", "-m", "realms_core.supervisor",
                    "cleanup", str(self.home), realm_id, generation,
                ]
                command = guardian_command(record, supervisor, env=env, cleanup_argv=cleanup)
                try:
                    if before_start is not None:
                        before_start(prepared=record)
                    monitor = subprocess.Popen(
                        command,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        close_fds=True,
                        start_new_session=True,
                    )
                except BaseException:
                    remove_runtime(self.registry, record)
                    raise
            import threading

            threading.Thread(
                target=monitor.wait, name="realm-launcher-reaper", daemon=True
            ).start()
            deadline = time.monotonic() + 30
            try:
                while not (runtime / "ready.json").exists():
                    if (runtime / "error.json").exists() or monitor.poll() is not None:
                        raise RealmError(
                            "realm startup failed; see "
                            + str(logfile)
                            + "\n"
                            + logfile.read_text(encoding="utf-8")[-5000:]
                        )
                    if time.monotonic() > deadline:
                        raise RealmError("realm startup timed out; see " + str(logfile))
                    time.sleep(0.1)
                ready = json.loads((runtime / "ready.json").read_text(encoding="utf-8"))
                record["supervisor"] = json.loads(
                    (runtime / "supervisor.json").read_text(encoding="utf-8")
                )
                info = unit_info(record, "scope")
                if info.get("ActiveState") != "active":
                    raise RealmError("realm scope is not active")
                record.update(
                    status="running",
                    last_activity=time.time(),
                    processes=ready["processes"],
                    vnc_port=None,
                    vnc_socket=ready["vnc_socket"],
                    guardian_invocation_id=unit_info(record, "guardian_unit")[
                        "InvocationID"
                    ],
                    invocation_id=info["InvocationID"],
                    cgroup=info["ControlGroup"],
                )
                atomic_json(runtime / "owner.json", record)
                self.registry.put(record)
                return record
            except BaseException:
                stop_scope(record)
                remove_runtime(self.registry, record)
                raise

    def env(self, realm_id):
        with self.registry.lock():
            record = self.registry.get(realm_id)
            if record["status"] != "running":
                raise RealmError("realm is not running")
            validate_live(record)
            env = json.loads((Path(record["runtime_dir"]) / "ready.json").read_text(encoding="utf-8"))[
                "env"
            ]
            validate_environment(record, env)
            record["last_activity"] = time.time()
            self.registry.put(record)
            return env

    def _rpc(self, realm_id, *, fds=(), **request):
        self.env(realm_id)
        with self.registry.lock():
            record = self.registry.get(realm_id)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(15)
            connection.connect(str(Path(record["runtime_dir"]) / "control"))
            pid, uid, _ = struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != os.getuid() or identity(pid) != record["processes"]["worker"]:  # windows-footgun: ok — runtime package rejects non-Linux hosts
                raise OwnershipError("realm control endpoint ownership mismatch")
            payload = json.dumps(request).encode() + b"\n"
            if fds:
                import array

                sent = connection.sendmsg(
                    [payload],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))],
                )
                if sent < len(payload):
                    connection.sendall(payload[sent:])
            else:
                connection.sendall(payload)
            with connection.makefile("rb") as stream:
                response = json.loads(stream.readline(RPC_RESPONSE_LIMIT_BYTES))
        if "error" in response:
            raise RealmError(response["error"])
        return response["result"]

    def shot(self, realm_id, path):
        return self._rpc(
            realm_id, op="shot", path=str(Path(path).expanduser().resolve())
        )

    def resize(self, realm_id, size):
        from .config import parse_size

        parse_size(size)
        self._rpc(realm_id, op="resize", size=size)
        with self.registry.lock():
            record = self.registry.get(realm_id)
            record["size"] = size
            self.registry.put(record)
            return record

    def command_prefix(self, realm_id):
        """Prefix actual Popen argv; caller must use env(id) and sanitized env.

        An absolute script path avoids installing or injecting PYTHONPATH into
        the caller. Equivalent module CLI: python -m realms_core.launch HOME ID --.
        """
        self.env(realm_id)
        return (
            sys.executable,
            str(Path(__file__).with_name("launch.py").resolve()),
            str(self.home),
            realm_id,
            "--",
        )

    def doctor(self, realm_id=None):
        from .lifecycle import host_control_env
        from .install_driver import current_driver
        from .units import UnitError, capabilities, pids, select, systemd_available

        tools = {
            name: shutil.which(name)
            for name in (
                "labwc",
                "wayvnc",
                "wlr-randr",
                "grim",
                "dbus-daemon",
                "gdbus",
                "Xwayland",
                "bwrap",
            )
        }
        for path in ("/usr/lib/at-spi-bus-launcher", "/usr/lib/at-spi2-registryd"):
            tools[Path(path).name] = path if os.access(path, os.X_OK) else None
        try:
            backend = select(self.config.process_backend)
            backend_error = None
        except UnitError as exc:
            backend, backend_error = None, str(exc)
        driver = current_driver(self.home)
        report = {
            "ok": sys.platform == "linux" and all(tools.values()) and backend is not None
            and driver is not None,
            "driver_ready": driver is not None,
            "driver": driver,
            "platform": sys.platform,
            "tools": tools,
            "systemd_user": systemd_available(),
            "process_backend": capabilities(backend) if backend else {"error": backend_error},
            "security_boundary": "GUI separation and owned process lifecycle; not a hostile-code sandbox",
        }
        if realm_id is not None:
            try:
                self.env(realm_id)
                with self.registry.lock():
                    record = self.registry.get(realm_id)
                owned = set(pids(record))
                guarded = True
                if record.get("backend", "systemd") == "systemd":
                    inhibitors = json.loads(
                        subprocess.run(
                            ["systemd-inhibit", "--list", "--json=short"],
                            env=host_control_env(),
                            capture_output=True,
                            text=True,
                            check=True,
                            timeout=5,
                        ).stdout
                    )
                    own = [
                        item
                        for item in inhibitors
                        if item["pid"] in owned and item["who"] == "Claude Realms"
                    ]
                    guarded = bool(own) and all(
                        item["what"] == "sleep" and item["mode"] == "block" for item in own
                    )
                detail = next(
                    (
                        line.split("GL renderer:", 1)[1].strip()
                        for line in Path(record["log_path"]).read_text(encoding="utf-8").splitlines()
                        if "GL renderer:" in line
                    ),
                    record["renderer"],
                )
                outputs = self._rpc(realm_id, op="outputs")
                for output in outputs:
                    output["current_mode"] = next(
                        (mode for mode in output["modes"] if mode["current"]), None
                    )
                report.update(
                    realm=record,
                    scope_owned=True,
                    owned_processes=len(owned),
                    sleep_only_inhibitor=guarded,
                    outputs=outputs,
                    renderer_detail=detail,
                )
                report["ok"] = report["ok"] and guarded
            except (RealmError, OSError, ValueError, subprocess.SubprocessError) as exc:
                report.update(ok=False, error=str(exc))
        return report

    def exec(self, realm_id, command, *, cwd=None, wait=False, timeout=30):
        """Run a captured command in the realm.

        Capture keeps the first 256 KiB per stream, drains overflow without
        breaking writes, and reports *_truncated plus output_complete. Results
        survive five minutes after exit and capture EOF; live output never
        expires. At 128 retained/active jobs, admission fails rather than
        evicting results. FD-proxied command_prefix output has no capture cap.
        wait waits for the direct child, not daemonized descendants.
        """
        if (
            not isinstance(command, (list, tuple))
            or not command
            or not all(isinstance(arg, str) and "\x00" not in arg for arg in command)
        ):
            raise ValueError("command must be a nonempty argv list")
        job = self._rpc(
            realm_id,
            op="exec",
            command=list(command),
            cwd=str(Path(cwd or Path.cwd()).resolve()),
        )
        if not wait:
            return job
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._rpc(realm_id, op="job", job_id=job["job_id"])
            if status["returncode"] is not None:
                return status
            time.sleep(0.05)
        raise RealmError(
            "command wait timed out; job remains owned by realm: " + job["job_id"]
        )

    def delete_snapshot(self, realm_id, *, session_id):
        """Read-only confirmation capture; never reconcile or stop compute."""
        with self.registry.lock():
            record = self.registry.get(realm_id)
            if not session_id or record["session_id"] != session_id:
                raise OwnershipError("workspace belongs to another session")
            return self._delete_snapshot_locked(record)

    def _delete_snapshot_locked(self, record):
        from .workspace import begin_deletion, validate_deletion

        if record["status"] not in ("stopped", "deleting"):
            raise RealmError("only a stopped workspace can be deleted")
        units = {key: unit_info(record, key) for key in ("scope", "guardian_unit")}
        for value in units.values():
            value.pop("_root", None)
        if any(info["ActiveState"] not in ("inactive", "failed") for info in units.values()):
            raise RealmError("realm compute has not stopped")
        if record["status"] == "deleting":
            validate_deletion(record)
            receipt = record["deletion"]
        else:
            receipt = begin_deletion(record)["deletion"]
        info = self.registry.path(record["id"]).stat()
        # Every atomic registry publication invalidates the prompt, even if a
        # stop/start/stop restores identical record bytes and stable IDs.
        return {"record": record, "deletion": receipt, "compute": units,
                "publication": [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]}

    def delete(self, realm_id, *, session_id=None, expected_snapshot=None):
        """Explicit data deletion, never used by stop/reconciliation.

        This is a profile-administrative API like stop(id). Session surfaces
        must pass their resolved owner, not an owner supplied by the client.
        """
        from .workspace import begin_deletion, finish_deletion, sync_directory

        with self.registry.lock():
            if not self.registry.path(realm_id).exists():
                return False
            record = self.registry.get(realm_id)
            if session_id is not None and record["session_id"] != session_id:
                raise OwnershipError("workspace belongs to another session")
            if expected_snapshot is not None and self._delete_snapshot_locked(record) != expected_snapshot:
                raise OwnershipError("Delete confirmation target changed; confirm again")
            if record["status"] not in ("stopped", "deleting"):
                raise RealmError("only a stopped workspace can be deleted")
            if any(unit_info(record, key)["ActiveState"] not in ("inactive", "failed")
                   for key in ("scope", "guardian_unit")):
                raise RealmError("realm compute has not stopped")
            if record["status"] != "deleting":
                record = begin_deletion(record)
                self.registry.put(record)
            finish_deletion(record)
            self.registry.remove(realm_id)
            sync_directory(self.registry.root)
            return True

    def preflight_stop(self, session_id=None):
        """Validate the whole affected set without triggering reconciliation.

        Integration must call before revoking authority; stop still rechecks
        the current record under the same registry lock at its own boundary.
        """
        with self.registry.lock():
            records = [record for record in self.registry.records()
                       if session_id is None or record["session_id"] == session_id]
            for record in records:
                require_retained_workspace(record)
            return records

    def stop(self, realm_id):
        with self.registry.lock():
            if not self.registry.path(realm_id).exists():
                return False
            record = self.registry.get(realm_id)
            require_retained_workspace(record)
            if record["status"] in ("stopped", "deleting"):
                return True
            stop_scope(record)
            remove_runtime(self.registry, record)
        deadline = time.monotonic() + 8
        while alive(record.get("supervisor")) and time.monotonic() < deadline:
            time.sleep(0.1)
        return True
