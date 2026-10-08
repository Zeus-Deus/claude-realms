# Realms for Claude Code

Private Linux desktops for Claude. When Claude needs to run and test a GUI app,
it gets a desktop of its own, a lightweight **realm** or a full **Omarchy VM**,
and never touches your screen, mouse or clipboard. You watch it live next to the
conversation and can take over at any time.

```
┌ conversation ───────────────────────────┬ Realm r-27ce…  1920x1080 · agent has control ┐
│ ❯ test the settings dialog of my app     │                                              │
│ ● realm_launch ./build/myapp             │     (the realm's screen, live)               │
│ ● click (412, 230)                       │                                              │
│ ● get_desktop_state  ✓ dialog opened     │ [ Take control ] [ Full view ] [ Stop ]      │
└──────────────────────────────────────────┴──────────────────────────────────────────────┘
```

## Install

```sh
claude plugin marketplace add Zeus-Deus/claude-realms && claude plugin install realms@realms
```

Restart Claude Code, then ask Claude to test something with a window. It sets
itself up on first use. If your system is missing a package, Claude tells you
exactly what to install (the ready-to-run command on Arch and Omarchy).

## Uninstall

```sh
claude plugin uninstall realms@realms && claude plugin marketplace remove realms
```

This removes the plugin and everything it stored: realm homes, VM disks and the
computer-use driver.

## Requirements

- Linux (x86-64 or arm64), Claude Code 2.1.287 or newer, and [uv](https://docs.astral.sh/uv/)
- `labwc xorg-xwayland wayvnc grim wlr-randr glib2 dbus at-spi2-core bubblewrap`
- For the Omarchy VM, also: `qemu-full edk2-ovmf mtools openssh socat jq`, KVM,
  a systemd user session and an existing `~/.ssh/id_ed25519`. The VM image
  builds itself on first use (it reuses one that hermes-realms already built, or
  downloads the signed Omarchy ISO, about 5 GB).

## Watching

The Realm pane opens by itself when a realm starts. How sharp it looks depends
on your terminal:

| Terminal | Realm pane |
| --- | --- |
| **Ghostty**, **kitty** | Sharp, full-resolution picture |
| **Codemux** | Opens in a Codemux browser pane (sharp) |
| Everything else (foot, Alacritty, WezTerm, GNOME Terminal, tmux, herdr, ...) | Low-resolution preview: you see the layout, not readable text |

Anywhere, **Full view** (`f` in the pane) opens the live desktop sharp in your
browser. **Take control** (`t`) lets you click and type in it yourself. The
`◈ realm live` button at the right of the prompt footer shows or hides the pane.

## Commands

| Command | |
|---|---|
| `/realm` | status of this session's realm |
| `/realm view` / `/realm full` | show the pane / open it in your browser |
| `/realm on [omarchy]` / `/realm stop` | start a realm or the Omarchy VM / stop it |
| `/realm list` | every realm and VM disk kept on this machine, with sizes |
| `/realm delete ID` / `/realm clean` | delete one stopped realm / all stopped ones |
| `/realm help` | everything else |

A realm stops when its session ends or after 30 idle minutes, and comes back
with its files on the next start. Stopped ones unused for 30 days are deleted
automatically. Claude can list them, but only you can delete them.

## More

- [Settings](docs/settings.md): options, the realm config file and how the computer-use driver stays up to date
- [Safety model](docs/safety.md): what a realm isolates, and how the pieces fit together
- [Development](docs/development.md): tests in a disposable Docker container

This is the Claude Code port of [hermes-realms](https://github.com/Zeus-Deus/hermes-realms).

## License

MIT. Vendored noVNC, pako and omarchy-vm keep their own licenses (see
`src/realms_core/web/THIRD_PARTY.md` and `src/realms_core/vendor/VENDOR.md`).
