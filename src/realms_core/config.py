"""Realm settings for one data home; never change the user's desktop config.

The core is host-neutral. An agent host (Claude Code, Hermes, ...) chooses the
data home and passes it explicitly or through ``REALMS_HOME``; settings live in
``<home>/config.json``. Unknown keys are left for the host adapter's own use.
"""

from dataclasses import dataclass, field
import json
from pathlib import Path
import os


def effective_home(home=None):
    if home is not None:
        return Path(home).expanduser().resolve()
    configured = os.environ.get("REALMS_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return (Path(data) / "realms").resolve()


def driver_root(home=None):
    """Writable per-home driver releases; installed plugin code may be sealed."""
    return effective_home(home) / "drivers"


def vm_data_path(home=None):
    """Writable per-home Omarchy VM images; installed plugin code may be sealed."""
    return effective_home(home) / "vm"


def settings_path(home=None):
    return effective_home(home) / "config.json"


def load_settings(home=None):
    """The whole settings document; a missing file is an empty one."""
    path = settings_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ValueError("invalid realm settings in " + str(path)) from exc
    if not isinstance(data, dict):
        raise ValueError("realm settings must be a JSON object: " + str(path))
    return data


# A realm kind selects an implementation, not a routing mode. ``realm`` is the
# labwc compositor that has always existed; ``omarchy-vm`` is a QEMU guest.
KINDS = ("realm", "omarchy-vm")


@dataclass(frozen=True)
class VmConfig:
    """Omarchy VM guest sizing and exposure. Applies at the next guest boot."""

    memory: int = 3072
    network: bool = True
    disk_size: str = "40G"
    omarchy_vm_path: str = ""
    boot_timeout: float = 180

    def __post_init__(self):
        import math
        import re

        if isinstance(self.memory, bool) or not isinstance(self.memory, int):
            raise ValueError("vm.memory must be an integer number of MiB")
        if not (1024 <= self.memory <= 262144):
            raise ValueError("vm.memory must be between 1024 and 262144 MiB")
        if not isinstance(self.network, bool):
            raise ValueError("vm.network must be a YAML boolean")
        if not isinstance(self.disk_size, str) or not re.fullmatch(
            r"[1-9][0-9]{0,3}G", self.disk_size
        ):
            raise ValueError("vm.disk_size must look like 40G")
        if not isinstance(self.omarchy_vm_path, str) or "\0" in self.omarchy_vm_path:
            raise ValueError("vm.omarchy_vm_path must be a NUL-free string")
        if self.omarchy_vm_path and not Path(self.omarchy_vm_path).is_absolute():
            raise ValueError("vm.omarchy_vm_path must be an absolute path")
        if (
            isinstance(self.boot_timeout, bool)
            or not isinstance(self.boot_timeout, (int, float))
            or not math.isfinite(self.boot_timeout)
            or not (10 <= self.boot_timeout <= 1800)
        ):
            raise ValueError("vm.boot_timeout must be 10 to 1800 seconds")


@dataclass(frozen=True)
class DriverConfig:
    """Where the computer-use driver comes from. Nothing here is a pin.

    ``channel`` picks a release at install/update time: ``stable`` (newest
    release at least ``stable_min_age_hours`` old that passes the smoke test),
    ``latest`` (newest release), ``nightly`` (newest build including nightly
    tags) or ``pinned`` (exactly ``version``). Checksums always come from the
    upstream release metadata, never from this code.
    """

    channel: str = "stable"
    version: str = ""
    repo: str = "trycua/cua"
    stable_min_age_hours: float = 24
    check_updates: bool = True
    keep_versions: int = 2
    tag_pattern: str = r"cua-driver-rs-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
    nightly_tag_pattern: str = r"nightly-cua-driver-rs-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+-nightly\.[0-9.]+)"
    asset_pattern: str = "cua-driver-rs-{version}-linux-{arch}.tar.gz"

    def __post_init__(self):
        import re

        if self.channel not in ("stable", "latest", "nightly", "pinned"):
            raise ValueError("driver.channel must be stable, latest, nightly or pinned")
        if self.channel == "pinned" and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?", self.version or ""):
            raise ValueError("driver.version must name a release when driver.channel is pinned")
        if not isinstance(self.repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("driver.repo must look like owner/name")
        if isinstance(self.stable_min_age_hours, bool) or not isinstance(self.stable_min_age_hours, (int, float)) \
                or not 0 <= self.stable_min_age_hours <= 24 * 30:
            raise ValueError("driver.stable_min_age_hours must be 0 to 720")
        if not isinstance(self.check_updates, bool):
            raise ValueError("driver.check_updates must be a boolean")
        if isinstance(self.keep_versions, bool) or not isinstance(self.keep_versions, int) \
                or not 1 <= self.keep_versions <= 10:
            raise ValueError("driver.keep_versions must be 1 to 10")
        for name in ("tag_pattern", "nightly_tag_pattern"):
            try:
                if "version" not in re.compile(getattr(self, name)).groupindex:
                    raise ValueError
            except (re.error, TypeError, ValueError):
                raise ValueError("driver." + name + " must be a regex with a (?P<version>...) group") from None
        if not isinstance(self.asset_pattern, str) or "{version}" not in self.asset_pattern:
            raise ValueError("driver.asset_pattern must contain {version}")


@dataclass(frozen=True)
class Config:
    default_mode: str = "realm"
    default_kind: str = "realm"
    size: str = "1920x1080"
    idle_ttl: float = 1800
    # ``auto`` resolves at realm start: GPU (gles2) when a render node is
    # usable, otherwise software (pixman).
    renderer: str = "auto"
    overlay: bool = True
    cursor_theme: str = "cua.default"
    # ``auto`` uses systemd scopes when a user manager runs, else direct.
    process_backend: str = "auto"
    # Open application main windows maximized (dialogs and popups keep their
    # own size), so the realm's screen shows the app rather than a small
    # window on an empty desktop.
    maximize_windows: bool = True
    vm: VmConfig = VmConfig()
    driver: DriverConfig = field(default_factory=DriverConfig)

    def __post_init__(self):
        import math

        parse_size(self.size)
        if self.default_mode not in ("realm", "host", "ask"):
            raise ValueError("default_mode must be realm, host or ask")
        if self.default_kind not in KINDS:
            raise ValueError("default_kind must be " + " or ".join(KINDS))
        if self.renderer not in ("auto", "gles2", "pixman"):
            raise ValueError("renderer must be auto, gles2 or pixman")
        if self.process_backend not in ("auto", "systemd", "direct"):
            raise ValueError("process_backend must be auto, systemd or direct")
        if not isinstance(self.maximize_windows, bool):
            raise ValueError("maximize_windows must be a boolean")
        if (
            isinstance(self.idle_ttl, bool)
            or not isinstance(self.idle_ttl, (int, float))
            or not math.isfinite(self.idle_ttl)
            or self.idle_ttl <= 0
        ):
            raise ValueError("idle_ttl must be a positive finite number of seconds")
        if not isinstance(self.overlay, bool):
            raise ValueError("overlay must be a YAML boolean")
        if not isinstance(self.cursor_theme, str) or not self.cursor_theme:
            raise ValueError("cursor_theme must be a nonempty string")
        # Accept the asdict() round-trip used for a realm's frozen launch spec.
        if isinstance(self.vm, dict):
            object.__setattr__(self, "vm", VmConfig(**self.vm))
        elif not isinstance(self.vm, VmConfig):
            raise ValueError("vm must be a mapping of Omarchy VM settings")
        if isinstance(self.driver, dict):
            object.__setattr__(self, "driver", DriverConfig(**self.driver))
        elif not isinstance(self.driver, DriverConfig):
            raise ValueError("driver must be a mapping of driver settings")

    @classmethod
    def load(cls, home):
        from dataclasses import fields

        settings = load_settings(home)
        try:
            return cls(
                **{f.name: settings[f.name] for f in fields(cls) if f.name in settings}
            )
        except TypeError as exc:
            raise ValueError("invalid realm settings: " + str(exc)) from exc

    def resolved_renderer(self):
        if self.renderer != "auto":
            return self.renderer
        return "gles2" if any(
            os.access(node, os.R_OK | os.W_OK) for node in Path("/dev/dri").glob("renderD*")
        ) else "pixman"


def parse_size(size):
    import re

    if not isinstance(size, str) or not re.fullmatch(
        r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", size
    ):
        raise ValueError("size must be WIDTHxHEIGHT")
    width, height = map(int, size.split("x"))
    if not (64 <= width <= 8192 and 64 <= height <= 8192):
        raise ValueError("size dimensions must be between 64 and 8192")
    return width, height
