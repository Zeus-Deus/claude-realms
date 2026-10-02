"""What each realm kind needs, and the consented steps that provide it.

Nothing here installs system packages: that needs root, and the person runs
the printed command themselves (``! sudo pacman ...`` in Claude Code). The
driver installs into the plugin's data home without root; the Omarchy base
image is a long, explicit download+install the person starts with
``/realm setup omarchy``.
"""

import os
from pathlib import Path
import shutil
import threading
import time

from realms_core.setup_plan import PACKAGES, distribution

REALM_TOOLS = ("labwc", "Xwayland", "wayvnc", "dbus-daemon", "grim", "wlr-randr", "gdbus", "bwrap",
               "/usr/lib/at-spi-bus-launcher", "/usr/lib/at-spi2-registryd")
VM_TOOLS = ("qemu-system-x86_64", "qemu-img", "ssh", "scp", "socat", "jq", "mcopy", "mformat",
            "openssl", "curl", "gpg", "systemd-run", "systemd-inhibit")


def _missing(tools):
    return [name for name in tools
            if not (os.access(name, os.X_OK) if name.startswith("/") else shutil.which(name))]


def install_command(missing):
    packages = sorted({PACKAGES.get(name, name) for name in missing})
    if not packages:
        return None
    if distribution() == "arch":
        return "sudo pacman -S --needed " + " ".join(packages)
    return "install these packages with your distribution's package manager: " + ", ".join(packages)


def readiness(home, kind):
    from realms_core.install_driver import current_driver

    driver = current_driver(home)
    blockers, steps = [], []
    if kind == "omarchy-vm":
        missing = _missing(VM_TOOLS)
        from realms_core.units import systemd_available

        if not systemd_available():
            blockers.append("the Omarchy VM needs a running systemd user manager")
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            blockers.append("KVM access (/dev/kvm) is required")
        key = Path.home() / ".ssh" / "id_ed25519"
        if not key.is_file() or not key.with_suffix(".pub").is_file():
            blockers.append("an existing ~/.ssh/id_ed25519 key pair is required (setup never creates keys)")
        from realms_core.setup_plan import base_present

        try:
            has_base = base_present(home)
        except (OSError, ValueError, RuntimeError):
            has_base = False
        if not has_base:
            steps.append("build the Omarchy base image: /realm setup omarchy (downloads the signed ISO, ~5 GB)")
    else:
        missing = _missing(REALM_TOOLS)
    if driver is None:
        steps.append("install the computer-use driver: /realm setup")
    command = install_command(missing)
    if command:
        steps.insert(0, "install packages: " + command)
    ready = not missing and not blockers and not steps
    parts = blockers + steps
    return {
        "kind": kind,
        "ready": ready,
        "missing": missing,
        "blockers": blockers,
        "steps": steps,
        "install_command": command,
        "driver": {key: driver[key] for key in ("version", "channel", "previous") if key in driver} if driver else None,
        "message": ("Realm setup required: " + "; ".join(parts) + ".") if parts else "Ready.",
    }


class Jobs:
    """Long setup work (driver download, base image install) off the request path."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}

    def start(self, name, function):
        with self._lock:
            job = self._jobs.get(name)
            if job and job["state"] == "running":
                return job
            job = {"name": name, "state": "running", "started_at": time.time(), "log": [], "result": None}
            self._jobs[name] = job

        def progress(message):
            job["log"].append(str(message))
            del job["log"][:-50]

        def run():
            try:
                job["result"] = function(progress)
                job["state"] = "done"
            except Exception as exc:  # noqa: BLE001 - reported to the person verbatim
                job["state"] = "failed"
                job["error"] = str(exc)
            job["finished_at"] = time.time()

        threading.Thread(target=run, name="realms-setup-" + name, daemon=True).start()
        return job

    def get(self, name):
        return self._jobs.get(name)

    def all(self):
        return {name: {k: v for k, v in job.items() if k != "result"} for name, job in self._jobs.items()}
