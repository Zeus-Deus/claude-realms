"""The MCP server over real stdio, before any driver or desktop exists."""
import json
import os
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from claude_realms.drivers import expose
from claude_realms.host import ClaudeSettings


async def call(home, steps):
    env = dict(os.environ, REALMS_HOME=str(home), CLAUDE_CODE_SESSION_ID="unit-test")
    env.pop("CLAUDE_PLUGIN_DATA", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "claude_realms.server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            results = {"init": init, "tools": (await session.list_tools()).tools}
            for name, args in steps:
                results[name + json.dumps(args)] = await session.call_tool(name, args)
            return results


def test_without_a_driver_only_realm_control_is_offered(tmp_path):
    # With auto_setup off the driver is not fetched (no network in unit tests).
    (tmp_path / "config.json").write_text(json.dumps({"claude": {"auto_setup": False}}))
    results = anyio.run(call, tmp_path, [("realm", {"action": "status"}), ("realm_exec", {"command": "true"})])
    assert results["init"].serverInfo.name == "claude-realms"
    assert [tool.name for tool in results["tools"]] == ["realm", "realm_exec", "realm_launch"]
    status = results['realm{"action": "status"}']
    data = json.loads(status.content[1].text)
    assert data["owner"] == "claude-unit-test"
    assert data["setup"]["driver"] is None
    assert "/realm setup" in status.content[0].text
    refused = results['realm_exec{"command": "true"}']
    assert refused.isError and "setup" in refused.content[0].text


def test_person_holding_control_is_reported(tmp_path):
    results = anyio.run(call, tmp_path, [("realm", {"action": "control", "control": True}),
                                         ("realm", {"action": "status"})])
    data = json.loads(results['realm{"action": "status"}'].content[1].text)
    assert data["controlled"] is True


def test_driver_catalog_is_offered_with_settings_applied():
    catalog = [
        types.Tool(name="click", description="Click", inputSchema={
            "type": "object", "properties": {"x": {"type": "number"}, "screenshot_out_file": {"type": "string"}},
            "required": ["screenshot_out_file"]}),
        types.Tool(name="set_config", description="Admin", inputSchema={"type": "object"}),
    ]
    offered = expose(catalog, ClaudeSettings())
    assert [tool.name for tool in offered] == ["click"]
    assert "screenshot_out_file" not in offered[0].inputSchema["properties"]
    assert offered[0].inputSchema["required"] == []
    assert offered[0].description.startswith("Acts inside this session's private realm")
    prefixed = expose(catalog, ClaudeSettings(tool_prefix="realm_", exclude_tools=()))
    assert [tool.name for tool in prefixed] == ["realm_click", "realm_set_config"]


def test_settings_come_from_the_claude_section(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "size": "1280x800", "driver": {"channel": "latest"},
        "claude": {"auto_start": False, "exclude_tools": ["kill_app"], "future_option": 1}}))
    settings = ClaudeSettings.load(tmp_path)
    assert settings.auto_start is False and settings.exclude_tools == ("kill_app",)
    assert settings.extra == {"future_option": 1}
    from realms_core.config import Config
    config = Config.load(tmp_path)
    assert config.size == "1280x800" and config.driver.channel == "latest"


@pytest.mark.parametrize("command, blocked", [
    ("DISPLAY=:0 xdotool click 1", True),
    ("env WAYLAND_DISPLAY=wayland-1 foot", True),
    ("export XDG_RUNTIME_DIR=/run/user/1000; app", True),
    ("echo DISPLAY=:0", False),
    ("DISPLAY= app", False),
    ("ls -la", False),
])
def test_host_guard(command, blocked):
    from realms_core.host_guard import host_escape
    assert (host_escape(command) is not None) is blocked


def test_separate_realm_is_only_for_subagents(tmp_path):
    results = anyio.run(call, tmp_path, [("realm", {"action": "on", "separate": True})])
    refused = results['realm{"action": "on", "separate": true}']
    assert refused.isError and "subagent" in refused.content[0].text


def test_auto_start_off_asks_for_an_explicit_start(tmp_path):
    from claude_realms.host import ClaudeSettings
    from claude_realms.service import RealmService
    from realms_core.lifecycle import RealmError

    service = RealmService(tmp_path, "claude-no-auto", ClaudeSettings(auto_start=False))
    with pytest.raises(RealmError, match="action: on"):
        service.ensure(agent=True)


def test_stopping_hands_control_back(tmp_path):
    results = anyio.run(call, tmp_path, [("realm", {"action": "control", "control": True}),
                                         ("realm", {"action": "stop"}),
                                         ("realm", {"action": "status"})])
    assert json.loads(results['realm{"action": "status"}'].content[1].text)["controlled"] is False


def test_listing_is_open_but_deleting_is_the_persons(tmp_path):
    results = anyio.run(call, tmp_path, [("realm", {"action": "list"}),
                                         ("realm", {"action": "delete", "id": "r-0123456789abcdef01234567"}),
                                         ("realm", {"action": "clean"})])
    assert "No realms" in results['realm{"action": "list"}'].content[0].text
    for key in ('realm{"action": "delete", "id": "r-0123456789abcdef01234567"}', 'realm{"action": "clean"}'):
        assert results[key].isError and "person's decision" in results[key].content[0].text


def test_hermes_base_is_found_and_imported(tmp_path, monkeypatch):
    import json as _json
    from claude_realms.host import ClaudeSettings
    from claude_realms.service import RealmService, hermes_bases, import_base
    from realms_core.setup_plan import base_present

    hermes = tmp_path / "hermes"
    base = hermes / "profiles" / "coder" / "plugin-data" / "hermes-realms" / "vm" / "base"
    base.mkdir(parents=True)
    (base / "disk.qcow2").write_bytes(b"qcow" * 1024)
    (base / "base.json").write_text(_json.dumps({"built_at": 1.0, "iso": "omarchy-4.0.3.iso", "generation": "a" * 32}))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    found = hermes_bases()
    assert [b["path"] for b in found] == [str(base)]
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    service = RealmService(home, "claude-import", ClaudeSettings())
    assert not base_present(home)
    assert import_base(service, found[0]["path"])["imported"] is True
    assert base_present(home)
    assert import_base(service, found[0]["path"])["imported"] is False
