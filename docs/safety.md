# Safety model

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
