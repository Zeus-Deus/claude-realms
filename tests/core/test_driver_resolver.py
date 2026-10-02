"""Driver releases are chosen by channel and verified against upstream metadata.

A fake upstream serves a release listing and a tarball holding a fake driver,
so install, update and rollback run for real without the network.
"""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import time

import pytest

from realms_core import install_driver as drivers
from realms_core.config import Config, DriverConfig

ARCH = drivers.arch()


def fake_driver(version, *, broken=False):
    script = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "verb = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        f"if verb == '--version': print('cua-driver {'0.0.0' if broken else version}')\n"
        f"elif verb == 'manifest': print(json.dumps({{'binary_version': '{version}', 'subcommands': [{{'name': 'mcp'}}, {{'name': 'serve'}}], 'mcp_invocation': {{'args': ['mcp']}}}}))\n"
        "elif verb == 'list-tools': print('click: Click\\nget_desktop_state: Capture')\n"
    )
    return script.encode()


def tarball(version, *, broken=False, extra=None):
    buffer = io.BytesIO()
    top = f"cua-driver-rs-{version}-linux-{ARCH}"
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        def add(name, data, mode=0o755, kind=tarfile.REGTYPE, link=""):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.type, info.linkname = len(data), mode, kind, link
            archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        add(top, b"", kind=tarfile.DIRTYPE)
        add(top + "/cua-driver", fake_driver(version, broken=broken))
        add(top + "/LICENSE", b"MIT")
        if extra:
            extra(add, top)
    return buffer.getvalue()


class Upstream:
    def __init__(self):
        self.releases = []
        self.files = {}
        self.listings = 0

    def publish(self, version, *, age_hours=48, nightly=False, digest=True, sums=False, data=None):
        name = f"cua-driver-rs-{version}-linux-{ARCH}.tar.gz"
        data = data if data is not None else tarball(version)
        tag = (f"nightly-cua-driver-rs-v{version}" if nightly else f"cua-driver-rs-v{version}")
        url = f"https://example.invalid/{tag}/{name}"
        self.files[url] = data
        assets = [{"name": name, "browser_download_url": url, "size": len(data),
                   "digest": "sha256:" + hashlib.sha256(data).hexdigest() if digest else None}]
        if sums:
            sums_url = f"https://example.invalid/{tag}/SHA256SUMS"
            self.files[sums_url] = (sums if isinstance(sums, bytes) else
                                    f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode())
            assets.append({"name": "SHA256SUMS", "browser_download_url": sums_url})
        published = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_hours * 3600))
        self.releases.insert(0, {"tag_name": tag, "published_at": published, "draft": False, "assets": assets})

    def fetch(self, url, timeout=30):
        if url.startswith("https://api.github.com/"):
            self.listings += 1
            page = int(url.rsplit("page=", 1)[1])
            return json.dumps(self.releases if page == 1 else []).encode()
        return self.files[url]


@pytest.fixture
def upstream():
    return Upstream()


def config(**driver):
    return Config(driver=DriverConfig(**driver))


def test_channels_choose_without_any_pin(upstream, tmp_path):
    upstream.publish("0.30.0", age_hours=200)
    upstream.publish("0.31.0", age_hours=30)
    upstream.publish("0.32.0", age_hours=2)
    upstream.publish("0.32.1-nightly.20261002.1", age_hours=1, nightly=True)
    releases = drivers.list_releases(tmp_path, "trycua/cua", fetch=upstream.fetch)
    pick = lambda **kw: drivers.choose(releases, DriverConfig(**kw))["version"]
    assert pick() == "0.31.0"  # stable: newest release at least a day old
    assert pick(stable_min_age_hours=0) == "0.32.0"
    assert pick(channel="latest") == "0.32.0"
    assert pick(channel="nightly") == "0.32.1-nightly.20261002.1"
    assert pick(channel="pinned", version="0.30.0") == "0.30.0"


def test_listing_is_cached(upstream, tmp_path):
    upstream.publish("0.31.0")
    drivers.list_releases(tmp_path, "trycua/cua", fetch=upstream.fetch)
    drivers.list_releases(tmp_path, "trycua/cua", fetch=upstream.fetch)
    assert upstream.listings == 1
    drivers.list_releases(tmp_path, "trycua/cua", fetch=upstream.fetch, use_cache=False)
    assert upstream.listings == 2


def test_renamed_upstream_needs_only_settings(upstream, tmp_path):
    data = tarball("2.0.0")
    upstream.publish("2.0.0", data=data)
    release = upstream.releases[0]
    release["tag_name"] = "driver-v2.0.0"
    release["assets"][0]["name"] = f"driver-2.0.0-{ARCH}.tgz"
    custom = DriverConfig(tag_pattern=r"driver-v(?P<version>[0-9.]+)", asset_pattern="driver-{version}-{arch}.tgz")
    releases = drivers.list_releases(tmp_path, "trycua/cua", fetch=upstream.fetch, config=custom)
    assert drivers.choose(releases, custom)["version"] == "2.0.0"


def test_install_verifies_then_activates(upstream, tmp_path):
    upstream.publish("0.31.0", sums=True)
    progress = []
    current = drivers.install(tmp_path, config(), fetch=upstream.fetch, progress=progress.append)
    assert current["version"] == "0.31.0"
    assert set(current["checks"]) == {"github-asset-digest", "SHA256SUMS"}
    assert current["smoke"]["tool_count"] == 2
    assert Path(current["path"]).is_file() and Path(current["path"]).name == "cua-driver"
    assert drivers.driver_executable(tmp_path) == current["path"]
    assert progress[0] == "resolving" and progress[-1] == "installed 0.31.0"


def test_tampered_download_is_refused(upstream, tmp_path):
    upstream.publish("0.31.0")
    url = upstream.releases[0]["assets"][0]["browser_download_url"]
    upstream.files[url] = tarball("0.31.0") + b"tampered"
    with pytest.raises(drivers.DriverError, match="digest"):
        drivers.install(tmp_path, config(), fetch=upstream.fetch)
    assert drivers.current_driver(tmp_path) is None


def test_mismatched_sha256sums_is_refused(upstream, tmp_path):
    upstream.publish("0.31.0", sums=b"0" * 64 + b"  cua-driver-rs-0.31.0-linux-" + ARCH.encode() + b".tar.gz\n")
    with pytest.raises(drivers.DriverError, match="SHA256SUMS"):
        drivers.install(tmp_path, config(), fetch=upstream.fetch)


def test_release_without_checksums_is_refused(upstream, tmp_path):
    upstream.publish("0.31.0", digest=False)
    with pytest.raises(drivers.DriverError, match="no checksum"):
        drivers.install(tmp_path, config(), fetch=upstream.fetch)


def test_release_that_fails_its_smoke_test_is_refused(upstream, tmp_path):
    upstream.publish("0.31.0", data=tarball("0.31.0", broken=True))
    with pytest.raises(drivers.DriverError, match="version"):
        drivers.install(tmp_path, config(), fetch=upstream.fetch)


def test_archive_with_links_or_escapes_is_refused(upstream, tmp_path):
    def link(add, top):
        add(top + "/evil", b"", kind=tarfile.SYMTYPE, link="/etc/passwd")
    upstream.publish("0.31.0", data=tarball("0.31.0", extra=link))
    with pytest.raises(drivers.DriverError, match="link"):
        drivers.install(tmp_path, config(), fetch=upstream.fetch)


def test_update_and_rollback_keep_the_previous_version(upstream, tmp_path):
    upstream.publish("0.30.0")
    drivers.install(tmp_path, config(), fetch=upstream.fetch)
    upstream.publish("0.31.0")
    result = drivers.check_update(tmp_path, config(), fetch=upstream.fetch, use_cache=False)
    assert result == {"channel": "stable", "current": "0.30.0", "candidate": "0.31.0", "update_available": True}
    assert drivers.update(tmp_path, config(), fetch=upstream.fetch)["version"] == "0.31.0"
    assert drivers.current_driver(tmp_path)["previous"] == "0.30.0"
    assert drivers.rollback(tmp_path)["version"] == "0.30.0"


def test_failed_update_restores_the_working_driver(upstream, tmp_path):
    upstream.publish("0.30.0")
    drivers.install(tmp_path, config(), fetch=upstream.fetch)
    upstream.publish("0.31.0", data=tarball("0.31.0", broken=True))
    with pytest.raises(drivers.DriverError):
        drivers.update(tmp_path, config(), fetch=upstream.fetch)
    assert drivers.current_driver(tmp_path)["version"] == "0.30.0"


def test_old_versions_are_pruned(upstream, tmp_path):
    for version in ("0.28.0", "0.29.0", "0.30.0", "0.31.0"):
        upstream.publish(version)
        drivers.update(tmp_path, config(), fetch=upstream.fetch)
    assert drivers.installed_versions(tmp_path) == ["0.30.0", "0.31.0"]


def test_changed_binary_is_no_longer_trusted(upstream, tmp_path):
    upstream.publish("0.31.0")
    current = drivers.install(tmp_path, config(), fetch=upstream.fetch)
    Path(current["path"]).write_bytes(b"#!/bin/sh\necho changed\n")
    assert drivers.current_driver(tmp_path) is None
    with pytest.raises(drivers.DriverError):
        drivers.driver_executable(tmp_path)


def test_pinned_channel_requires_a_version():
    with pytest.raises(ValueError):
        DriverConfig(channel="pinned")
    with pytest.raises(ValueError):
        DriverConfig(tag_pattern="no-version-group")


def test_rate_limited_listing_falls_back_to_the_last_one(upstream, tmp_path):
    import urllib.error
    upstream.publish("0.31.0")
    drivers.list_releases(tmp_path, "trycua/cua", fetch=upstream.fetch)

    def limited(url, timeout=30):
        if url.startswith("https://api.github.com/"):
            raise urllib.error.HTTPError(url, 403, "rate limit exceeded", None, None)
        return upstream.fetch(url, timeout)

    releases = drivers.list_releases(tmp_path, "trycua/cua", fetch=limited, use_cache=False)
    assert [r["version"] for r in releases] == ["0.31.0"]
    assert drivers.install(tmp_path, config(), fetch=limited, use_cache=False)["version"] == "0.31.0"


def test_rate_limited_without_any_listing_says_what_to_do(tmp_path):
    import urllib.error

    def limited(url, timeout=30):
        raise urllib.error.HTTPError(url, 403, "rate limit exceeded", None, None)

    with pytest.raises(drivers.DriverError, match="GITHUB_TOKEN"):
        drivers.list_releases(tmp_path, "trycua/cua", fetch=limited)
