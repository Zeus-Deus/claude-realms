"""Retention and binding checks without compositor or systemd processes."""
import os
from pathlib import Path
from runpy import run_path
import uuid

import pytest
from realms_test_paths import PLUGIN_ROOT


ROOT = PLUGIN_ROOT
load = run_path(str(ROOT / "realms_core/_binding.py"))["load_runtime"]


def record_for(home):
    generation = uuid.uuid4().hex
    return {
        "id": "r-" + generation[:24], "generation": generation,
        "uid": os.getuid(), "home": str(home), "session_id": "owner-one",
        "runtime_dir": f"/run/user/{os.getuid()}/cr-{generation[:16]}",
        "scope": f"claude-realm-{generation}.scope",
        "guardian_unit": f"claude-realm-{generation}-guard.service",
        "status": "starting", "created_at": 0, "last_activity": 0,
    }


@pytest.mark.platforms("linux")
def test_workspace_retained_across_start_failure_and_owner_checks(tmp_path):
    life = load("lifecycle")
    workspace = load("workspace")
    registry = life.Registry(tmp_path)
    record = record_for(tmp_path)
    workspace.create(record)
    home = Path(record["workspace_dir"]) / "home"
    (home / "sentinel").write_bytes(b"retained-work")
    with registry.lock():
        registry.put(record)
        life.remove_runtime(registry, record)
    assert registry.get(record["id"])["status"] == "stopped"
    assert (home / "sentinel").read_bytes() == b"retained-work"
    # Same profile is not permission to rebind a different conversation's HOME.
    forged = dict(record, session_id="owner-two")
    with pytest.raises(life.OwnershipError, match="binding"):
        workspace.validate(forged)
    forged = dict(record, workspace_dir=str(tmp_path))
    with pytest.raises(life.OwnershipError, match="path"):
        workspace.validate(forged)
    with pytest.raises(life.OwnershipError):
        workspace.validate(dict(record, home=str(tmp_path / "foreign-profile")))
    # A missing durable directory must never become a silently empty replacement.
    home.rename(home.with_name("saved-home"))
    with pytest.raises(life.OwnershipError, match="recovery"):
        registry.get(record["id"])
    assert (home.with_name("saved-home") / "sentinel").read_bytes() == b"retained-work"
    home.with_name("saved-home").rename(home)
    downgraded = dict(record)
    downgraded.pop("workspace_dir")
    with registry.lock():
        registry.put(downgraded)
    with pytest.raises(life.OwnershipError, match="workspace"):
        registry.get(record["id"])


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("failure", ["home", "receipt", "directory", "registry"])
def test_explicit_delete_resumes_after_partial_removal(tmp_path, monkeypatch, failure):
    import shutil

    life = load("lifecycle")
    workspace = load("workspace")
    managers = load("manager")
    manager = managers.Manager(tmp_path)
    record = record_for(tmp_path)
    record["status"] = "stopped"
    root = workspace.create(record)
    (root / "home/sentinel").write_bytes(b"private-work")
    peer = dict(record_for(tmp_path), status="stopped", session_id="owner-two")
    peer_root = workspace.create(peer)
    (peer_root / "home/sentinel").write_bytes(b"peer-work")
    with manager.registry.lock():
        manager.registry.put(record)
        manager.registry.put(peer)
    monkeypatch.setattr(managers, "unit_info", lambda record, key: {"ActiveState": "inactive"})
    original_rmtree, original_unlink, original_rmdir = shutil.rmtree, Path.unlink, Path.rmdir

    def rmtree(path, *args, **kwargs):
        if failure == "home" and Path(path) == root / "home":
            raise OSError("injected deletion interruption")
        return original_rmtree(path, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        selected = root / "owner.json" if failure == "receipt" else manager.registry.path(record["id"])
        if failure in ("receipt", "registry") and path == selected:
            raise OSError("injected deletion interruption")
        return original_unlink(path, *args, **kwargs)

    def rmdir(path, *args, **kwargs):
        if failure == "directory" and path == root:
            raise OSError("injected deletion interruption")
        return original_rmdir(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(shutil, "rmtree", rmtree)
        fault.setattr(Path, "unlink", unlink)
        fault.setattr(Path, "rmdir", rmdir)
        with pytest.raises(OSError, match="injected deletion"):
            manager.delete(record["id"], session_id="owner-one")
    # No manager-local state may be needed to enumerate or finish this Delete.
    fresh = managers.Manager(tmp_path)
    rows = {row["id"]: row for row in fresh.list()}
    assert rows[record["id"]]["status"] == "deleting"
    assert rows[peer["id"]]["status"] == "stopped"
    with pytest.raises(life.OwnershipError, match="another session"):
        fresh.delete(record["id"], session_id="owner-two")
    with pytest.raises(life.RealmError, match="deleting"):
        fresh.start("owner-one")
    assert fresh.stop(record["id"]), "Stop must not reset authorized deletion to stopped"
    current = fresh.registry.get(record["id"])
    assert current["status"] == "deleting"
    load("supervisor").cleanup(tmp_path, record["id"], record["generation"])
    with fresh.registry.lock():
        life.remove_runtime(fresh.registry, current)
    assert fresh.registry.get(record["id"])["status"] == "deleting"
    assert fresh.delete(record["id"], session_id="owner-one")
    assert not root.exists()
    assert not fresh.registry.path(record["id"]).exists()
    assert (peer_root / "home/sentinel").read_bytes() == b"peer-work"
    assert [row["id"] for row in fresh.list()] == [peer["id"]]


@pytest.mark.platforms("linux")
def test_delete_authority_cannot_bless_missing_or_replaced_home(tmp_path, monkeypatch):
    life, workspace, managers = load("lifecycle"), load("workspace"), load("manager")
    manager = managers.Manager(tmp_path)
    record = dict(record_for(tmp_path), status="stopped")
    root = workspace.create(record)
    (root / "home/sentinel").write_bytes(b"do-not-delete")
    manager.registry.put(record)
    monkeypatch.setattr(managers, "unit_info", lambda record, key: {"ActiveState": "inactive"})
    (root / "home").rename(root / "saved")
    for candidate in (record, dict(record, status="deleting")):
        manager.registry.put(candidate)
        with pytest.raises(life.OwnershipError):
            managers.Manager(tmp_path).delete(record["id"])
    (root / "saved").rename(root / "home")
    manager.registry.put(record)
    original_unlink = Path.unlink

    def interrupt(path, *args, **kwargs):
        if path == root / "owner.json":
            raise OSError("receipt interruption")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", interrupt)
        with pytest.raises(OSError, match="receipt interruption"):
            manager.delete(record["id"])
    current = manager.registry.get(record["id"])
    # Never let a replacement HOME (including an external symlink) inherit deletion.
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "sentinel").write_bytes(b"unrelated")
    (root / "home").symlink_to(outside, target_is_directory=True)
    with pytest.raises(life.OwnershipError):
        managers.Manager(tmp_path).delete(record["id"])
    (root / "home").unlink()
    for key, value in (("session_id", "owner-two"), ("generation", uuid.uuid4().hex)):
        with pytest.raises(life.OwnershipError):
            workspace.validate(dict(current, **{key: value}))
    assert (outside / "sentinel").read_bytes() == b"unrelated"
    # An unexpected sibling survives; removing it explicitly permits resumption.
    (root / "extra").write_bytes(b"not-authorized")
    with pytest.raises(OSError):
        managers.Manager(tmp_path).delete(record["id"])
    assert manager.list()[0]["status"] == "deleting"
    assert (root / "extra").read_bytes() == b"not-authorized"
    (root / "extra").unlink()
    assert managers.Manager(tmp_path).delete(record["id"])


@pytest.mark.integration
@pytest.mark.platforms("linux")
def test_legacy_only_copy_is_retained_without_live_migration(tmp_path):
    import shutil

    life = load("lifecycle")
    registry = life.Registry(tmp_path)
    record = record_for(tmp_path)
    runtime = Path(record["runtime_dir"])
    runtime.mkdir(mode=0o700)
    home = runtime / "home"
    home.mkdir(mode=0o700)
    (home / "sentinel").write_bytes(b"legacy-only-copy")
    try:
        with registry.lock():
            registry.put(record)
            life.remove_runtime(registry, record)
        assert (home / "sentinel").is_file(), "cleanup discarded legacy work"
        assert (home / "sentinel").read_bytes() == b"legacy-only-copy"
        retained = registry.get(record["id"])
        assert retained["status"] == "stopped"
        assert retained["legacy_workspace_dir"] == str(home)
        with pytest.raises(life.RealmError, match="legacy.*recover"):
            load("manager").Manager(tmp_path).start("owner-one")
        # Existing generation/ownership validation still precedes any cleanup.
        with pytest.raises(life.OwnershipError, match="generation"):
            life.remove_runtime(registry, dict(record, generation=uuid.uuid4().hex))
        assert (home / "sentinel").read_bytes() == b"legacy-only-copy"
        # Legacy allocations which never acquired a HOME remain removable.
        shutil.rmtree(home)
        with registry.lock():
            registry.put(record)
            life.remove_runtime(registry, record)
        assert not registry.path(record["id"]).exists()
        assert not runtime.exists()
    finally:
        # No processes were allocated; delete this synthetic fixture only.
        shutil.rmtree(runtime, ignore_errors=True)
