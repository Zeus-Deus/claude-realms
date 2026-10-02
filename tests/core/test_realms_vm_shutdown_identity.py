"""Shutdown must use pinned SSH and refuse a substituted live invocation."""
from pathlib import Path
import runpy
import subprocess

import pytest
from realms_test_paths import PLUGIN_ROOT

load = runpy.run_path(str(PLUGIN_ROOT / 'realms_core/_binding.py'))['load_runtime']
pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize('key_failure', [False, True])
def test_shutdown_keeps_pinned_ssh_even_when_graceful_shutdown_fails(tmp_path, monkeypatch, key_failure):
    vm, ssh = load('vm_manager'), load('vm_ssh')
    manager = vm.VmManager(tmp_path/'profile')
    record = {'unit':'fixture.service','invocation_id':'owned','runtime_dir':str(tmp_path),
              'session_dir':str(tmp_path),'ssh_port':2222}
    (tmp_path/'ssh_known_hosts').write_text('fixture pin\n')
    (tmp_path/'ssh_known_hosts').chmod(0o600)
    monkeypatch.setattr(vm,'validate_vm_record',lambda _:None)
    active = [True]
    monkeypatch.setattr(ssh,'scope_info',lambda _: {'ActiveState':'active' if active[0] else 'inactive','InvocationID':'owned'})
    monkeypatch.setattr(manager,'_run_script',lambda *a,**k:pytest.fail('shutdown used the unpinned vendor route'))
    removed, commands = [], []
    monkeypatch.setattr(manager,'_remove_locked',lambda r:removed.append(r))
    run = subprocess.run
    def send(argv, **kwargs):
        assert argv[0] == 'ssh'
        # Real OpenSSH parses the produced options, but never opens a network
        # connection or executes the shutdown command in this protocol test.
        config = run(['ssh','-G',*argv[1:]],capture_output=True,text=True,check=True,timeout=10)
        settings = dict(line.split(' ',1) for line in config.stdout.splitlines() if ' ' in line)
        assert settings['stricthostkeychecking'] == 'true'
        assert settings['userknownhostsfile'] == str(tmp_path/'ssh_known_hosts')
        assert settings['globalknownhostsfile'] == '/dev/null'
        assert argv[-1] == 'poweroff'
        commands.append(argv)
        active[0] = key_failure
        return subprocess.CompletedProcess(argv,255 if key_failure else 0)
    monkeypatch.setattr(subprocess,'run',send)
    manager._stop_locked(record)
    assert commands and removed == [record]


@pytest.mark.parametrize('replacement', ['before_ssh','after_ssh'])
def test_shutdown_preserves_ownership_record_when_live_unit_changes(tmp_path, monkeypatch, replacement):
    vm, ssh = load('vm_manager'), load('vm_ssh')
    manager = vm.VmManager(tmp_path/'profile')
    record = {'unit':'fixture.service','invocation_id':'owned','runtime_dir':str(tmp_path),
              'session_dir':str(tmp_path),'ssh_port':2222}
    (tmp_path/'ssh_known_hosts').write_text('fixture pin\n')
    (tmp_path/'ssh_known_hosts').chmod(0o600)
    monkeypatch.setattr(vm,'validate_vm_record',lambda _:None)
    invocation = ['replacement' if replacement == 'before_ssh' else 'owned']
    monkeypatch.setattr(ssh,'scope_info',lambda _: {'ActiveState':'active','InvocationID':invocation[0]})
    monkeypatch.setattr(manager,'_run_script',lambda *a,**k:pytest.fail('unpinned shutdown attempted'))
    monkeypatch.setattr(manager,'_remove_locked',lambda _:pytest.fail('unowned live unit was reclaimed'))
    def send(argv, **kwargs):
        assert replacement == 'after_ssh'
        invocation[0] = 'replacement'
        return subprocess.CompletedProcess(argv,255)
    monkeypatch.setattr(subprocess,'run',send)
    with pytest.raises(vm.OwnershipError,match='invocation'):
        manager._stop_locked(record)
