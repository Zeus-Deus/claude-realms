"""Claude Code as the realm host: data home, session identity, adapter settings.

The core reads its own settings from ``<home>/config.json``; this module reads
the ``claude`` section of the same file, so a person edits one document.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import uuid

# Driver tools that administer the driver itself or act outside the realm's
# desktop. Hidden by default; a person may change the list in config.json.
DEFAULT_EXCLUDED_TOOLS = (
    "check_for_update",
    "install_extension",
    "install_ffmpeg",
    "set_config",
    "replay_trajectory",
)


def data_home():
    """Claude Code gives each plugin a persistent data directory."""
    for key in ("REALMS_HOME", "CLAUDE_PLUGIN_DATA"):
        value = os.environ.get(key)
        if value and "${" not in value:
            return Path(value).expanduser().resolve()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return (Path(base) / "claude-realms").resolve()


def session_owner():
    """The realm owner for this server: the Claude Code session when known."""
    for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        value = os.environ.get(key, "").strip()
        if value and len(value) <= 200 and "${" not in value:
            return "claude-" + value
    return "claude-" + uuid.uuid4().hex


@dataclass(frozen=True)
class ClaudeSettings:
    # Start the default realm kind when the agent first uses a desktop tool.
    auto_start: bool = True
    # Driver tools never offered to the model.
    exclude_tools: tuple = DEFAULT_EXCLUDED_TOOLS
    # Prefix for driver tool names, so they read as realm tools in a listing.
    tool_prefix: str = ""
    # Daemon authorization mode passed to the driver's serve verb when supported.
    driver_permission_mode: str = ""
    # Block Bash commands that re-point GUI tools at the host while a realm is on.
    host_guard: bool = True
    # Stop this session's realms when the session's server exits.
    stop_on_exit: bool = True
    # Install the computer-use driver by itself on first use (verified, no root).
    auto_setup: bool = True
    # Delete stopped realm homes and VM disks unused for this many days, when a
    # session starts. 0 keeps everything until deleted with /realm delete.
    retention_days: float = 30
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, home):
        from realms_core.config import load_settings

        section = load_settings(home).get("claude", {}) or {}
        if not isinstance(section, dict):
            raise ValueError("config.json: claude must be an object")
        known = {name for name in cls.__dataclass_fields__ if name != "extra"}
        values = {key: section[key] for key in known if key in section}
        if "exclude_tools" in values:
            values["exclude_tools"] = tuple(values["exclude_tools"])
        return cls(**values, extra={k: v for k, v in section.items() if k not in known})
