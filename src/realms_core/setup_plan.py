"""Read-only setup facts: prerequisite packages, path confinement, base presence.

Hosts build their own consented setup flows from these.
"""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
from types import SimpleNamespace

from .config import Config, KINDS, effective_home, vm_data_path

PACKAGES = {
    "labwc": "labwc", "Xwayland": "xorg-xwayland", "wayvnc": "wayvnc",
    "dbus-daemon": "dbus", "systemd-run": "systemd", "systemd-inhibit": "systemd",
    "grim": "grim", "wlr-randr": "wlr-randr", "gdbus": "glib2", "bwrap": "bubblewrap",
    "/usr/lib/at-spi-bus-launcher": "at-spi2-core",
    "/usr/lib/at-spi2-registryd": "at-spi2-core",
    "qemu-system-x86_64": "qemu-full", "qemu-img": "qemu-full",
    "ssh": "openssh", "scp": "openssh", "socat": "socat", "jq": "jq",
    "mcopy": "mtools", "mformat": "mtools", "openssl": "openssl",
    "curl": "curl", "gpg": "gnupg", "pacman": "pacman",
    "qemu-full": "qemu-full", "edk2-ovmf": "edk2-ovmf", "mtools": "mtools",
}
NATIVE_TOOLS = tuple(PACKAGES)[:12]
VM_TOOLS = ("qemu-system-x86_64", "qemu-img", "ssh", "scp", "socat", "jq",
            "mcopy", "mformat", "openssl", "systemd-run", "systemd-inhibit", "curl", "gpg", "pacman")


def confined(home, path):
    home, path = effective_home(home), Path(path)
    relative = path.relative_to(home)
    cursor = home
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Realms setup paths must not contain symlink redirects")
        if cursor.exists() and cursor.stat().st_uid != os.getuid():  # windows-footgun: ok — Linux-only plugin
            raise PermissionError("Realms setup path has unsafe ownership")
    return path


def package_plan(missing, distribution):
    if missing and distribution != "arch":
        raise ValueError("Unsupported distribution: install prerequisites with your package manager")
    if any(name not in PACKAGES for name in missing):
        raise ValueError("Unsupported prerequisite")
    return sorted({PACKAGES[name] for name in missing})


def distribution():
    try:
        return "arch" if platform.freedesktop_os_release().get("ID") == "arch" else "unsupported"
    except OSError:
        return "unsupported"


def base_present(home, *, base=None):
    from .vm_base import selected
    base = selected(home) if base is None else confined(home, base)
    disk = confined(home, base / "disk.qcow2")
    receipt = confined(home, base / "base.json")
    try:
        metadata = json.loads(receipt.read_text(encoding="utf-8"))
        return disk.is_file() and disk.stat().st_size > 0 and isinstance(metadata.get("built_at"), (int, float))
    except (OSError, ValueError, AttributeError):
        return False
