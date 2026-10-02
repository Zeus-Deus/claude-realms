"""Real subprocess streaming: bounded diagnostics and owned-child cleanup."""
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest
from realms_test_paths import PLUGIN_ROOT

load = runpy.run_path(str(PLUGIN_ROOT / 'realms_core/_binding.py'))['load_runtime']
pytestmark = pytest.mark.platforms("linux")


def test_stream_drains_diagnostics_without_inheriting_ambient_secret(monkeypatch):
    transport = load('vm_transfer')
    monkeypatch.setenv('TRANSFER_HOST_SECRET','fixture-only-canary')
    result = transport.run_stream([sys.executable,'-I','-S','-c',
        "import os; assert 'TRANSFER_HOST_SECRET' not in os.environ; os.write(2,b'x'*1000000); os.write(1,b'exact response')"],timeout=5)
    assert result == b'exact response'


@pytest.mark.parametrize('failure', ['overflow','timeout','exit'])
def test_stream_failure_reaps_its_owned_child(monkeypatch, failure):
    transport = load('vm_transfer')
    processes = []
    popen = subprocess.Popen
    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(transport.subprocess,'Popen',start)
    bodies = {
        'overflow': "import os,time; os.write(1,b'x'*100000); time.sleep(60)",
        'timeout': "import time; time.sleep(60)",
        'exit': "import sys; sys.stderr.write('source not found'); sys.exit(7)",
    }
    errors = {'overflow':ValueError,'timeout':subprocess.TimeoutExpired,'exit':subprocess.CalledProcessError}
    with pytest.raises(errors[failure]):
        transport.run_stream([sys.executable,'-I','-S','-c',bodies[failure]],timeout=2)
    assert len(processes) == 1 and processes[0].returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid,os.WNOHANG)
