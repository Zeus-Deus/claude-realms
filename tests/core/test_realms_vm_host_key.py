"""Normal guest SSH connections require the generation's enrolled host key."""
from pathlib import Path
import runpy
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from realms_test_paths import PLUGIN_ROOT


@pytest.mark.platforms("linux")
def test_guest_connections_use_only_the_generation_pin(tmp_path):
    load = runpy.run_path(str(PLUGIN_ROOT / "realms_core/_binding.py"))["load_runtime"]
    module = load("vm_manager")
    manager = module.VmManager.__new__(module.VmManager)
    runtime = tmp_path / "generation"
    runtime.mkdir()
    record = {"ssh_port": 2222, "runtime_dir": str(runtime)}
    command = manager.ssh_argv(record)
    result = subprocess.run([command[0], "-G", *command[1:]], capture_output=True,
                            text=True, timeout=10, check=True)
    settings = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
    assert settings["stricthostkeychecking"] == "true"
    assert settings["userknownhostsfile"] == str(runtime / "ssh_known_hosts")
    assert settings["globalknownhostsfile"] == "/dev/null"
    assert settings["updatehostkeys"] == "false"
    assert settings["forwardagent"] == "no"


@pytest.mark.platforms("linux")
def test_reuse_does_not_reenroll_a_lost_host_key(tmp_path, monkeypatch):
    load = runpy.run_path(str(PLUGIN_ROOT / "realms_core/_binding.py"))["load_runtime"]
    module = load("vm_manager")
    manager = module.VmManager.__new__(module.VmManager)
    record = {"id": "fixture-vm", "session_id": "fixture-owner", "status": "running",
              "unit": "fixture.service", "runtime_dir": str(tmp_path), "ssh_host_key_pinned": True}
    manager.registry = SimpleNamespace(lock=nullcontext, records=lambda: [record], put=lambda _: None)
    monkeypatch.setattr(manager, "base_home", lambda: tmp_path)
    monkeypatch.setattr(manager, "base_status", lambda: {"present": True})
    monkeypatch.setattr(manager, "_reconcile_locked", lambda: None)
    monkeypatch.setattr(manager, "_bind_to_owner", lambda _: None)
    monkeypatch.setattr(module, "unit_active", lambda _: True)
    with pytest.raises(module.OwnershipError, match="host-key pin is missing"):
        manager.start("fixture-owner")
