# Realms for Claude Code

Private Linux desktops for Claude. When Claude needs to run and test a GUI app,
it gets a desktop of its own, a headless **realm** (labwc/Wayland) or an
**Omarchy VM** (QEMU/KVM), and never touches your screen, pointer or clipboard.
You watch it live in a pane beside the conversation, and you can take over.

```
┌ conversation ───────────────────────────┬ Realm r-27ce…  1920x1080 · agent has control ┐
│ ❯ test the settings dialog of my app     │ ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ │
│ ● realm_launch ./build/myapp             │   (the realm's screen, live: real pixels    │
│ ● get_desktop_state                      │    in Ghostty/kitty, coloured cells in any   │
│ ● click (412, 230)                       │    other terminal)                           │
│ ● get_desktop_state  ✓ dialog opened     │ [ Take control ] [ Browser link ] [ Stop ]   │
└──────────────────────────────────────────┴──────────────────────────────────────────────┘
```

This is the Claude Code port of [hermes-realms](https://github.com/Zeus-Deus/hermes-realms).
The realm engine is shared (see [src/realms_core/UPSTREAM.md](src/realms_core/UPSTREAM.md)).

## Install

```sh
/plugin marketplace add Zeus-Deus/claude-realms
/plugin install realms@realms
```

Or try it from a checkout for one session: `claude --plugin-dir /path/to/claude-realms`.

That's it. The first time Claude needs a desktop it installs the computer-use
driver by itself (verified, no root) while a little portal animation plays above
the prompt. If system packages are missing, Claude shows you one command to run
(`! sudo pacman -S --needed ...` on Arch/Omarchy); `/realm setup` does the same check by hand.

**Needs:** Linux x86-64 (or arm64), [uv](https://docs.astral.sh/uv/), Claude Code 2.1.287+, and
`labwc xorg-xwayland wayvnc grim wlr-randr glib2 dbus at-spi2-core bubblewrap`.
The Omarchy VM also needs `qemu-full edk2-ovmf mtools openssh socat jq`, KVM
access, a systemd user session and an existing `~/.ssh/id_ed25519`. `/realm setup omarchy`
reuses the base image hermes-realms already built on this machine (an instant
copy on btrfs), or else builds one from Omarchy's signed ISO (about 5 GB).

## Use

Just ask Claude to test something with a GUI. It reads the plugin's `realms`
skill and starts a realm on its first desktop action. The pane opens by itself the
first time (in a terminal at least 144 columns wide; otherwise use `/realm view`).

| Command | |
|---|---|
| `/realm` | status of this session's realm |
| `/realm view [image\|raster]` | watch it live in a pane |
| `/realm on [omarchy]` | start it (regular realm, or the Omarchy VM) |
| `/realm launch CMD` | start a program on the realm desktop yourself |
| `/realm repair` | reconnect only the desktop driver; the desktop and its apps stay |
| `/realm list` | every realm and VM disk kept on this machine, with sizes |
| `/realm delete ID` / `/realm clean` | delete one stopped realm / every stopped one except this session's |
| `/realm control` | take or hand back the desktop (the agent waits meanwhile) |
| `/realm full` | open it full size in your browser (noVNC, with control) |
| `/realm watch` | copy that browser link instead |
| `/realm stop` / `/realm off` | power it down (home kept) / also disable agent use this session |
| `/realm size 1280x800` | resize the regular realm |
| `/realm setup [omarchy]` | install what it needs |
| `/realm driver [check\|update\|rollback]` | the computer-use driver |
| `/realm doctor` | diagnostics |

**What Claude gets:** `realm` (control), `realm_exec` (run a command in the
realm and get its output), `realm_launch` (start a GUI program), plus the
computer-use driver's own tools: screenshots, clicks, typing, scrolling, drag,
window and accessibility inspection, etc. These are passed through exactly as the
installed driver describes them, so new driver features show up without a plugin update.

**Live view:** Claude Code draws real pixels only in terminals that speak the
kitty graphics protocol and identify as kitty or Ghostty; there the Realm pane
shows the realm's screen sharp and live. Everywhere else a terminal can only show
text cells, so:

- **In Codemux** (an xterm.js terminal, no picture support) the realm opens in a
  **Codemux browser pane** instead: the full 1920x1080 desktop through noVNC,
  sharp, with control. It opens by itself the first time a realm goes live.
- **In other terminals** the pane shows a coloured-cell preview (2x2 area-averaged
  pixels per cell: layout and windows, not readable text), and **Full view**
  (`f`, or `/realm full`) opens the sharp view in your browser.
- The "Watch the realm in" option (`auto` / `pane` / `browser`) overrides this;
  `/realm view raster` always shows the cell preview.

`Take control` (`t`) in the pane, or the noVNC view's own control, lets you click
and type into the realm. The pane keeps the realm's 16:9 shape from the
terminal's cell shape (picked per terminal; "Terminal cell shape" overrides it).

**Footer toggle:** while a realm is live, `◈ realm live` sits at the right of the
prompt footer; click it to open or close the Realm pane.

**Lifecycle:** a realm belongs to its session. It stops when the session ends
(even if Claude Code is killed), after 30 minutes idle, or on `/realm stop`. Its
HOME (or VM disk) is kept and comes back on the next start in the same session.
Stopped ones unused for 30 days are deleted when a session starts
(`claude.retention_days`, `0` keeps them); `/realm list` shows what is kept and
`/realm delete` / `/realm clean` free space now. Claude can list them but only
you can delete them.

## Settings

`/config` lists the plugin's options: open the pane automatically, live view mode
(auto/image/raster), frames per second, and the host guard. Realm settings live
in the plugin's data directory (`~/.claude/plugins/data/realms-*/config.json`):

```json
{
  "size": "1920x1080",
  "idle_ttl": 1800,
  "default_kind": "realm",
  "renderer": "auto",
  "process_backend": "auto",
  "vm": { "memory": 3072, "network": true, "disk_size": "40G" },
  "driver": { "channel": "stable", "stable_min_age_hours": 24, "check_updates": true, "keep_versions": 2 },
  "claude": { "auto_setup": true, "retention_days": 30,
              "exclude_tools": ["check_for_update", "install_extension", "install_ffmpeg", "set_config", "replay_trajectory"] }
}
```

### The driver is never pinned

The computer-use driver ([trycua/cua](https://github.com/trycua/cua) `cua-driver`)
moves fast, so no version, URL or checksum is written in this code:

- **Channels:** `stable` (default: the newest release at least a day old that
  passes a smoke test), `latest`, `nightly`, or `pinned` with `"version": "x.y.z"`.
- **Verified:** every download is checked against the checksum the upstream
  release publishes (GitHub's asset digest, plus `SHA256SUMS` when present).
  An unverifiable or tampered release is refused.
- **Tested before use:** the release must report its version, describe itself
  (`manifest`) and list its tools, or it never becomes current.
- **Undoable:** `/realm driver update` keeps the previous version; a failed update
  restores it, and `/realm driver rollback` switches back.
- **Discovered, not assumed:** the tool list and launch arguments come from the
  driver itself (`tools/list`, `manifest`). Upstream renames of tags or assets
  are settings (`tag_pattern`, `asset_pattern`), not code.

## Safety model

- A regular realm is **GUI separation**, not a sandbox for hostile code. It shares
  the kernel and filesystem, so the project is right there to build and run. The
  realm's processes get only their own display, D-Bus and accessibility bus, and
  never your session's.
- The driver runs in bubblewrap with no network, no host PIDs and no input
  devices, seeing only the realm's runtime directory.
- While a realm is live, Bash commands that re-point GUI tools at your display
  (`DISPLAY=:0 ...`) are refused, so Claude can't drive your real screen while
  reporting that it works privately.
- The Omarchy VM is the stronger boundary: its own kernel and disk, with files moved
  only by explicit push/pull. Its disk is unencrypted and has passwordless sudo,
  so don't put secrets in it.
- The noVNC link binds to loopback only, with a short-lived ticket; view-only by default.
- The pane and `/realm` talk to the plugin's server over a private Unix socket
  (mode 0600). The model's tool calls keep going through Claude Code's normal
  permission prompts.

## How it fits together

```
Claude Code ─┬─ mod (hooks/register.tsx) ── pane · /realm · status · host guard
             │        │ control socket            │ frames (claude_realms.frames)
             │        ▼                           ▼
             └─ MCP server (claude_realms.server) ── realm VNC socket
                      │ realm, realm_exec, realm_launch, driver tools (passed through)
                      ▼
                realms_core ── labwc realm (systemd scope or direct process tree)
                           └── Omarchy VM (QEMU via systemd user units)
```

## Development

Everything is tested in a disposable, unprivileged Docker container (uid 1000,
all capabilities dropped; seccomp relaxed only so bubblewrap can create
namespaces). See `test/docker/`.

```sh
docker build -t claude-realms-rig test/docker
test/docker/run.sh -- bash -lc 'cd /plugin && uv sync --frozen -q && /home/tester/venv/bin/python -m pytest -q'
test/docker/run.sh -- bash -lc 'cd /plugin && uv sync --frozen -q && /home/tester/venv/bin/python -m pytest -q -m integration tests/integration'
# mod tests and validation need the claude binary mounted:
test/docker/run.sh --mount "$(command -v claude):/usr/local/bin/claude:ro" -- bash -lc 'cp -r /plugin /tmp/p && claude plugin test /tmp/p'
```

`test/e2e/tui.sh` drives a real interactive Claude Code session in tmux inside
the container (the `/realm` UI and the pane need no model; agent turns need
`CLAUDE_CODE_OAUTH_TOKEN`).

## License

MIT. Vendored noVNC, pako and omarchy-vm keep their own licenses (see
`src/realms_core/web/THIRD_PARTY.md` and `src/realms_core/vendor/VENDOR.md`).
