"""Computer-use driver releases, resolved at runtime and verified, never pinned.

No version, URL or checksum lives in this code. A release is chosen from the
upstream release list by the configured channel (see ``DriverConfig``); the
download is verified against the checksum the upstream release publishes (the
per-asset ``digest`` GitHub records, cross-checked with ``SHA256SUMS`` when the
release ships one); the unpacked binary must report the expected version,
describe itself (``manifest``) and list its tools before it becomes current.
Earlier versions are kept for rollback.

Layout under ``<home>/drivers``::

    <version>/              unpacked release (binary + its support files)
    <version>/receipt.json  what was verified, by whom, when
    current.json            {"version": ..., "previous": ...}
    releases-cache.json     the last release listing (avoids API rate limits)
"""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request

API = "https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
# Upstream naming conventions. They are defaults of DriverConfig (tag_pattern,
# nightly_tag_pattern, asset_pattern), so a renamed upstream needs a setting,
# not a code change.
BINARY = "cua-driver"
CACHE_SECONDS = 6 * 3600
_SAFE_ENV = {"CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"}


class DriverError(RuntimeError):
    pass


def _root(home):
    from .config import driver_root

    root = driver_root(home)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise DriverError("driver directory has unsafe ownership: " + str(root))
    return root


def arch():
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise DriverError("no driver builds for this CPU architecture: " + machine)


def driver_env(extra=None):
    """A neutral environment for metadata commands: no display, no host bus."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **_SAFE_ENV}
    scratch = tempfile.mkdtemp(prefix="realms-driver-meta-")
    env.update(HOME=scratch, XDG_CONFIG_HOME=scratch, XDG_CACHE_HOME=scratch,
               XDG_STATE_HOME=scratch, XDG_DATA_HOME=scratch)
    if extra:
        env.update(extra)
    return env, scratch


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, value):
    from .lifecycle import atomic_json

    atomic_json(path, value)


# --------------------------------------------------------------------------
# Release discovery


def _fetch(url, timeout=30):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "realms-driver-resolver",
        **({"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"]} if os.environ.get("GITHUB_TOKEN") else {}),
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _parse_time(value):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def list_releases(home, repo, *, use_cache=True, fetch=_fetch, pages=3, config=None):
    """Driver releases newest first: ``[{version, tag, nightly, published, assets}]``."""
    from .config import DriverConfig

    config = config or DriverConfig(repo=repo)
    stable_tag = re.compile(config.tag_pattern)
    nightly_tag = re.compile(config.nightly_tag_pattern)
    cache = _root(home) / "releases-cache.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("repo") != repo:
            cached = None
    except (FileNotFoundError, ValueError, KeyError):
        cached = None
    if use_cache and cached and time.time() - cached["fetched_at"] < CACHE_SECONDS:
        return cached["releases"]
    try:
        return _fetch_releases(home, repo, fetch=fetch, pages=pages, stable_tag=stable_tag,
                               nightly_tag=nightly_tag, cache=cache)
    except (OSError, ValueError) as exc:
        # Rate limited or offline: an older listing still names verifiable
        # releases (every download is checked against its own digest).
        if cached:
            return cached["releases"]
        raise DriverError("could not list driver releases (" + str(exc) + "); set GITHUB_TOKEN "
                          "if GitHub rate-limits this network") from exc


def _fetch_releases(home, repo, *, fetch, pages, stable_tag, nightly_tag, cache):
    releases = []
    for page in range(1, pages + 1):
        batch = json.loads(fetch(API.format(repo=repo, page=page)))
        if not isinstance(batch, list):
            raise DriverError("unexpected release listing from " + repo)
        for item in batch:
            tag = item.get("tag_name", "")
            nightly = nightly_tag.fullmatch(tag)
            stable = stable_tag.fullmatch(tag)
            if not (nightly or stable) or item.get("draft"):
                continue
            releases.append({
                "version": (nightly or stable).group("version"),
                "tag": tag,
                "nightly": bool(nightly),
                "published": _parse_time(item["published_at"]),
                "assets": {
                    asset["name"]: {
                        "url": asset["browser_download_url"],
                        "digest": asset.get("digest"),
                        "size": asset.get("size"),
                    }
                    for asset in item.get("assets", [])
                },
            })
        if len(batch) < 100:
            break
    releases.sort(key=lambda release: release["published"], reverse=True)
    _atomic_json(cache, {"repo": repo, "fetched_at": time.time(), "releases": releases})
    return releases


def choose(releases, config, *, now=None, exclude=()):
    """The release the channel names, or ``None``. ``exclude`` skips versions."""
    now = time.time() if now is None else now
    candidates = [
        release for release in releases
        if release["version"] not in exclude
        and config.asset_pattern.format(version=release["version"], arch=arch()) in release["assets"]
    ]
    if config.channel == "pinned":
        return next((r for r in candidates if r["version"] == config.version), None)
    if config.channel == "nightly":
        return candidates[0] if candidates else None
    stable = [release for release in candidates if not release["nightly"]]
    if config.channel == "latest":
        return stable[0] if stable else None
    settled = [
        release for release in stable
        if now - release["published"] >= config.stable_min_age_hours * 3600
    ]
    return settled[0] if settled else (stable[-1] if stable else None)


# --------------------------------------------------------------------------
# Installed versions


def _version_dir(home, version):
    if not re.fullmatch(r"[0-9A-Za-z.\-]+", version or ""):
        raise DriverError("invalid driver version " + repr(version))
    return _root(home) / version


def receipt(home, version):
    try:
        return json.loads((_version_dir(home, version) / "receipt.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _verified(home, version):
    """Receipt for ``version`` when its binary still matches, else ``None``."""
    data = receipt(home, version)
    if data is None:
        return None
    binary = _version_dir(home, version) / BINARY
    try:
        info = binary.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not info.st_mode & 0o100:
        return None
    identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    if data.get("stat") != identity:
        # Changed inode metadata: only a fresh hash can vouch for it again.
        if _sha256(binary) != data["binary_sha256"]:
            return None
        data["stat"] = identity
        _atomic_json(_version_dir(home, version) / "receipt.json", data)
    return data


def current_driver(home):
    """``{version, path, binary_sha256, ...}`` of the active driver, or ``None``."""
    try:
        pointer = json.loads((_root(home) / "current.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, DriverError):
        return None
    data = _verified(home, pointer.get("version", ""))
    if data is None:
        return None
    return dict(data, path=str(_version_dir(home, data["version"]) / BINARY),
                previous=pointer.get("previous"))


def driver_executable(home):
    current = current_driver(home)
    if current is None:
        raise DriverError("no verified computer-use driver installed; run /realm setup")
    return current["path"]


def binary_sha256(home):
    current = current_driver(home)
    if current is None:
        raise DriverError("no verified computer-use driver installed; run /realm setup")
    return current["binary_sha256"]


def installed_versions(home):
    root = _root(home)
    return sorted(
        (path.name for path in root.iterdir() if path.is_dir() and (path / "receipt.json").exists()),
        key=lambda name: [int(part) if part.isdigit() else part for part in re.split(r"[.\-]", name)],
    )


def manifest(binary):
    """The driver's own description of its CLI and MCP invocation."""
    env, scratch = driver_env()
    try:
        result = subprocess.run([str(binary), "manifest"], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=20)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if result.returncode:
        raise DriverError("driver manifest failed: " + result.stderr.strip()[:500])
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or "subcommands" not in data:
        raise DriverError("driver manifest is not a CLI description")
    return data


def smoke_test(binary, version):
    """Run the release before trusting it: version, manifest, tool listing."""
    env, scratch = driver_env()
    try:
        result = subprocess.run([str(binary), "--version"], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=20)
        if result.returncode or version.split("-")[0] not in result.stdout:
            raise DriverError("driver did not report version " + version + ": " + result.stdout.strip()[:200])
        described = manifest(binary)
        listed = subprocess.run([str(binary), "list-tools"], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        tools = [line.split(":", 1)[0] for line in listed.stdout.splitlines() if ":" in line]
        if listed.returncode or not tools:
            raise DriverError("driver listed no tools")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    verbs = {command.get("name") for command in described.get("subcommands", [])}
    return {"manifest_version": described.get("binary_version"), "verbs": sorted(v for v in verbs if v),
            "tool_count": len(tools)}


# --------------------------------------------------------------------------
# Install / update / rollback


def _verify_download(archive, release, asset_name, *, fetch=_fetch):
    asset = release["assets"][asset_name]
    actual = _sha256(archive)
    checks = []
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        if digest.removeprefix("sha256:") != actual:
            raise DriverError("downloaded driver does not match the release's recorded digest")
        checks.append("github-asset-digest")
    sums = release["assets"].get("SHA256SUMS")
    if sums:
        listing = fetch(sums["url"]).decode("utf-8", "replace")
        expected = {
            parts[-1].lstrip("*"): parts[0]
            for parts in (line.split() for line in listing.splitlines()) if len(parts) >= 2
        }
        if asset_name not in expected or expected[asset_name] != actual:
            raise DriverError("downloaded driver does not match the release's SHA256SUMS")
        checks.append("SHA256SUMS")
    if not checks:
        raise DriverError("release publishes no checksum for " + asset_name + "; refusing to install")
    return actual, checks


def _extract(archive, target):
    """Unpack one top-level release directory into ``target`` without escapes."""
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        tops = {Path(member.name).parts[0] for member in members if member.name}
        if len(tops) != 1:
            raise DriverError("driver archive must contain exactly one directory")
        top = tops.pop()
        for member in members:
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise DriverError("driver archive has an unsafe path: " + member.name)
            if not (member.isfile() or member.isdir()):
                raise DriverError("driver archive has a link or device: " + member.name)
        package.extractall(target, filter="data")
    unpacked = Path(target) / top
    binary = unpacked / BINARY
    if not binary.is_file():
        raise DriverError("driver archive has no " + BINARY + " binary")
    return unpacked


def install(home, config=None, *, version=None, progress=None, use_cache=True, fetch=_fetch,
            exclude=()):
    """Install the channel's release (or ``version``) and make it current."""
    from dataclasses import replace
    from .config import Config

    config = (config or Config.load(home)).driver
    if version:
        config = replace(config, channel="pinned", version=version)
    say = progress or (lambda message: None)
    say("resolving")
    release = choose(list_releases(home, config.repo, use_cache=use_cache, fetch=fetch, config=config),
                     config, exclude=exclude)
    if release is None:
        raise DriverError("no driver release matches channel " + config.channel
                          + (" " + config.version if config.version else ""))
    current = current_driver(home)
    if current and current["version"] == release["version"]:
        say("current")
        return current
    existing = _verified(home, release["version"])
    if existing is None:
        asset_name = config.asset_pattern.format(version=release["version"], arch=arch())
        root = _root(home)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=root) as scratch:
            archive = Path(scratch) / asset_name
            say("downloading " + release["version"])
            archive.write_bytes(fetch(release["assets"][asset_name]["url"], timeout=300))
            say("verifying")
            archive_sha, checks = _verify_download(archive, release, asset_name, fetch=fetch)
            unpacked = _extract(archive, Path(scratch) / "unpacked")
            binary = unpacked / BINARY
            binary.chmod(0o700)
            say("testing")
            smoke = smoke_test(binary, release["version"])
            info = binary.stat()
            data = {
                "version": release["version"],
                "tag": release["tag"],
                "nightly": release["nightly"],
                "repo": config.repo,
                "channel": config.channel,
                "asset": asset_name,
                "archive_sha256": archive_sha,
                "checks": checks,
                "binary_sha256": _sha256(binary),
                "smoke": smoke,
                "installed_at": time.time(),
            }
            destination = _version_dir(home, release["version"])
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(unpacked, destination)
            os.chmod(destination, 0o700)
            info = (destination / BINARY).stat()
            data["stat"] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
            _atomic_json(destination / "receipt.json", data)
    activate(home, release["version"])
    _prune(home, config.keep_versions)
    say("installed " + release["version"])
    return current_driver(home)


def activate(home, version):
    if _verified(home, version) is None:
        raise DriverError("driver " + version + " is not installed and verified")
    pointer = _root(home) / "current.json"
    try:
        previous = json.loads(pointer.read_text(encoding="utf-8")).get("version")
    except (FileNotFoundError, ValueError):
        previous = None
    _atomic_json(pointer, {"version": version, "previous": previous if previous != version else None,
                           "activated_at": time.time()})


def rollback(home):
    current = current_driver(home)
    if current is None or not current.get("previous"):
        raise DriverError("no earlier driver to roll back to")
    activate(home, current["previous"])
    return current_driver(home)


def _prune(home, keep):
    """Drop the oldest unprotected versions beyond ``keep``; current and previous stay."""
    current = current_driver(home) or {}
    protected = {v for v in (current.get("version"), current.get("previous")) if v}
    others = [v for v in installed_versions(home) if v not in protected]
    room = max(0, keep - len(protected))
    for version in others[: max(0, len(others) - room)]:
        shutil.rmtree(_version_dir(home, version), ignore_errors=True)


def check_update(home, config=None, *, use_cache=True, fetch=_fetch):
    from .config import Config

    config = (config or Config.load(home)).driver
    current = current_driver(home)
    release = choose(list_releases(home, config.repo, use_cache=use_cache, fetch=fetch, config=config), config)
    return {
        "channel": config.channel,
        "current": current["version"] if current else None,
        "candidate": release["version"] if release else None,
        "update_available": bool(release) and (current is None or release["version"] != current["version"]),
    }


def update(home, config=None, *, progress=None, fetch=_fetch):
    """Install the channel's newest release; restore the previous one on failure."""
    before = current_driver(home)
    try:
        return install(home, config, progress=progress, use_cache=False, fetch=fetch)
    except Exception:
        if before is not None:
            activate(home, before["version"])
        raise


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("home")
    parser.add_argument("action", choices=("install", "update", "check", "rollback", "status"))
    parser.add_argument("--version")
    args = parser.parse_args(argv)
    if args.action == "install":
        result = install(args.home, version=args.version, progress=lambda m: print(m, flush=True))
    elif args.action == "update":
        result = update(args.home, progress=lambda m: print(m, flush=True))
    elif args.action == "check":
        result = check_update(args.home, use_cache=False)
    elif args.action == "rollback":
        result = rollback(args.home)
    else:
        result = current_driver(args.home)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
