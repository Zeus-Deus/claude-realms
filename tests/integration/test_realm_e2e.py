"""Real realms, real driver, real MCP: run only in the disposable Docker rig.

    test/docker/run.sh -- bash -lc 'cd /plugin && uv sync --frozen -q &&
        /home/tester/venv/bin/python -m pytest -m integration tests/integration'

Downloads the current driver release from upstream (network required).
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.integration

if not os.environ.get("REALMS_HOME", "").startswith("/home/tester/"):
    pytest.skip("integration tests start desktops; run them in test/docker/run.sh", allow_module_level=True)

HOME = Path(os.environ["REALMS_HOME"])


def realm_processes():
    out = subprocess.run(["ps", "-eo", "comm="], capture_output=True, text=True).stdout.split()
    return [name for name in out if name in ("labwc", "wayvnc", "Xwayland", "cua-driver", "gtk3-demo")]


@pytest.fixture(scope="module")
def driver():
    from realms_core import install_driver

    return install_driver.install(HOME)


def test_realm_lifecycle_on_the_direct_backend(driver):
    from claude_realms.host import ClaudeSettings
    from claude_realms.service import RealmService

    service = RealmService(HOME, "claude-it-lifecycle", ClaudeSettings.load(HOME))
    record = service.ensure()
    try:
        assert record["backend"] == "direct"
        result = service.exec("echo $XDG_CURRENT_DESKTOP $WAYLAND_DISPLAY; test -z \"$SSH_AUTH_SOCK\"")
        assert result["returncode"] == 0 and result["stdout"].startswith("labwc wayland-")
        # The realm's HOME is durable and private to this owner.
        home = service.exec("echo $HOME")["stdout"].strip()
        assert home.startswith(str(HOME)) and "workspaces" in home
        service.exec("echo kept > ~/marker")
        shot = service.shot()
        assert (shot["width"], shot["height"]) == (1920, 1080)
        service.resize("1280x800")
        assert service.shot()["width"] == 1280
    finally:
        service.stop()
    assert not realm_processes()
    # A restart gets the same HOME back.
    record = service.ensure()
    try:
        assert service.exec("cat ~/marker")["stdout"].strip() == "kept"
    finally:
        service.stop()


def test_idle_realm_expires_by_itself(driver, tmp_path):
    from claude_realms.host import ClaudeSettings
    from claude_realms.service import RealmService

    home = tmp_path / "idle-home"
    home.mkdir(mode=0o700)
    (home / "config.json").write_text(json.dumps({"idle_ttl": 2}))
    # The installed driver is not needed for a bare desktop.
    service = RealmService(home, "claude-it-idle", ClaudeSettings.load(home))
    service.require_ready = lambda kind: None
    record = service.manager.start(service.owner)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and service.running() is not None:
        time.sleep(0.5)
    assert service.running() is None
    assert [r["status"] for r in service.records()] == ["stopped"]
    assert record["id"]


def test_live_view_streams_the_real_screen(driver):
    # A still screen sends one frame and then nothing until something changes.
    from claude_realms.host import ClaudeSettings
    from claude_realms.service import RealmService

    service = RealmService(HOME, "claude-it-frames", ClaudeSettings.load(HOME))
    record = service.ensure()
    try:
        for mode in ("image", "raster"):
            result = subprocess.run(
                [sys.executable, "-P", "-m", "claude_realms.frames", "--socket", record["vnc_socket"],
                 "--mode", mode, "--columns", "60", "--rows", "20", "--frames", "1"],
                capture_output=True, text=True, timeout=30)
            lines = result.stdout.splitlines()
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads(lines[0].removeprefix("@hello "))["width"] == 1920
            assert lines[1].startswith("@file " if mode == "image" else "@raster 60 20 ")
    finally:
        service.stop()


async def mcp_session(steps):
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID="it-mcp")
    params = StdioServerParameters(command=sys.executable, args=["-P", "-m", "claude_realms.server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await steps(session)


def images(result):
    return [base64.b64decode(block.data) for block in result.content if block.type == "image"]


def test_agent_sees_clicks_and_verifies_inside_the_realm(driver):
    async def steps(session):
        names = [tool.name for tool in (await session.list_tools()).tools]
        assert {"realm", "realm_exec", "realm_launch", "click", "get_desktop_state"} <= set(names)
        assert "set_config" not in names  # driver administration stays hidden
        launched = await session.call_tool("realm_launch", {"command": "gtk3-demo"})
        assert not launched.isError, launched.content
        await anyio.sleep(3)
        before = await session.call_tool("get_desktop_state", {"max_image_dimension": 1280, "session": "it"})
        assert not before.isError and images(before)
        # "Button Boxes" in gtk3-demo's sidebar at its default placement
        click = await session.call_tool("click", {"scope": "desktop", "x": 419, "y": 251, "session": "it"})
        assert not click.isError, click.content
        await anyio.sleep(1.5)
        after = await session.call_tool("get_desktop_state", {"max_image_dimension": 1280, "session": "it"})
        assert hashlib.sha256(images(before)[0]).digest() != hashlib.sha256(images(after)[0]).digest()
        held = await session.call_tool("realm", {"action": "control", "control": True})
        assert not held.isError
        blocked = await session.call_tool("get_desktop_state", {})
        assert blocked.isError and "taken control" in blocked.content[0].text
        await session.call_tool("realm", {"action": "control", "control": False})
        status = json.loads((await session.call_tool("realm", {"action": "status"})).content[1].text)
        assert status["live"]["kind"] == "realm"
        return status

    status = anyio.run(mcp_session, steps)
    assert status["driver_tools"] > 20
    # The session ended: its desktop and driver are gone.
    deadline = time.monotonic() + 10
    while realm_processes() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not realm_processes()


def test_browser_viewer_link_serves_novnc(driver):
    import urllib.request

    from claude_realms.host import ClaudeSettings
    from claude_realms.service import RealmService

    service = RealmService(HOME, "claude-it-watch", ClaudeSettings.load(HOME))
    service.ensure()
    try:
        link = service.watch()
        assert link["url"].startswith("http://127.0.0.1:") and "#ticket=" in link["url"]
        page = urllib.request.urlopen(link["url"].split("#", 1)[0], timeout=5).read()
        assert b"viewer.js" in page
    finally:
        service.stop()


def test_subagent_separate_realm_is_its_own_desktop(driver):
    async def steps(session):
        main = await session.call_tool("realm_exec", {"command": "echo $HOME"})
        shared = await session.call_tool("realm_exec", {"command": "echo $HOME", "_agent": "sub-1"})
        own = await session.call_tool("realm", {"action": "on", "separate": True, "_agent": "sub-2"})
        assert not own.isError, own.content
        separate = await session.call_tool("realm_exec", {"command": "echo $HOME", "_agent": "sub-2"})
        homes = [json.loads(r.content[0].text)["stdout"].strip() for r in (main, shared, separate)]
        # sub-1 shares the session's desktop; sub-2 asked for, and got, its own.
        assert homes[0] == homes[1] and homes[2] != homes[0]
        status = json.loads((await session.call_tool("realm", {"action": "status"})).content[1].text)
        return status

    status = anyio.run(mcp_session, steps)
    assert status["live"] is not None
    deadline = time.monotonic() + 10
    while realm_processes() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not realm_processes()


def test_first_use_installs_the_driver_by_itself(tmp_path):
    home = tmp_path / "fresh"
    home.mkdir(mode=0o700)

    async def steps(session):
        names = [tool.name for tool in (await session.list_tools()).tools]
        assert "click" not in names  # no driver yet
        launched = await session.call_tool("realm_launch", {"command": "xterm"})
        assert not launched.isError, launched.content
        names = [tool.name for tool in (await session.list_tools()).tools]
        assert "click" in names
        status = json.loads((await session.call_tool("realm", {"action": "status"})).content[1].text)
        return status

    env = dict(os.environ, REALMS_HOME=str(home), CLAUDE_CODE_SESSION_ID="it-auto")
    seed = HOME / "drivers" / "releases-cache.json"
    if seed.exists():  # reuse the listing: no extra GitHub API call
        (home / "drivers").mkdir(mode=0o700)
        (home / "drivers" / "releases-cache.json").write_bytes(seed.read_bytes())

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-P", "-m", "claude_realms.server"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await steps(session)

    status = anyio.run(run)
    assert status["jobs"]["driver"]["state"] == "done"
    assert any("installed the computer-use driver" in event["text"] for event in status["events"])


def test_list_and_clean_free_stopped_realms(driver):
    from claude_realms.host import ClaudeSettings
    from claude_realms.service import RealmService, clean, inventory

    old = RealmService(HOME, "claude-it-old", ClaudeSettings.load(HOME))
    old.ensure()
    old.exec("head -c 5000000 /dev/urandom > ~/blob")
    old.stop()
    current = RealmService(HOME, "claude-it-current", ClaudeSettings.load(HOME))
    rows = {row["id"]: row for row in inventory(current)}
    stopped = [row for row in rows.values() if row["state"] == "stopped"]
    assert stopped and max(row["bytes"] for row in stopped) >= 5_000_000
    result = clean(current)
    assert result["freed_bytes"] >= 5_000_000 and not result["kept"]
    assert all(row["state"] != "stopped" for row in inventory(current))
