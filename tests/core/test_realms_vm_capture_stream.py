"""Shell-protocol fixtures, not desktop pixels; real VM proof is separate."""
import base64
from pathlib import Path
import runpy
import subprocess
import sys

import pytest
from realms_test_paths import PLUGIN_ROOT

load = runpy.run_path(str(PLUGIN_ROOT / 'realms_core/_binding.py'))['load_runtime']
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=')
pytestmark = pytest.mark.platforms("linux")


def executable(path, source):
    path.write_text('#!' + sys.executable + '\n' + source)
    path.chmod(0o700)
    return path


def test_capture_streams_from_desktop_user_without_guest_temporary_file(tmp_path, monkeypatch):
    vm = load('vm_manager')
    manager = vm.VmManager(tmp_path/'profile')
    monkeypatch.setattr(manager, 'validate', lambda _: {})
    monkeypatch.setattr(manager, 'guest_user', lambda _: 'fixture')
    tools = tmp_path/'bin'
    tools.mkdir()
    image = tmp_path/'fixture.png'
    image.write_bytes(PNG)
    executable(tools/'grim', '''import os,sys
from pathlib import Path
if sys.argv[1:] != ['-'] or os.environ.get('CAPTURE_USER') != 'fixture':
    sys.exit('capture must stream as the desktop account')
sys.stdout.buffer.write(Path(os.environ['CAPTURE_IMAGE']).read_bytes())
''')
    executable(tools/'runuser', '''import os,sys
os.environ['CAPTURE_USER'] = sys.argv[sys.argv.index('-u')+1]
command = sys.argv[sys.argv.index('--')+1:]
os.execvpe(command[0], command, os.environ)
''')
    # The synthetic shell endpoint must not discover a host display either.
    executable(tools/'id', "print('99119')\n")
    ssh = executable(tmp_path/'ssh-fixture', '''import os,sys
env = {'PATH':sys.argv[1], 'HOME':sys.argv[2], 'CAPTURE_IMAGE':sys.argv[3]}
os.execve('/bin/bash', ['bash','--noprofile','--norc','-c',sys.argv[-1]], env)
''')
    monkeypatch.setattr(manager, 'ssh_argv', lambda _: [str(ssh),str(tools)+':/usr/bin:/bin',str(tmp_path),str(image)])
    target = tmp_path/'capture.png'
    assert manager.shot('fixture', target) == str(target)
    assert target.read_bytes() == PNG
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('failure', ['exit', 'invalid'])
def test_capture_failure_preserves_previous_host_image(tmp_path, monkeypatch, failure):
    vm = load('vm_manager')
    manager = vm.VmManager(tmp_path/'profile')
    monkeypatch.setattr(manager, 'validate', lambda _: {})
    monkeypatch.setattr(manager, 'guest_user', lambda _: 'fixture')
    child = executable(tmp_path/'failed-capture', "import sys; sys.stdout.write('incomplete'); sys.exit("+('7' if failure == 'exit' else '0')+")\n")
    monkeypatch.setattr(manager, 'ssh_argv', lambda _: [str(child)])
    target = tmp_path/'capture.png'
    target.write_bytes(PNG)
    before = set(tmp_path.iterdir())
    with pytest.raises((vm.VmError, subprocess.CalledProcessError)):
        manager.shot('fixture', target)
    assert target.read_bytes() == PNG
    assert set(tmp_path.iterdir()) == before
