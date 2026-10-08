# Settings

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

## The driver is never pinned

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
