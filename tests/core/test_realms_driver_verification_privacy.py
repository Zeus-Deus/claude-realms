"""Driver setup diagnostics must not opt into vendor telemetry."""
import json
import runpy
import sys

import pytest
from realms_test_paths import PLUGIN_ROOT


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("inherited", [None, "1"])
def test_smoke_test_disables_telemetry(tmp_path, monkeypatch, inherited):
    if inherited is None:
        monkeypatch.delenv("CUA_DRIVER_RS_TELEMETRY_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CUA_DRIVER_RS_TELEMETRY_ENABLED", inherited)
    module = runpy.run_path(str(PLUGIN_ROOT / "realms_core/install_driver.py"))
    observed = tmp_path / "observed.jsonl"
    binary = tmp_path / "driver"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"with open({str(observed)!r}, 'a') as log: log.write(json.dumps([sys.argv[1], os.getenv('CUA_DRIVER_RS_TELEMETRY_ENABLED'), os.getenv('DISPLAY'), os.getenv('HOME')]) + '\\n')\n"
        "verb = sys.argv[1]\n"
        "if verb == '--version': print('cua-driver 9.8.7')\n"
        "elif verb == 'manifest': print(json.dumps({'binary_version': '9.8.7', 'subcommands': [{'name': 'mcp', 'args': []}]}))\n"
        "elif verb == 'list-tools': print('click: Click something')\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("DISPLAY", ":0")
    result = module["smoke_test"](binary, "9.8.7")
    assert result["tool_count"] == 1 and result["verbs"] == ["mcp"]
    calls = [json.loads(line) for line in observed.read_text().splitlines()]
    assert [verb for verb, *_ in calls] == ["--version", "manifest", "list-tools"]
    # Telemetry off, no host display, a scratch HOME: metadata commands touch nothing of the person's.
    assert all(telemetry == "0" and display is None and home != str(tmp_path.home()) for _, telemetry, display, home in calls)
