# Upstream: hermes-realms

`realms_core` is the realm engine of [Zeus-Deus/hermes-realms](https://github.com/Zeus-Deus/hermes-realms),
copied at a fixed commit so this plugin could be built without touching that
project. The plan is to extract one shared `realms-core` package later and
have both plugins (and future agent adapters) depend on it.

| | |
|---|---|
| Source | `Zeus-Deus/hermes-realms`, package `realms/` |
| Commit | `995ce317a4db0951e574c4569a047ed847dcb20b` (2026-10-02, "Merge pull request #2 from Zeus-Deus/feat/readable-list-viewer") |
| Copied | 36 of 47 modules (plus the new `units.py`), `web/` (viewer + vendored noVNC), `vendor/` (omarchy-vm) |
| Not copied (Hermes glue) | `integration.py`, `permission_transition.py`, `bulk_review.py`, `target_contexts.py`, `setup_continuation.py`, `setup_flow.py`, `setup_worker.py`, `degraded.py`, `realm_state.py`, `delete_flow.py`, `cli.py` |

To see what changed upstream since the copy:

```sh
git -C hermes-realms diff 995ce317a4db0951e574c4569a047ed847dcb20b -- realms/
```

## Local changes

Mechanical renames, so both plugins can run side by side on one machine:

- Package `realms` → `realms_core`; path-hashed runtime package `_hermes_realms_*` → `_claude_realms_*`.
- systemd units `hermes-realm-*`, `hermes-vm-*` → `claude-realm-*`, `claude-vm-*`; descriptions "Hermes realm" → "Claude realm"; inhibitor "Hermes Realms" → "Claude Realms".
- Runtime directories `/run/user/UID/hr-*` → `cr-*`; SSH `HostKeyAlias` `hermes-omarchy-guest` → `claude-omarchy-guest`.
- Guest environment `HERMES_REALM_KIND/ID` → `CLAUDE_REALM_KIND/ID`; transfer markers `hermes_transfer_error`, `.hermes-transfer-*` → `realms_transfer_error`, `.realms-transfer-*`.

Host neutrality:

- `config.py`: no `hermes_constants` / `hermes_cli`. The data home is passed in or read from `REALMS_HOME` (default `~/.local/share/realms`); settings are `<home>/config.json`. New settings: `process_backend`, `driver` (see below); `renderer` gains `auto` (GPU when a render node is usable, else pixman).
- `setup_plan.py`: keeps the read-only facts (`PACKAGES`, `confined`, `distribution`, `base_present`); the Hermes desktop setup-plan builders are gone (each host builds its own consented flow).

No hardcoded driver:

- `install_driver.py` is rewritten. Hermes pinned `cua-driver-rs 0.23.2` with SHA-256 constants; this resolves a release at runtime by channel (`stable`, `latest`, `nightly`, `pinned`), verifies it against the checksum the upstream release publishes (GitHub asset digest, plus `SHA256SUMS` when present), refuses archives with links or escapes, smoke-tests it (`--version`, `manifest`, `list-tools`) before activating, keeps the previous version for rollback and prunes old ones. Tag and asset naming are settings, not code.
- `vm_cua.py` reads the expected binary digest from the installed release's receipt instead of a constant.
- `driver.py` binds the driver's whole release directory read-only into the sandbox (support files sit beside the binary).

Process ownership without systemd:

- New `units.py`: the realm's guardian and desktop scope go through a backend. `systemd` is the original design (transient user service and scope, cgroup membership). `direct` is new, for hosts without a user manager (containers, minimal installs): subreaper session leaders, membership by process tree with PID-plus-start-time identities, receipts under `<home>/realms/units/`.
- `lifecycle.py`, `manager.py`, `supervisor.py`, `bootstrap.py` call the backend instead of `systemctl`/cgroup paths directly; a record stores its backend so later checks use the same rules.
- `manager.start(..., owner_process=)`: the supervisor stops the desktop (keeping its workspace) as soon as the owning host process is gone, however it ended.

The Omarchy VM kind is unchanged in mechanism and still requires a systemd user manager (the vendored `omarchy-vm` itself starts QEMU with `systemd-run --user`).

## Tests

Hermes-independent tests from `hermes-realms/tests` are in `tests/core/` with the same renames. Changes there:
`test_realms_driver_verification_privacy.py` targets the new `smoke_test`; `test_realms_vm_cua.py` writes a driver receipt instead of patching the pin;
`test_realms_workspace.py` patches `unit_info`; `test_realms_dependency_floor.py` reads floors from `pyproject.toml`;
`test_realms_control_retention.py` uses `websockets.frames.Opcode` (websockets 17).
