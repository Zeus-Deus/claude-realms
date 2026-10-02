"""Only this attempt's still-empty enrollment file may be removed on failure."""
from pathlib import Path
import runpy
import subprocess

import pytest
from realms_test_paths import PLUGIN_ROOT

load = runpy.run_path(str(PLUGIN_ROOT / 'realms_core/_binding.py'))['load_runtime']
pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize('failure_state', ['empty', 'written', 'replacement'])
def test_enrollment_retry_preserves_established_or_replaced_pins(tmp_path, monkeypatch, failure_state):
    vm, ssh = load('vm_manager'), load('vm_ssh')
    record = {'runtime_dir':str(tmp_path),'unit':'fixture.service','invocation_id':'owned','ssh_port':2222}
    pin = tmp_path/'ssh_known_hosts'
    monkeypatch.setattr(vm,'validate_vm_record',lambda _:None)
    monkeypatch.setattr(ssh,'scope_info',lambda _: {'ActiveState':'active','InvocationID':'owned'})
    calls = []
    def connect(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            if failure_state == 'written':
                pin.write_text('established public pin\n')
            elif failure_state == 'replacement':
                pin.rename(tmp_path/'original')
                pin.touch(mode=0o600)
            raise subprocess.CalledProcessError(255,argv)
        expected = 'yes' if failure_state == 'written' else 'accept-new'
        assert 'StrictHostKeyChecking='+expected in argv
        if not pin.read_bytes():
            pin.write_text('established public pin\n')
        return subprocess.CompletedProcess(argv,0)
    monkeypatch.setattr(subprocess,'run',connect)
    with pytest.raises(vm.OwnershipError,match='enroll'):
        ssh.enroll_host_key(record)
    if failure_state == 'replacement':
        assert pin.exists() and pin.read_bytes() == b''
        return
    assert pin.exists() == (failure_state == 'written')
    ssh.enroll_host_key(record)
    assert pin.read_text() == 'established public pin\n'
    assert pin.stat().st_mode & 0o777 == 0o600
