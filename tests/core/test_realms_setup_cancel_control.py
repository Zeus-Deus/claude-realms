"""Invalid setup control cannot stall or exhaust its lifetime supervisor."""
import ctypes
import errno
import json
import os
from pathlib import Path
import runpy
import select
import signal
import subprocess
import sys
import time

import pytest
from realms_test_paths import PLUGIN_ROOT

PROCESS = PLUGIN_ROOT / "realms_core/setup_process.py"
pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize("kind", ["running", "cancelling", "fifo", "directory", "symlink", "missing", "malformed", "oversized", "nonmapping", "unknown"])
def test_cancel_control_is_bounded_and_closes_rejected_descriptors(tmp_path, kind):
    path = tmp_path / "control"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text('{"state":"running"}')
        path.symlink_to(target)
    elif kind != "missing":
        contents = {
            "running": '{"state":"running"}',
            "cancelling": '{"state":"cancelling"}',
            "malformed": '{"state":',
            "oversized": json.dumps({"state": "running", "padding": "x" * 65536}),
            "nonmapping": '[]',
            "unknown": '{"state":"unrecognized"}',
        }
        path.write_text(contents[kind])
    code = (
        "import json,os,runpy,sys; m=runpy.run_path(sys.argv[1]); "
        "before=len(os.listdir('/proc/self/fd')); "
        "values=[m['cancellation_requested'](sys.argv[2]) for _ in range(3)]; "
        "print(json.dumps({'values':values,'growth':len(os.listdir('/proc/self/fd'))-before}))"
    )
    with subprocess.Popen([sys.executable, "-I", "-c", code, str(PROCESS), str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        try:
            stdout, stderr = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            pytest.fail("Cancellation control blocked the supervisor's reader")
    assert child.returncode == 0, stderr
    receipt = json.loads(stdout)
    assert receipt["values"] == [kind != "running"] * 3
    assert receipt["growth"] == 0


def test_invalid_control_retires_running_owned_work(tmp_path):
    control = tmp_path / "control"
    control.write_text('{"state":"running"}')
    witness = tmp_path / "child-pid"
    lifetime = runpy.run_path(str(PROCESS))
    code = (
        "import os,runpy,sys; m=runpy.run_path(sys.argv[1]); "
        "fd=m['open_pidfd'](os.getpid()); "
        "argv=[sys.executable,'-I','-c',"
        "'import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)',sys.argv[3]]; "
        "rc=m['supervise'](argv,{'PATH':'/usr/bin:/bin'},30,fd,cancel_path=sys.argv[2]); "
        "os.close(fd); sys.exit(rc)"
    )
    owned_fd = None
    with subprocess.Popen([sys.executable, "-I", "-c", code, str(PROCESS), str(control), str(witness)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as supervisor:
        try:
            deadline = time.monotonic() + 5
            while not witness.exists() or not witness.read_text():
                assert supervisor.poll() is None, "Supervisor exited before launching its child"
                assert time.monotonic() < deadline, "Owned child did not report readiness"
                time.sleep(.02)
            owned_fd = lifetime['open_pidfd'](int(witness.read_text()))
            watcher = select.poll()
            watcher.register(owned_fd, select.POLLIN)
            assert not watcher.poll(0)
            replacement = tmp_path / "invalid-control"
            os.mkfifo(replacement)
            replacement.replace(control)
            try:
                _, stderr = supervisor.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pytest.fail("Invalid control stopped supervision instead of retiring owned work")
            assert supervisor.returncode == 125, stderr
            assert watcher.poll(2000), "Supervisor exited with its owned child still alive"
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.communicate()
            if owned_fd is not None:
                try:
                    # Some standalone Python builds omit the signal wrapper.
                    libc = ctypes.CDLL(None, use_errno=True)
                    if libc.pidfd_send_signal(owned_fd, signal.SIGKILL, None, 0) < 0:
                        error = ctypes.get_errno()
                        if error != errno.ESRCH:
                            raise OSError(error, os.strerror(error))
                finally:
                    os.close(owned_fd)
