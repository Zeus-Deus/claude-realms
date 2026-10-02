"""Pixel live view, end to end, with a terminal that speaks the kitty graphics protocol.

An outer realm runs kitty (X11 through Xwayland, software GL). Inside kitty runs
Claude Code with this plugin; its /realm commands start an inner realm whose
screen the Realm pane draws as real pixels. A screenshot of the outer realm is
what a person would see. Keys reach kitty over the outer realm's VNC socket.

Run in the Docker rig with the claude binary mounted (no model turns needed):
    python test/e2e/kitty_nested.py /artifacts
"""
import json
import os
from pathlib import Path
import sys
import time

from claude_realms.frames import RFB, apply_input
from claude_realms.host import ClaudeSettings
from claude_realms.service import RealmService
from realms_core import install_driver

ART = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp")
HOME = Path(os.environ["REALMS_HOME"])


def type_text(record, text):
    rfb = RFB(record["vnc_socket"])
    try:
        # Click into kitty first so the first key is not spent on focus.
        for kind in ("down", "up"):
            apply_input(rfb, {"t": "ptr", "type": kind, "button": "left", "x": 800, "y": 940,
                              "columns": rfb.width, "rows": rfb.height})
        time.sleep(0.3)
        for ch in text:
            if ch == "\n":
                apply_input(rfb, {"t": "key", "key": "return"})
            else:
                apply_input(rfb, {"t": "key", "key": ch})
            time.sleep(0.03)
    finally:
        rfb.sock.close()


def shot(service, name):
    data = service.shot()["png"]
    (ART / name).write_bytes(data)
    print("saved", ART / name, flush=True)


def outer_setup():
    """An outer realm running Claude Code (with this plugin) inside kitty."""
    outer = RealmService(HOME, "claude-outer", ClaudeSettings.load(HOME))
    record = outer.ensure()
    outer.resize("1600x1000")
    realm_home = outer.exec("echo $HOME")["stdout"].strip()
    key = "sk-ant-api03-dummy-key-for-local-ui-tests-only-0000000000000000AA"
    config = {"hasCompletedOnboarding": True, "theme": "dark",
              "projects": {realm_home + "/proj": {"hasTrustDialogAccepted": True, "allowedTools": []}},
              "customApiKeyResponses": {"approved": [key[-20:]], "rejected": []}}
    outer.exec("mkdir -p ~/proj && cat > ~/.claude.json <<'EOF'\n" + json.dumps(config) + "\nEOF")
    # Share the outer release listing, so the inner Claude needs no API call.
    # (A regular realm shares this filesystem, so a copy is enough.)
    listing = HOME / "drivers" / "releases-cache.json"
    outer.exec("mkdir -p ~/.claude/plugins/data/realms-inline/drivers && cp " + str(listing)
               + " ~/.claude/plugins/data/realms-inline/drivers/releases-cache.json")
    script = (
        "cd ~/proj && exec env -u WAYLAND_DISPLAY ANTHROPIC_API_KEY=" + key
        + " DISABLE_AUTOUPDATER=1 claude --plugin-dir /plugin"
    )
    outer.launch(["env", "KITTY_DISABLE_WAYLAND=1", "LIBGL_ALWAYS_SOFTWARE=1", "kitty",
                  "-o", "font_size=8", "-o", "remember_window_size=no", "-o", "initial_window_width=1590",
                  "-o", "initial_window_height=990", "-o", "placement_strategy=top-left",
                  "bash", "-lc", script])
    return outer, record


def main():
    install_driver.install(HOME)
    outer, record = outer_setup()
    time.sleep(25)
    shot(outer, "kitty-1-started.png")
    for command, wait in (("/realm setup", 20), ("/realm on", 10), ("/realm launch gtk3-demo", 10)):
        type_text(record, command + "\n")
        time.sleep(wait)
    shot(outer, "kitty-2-pane.png")
    type_text(record, "/realm launch xterm -bg navy -fg white -geometry 60x10+40+600\n")
    time.sleep(8)
    shot(outer, "kitty-3-two-windows.png")
    # Take control from the pane, then click "Button Boxes" in the live picture:
    # the click must land in the inner realm's gtk3-demo.
    click(record, 1100, 384)
    time.sleep(2)
    shot(outer, "kitty-4-control.png")
    click(record, 1240, 157)
    time.sleep(3)
    shot(outer, "kitty-5-clicked.png")
    print(outer.exec("cat ~/.claude/plugins/data/realms-inline/realms/input/*.json")["stdout"][-1500:], flush=True)
    outer.stop()


def click(record, x, y):
    rfb = RFB(record["vnc_socket"])
    try:
        for kind in ("move", "down", "up"):
            apply_input(rfb, {"t": "ptr", "type": "down" if kind == "down" else ("up" if kind == "up" else "move"),
                              "button": "left", "x": x, "y": y, "columns": rfb.width, "rows": rfb.height})
            time.sleep(0.1)
    finally:
        rfb.sock.close()


if __name__ == "__main__":
    main()
