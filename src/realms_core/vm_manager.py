"""Per-conversation disposable Omarchy guests in QEMU/KVM.

A second realm kind beside :mod:`realms_core.manager`. Where a labwc realm separates
GUIs inside this login session, a VM realm is a separate machine: its own
kernel, disk and desktop. That is what makes `omarchy-shell` (one Hyprland and
one quickshell per session) hostable at all, and it is why breaking the guest
cannot break the host.

The guest itself is not ours. It is installed by Omarchy's own signed ISO
through the vendored ``omarchy vm`` script, and every session boots a
copy-on-write clone of one installed base image, so a chat costs 196 KiB and a
few seconds rather than a download and an install.

Isolation honesty, same as the labwc realm's: project files are *copied* in and
out, never mounted; the guest reaches the network through QEMU user-mode
networking unless ``plugins.realms.vm.network`` is false; the disk has no
encryption and passwordless sudo, so nothing secret may be put in it.
"""

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from .config import Config, effective_home, vm_data_path
from .vm_ssh import enroll_host_key, require_host_key, shutdown_guest, ssh_options
from .vm_resources import require_resources
from .lifecycle import (
    RealmError,
    OwnershipError,
    alive,
    atomic_json,
    host_control_env,
    scope_info,
)


VENDORED_SCRIPT = Path(__file__).resolve().with_name("vendor") / "omarchy-vm"
# Upstream omacom/omarchy PR #10977 `bin/omarchy-vm` as vendored, before the
# local patches recorded in vendor/VENDOR.md. Checked on every launch so a
# tampered or silently upgraded copy cannot be executed.
UPSTREAM_SHA256 = "19a51517c2713e033b2f703bfa387a79a9880c1c7a918e327e1327cfde47c3b6"
# The vendored copy *with* those patches applied. Pinned in code rather than in
# a file beside the script: a checksum an attacker can rewrite pins nothing.
# Includes the synchronous installer-owned setup-stage hook.
VENDORED_SHA256 = "51a412e77243e8a7a24e1a3d316fc18160d233d64c6b2605f086b8611abeb015"

# Ports the per-session guests forward SSH on, loopback only. A realm picks the
# first free one and records it; a collision fails the launch rather than
# silently attaching to another guest.
SSH_PORT_RANGE = range(2300, 2400)

# Guest-bound environment. The host process environment holds this profile's
# API keys and desktop handles; forwarding it into a disposable guest with an
# unencrypted disk would put them on that disk. Only terminal presentation
# crosses the boundary.
FORWARDED_ENV = ("TERM", "LANG", "LC_ALL", "COLUMNS", "LINES")


class VmError(RealmError):
    pass


def _base_runtime(generation):
    return Path(f"/run/user/{os.getuid()}/hvb-{generation[:16]}")  # windows-footgun: ok — runtime package rejects non-Linux hosts


def _generation_paths(uid, generation):
    return Path(f"/run/user/{uid}/hv-{generation[:16]}")


class VmRegistry:
    """Crash-safe per-profile VM records, beside the labwc realm registry."""

    def __init__(self, home):
        self.root = Path(home) / "realms"
        if self.root.is_symlink():
            raise OwnershipError("registry must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_uid != os.getuid():  # windows-footgun: ok — runtime package rejects non-Linux hosts
            raise OwnershipError("registry has a foreign owner")
        os.chmod(self.root, 0o700)

    def lock(self):
        from .lifecycle import Registry

        return Registry(self.root.parent).lock()

    def path(self, vm_id):
        if not isinstance(vm_id, str) or not re.fullmatch(r"v-[0-9a-f]{24}", vm_id):
            raise VmError("invalid realm id")
        return self.root / (vm_id + ".json")

    def get(self, vm_id):
        try:
            value = json.loads(self.path(vm_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise VmError("realm not found: " + vm_id) from exc
        if (
            value.get("id") != vm_id
            or value.get("uid") != os.getuid()  # windows-footgun: ok — runtime package rejects non-Linux hosts
            or value.get("home") != str(self.root.parent)
        ):
            raise OwnershipError("registry ownership mismatch")
        validate_vm_record(value)
        return value

    def put(self, record):
        atomic_json(self.path(record["id"]), record)

    def records(self):
        return [self.get(p.stem) for p in sorted(self.root.glob("v-*.json"))]

    def remove(self, vm_id):
        self.path(vm_id).unlink(missing_ok=True)


def validate_vm_record(record):
    """Every identity in the record must be derivable from its generation.

    A record is the only thing standing between ``stop`` and a systemd unit, so
    a record that names a unit, runtime directory or socket it could not have
    created is refused rather than acted on.
    """
    generation = record.get("generation", "")
    if (
        not re.fullmatch(r"[0-9a-f]{32}", generation)
        or record.get("id") != "v-" + generation[:24]
    ):
        raise OwnershipError("invalid realm generation")
    runtime = _generation_paths(os.getuid(), generation)  # windows-footgun: ok — runtime package rejects non-Linux hosts
    if (
        record.get("runtime_dir") != str(runtime)
        or record.get("unit") != f"claude-vm-{generation}.service"
        or record.get("guardian_unit") != f"claude-vm-{generation}-guard.service"
    ):
        raise OwnershipError("realm runtime or unit ownership mismatch")
    home = Path(record.get("home", ""))
    session = home / "realms" / "vm" / generation
    if (not home.is_absolute() or record.get("uid") != os.getuid()  # windows-footgun: ok — runtime package rejects non-Linux hosts
            or home.resolve() != home or record.get("session_dir") != str(session)
            or session.resolve() != session or session.is_symlink()):
        raise OwnershipError("realm profile or session ownership mismatch")
    if record.get("status") == "running" and record.get("vnc_socket") != str(
        runtime / "vnc.sock"
    ):
        raise OwnershipError("realm VNC endpoint identity changed")
    if runtime.is_symlink():
        raise OwnershipError("realm runtime is a symlink")
    if runtime.exists():
        info = runtime.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o700:  # windows-footgun: ok — runtime package rejects non-Linux hosts
            raise OwnershipError("realm runtime permissions changed")


def unit_active(unit):
    # Failed observation is unknown, never permission to discard a live guest.
    return scope_info(unit).get("ActiveState") == "active"


class VmManager:
    KIND = "omarchy-vm"

    def __init__(self, home=None, *, vm_id=None):
        self.home = effective_home(home)
        self.registry = VmRegistry(self.home)
        self.data = vm_data_path(self.home)
        if vm_id is None:
            self.config = Config.load(self.home)
        else:
            # Payload-only launchers attach to an already owned generation. Its
            # validated launch spec, not the host's current config, governs that
            # guest; importing the host core here would resolve against whichever
            # host is on sys.path rather than the one that started the realm.
            self.config = Config(**json.loads(
                (Path(self.registry.get(vm_id)["session_dir"]) / "spec.json")
                .read_text(encoding="utf-8")))

    # --- vendored script ---

    def script(self, *, config=None):
        """The ``omarchy vm`` implementation to drive.

        The vendored copy is the default even when a system ``omarchy vm``
        exists: upstream opens an SDL window on the user's own desktop, which
        is exactly what a realm exists to avoid. ``vm.omarchy_vm_path`` lets an
        operator point at their own already-headless copy.
        """
        configured = (config or self.config).vm.omarchy_vm_path
        if configured:
            path = Path(configured)
            if not os.access(path, os.X_OK):
                raise VmError("configured vm.omarchy_vm_path is not executable")
            return path
        return VENDORED_SCRIPT

    def _script_env(self, *, vm_home, ssh_port=None, unit=None, runtime=None, config=None):
        """Host-side environment for one vendored-script invocation.

        Built from nothing rather than inherited: the script runs ssh, qemu and
        systemctl, and a realm-sanitized or secret-bearing environment reaching
        any of them is a bug either way.
        """
        config = config or self.config
        uid = os.getuid()  # windows-footgun: ok — runtime package rejects non-Linux hosts
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "USER": os.environ.get("USER") or Path.home().name,
            "XDG_RUNTIME_DIR": f"/run/user/{uid}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
            "OMARCHY_VM_HOME": str(vm_home),
            "OMARCHY_VM_ISO_DIR": str(self.data / "iso"),
            "OMARCHY_VM_MEMORY": str(config.vm.memory),
            "OMARCHY_VM_DISK_SIZE": config.vm.disk_size,
        }
        if ssh_port is not None:
            env["OMARCHY_VM_SSH_PORT"] = str(ssh_port)
        if unit is not None:
            env["OMARCHY_VM_UNIT"] = unit.removesuffix(".service")
        if runtime is not None:
            env["OMARCHY_VM_VNC_SOCKET"] = str(runtime / "vnc.sock")
            env["OMARCHY_VM_QMP_SOCKET"] = str(runtime / "qmp.sock")
        if not config.vm.network:
            # QEMU user networking with every outbound route refused. The guest
            # keeps its SSH forward, which is how we reach it at all.
            env["OMARCHY_VM_NETDEV_EXTRA"] = ",restrict=on"
        return env

    def _run_script(self, arguments, *, vm_home, ssh_port=None, unit=None,
                    runtime=None, timeout=120, check=True, stdout=None, setup_job=None, before_start=None, config=None):
        script = self.script(config=config)
        if script == VENDORED_SCRIPT:
            import hashlib

            digest = hashlib.sha256(script.read_bytes()).hexdigest()
            if digest != VENDORED_SHA256:
                raise VmError("vendored omarchy-vm script does not match its pin")
        env = self._script_env(vm_home=vm_home, ssh_port=ssh_port, unit=unit, runtime=runtime, config=config)
        if setup_job is not None and script == VENDORED_SCRIPT:
            # A synchronous, installer-owned hook, never stdout/log inference.
            env["OMARCHY_VM_PROGRESS_PYTHON"] = sys.executable
            env["OMARCHY_VM_SETUP_PROGRESS"] = json.dumps([
                str(Path(__file__).with_name("_binding.py")), str(self.home), *setup_job,
            ])
        if before_start is not None:
            before_start()
        return subprocess.run(
            [str(script), *arguments],
            env=env,
            capture_output=stdout is None,
            stdout=stdout,
            stderr=subprocess.PIPE if stdout is not None else None,
            text=stdout is None,
            timeout=float(timeout),
            check=check,
        )

    # --- base image ---

    def base_home(self):
        from .vm_base import selected
        return selected(self.home)

    def base_status(self):
        from .setup_plan import base_present
        base = self.base_home()
        disk = base / "disk.qcow2"
        info = {"present": base_present(self.home, base=base), "path": str(disk)}
        if info["present"]:
            info["bytes"] = disk.stat().st_size
            info["built_at"] = disk.stat().st_mtime
            metadata = base / "base.json"
            if metadata.is_file():
                try:
                    info.update(json.loads(metadata.read_text(encoding="utf-8")))
                except ValueError:
                    pass
        return info

    def install_base(self, *, iso=None, timeout=5400, stdout=None, generation=None, setup_job=None, update=False, release=None, expected_selection=None):
        """Install the shared base guest. Explicit, consented, never implicit.

        Downloads and signature-verifies the official ISO through the vendored
        script, installs unattended, provisions autologin and passwordless sudo
        in the guest, then powers it off so session clones start from a
        consistent disk. With update=True, select a new physical generation;
        never move, rewrite or delete the previous base or any workspace.
        release pins a literal stable ISO version through signed download,
        without resolving latest; it excludes local ISOs and custom installers.
        """
        if release is not None:
            from .vm_base import validate_release
            try:
                validate_release(release)
            except ValueError as exc:
                raise VmError(str(exc)) from exc
            if iso is not None:
                raise VmError("An explicit release cannot be combined with a local ISO")
            if self.config.vm.omarchy_vm_path:
                raise VmError("An explicit release requires the pinned vendored installer, not a custom installer")
        from .setup_plan import confined
        from .setup_process import vm_lifetime
        generation = generation or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise VmError("invalid base install generation")
        from .vm_base import selection_receipt, sync_assets, sync_directory
        with self.registry.lock():
            observed = json.loads(json.dumps(selection_receipt(self.home)))
            expected = observed if expected_selection is None else json.loads(json.dumps(expected_selection))
            if observed != expected:
                raise VmError("Base selection changed since review; prepare again")
            base = confined(self.home, self.data / "bases" / generation if update else self.base_home())
            if base.exists():
                raise VmError("a base image directory already exists; remove it explicitly before rebuilding")
        require_resources(self.config, self.data, install=True)
        self.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        confined(self.home, self.data / "iso").mkdir(mode=0o700, exist_ok=True)
        staging = self.data / (".install-" + generation)
        staging.mkdir(mode=0o700)
        unit = f"claude-vm-base-{generation}.service"
        runtime = _base_runtime(generation)
        runtime_created = False
        try:
            runtime.mkdir(mode=0o700)
            runtime_created = True
            port = self._free_port(set())
            with vm_lifetime(unit, timeout=timeout, env=host_control_env()):
                self._run_script(
                    ["install", *(["--version", release] if release is not None else []),
                     *(["--iso", str(iso)] if iso else [])],
                    vm_home=staging, ssh_port=port, unit=unit, runtime=runtime,
                    timeout=timeout, stdout=stdout,
                    **({"setup_job": setup_job} if setup_job is not None else {}),
                )
                self._run_script(
                    ["stop"], vm_home=staging, ssh_port=port, unit=unit, runtime=runtime,
                    timeout=120, stdout=stdout,
                )
            # The detached unit is observed stopped before publishing its disk.
            disk = staging / "disk.qcow2"
            if not disk.is_file() or disk.stat().st_size == 0:
                raise VmError("base installer did not produce a disk")
            if update:
                sync_assets(staging)
            atomic_json(staging / "base.json", {
                "built_at": time.time(),
                "iso": f"omarchy-{release}.iso" if release is not None else _installed_iso(self.data / "iso"),
                "generation": generation,
            })
            # Slow installation is outside the lock. Only a completed, stopped
            # candidate whose captured selection still matches may publish.
            from contextlib import nullcontext
            from dataclasses import asdict
            from .setup_base_update import publication
            guard = nullcontext(None)
            if update and setup_job is not None:
                if setup_job[1] is not None or expected_selection is None or release is None:
                    raise VmError("Profile update requires its reviewed selection and release")
                guard = publication(self.home, setup_job[0], generation, expected, asdict(self.config), release)
            with guard as admit, self.registry.lock():
                if json.loads(json.dumps(selection_receipt(self.home))) != expected:
                    raise VmError("Base selection changed during install; competing update retained")
                confined(self.home, base)
                if base.exists():
                    raise VmError("Base destination appeared during install; refusing replacement")
                if admit is not None:
                    admit()
                base.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                staging.rename(base)
                if update:
                    sync_directory(base.parent)
                    sync_directory(self.data)
                    atomic_json(self.data / "current-base.json", {
                        "version": 1, "home": str(self.home), "uid": os.getuid(),  # windows-footgun: ok — runtime package rejects non-Linux hosts
                        "generation": generation,
                    })
        except (subprocess.SubprocessError, OSError, VmError) as exc:
            raise VmError("Base image install failed; check prerequisites and retry") from exc
        finally:
            if runtime_created:
                self._force_stop(unit)
                shutil.rmtree(runtime, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)
        return self.base_status()

    def remove_base(self):
        with self.registry.lock():
            if self.registry.records() or any((self.registry.root / "vm").glob("*")):
                raise VmError("VM workspaces are retained; explicitly delete or recover them before removing the base image")
            from .setup_plan import confined
            if ((self.data / "current-base.json").exists()
                    or (self.data / "bases").exists()):
                raise VmError("Versioned base generations are retained; explicit generation recovery is required")
            shutil.rmtree(confined(self.home, self.base_home()), ignore_errors=True)
        return True

    def storage(self):
        def total(path):
            root = Path(path)
            if not root.exists():
                return 0
            return sum(
                f.stat().st_size for f in root.rglob("*") if f.is_file()
            )

        sessions = self.registry.root / "vm"
        return {
            "iso_bytes": total(self.data / "iso"),
            "base_bytes": total(self.data / "base") + total(self.data / "bases"),
            "session_bytes": total(sessions),
        }

    def latest_version(self, *, timeout=5):
        """Newest published Omarchy release, or None when it cannot be read.

        The same source the vendored script installs from, so a base built by
        that script is comparable to it. Best effort on purpose: a settings
        panel must still render with no network, and "unknown" is an honest
        answer that never fabricates an update prompt.
        """
        import json as _json
        import urllib.request

        request = urllib.request.Request(
            "https://api.github.com/repos/omacom/omarchy/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                tag = _json.loads(response.read(1 << 20)).get("tag_name") or ""
        except (OSError, ValueError):
            return None
        return tag.lstrip("v") or None

    def settings(self, *, check_updates=False):
        """The Desktop settings block: base image, storage, and the VM knobs.

        ``check_updates`` is opt-in because it costs a network round trip;
        rendering the panel must never depend on reaching GitHub.
        """
        base = self.base_status()
        installed = _iso_version(base.get("iso"))
        latest = self.latest_version() if check_updates else None
        return {
            "base": {**base, "version": installed},
            "storage": self.storage(),
            "memory_mb": self.config.vm.memory,
            "network": self.config.vm.network,
            "update": {
                "installed": installed,
                "latest": latest,
                # None means "not checked / unreachable", never "up to date".
                "available": None if (latest is None or installed is None)
                else latest != installed,
            },
        }

    # --- lifecycle ---

    def list(self):
        with self.registry.lock():
            self._reconcile_locked()
            return self.registry.records()

    def _reconcile_locked(self):
        """Retire records whose guest is gone, and guests nobody is using.

        A VM realm costs its full ``-m`` figure in host RAM for as long as it
        runs — there is no balloon device and the guest touches all of it. A
        conversation that ends without finalizing would otherwise pin that
        memory until the user notices, so the labwc realm's ``idle_ttl`` governs
        this kind too. Reconciliation runs on every list/start, which is the
        same cadence the registry is read at.
        """
        idle_ttl = self.config.idle_ttl
        now = time.time()
        for record in self.registry.records():
            if record["status"] == "stopped":
                from .vm_workspace import validate as validate_workspace
                try:
                    validate_workspace(record, allow_unlaunched=record.get("launch_pending") is True)
                except OwnershipError as exc:
                    record.update(status="recovery-required", recovery_reason=str(exc))
                    self.registry.put(record)
            elif record["status"] == "running" and not unit_active(record["unit"]):
                self._remove_locked(record)
            elif record["status"] == "starting":
                from .vm_owner_lifetime import owner_unit
                if (scope_info(record["unit"])["ActiveState"] in {"inactive", "failed"}
                        and scope_info(owner_unit(record))["ActiveState"] in {"inactive", "failed"}):
                    self._remove_locked(record)
            elif (record["status"] == "running"
                  and now - record.get("last_activity", now) > idle_ttl):
                self._stop_locked(record)
            elif record["status"] not in ("running", "starting", "stopped", "recovery-required", "deleting"):
                self._remove_locked(record)

    def _stop_locked(self, record):
        """Power down compute without deleting guest work. Caller holds the lock."""
        shutdown_guest(record, self.ssh_argv(record))
        self._remove_locked(record)

    def _retirement_failed(self, record):
        record.update(status="recovery-required", cleanup_required=True,
                      recovery_reason="Compute retirement was not confirmed; recover ownership and retry Stop")
        self.registry.put(record)

    def _remove_locked(self, record):
        from .vm_owner_lifetime import remove_dropin, retire

        validate_vm_record(record)
        try:
            retire(record)
            remove_dropin(record)
        except BaseException:
            self._retirement_failed(record)
            raise
        from .vm_workspace import retain_pin, validate as validate_workspace
        # Persist retirement even if metadata is incomplete. Never erase legacy
        # work, pins, or an interrupted launch merely because it cannot resume.
        record.update(status="stopped", stopped_at=time.time(), cleanup_required=False)
        record.pop("recovery_reason", None)
        try:
            if not isinstance(record.get("workspace"), dict) and record.get("ssh_host_key_pinned"):
                # Preserve legacy trust bytes without blessing unknown disk/base
                # metadata as a resumable workspace.
                retain_pin(record)
            validate_workspace(record, allow_unlaunched=record.get("launch_pending") is True)
        except (OwnershipError, OSError) as exc:
            record.update(status="recovery-required", recovery_reason=str(exc))
        self.registry.put(record)

    def _force_stop(self, unit):
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", unit],
                env=host_control_env(), capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _free_port(self, taken):
        for port in SSH_PORT_RANGE:
            if port in taken:
                continue
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    continue
            return port
        raise VmError("no free loopback SSH port for a VM realm")

    def _reserved_ports(self):
        # Retained data is not compute. Unknown/failed retirement still reserves
        # its endpoint even if a transient bind probe would succeed.
        return {r.get("ssh_port") for r in self.registry.records()
                if r.get("cleanup_required") is not False
                or r["status"] not in {"stopped", "recovery-required", "deleting"}}

    def start(self, session_id, *, before_allocate=None, before_start=None):
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError("session_id must contain 1 to 256 characters")
        with self.registry.lock():
            self._reconcile_locked()
            existing = [
                r for r in self.registry.records() if r["session_id"] == session_id
            ]
            for record in existing:
                if record["status"] == "running" and unit_active(record["unit"]):
                    # Reject a replaced invocation before guest SSH or renewal.
                    self._bind_to_owner(record)
                    if not record.get("ssh_host_key_pinned"):
                        enroll_host_key(record)
                        record["ssh_host_key_pinned"] = True
                    else:
                        require_host_key(record)
                    record["last_activity"] = time.time()
                    self.registry.put(record)
                    return record
                if record["status"] == "stopped":
                    return self._resume_locked(record, before_start=before_start)
                raise VmError("Retained VM workspace requires recovery; refusing a fresh replacement")

            # Caller setup applies only to fresh allocation. Keep it under the
            # owner-resolution lock: a precheck must not authorize a different
            # allocation after a retained workspace disappears or changes.
            if before_allocate is not None:
                before_allocate()
            base_disk = self.base_home() / "disk.qcow2"
            if not self.base_status()["present"]:
                raise VmError(
                    "No Omarchy base image yet. Build one with realm action "
                    "'setup' or /realm setup omarchy (downloads the signed ISO, ~5 GB, then "
                    "installs unattended); host fallback is disabled."
                )

            # Without a registry binding an orphan may be this conversation's
            # retained work. Do not silently create a replacement alongside it.
            registered = {r["session_dir"] for r in self.registry.records()}
            orphans = [p for p in (self.registry.root / "vm").glob("*")
                       if str(p) not in registered]
            if orphans:
                raise VmError("Unregistered VM workspace requires explicit recovery before new allocation: "
                              + str(orphans[0]))
            require_resources(self.config, self.registry.root / "vm")
            port = self._free_port(self._reserved_ports())
            if before_start is not None:
                before_start()
            generation = uuid.uuid4().hex
            vm_id = "v-" + generation[:24]
            runtime = _generation_paths(os.getuid(), generation)  # windows-footgun: ok — runtime package rejects non-Linux hosts
            runtime.mkdir(mode=0o700)
            session_dir = self.registry.root / "vm" / generation
            session_dir.mkdir(mode=0o700, parents=True)
            record = {
                "id": vm_id,
                "session_id": session_id,
                "generation": generation,
                "compute_generation": uuid.uuid4().hex,
                "kind": self.KIND,
                "uid": os.getuid(),  # windows-footgun: ok — runtime package rejects non-Linux hosts
                "home": str(self.home),
                "runtime_dir": str(runtime),
                "session_dir": str(session_dir),
                "unit": f"claude-vm-{generation}.service",
                "guardian_unit": f"claude-vm-{generation}-guard.service",
                "owner_protocol": "pidfd-v2",
                "status": "starting",
                "created_at": time.time(),
                "last_activity": time.time(),
                "ssh_port": port,
                "memory": self.config.vm.memory,
                "network": self.config.vm.network,
                "base_disk": str(base_disk),
            }
            self.registry.put(record)
            try:
                atomic_json(session_dir / "spec.json", asdict(self.config))
                record["workspace"] = self._clone_base(session_dir, base_disk)
                from .vm_workspace import retain_pin
                self.registry.put(record)
                admit = lambda: self._check_start(record, before_start)
                admit()
                self._launch(record, before_start=admit)
                enroll_host_key(record)
                retain_pin(record)
                info = scope_info(record["unit"])
                if (info["ActiveState"] != "active"
                        or info.get("InvocationID") != record.get("invocation_id")):
                    raise OwnershipError("VM invocation changed during startup")
                record.update(
                    status="running",
                    ssh_host_key_pinned=True,
                    vnc_socket=str(runtime / "vnc.sock"),
                    qmp_socket=str(runtime / "qmp.sock"),
                    last_activity=time.time(),
                )
                self.registry.put(record)
                return record
            except BaseException:
                self._remove_locked(record)
                raise

    def _resume_locked(self, record, *, before_start=None):
        from .vm_workspace import restore_pin, validate as validate_workspace

        try:
            frozen = validate_workspace(record, allow_unlaunched=record.get("launch_pending") is True)
        except OwnershipError as exc:
            record.update(status="recovery-required", recovery_reason=str(exc))
            self.registry.put(record)
            raise
        require_resources(frozen, Path(record["session_dir"]))
        if before_start is not None:
            before_start()
        # Revalidate compute retirement before replacing ephemeral receipts.
        self._remove_locked(record)
        port = self._free_port(self._reserved_ports())
        runtime = Path(record["runtime_dir"])
        if runtime.exists():
            shutil.rmtree(runtime)
        runtime.mkdir(mode=0o700)
        unlaunched = record.pop("launch_pending", False)
        if not unlaunched:
            restore_pin(record)
        previous = {key: record[key] for key in (
            "invocation_id", "owner_invocation_id", "guardian_invocation_id", "cgroup"
        ) if key in record}
        record["previous_compute"] = previous
        for key in previous:
            record.pop(key)
        record.update(status="starting", owner_protocol="pidfd-v2",
                      compute_generation=uuid.uuid4().hex, ssh_port=port)
        record.pop("recovery_reason", None)
        self.registry.put(record)
        try:
            def admit():
                self._check_start(record, before_start)
            admit()
            self._launch(record, before_start=admit, config=frozen)
            # Existing pin means strict verification; only a never-launched clone
            # may enroll its first key after a fresh operation is admitted.
            enroll_host_key(record)
            if unlaunched:
                from .vm_workspace import retain_pin
                retain_pin(record)
            info = scope_info(record["unit"])
            if info["ActiveState"] != "active" or info.get("InvocationID") != record.get("invocation_id"):
                raise OwnershipError("VM invocation changed during restart")
            record.update(status="running", ssh_host_key_pinned=True,
                          vnc_socket=str(runtime / "vnc.sock"),
                          qmp_socket=str(runtime / "qmp.sock"), last_activity=time.time())
            self.registry.put(record)
            return record
        except BaseException:
            self._remove_locked(record)
            raise

    def _clone_base(self, session_dir, base_disk):
        """Copy-on-write clone. 196 KiB and hundredths of a second per chat."""
        subprocess.run(
            [
                "qemu-img", "create", "-f", "qcow2",
                "-b", str(base_disk), "-F", "qcow2",
                str(session_dir / "disk.qcow2"),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )
        shutil.copyfile(
            base_disk.parent / "OVMF_VARS.4m.fd", session_dir / "OVMF_VARS.4m.fd"
        )
        for name in ("credentials", "cidata.img"):
            source = base_disk.parent / name
            if source.exists():
                shutil.copyfile(source, session_dir / name)
                os.chmod(session_dir / name, 0o600)
        from .vm_workspace import create
        record = self.registry.get("v-" + session_dir.name[:24])
        create(record)
        return record["workspace"]

    @staticmethod
    def _check_start(record, before_start):
        if before_start is not None:
            try:
                before_start(prepared=record)
            except BaseException:
                # No guest was started at these admission boundaries. Preserve
                # the cloned disk for a fresh operation, without inventing an SSH
                # pin or treating an interrupted actual boot as never launched.
                if not record.get("ssh_host_key_pinned"):
                    record["launch_pending"] = True
                raise

    def _launch(self, record, *, before_start=None, config=None):
        from .vm_owner_lifetime import install

        # The vendor waits for SSH after detaching QEMU. Arm the dependency
        # before invoking it, not after that potentially minutes-long wait.
        install(record, before_start=before_start)
        self.registry.put(record)
        runtime = Path(record["runtime_dir"])
        self._run_script(
            ["launch"],
            vm_home=record["session_dir"], ssh_port=record["ssh_port"],
            unit=record["unit"], runtime=runtime,
            timeout=(config or self.config).vm.boot_timeout + 30, before_start=before_start, config=config,
        )
        info = scope_info(record["unit"])
        if info["ActiveState"] != "active" or not info.get("InvocationID"):
            raise VmError("VM realm unit is not active after launch")
        record.update(invocation_id=info["InvocationID"], cgroup=info["ControlGroup"])
        self.registry.put(record)
        # The viewer bridge only accepts a mode-0600 socket directly inside the
        # realm's own mode-0700 runtime directory.
        socket_path = runtime / "vnc.sock"
        if not socket_path.is_socket():
            raise VmError("VM realm did not publish its viewer socket")
        os.chmod(socket_path, 0o600)
        self._guard(record)

    def _bind_to_owner(self, record):
        """Adopt without restarting the stable, guest-critical guardian."""
        from .vm_owner_lifetime import handoff
        from .vm_owner_migration import is_legacy, migrate

        if record.get("home") != str(self.home):
            raise OwnershipError("VM owner profile changed")
        if is_legacy(record):
            migrate(record, self.registry)
        else:
            handoff(record)

    def _guard(self, record):
        """Hold a sleep-only inhibitor for exactly as long as the guest runs.

        ``BindsTo`` makes systemd retire the inhibitor with the VM unit, so a
        crashed or stopped guest cannot leave the machine unable to suspend.
        """
        subprocess.run(
            [
                "systemd-run", "--user", "--quiet", "--collect",
                "--unit=" + record["guardian_unit"],
                "--property=BindsTo=" + record["unit"],
                "--property=After=" + record["unit"],
                "systemd-inhibit", "--what=sleep", "--who=Claude Realms",
                "--why=Omarchy VM realm", "--mode=block",
                "sleep", "infinity",
            ],
            env=host_control_env(), capture_output=True, text=True,
            timeout=30, check=True,
        )

        info = scope_info(record["guardian_unit"])
        if info["ActiveState"] != "active" or not info.get("InvocationID"):
            raise VmError("VM sleep inhibitor unit is not active")
        record["guardian_invocation_id"] = info["InvocationID"]

    def stop(self, vm_id):
        with self.registry.lock():
            if not self.registry.path(vm_id).exists():
                return False
            record = self.registry.get(vm_id)
            validate_vm_record(record)
            if record["status"] == "deleting":
                raise VmError("VM deletion is in progress; retry explicit Delete")
            try:
                self._stop_locked(record)
            except BaseException:
                self._retirement_failed(record)
                raise
            return True

    def delete_snapshot(self, vm_id, *, session_id):
        """Read-only confirmation capture; selection is not authentication."""
        with self.registry.lock():
            record = self.registry.get(vm_id)
            if not session_id or record["session_id"] != session_id:
                raise OwnershipError("workspace belongs to another session")
            return self._delete_snapshot_locked(record)

    def _delete_snapshot_locked(self, record):
        from copy import deepcopy
        from .vm_owner_lifetime import owner_unit
        from .vm_workspace import begin_deletion, validate_deletion

        if record["status"] not in {"stopped", "deleting"}:
            raise VmError("Only a verified stopped VM workspace can be deleted")
        units = {unit: scope_info(unit) for unit in
                 (record["unit"], owner_unit(record), record["guardian_unit"])}
        if any(info["ActiveState"] not in {"inactive", "failed"} for info in units.values()):
            raise OwnershipError("VM compute is not stopped; deletion refused")
        candidate = deepcopy(record)
        if candidate["status"] == "deleting":
            validate_deletion(candidate)
        else:
            begin_deletion(candidate)
        info = self.registry.path(record["id"]).stat()
        # Bind every publication as well as compute and workspace receipts.
        return {"record": record, "deletion": candidate["deletion"], "compute": units,
                "publication": [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]}

    def delete(self, vm_id, *, session_id=None, expected_snapshot=None):
        """Explicit administrative deletion of this profile's stopped data only.

        Not a model tool. Callers must separately authorize the conversation;
        an incomplete recovery record is not permission to erase its data.
        """
        from .vm_owner_lifetime import owner_unit, remove_dropin
        from .vm_workspace import begin_deletion, validate_deletion

        with self.registry.lock():
            if not self.registry.path(vm_id).exists():
                return False
            record = self.registry.get(vm_id)
            if session_id is not None and record["session_id"] != session_id:
                raise OwnershipError("workspace belongs to another session")
            if expected_snapshot is not None and self._delete_snapshot_locked(record) != expected_snapshot:
                raise OwnershipError("Delete confirmation target changed; confirm again")
            if record["status"] not in {"stopped", "deleting"}:
                raise VmError("Only a verified stopped VM workspace can be deleted")
            if record["status"] == "stopped":
                begin_deletion(record)
            else:
                validate_deletion(record)
            for unit in (record["unit"], owner_unit(record), record["guardian_unit"]):
                if scope_info(unit)["ActiveState"] not in {"inactive", "failed"}:
                    raise OwnershipError("VM compute is not stopped; deletion refused")
            remove_dropin(record)
            # Publish authorization before deleting any receipt it depends on.
            record["status"] = "deleting"
            self.registry.put(record)
            for path in (record["runtime_dir"], record["session_dir"]):
                if Path(path).exists():
                    shutil.rmtree(path)
            self.registry.remove(vm_id)
            return True

    # --- routing surface (mirrors Manager) ---

    def validate(self, vm_id):
        from .vm_owner_lifetime import validate_owner

        with self.registry.lock():
            record = self.registry.get(vm_id)
            if record["status"] != "running":
                raise VmError("realm is not running")
            info = scope_info(record["unit"])
            if (
                info.get("ActiveState") != "active"
                or info.get("InvocationID") != record.get("invocation_id")
            ):
                raise OwnershipError("VM realm unit is not the owned live invocation")
            validate_owner(record)
            require_host_key(record)
            record["last_activity"] = time.time()
            self.registry.put(record)
            return record

    def env(self, vm_id):
        """Environment the routed terminal runs with on the *host* side.

        The command itself executes in the guest; these values only mark the
        routing and are what ``pre_tool`` and the escape guard read back.
        """
        record = self.validate(vm_id)
        return {
            "CLAUDE_REALM_KIND": self.KIND,
            "CLAUDE_REALM_ID": record["id"],
        }

    def command_prefix(self, vm_id):
        self.validate(vm_id)
        return (
            sys.executable,
            str(Path(__file__).with_name("vm_launch.py").resolve()),
            str(self.home),
            vm_id,
            "--",
        )

    def guest_home(self, vm_id):
        """The guest desktop user's home, read from the guest itself.

        Where a routed session starts. Asking the guest beats assuming a path:
        the user name comes from the host at install time, so it is not a
        constant we get to hardcode.
        """
        result = self.guest_run(
            vm_id, ["sh", "-c", "getent passwd 1000 | cut -d: -f6"], timeout=30)
        home = result["stdout"].strip()
        if result["returncode"] != 0 or not home.startswith("/"):
            raise VmError("could not resolve the guest user's home directory")
        return home

    def ssh_argv(self, record, *, user="root", tty=False):
        """SSH into this realm's guest, with the host's ssh config overridden.

        Every forwarding option is pinned OFF rather than left to defaults.
        ``ssh`` merges ``~/.ssh/config``, and a ``Host *`` block with
        ``ForwardAgent yes`` is a common setup: it would hand the user's
        private-key agent to a disposable guest that has passwordless sudo and
        no disk encryption, so anything running in there could authenticate as
        them. ``ForwardX11 yes`` is the same hazard pointed the other way — a
        route from the guest back to the host's display. Agent forwarding is a
        per-command, user-approved opt-in, never an ambient default.
        ``IdentitiesOnly`` keeps ssh from offering unrelated host keys.
        """
        return [
            "ssh", "-p", str(record["ssh_port"]),
            *(["-tt"] if tty else ["-T"]),
            *ssh_options(record),
            f"{user}@127.0.0.1",
        ]

    def guest_run(self, vm_id, command, *, timeout=60, user="root"):
        """One captured command in the guest, as an argv list."""
        record = self.validate(vm_id)
        if (
            not isinstance(command, (list, tuple))
            or not command
            or not all(isinstance(a, str) and "\0" not in a for a in command)
        ):
            raise ValueError("command must be a nonempty argv list")
        result = subprocess.run(
            [*self.ssh_argv(record, user=user), "--", shlex.join(command)],
            capture_output=True, text=True, timeout=timeout,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def shot(self, vm_id, path):
        """Stream as the desktop user, publishing only a successful capture."""
        from .vm_launch import guest_shell_init

        record = self.validate(vm_id)
        target = Path(path).expanduser().resolve()
        command = ["runuser", "-u", self.guest_user(vm_id), "--", "bash", "-c",
                   guest_shell_init() + "exec grim -"]
        # No predictable guest filename; a failed stream must not destroy the
        # previous host image. Publish atomically on the destination filesystem.
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".realm-shot-", delete=False) as sink:
            temporary = Path(sink.name)
            try:
                subprocess.run(
                    [*self.ssh_argv(record), "--", shlex.join(command)],
                    stdout=sink, stderr=subprocess.PIPE, timeout=90, check=True,
                )
                with temporary.open("rb") as image:
                    if image.read(8) != b"\x89PNG\r\n\x1a\n":
                        raise VmError("guest capture did not return a PNG")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return str(target)

    def guest_user(self, vm_id):
        """Resolve the desktop account rather than uploading root-owned work."""
        result = self.guest_run(vm_id, ["id", "-nu", "1000"], timeout=30)
        user = result["stdout"].strip()
        if result["returncode"] != 0 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*\$?", user):
            raise VmError("could not resolve the guest desktop account")
        return user

    def push(self, vm_id, source, destination=None):
        """Copy a host path into the guest. Copies, never mounts."""
        from .vm_transfer import push
        return push(self, vm_id, source, destination)

    def pull(self, vm_id, source, destination):
        """Copy a guest path out. Only on an explicit request."""
        from .vm_transfer import pull
        return pull(self, vm_id, source, destination)

    def stats(self, vm_id):
        """What the guest costs, read from its own cgroup and disk."""
        record = self.registry.get(vm_id)
        cgroup = Path("/sys/fs/cgroup") / record.get("cgroup", "").lstrip("/")
        result: dict[str, int | None] = {
            "memory_bytes": None, "cpu_usec": None, "disk_bytes": None}
        try:
            result["memory_bytes"] = int(
                (cgroup / "memory.current").read_text(encoding="utf-8").strip()
            )
        except (OSError, ValueError):
            pass
        try:
            for line in (cgroup / "cpu.stat").read_text(encoding="utf-8").splitlines():
                if line.startswith("usage_usec "):
                    result["cpu_usec"] = int(line.split()[1])
        except (OSError, ValueError):
            pass
        try:
            result["disk_bytes"] = (
                Path(record["session_dir"]) / "disk.qcow2"
            ).stat().st_size
        except OSError:
            pass
        return result

    def doctor(self, vm_id=None):
        tools = {
            name: shutil.which(name)
            for name in ("qemu-system-x86_64", "qemu-img", "ssh", "scp", "socat",
                         "jq", "mcopy", "mformat", "openssl", "systemd-run",
                         "systemd-inhibit")
        }
        firmware = {
            path: os.access(path, os.R_OK)
            for path in ("/usr/share/edk2/x64/OVMF_CODE.4m.fd",
                         "/usr/share/edk2/x64/OVMF_VARS.4m.fd")
        }
        key = Path.home() / ".ssh" / "id_ed25519.pub"
        base = self.base_status()
        report = {
            "kind": self.KIND,
            "platform": sys.platform,
            "tools": tools,
            "firmware": firmware,
            "kvm": os.access("/dev/kvm", os.R_OK | os.W_OK),
            "ssh_key": key.is_file(),
            "base_image": base,
            "script": str(self.script()),
            "security_boundary": (
                "separate kernel, disk and desktop; files are copied, not mounted. "
                "The guest disk is unencrypted with passwordless sudo: never put a "
                "secret in it"
            ),
        }
        report["missing"] = [
            name for name, found in tools.items() if not found
        ] + [
            path for path, ok in firmware.items() if not ok
        ] + (
            [] if report["kvm"] else ["/dev/kvm access (add your user to the kvm group)"]
        ) + (
            [] if report["ssh_key"] else ["~/.ssh/id_ed25519.pub"]
        ) + (
            [] if base["present"] else ["Omarchy base image (/realm setup omarchy)"]
        )
        report["ok"] = sys.platform == "linux" and not report["missing"]
        if vm_id is not None:
            try:
                report["realm"] = self.validate(vm_id)
                report["stats"] = self.stats(vm_id)
                report["desktop"] = (
                    self.guest_run(vm_id, ["ls", "/run/user/1000/hypr"])["returncode"]
                    == 0
                )
                report["ok"] = report["ok"] and report["desktop"]
            except (RealmError, OSError, ValueError, subprocess.SubprocessError) as exc:
                report.update(ok=False, error=str(exc))
        return report


def _installed_iso(iso_dir):
    isos = sorted(Path(iso_dir).glob("omarchy-*.iso"))
    return isos[-1].name if isos else None


def _iso_version(name):
    """``omarchy-4.0.3.iso`` -> ``4.0.3``; None when there is no ISO recorded."""
    if not isinstance(name, str):
        return None
    match = re.fullmatch(r"omarchy-(.+)\.iso", name)
    return match.group(1) if match else None


def _reason(exc):
    output = getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or ""
    return (output.strip().splitlines() or [str(exc)])[-1][:400]
