"""Push/pull through real isolated Python byte streams, not a native VM/SSH proof."""
import hashlib
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest
from realms_test_paths import PLUGIN_ROOT

load = runpy.run_path(str(PLUGIN_ROOT / 'realms_core/_binding.py'))['load_runtime']
pytestmark = pytest.mark.platforms("linux")


class LocalTransport:
    """Replace only SSH admission with a credential-free local command receiver."""
    def __init__(self, root):
        self.home = root / 'profile'
        self.guest = root / 'guest'
        self.guest.mkdir()
        self.validations = []

    def validate(self, vm_id):
        assert vm_id == 'fixture'
        self.validations.append(vm_id)
        return {'session_id': vm_id}

    def guest_user(self, vm_id):
        assert vm_id == 'fixture'
        return 'fixture-user'

    def guest_home(self, vm_id):
        assert vm_id == 'fixture'
        return str(self.guest)

    def guest_run(self, vm_id, argv, *, user):
        assert vm_id == 'fixture' and user == 'fixture-user'
        result = subprocess.run(argv, capture_output=True, check=False)
        return {'returncode': result.returncode}

    def ssh_argv(self, record, *, user):
        assert record['session_id'] == 'fixture' and user in ('root', 'fixture-user')
        return [sys.executable, '-I', '-S', '-c',
                "import os,sys; os.execv('/bin/sh',['sh','-c',sys.argv[-1]])"]


@pytest.mark.parametrize('direction', ['push', 'pull'])
@pytest.mark.parametrize('directory', [False, True])
def test_selected_copy_binary_idempotence_additions_and_conflict(tmp_path, direction, directory):
    transport = load('vm_transfer')
    manager = LocalTransport(tmp_path)
    source_parent = tmp_path / 'selected'
    source_parent.mkdir()
    name = " project's 日本語 [1]*?\\.bin "
    source = source_parent / name
    payload = bytes(range(256)) * 513 + b'\x00\xff\r\n'
    if directory:
        source.mkdir(mode=0o700)
        selected = source / name
        (source / 'relative-link').symlink_to(name)
    else:
        selected = source
    selected.write_bytes(payload)
    destination = tmp_path / 'explicit copy [*] 日本語'
    argument = destination
    if directory and direction == 'pull':
        argument.mkdir()
        destination = argument / source.name
    operation = getattr(transport, direction)

    def copy():
        return operation(manager, 'fixture', str(source), str(argument))

    assert copy()['destination'] == str(destination)
    target = destination / name if directory else destination
    target.chmod(0o600)
    identity = target.stat().st_ino
    assert copy()['destination'] == str(destination)
    assert target.stat().st_ino == identity
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.stat().st_size == selected.stat().st_size == len(payload)
    assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(payload).digest()
    if directory:
        assert os.readlink(destination / 'relative-link') == name
        (source / 'addition').write_bytes(b'explicit additional copy')
        (destination / 'unrelated').write_bytes(b'keep local work')
        copy()
        assert (destination / 'addition').read_bytes() == b'explicit additional copy'
        assert (destination / 'unrelated').read_bytes() == b'keep local work'
        (source / 'must-not-merge').write_bytes(b'unpublished')
    selected.write_bytes(b'conflicting new source\x00\xff')
    with pytest.raises(ValueError, match='conflict') as caught:
        copy()
    diagnostic = str(caught.value)
    assert len(diagnostic) < 1000
    assert 'execv' not in diagnostic and 'Traceback' not in diagnostic
    assert target.read_bytes() == payload
    assert target.stat().st_ino == identity
    assert selected.read_bytes() == b'conflicting new source\x00\xff'
    if directory:
        assert not (destination / 'must-not-merge').exists()
    assert manager.validations
    assert not list(tmp_path.rglob('.realms-transfer-*'))
    assert list((manager.home / 'cache' / 'realms-transfers').iterdir()) == []


@pytest.mark.parametrize('direction', ['push', 'pull'])
def test_missing_literal_source_never_selects_glob_decoy(tmp_path, direction):
    transport = load('vm_transfer')
    manager = LocalTransport(tmp_path)
    (tmp_path / 'file1').write_bytes(b'decoy must not transfer')
    source = tmp_path / 'file[1]'
    destination = tmp_path / 'output'
    with pytest.raises((FileNotFoundError, subprocess.CalledProcessError)):
        getattr(transport, direction)(manager, 'fixture', str(source), str(destination))
    assert not destination.exists()
    assert (tmp_path / 'file1').read_bytes() == b'decoy must not transfer'
    assert list((manager.home / 'cache' / 'realms-transfers').iterdir()) == []
