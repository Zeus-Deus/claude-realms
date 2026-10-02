"""Claude Code inside herdr inside kitty (nested in a realm): does the Realm pane show pixels?"""
import json, sys, time
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from kitty_nested import ART, HOME, RFB, apply_input, install_driver, shot  # noqa: E402
from claude_realms.host import ClaudeSettings  # noqa: E402
from claude_realms.service import RealmService  # noqa: E402

def type_text(record, text):
    rfb = RFB(record["vnc_socket"])
    try:
        for warm in (" ", "backspace"):  # the first key after a pause can be lost
            apply_input(rfb, {"t": "key", "key": warm})
            time.sleep(0.3)
        for ch in text:
            apply_input(rfb, {"t": "key", "key": "return" if ch == "\n" else ch})
            time.sleep(0.08)
    finally:
        rfb.sock.close()


install_driver.install(HOME)
outer = RealmService(HOME, "claude-outer", ClaudeSettings.load(HOME))
record = outer.ensure()
outer.resize("1600x1000")
realm_home = outer.exec("echo $HOME")["stdout"].strip()
key = "sk-ant-api03-dummy-key-for-local-ui-tests-only-0000000000000000AA"
config = {"hasCompletedOnboarding": True, "theme": "dark",
          "projects": {realm_home + "/proj": {"hasTrustDialogAccepted": True, "allowedTools": []}},
          "customApiKeyResponses": {"approved": [key[-20:]], "rejected": []}}
outer.exec("mkdir -p ~/proj && cat > ~/.claude.json <<'EOF'\n" + json.dumps(config) + "\nEOF")
listing = HOME / "drivers" / "releases-cache.json"
outer.exec("mkdir -p ~/.claude/plugins/data/realms-inline/drivers && cp " + str(listing)
           + " ~/.claude/plugins/data/realms-inline/drivers/releases-cache.json")
# herdr's pane shell starts Claude Code directly (no typing into herdr needed).
outer.exec("mkdir -p ~/.config/herdr && printf '%s\\n' '#!/bin/bash' 'cd ~/proj' "
           "'export CLAUDE_CODE_FORCE_TERMINAL_IMAGES=${FORCE_IMAGES:-0}' 'exec claude --plugin-dir /plugin' > ~/start-claude.sh && chmod +x ~/start-claude.sh && "
           "printf 'onboarding = false\\n[terminal]\\ndefault_shell = \"%s/start-claude.sh\"\\n' \"$HOME\" > ~/.config/herdr/config.toml && "
           "cat ~/.config/herdr/config.toml")
import os
force = os.environ.get("FORCE_IMAGES", "0")
script = ("cd ~/proj && exec env -u WAYLAND_DISPLAY FORCE_IMAGES=" + force + " ANTHROPIC_API_KEY=" + key
          + " DISABLE_AUTOUPDATER=1 herdr")
outer.launch(["env", "KITTY_DISABLE_WAYLAND=1", "LIBGL_ALWAYS_SOFTWARE=1", "kitty",
              "-o", "font_size=8", "-o", "remember_window_size=no", "-o", "initial_window_width=1590",
              "-o", "initial_window_height=990", "bash", "-lc", script])
time.sleep(10)
shot(outer, "herdr-1-start.png")

time.sleep(60)
shot(outer, "herdr-2-claude.png")
for command, wait in (("/realm on", 10), ("/realm view image", 6), ("/realm launch gtk3-demo", 8)):
    type_text(record, command + "\n")
    time.sleep(wait)
shot(outer, "herdr-3-pane-force%s.png" % force)
outer.stop()
