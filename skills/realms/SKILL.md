---
name: realms
description: Test GUI applications in this session's private Linux desktop (a realm) instead of the person's own screen. Use when you need to launch, see, click, type into or verify a desktop app, website-in-a-browser, or anything with a window on Linux; also for Hyprland/Omarchy work in an isolated Omarchy VM.
---

# Realms: private test desktops

A realm is a desktop that belongs to this Claude Code session and nobody else:
a headless labwc/Wayland desktop (`realm`) or an Omarchy QEMU guest
(`omarchy`). The person can watch it live in the Realm pane. Realms are tools,
not your execution environment: editing, building, git and research stay on the
normal host tools (Bash, Read, Edit...). Never control the person's own screen.

## When to use which

- `realm` (default): ordinary Linux GUI tests. Starts in about a second. Shares
  the host kernel and filesystem, so the project checkout is right there. It
  is GUI separation, **not** a sandbox for hostile code.
- `omarchy`: a VM with its own kernel, disk and Omarchy/Hyprland desktop. Use
  it for Hyprland, Omarchy themes and plugins, and system-level changes. Files
  move only through `push` and `pull`.

Honor a kind the person asked for. Otherwise pick what the test needs and say which.

## The loop

1. Build the app with your normal tools in the project checkout.
2. Start or reuse the desktop: any desktop tool starts the selected kind on
   first use, or call `realm` with `action: "on"` (and `kind: "omarchy"` for the
   VM). `realm` `action: "status"` tells missing setup from a running desktop.
3. Run things *in the realm*:
   - `realm_launch` with a command starts a GUI program there (it returns at once).
   - `realm_exec` runs a shell command there and returns its output (a build in
     the VM, `ls` of the realm's HOME, a CLI the app needs).
   - Never start GUI programs with Bash: Bash runs on the person's desktop.
4. See and act with the desktop tools (the computer-use driver's own tools,
   e.g. `get_desktop_state`, `click`, `type_text`, `press_key`, `scroll`,
   `drag`, `list_windows`, `get_window_state`, `zoom`). Prefer the screenshot
   from `get_desktop_state` plus `click` with `scope: "desktop"` and its x/y for
   native Wayland apps. X11 apps (Xwayland) also show up in `list_windows` with
   window-level tools. Pass the same short `session` label on related calls.
5. Verify the effect in the app itself with a fresh capture. A successful click
   response is not proof that the click did what you wanted.
6. In a VM, copy test inputs in with `realm` `action: "push"` (`source`, optional
   `destination`) and results out with `action: "pull"` (`source`,
   `destination`). Never copy credentials, SSH agents or unrelated private data.
   Compare pulled files before overwriting project files.
7. Leave the realm running between steps; it stops by itself when idle or when
   the session ends, and its HOME is kept. Don't stop or restart it as a
   "repair".

## Setup and failures

Setup that needs no root happens by itself: the first desktop call installs
the verified computer-use driver, and the Omarchy VM's base image is built on
first use (or with `realm` `action: "setup"`, `kind: "omarchy"`). It reuses a
base hermes-realms already built on this machine (a quick local copy), else
downloads the signed ISO (~5 GB) and installs it, which takes a while. Do it
yourself; don't ask the person to run it. While the `omarchy-base` job runs,
tell the person once what is happening, follow it with `realm` status, and
continue when it is done. Report only what the tool output says about it.

What only the person can provide: system packages (the printed
`sudo pacman ...` command), KVM access, an `~/.ssh/id_ed25519` key pair. Tell
them exactly what is missing. Never install system packages yourself, and
never fall back to their real desktop.

`realm` `action: "list"` shows kept realms and VM disks with their sizes.
Deleting them is the person's call (`/realm delete ID`, `/realm clean`); suggest
it when space matters, don't do it.

If the desktop tools fail but the realm is live, check `realm` status and use
`realm_exec` to diagnose independently. If only the driver connection is broken,
`realm` `action: "repair"` reconnects it without touching the desktop; then take
a fresh capture. Never replay uncertain input automatically. Don't loop on an identical failing call
or start a replacement desktop to hide a failure; report the concrete blocker.

## The person in control

The person can take control of the desktop from the Realm pane. While they hold
it, desktop tools refuse with "The person has taken control". Wait for them to
hand it back, or ask what they need. Don't work around it with screenshots via
`realm_exec`, other input tools or `realm` `shot`.

`realm` `action: "off"` (or the person's `/realm off`) disables realms for
this session until the person runs `/realm on`. If that's why a tool refuses,
continue ordinary work and tell them; don't try the other kind or undo it.

While a realm is live, Bash commands that point GUI tools at the host
(`DISPLAY=:0 ...`, `WAYLAND_DISPLAY=...`) are refused on purpose. Use
`realm_exec` or `realm_launch` for the realm.

## Subagents

Subagents share the session's realm: same desktop, same rules. Parallel
subagents share one screen, so the parent decides who drives it.

## Verification

Before reporting success, have: a fresh capture showing the expected state,
input effects observed in the application, and your project changes intact on
the host. A running realm is not the same as working computer use, and a
component check is not an end-to-end test.
