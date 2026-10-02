"""Resource pressure blocks allocation, not reuse, without reclaiming peers."""
from contextlib import nullcontext
from pathlib import Path
import runpy
from types import SimpleNamespace

import psutil
import pytest
from realms_test_paths import PLUGIN_ROOT

load = runpy.run_path(str(PLUGIN_ROOT / "realms_core/_binding.py"))["load_runtime"]
pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize("resource", ["memory", "disk"])
@pytest.mark.parametrize("operation", ["start", "install"])
def test_pressure_refuses_new_allocation_before_side_effects(tmp_path, monkeypatch, resource, operation):
    vm = load("vm_manager")
    manager = vm.VmManager(tmp_path / "profile")
    allocation_dir = manager.registry.root / "vm" if operation == "start" else manager.data
    allocation_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=0 if resource == "memory" else 2**60))
    monkeypatch.setattr(vm.shutil, "disk_usage", lambda path: SimpleNamespace(
        free=0 if resource == "disk" and Path(path) == allocation_dir else 2**60))
    monkeypatch.setattr(manager, "_reconcile_locked", lambda: None)
    monkeypatch.setattr(manager, "_force_stop", lambda _: pytest.fail("pressure must not stop VMs"))
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(vm, "_generation_paths", lambda *_: runtime)
    monkeypatch.setattr(vm, "_base_runtime", lambda _: runtime)
    monkeypatch.setattr(manager, "_free_port", lambda _: 2322)
    monkeypatch.setattr(manager, "_remove_locked", lambda record: manager.registry.remove(record["id"]))
    monkeypatch.setattr(manager, "_clone_base", lambda *_: pytest.fail("allocation happened despite resource pressure"))
    monkeypatch.setattr(manager, "_run_script", lambda *a, **k: pytest.fail("installer ran despite resource pressure"))
    monkeypatch.setattr(load("setup_process"), "vm_lifetime", lambda *a, **k: nullcontext())
    if operation == "start":
        monkeypatch.setattr(manager, "base_status", lambda: {"present": True})
    with pytest.raises(load("lifecycle").RealmError, match=resource):
        manager.start("owner") if operation == "start" else manager.install_base()
    assert manager.registry.records() == []
    assert not runtime.exists()
    assert not list(manager.data.glob(".install-*"))


def test_pressure_does_not_prevent_reusing_the_existing_generation(tmp_path, monkeypatch):
    vm = load("vm_manager")
    manager = vm.VmManager(tmp_path / "profile")
    pin = tmp_path / "ssh_known_hosts"
    pin.write_text("fixture host-key pin\n")
    pin.chmod(0o600)
    record = {"id": "fixture-vm", "session_id": "owner", "status": "running",
              "unit": "fixture.service", "runtime_dir": str(tmp_path), "ssh_host_key_pinned": True}
    manager.registry = SimpleNamespace(lock=nullcontext, records=lambda: [record], put=lambda _: None)
    monkeypatch.setattr(manager, "base_status", lambda: {"present": True})
    monkeypatch.setattr(manager, "_reconcile_locked", lambda: None)
    monkeypatch.setattr(manager, "_bind_to_owner", lambda _: None)
    monkeypatch.setattr(vm, "unit_active", lambda _: True)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=0))
    monkeypatch.setattr(vm.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    assert manager.start("owner")["id"] == record["id"]
