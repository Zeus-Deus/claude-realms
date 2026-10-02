"""The direct process backend owns a whole tree, daemons included, without systemd."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time

import pytest

from realms_core import units


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met")


ROOT = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, {src!r})
    from realms_core.units import become_subreaper
    become_subreaper()
    child = os.fork()
    if child == 0:
        # A daemon: double fork and setsid, so its parent is gone.
        if os.fork() == 0:
            os.setsid()
            open({marker!r}, "w").write(str(os.getpid()))
            time.sleep(600)
        os._exit(0)
    os.waitpid(child, 0)
    time.sleep(600)
""")


@pytest.fixture
def tree(tmp_path):
    marker = tmp_path / "daemon.pid"
    src = str(Path(units.__file__).resolve().parents[1])
    process = subprocess.Popen([sys.executable, "-c", ROOT.format(src=src, marker=str(marker))],
                               start_new_session=True)
    daemon = int(wait_for(lambda: marker.exists() and marker.read_text()))
    yield process, daemon
    for pid in (daemon, process.pid):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def record(home, unit="claude-realm-x.scope"):
    return {"backend": "direct", "home": str(home), "scope": unit, "guardian_unit": "claude-realm-x-guard.service"}


def test_select_honours_explicit_direct(monkeypatch):
    monkeypatch.setenv("REALMS_PROCESS_BACKEND", "direct")
    assert units.select() == "direct"
    with pytest.raises(units.UnitError):
        units.select("bogus")


def test_reparented_daemon_stays_a_member_and_stop_kills_it(tmp_path, tree):
    process, daemon = tree
    rec = record(tmp_path)
    root = units.identity(process.pid)
    units.publish_direct(tmp_path, rec["scope"], root, "Claude realm x")
    info = units.info(rec, "scope")
    assert info["ActiveState"] == "active"
    assert info["ControlGroup"] == "direct:%d:%d" % (root["pid"], root["start_time"])
    # The daemon's own parent exited; the subreaper root adopted it.
    assert units.member(rec, daemon)
    assert not units.member(rec, os.getpid())
    units.stop(rec)
    wait_for(lambda: units.identity(daemon) is None and units.identity(process.pid) is None)
    assert units.info(rec, "scope")["ActiveState"] == "inactive"
    assert units.info(rec, "scope")["LoadState"] == "not-found"


def test_receipt_of_a_dead_root_reads_inactive(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    identity = units.identity(process.pid)
    rec = record(tmp_path)
    units.publish_direct(tmp_path, rec["scope"], identity, "Claude realm x")
    process.wait()
    wait_for(lambda: units.identity(process.pid) is None)
    assert units.info(rec, "scope")["ActiveState"] == "inactive"
    assert units.pids(rec) == []


def test_unit_names_are_validated(tmp_path):
    with pytest.raises(units.UnitError):
        units.publish_direct(tmp_path, "../escape.scope", units.identity(os.getpid()), "x")


def test_commands_wrap_only_for_systemd():
    systemd = {"backend": "systemd", "scope": "s.scope", "guardian_unit": "g.service"}
    direct = dict(systemd, backend="direct")
    argv = ["python", "-m", "worker"]
    assert units.scope_command(direct, argv, description="d", inhibit_who="w", inhibit_why="y") == argv
    wrapped = units.scope_command(systemd, argv, description="d", inhibit_who="w", inhibit_why="y")
    assert wrapped[:3] == ["systemd-run", "--user", "--scope"] and wrapped[-3:] == argv
    env = {"PYTHONPATH": "/x", "PATH": "/usr/bin"}
    assert units.guardian_command(direct, argv, env=env, cleanup_argv=["c"]) == argv
    assert units.guardian_command(systemd, argv, env=env, cleanup_argv=["c"])[0] == "systemd-run"
