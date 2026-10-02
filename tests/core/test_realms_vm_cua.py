"""Inert transport contracts; these tests do not qualify native guest input."""
import json
from pathlib import Path
import runpy

import pytest
from realms_test_paths import PLUGIN_ROOT

load = runpy.run_path(str(PLUGIN_ROOT / 'realms_core/_binding.py'))['load_runtime']
pytestmark = pytest.mark.platforms("linux")


def test_vm_proxy_only_maps_owned_socket_and_exact_approved_manifest(tmp_path):
    cua = load('vm_cua')
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    key = runtime / 'hc-0123456789ab.sock'
    policy = runtime / 'hc-manifest-test.json'
    contents = b'{ "version": 3, "note": "exact bytes" }\n'
    policy.write_bytes(contents)
    policy.chmod(0o600)
    args = ['serve', '--embedded', '--socket', str(key), '--no-permissions-gate',
            '--permission-mode', 'bounded', '--capability-manifest', str(policy),
            '--approve-capability-manifest', '--no-overlay']
    request = cua.parse_invocation(args, runtime)
    assert request['socket'] == key.name
    assert request['manifest'] == contents
    assert request['args'] == ['serve', '--embedded', '--socket', '@SOCKET@',
                              '--no-permissions-gate', '--permission-mode', 'bounded',
                              '--capability-manifest', '@MANIFEST@',
                              '--approve-capability-manifest', '--no-overlay']
    for bad in ([ 'mcp' ], ['mcp', '--socket', str(key)],
                ['status', '--socket', str(tmp_path / key.name)],
                ['call', 'get_desktop_state', json.dumps({'screenshot_out_file': '/host/sentinel'}), '--socket', str(key)]):
        with pytest.raises(ValueError):
            cua.parse_invocation(bad, runtime)
    policy.unlink()
    policy.symlink_to(tmp_path / 'foreign.json')
    with pytest.raises((ValueError, OSError)):
        cua.parse_invocation(args, runtime)


def test_guest_supervisor_reaps_ack_only_driver_before_retirement_receipt(tmp_path):
    import hashlib
    import os
    import subprocess
    import sys
    import time

    guest = load('vm_cua_guest')
    lifetime = load('vm_driver_lifetime')
    assets = tmp_path / 'assets'
    assets.mkdir(mode=0o700)
    driver = assets / 'cua-driver'
    driver.write_text('#!' + sys.executable + '''
import json, os, socket, sys, time
from pathlib import Path
args = sys.argv[1:]
p = Path(args[args.index('--socket')+1])
if args[0] == 'stop':
    print('ack')
    sys.exit(0)
s = socket.socket(socket.AF_UNIX)
s.bind(str(p))
s.listen()
Path('started').write_text(str(os.getpid()))
while True: time.sleep(.05)
''')
    driver.chmod(0o700)
    # Short owned AF_UNIX fixture path: not a Wayland endpoint.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='hvc-', dir='/tmp') as short:
        runtime = Path(short) / 'driver'
        runtime.mkdir(mode=0o700)
        script = tmp_path / 'supervise.py'
        script.write_text('import runpy,sys\n'
            + f"g=runpy.run_path({guest.__file__!r})\n"
            + f"s=runpy.run_path({lifetime.__file__!r})['GuestDriverSupervisor']\n"
            + f"g['supervise']({str(driver)!r}, {str(runtime)!r}, ['serve','--embedded','--socket',{str(runtime/'cua.sock')!r}], "
              "{'PATH':'/usr/bin:/bin','HOME':sys.argv[1]}, supervisor_class=s, "
              "timeouts={'exit_timeout':.15,'terminate_timeout':.2,'kill_timeout':.2})\n")
        p = subprocess.Popen([sys.executable, str(script), str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (runtime / 'started').exists() and p.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert (runtime / 'started').exists()
            pid = int((runtime / 'started').read_text())
            p.stdin.write(b'HEARTBEAT\nSTOP\n')
            p.stdin.flush()
            output, error = p.communicate(timeout=5)
            assert p.returncode == 0, error.decode()
            receipt = json.loads(output)
            assert receipt['driver_pid'] == pid
            assert receipt['driver_returncode'] is not None
            assert receipt['cleaned'] is True
            assert not runtime.exists()
            assert not Path(f'/proc/{pid}').exists()
        finally:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=5)


@pytest.mark.parametrize('permission_mode', ['standard', 'bounded', 'unrestricted'])
def test_host_proxy_stages_verified_guest_and_owns_control_without_core_stdin(tmp_path, monkeypatch, capfd, permission_mode):
    import hashlib
    import multiprocessing
    import os
    import subprocess
    import sys
    import tempfile
    import time
    from types import SimpleNamespace

    cua = load('vm_cua')
    guest = load('vm_cua_guest')
    with tempfile.TemporaryDirectory(prefix='hvc-', dir='/tmp') as short:
        root = Path(short)
        guest_root = root / 'guest'
        guest_root.mkdir(mode=0o700)
        runtime = root / 'host'
        runtime.mkdir(mode=0o700)
        pin = runtime / 'ssh_known_hosts'
        pin.write_text('inert-enrolled-key\n')
        pin.chmod(0o600)
        driver = root / 'driver'
        driver.write_text('#!' + sys.executable + '''
import json,os,socket,sys,time
from pathlib import Path
args=sys.argv[1:]
if args==['manifest']:
 print(json.dumps({'fixture':True, 'mcp_invocation':{'args':['mcp']}})); sys.exit(0)
p=Path(args[args.index('--socket')+1])
if args[0]=='serve':
 Path('observed.json').write_text(json.dumps({'args':args,'env':dict(os.environ)}))
 s=socket.socket(socket.AF_UNIX); s.bind(str(p)); s.listen()
 while True:
  c,_=s.accept(); c.close()
if args[0]=='status':
 s=socket.socket(socket.AF_UNIX); s.connect(str(p)); print('ready')
elif args[0]=='call':
 print(json.dumps({'screenshot_png_b64':'inline','screenshot_file_path':'/host/canary'}))
elif args[0]=='stop': print('ack only')
elif args[0]=='mcp':
 for line in sys.stdin:
  msg=json.loads(line)
  print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'structuredContent':{'screenshot_png_b64':'inline-mcp','screenshot_file_path':'/host/canary'}}}),flush=True)
''')
        driver.chmod(0o700)
        # The digest comes from the driver release's own install receipt.
        receipt = driver.with_name('receipt.json')
        receipt.write_text(json.dumps({'binary_sha256': hashlib.sha256(driver.read_bytes()).hexdigest()}))
        receipt.chmod(0o600)
        identity = {'uid': os.getuid(), 'home': str(guest_root), 'runtime': str(guest_root),
                    'wayland': 'inert-not-a-socket', 'device': 1, 'inode': 2,
                    'peer_pid': 3, 'peer_start': '4', 'boot_id': 'fixture'}
        ssh = root / 'ssh-fixture'
        ssh.write_text('#!' + sys.executable + '\nimport json,runpy,sys\n'
            + f"g=runpy.run_path({guest.__file__!r})\n"
            + f"g['main'].__globals__['desktop_identity']=lambda: {identity!r}\n"
            + "import shlex\na=shlex.split(sys.argv[-1]); a=a[a.index('-c')+2:]\n"
            + "raise SystemExit(g['main'](a))\n")
        ssh.chmod(0o700)
        record = {'id': 'v-fixture', 'generation': 'a'*32, 'home': str(tmp_path),
                  'runtime_dir': str(runtime), 'owner': 'owner', 'invocation_id': 'b'*32,
                  'ssh_port': 23456, 'uid': os.getuid()}
        vm = SimpleNamespace(home=tmp_path, validate=lambda _: dict(record),
            guest_user=lambda _: 'fixture', ssh_argv=lambda record, **kw: [str(ssh)])
        launcher = cua.create_vm_driver_launcher(vm, record['id'], str(driver))
        assert Path(launcher).is_absolute() and os.access(launcher, os.X_OK)
        spec = json.loads((runtime / 'cua-proxy.json').read_text())
        proxy = cua.VmDriverProxy(vm, spec)
        assert proxy.run(['manifest']) == 0
        assert json.loads(capfd.readouterr().out)['fixture'] is True
        attestor = cua.vm_desktop_attestor(vm, record['id'])
        assert attestor() == attestor()
        assert isinstance(attestor(), tuple)
        record['session_id'] = 'foreign-owner'
        with pytest.raises(ValueError, match='owner'):
            attestor()
        record.pop('session_id')
        key = runtime / 'hc-0123456789ab.sock'
        serve_args = ['serve','--embedded','--socket',str(key),'--no-permissions-gate','--permission-mode',permission_mode]
        policy_bytes = b'{ "version": 3, "fixture": "reviewed exact bytes" }\n'
        if permission_mode == 'bounded':
            policy = runtime / 'hc-manifest-fixture.json'
            policy.write_bytes(policy_bytes)
            policy.chmod(0o600)
            serve_args += ['--capability-manifest',str(policy),'--approve-capability-manifest']
        if permission_mode == 'unrestricted':
            serve_args += ['--dangerously-bypass-approvals']
        # Forked host main owns a real SSH subprocess/control writer. Its input
        # is /dev/null, exactly as _EmbeddedCuaDaemon launches the real adapter.
        def serve():
            with open(os.devnull, 'rb') as stream:
                os.dup2(stream.fileno(), 0)
            raise SystemExit(proxy.run(serve_args))
        child = multiprocessing.get_context('fork').Process(target=serve)
        child.start()
        try:
            deadline = time.monotonic() + 8
            ready = False
            while child.is_alive() and time.monotonic() < deadline:
                try:
                    ready = proxy.run(['status','--socket',str(key)]) == 0
                except (ValueError, OSError, subprocess.SubprocessError):
                    pass
                if ready: break
                time.sleep(.05)
            assert ready
            observed_path = next(guest_root.glob('*/d-*/observed.json'))
            observed = json.loads(observed_path.read_text())
            assert observed['args'][observed['args'].index('--permission-mode') + 1] == permission_mode
            assert ('--dangerously-bypass-approvals' in observed['args']) == (permission_mode == 'unrestricted')
            assert observed['env']['CUA_DRIVER_RS_TELEMETRY_ENABLED'] == '0'
            assert observed['env']['CUA_DRIVER_RS_ENABLE_WAYLAND'] == '1'
            assert not {'DISPLAY','SSH_AUTH_SOCK','SESSION_MANAGER'} & observed['env'].keys()
            if permission_mode == 'bounded':
                assert observed_path.with_name('manifest.json').read_bytes() == policy_bytes
            assert proxy.run(['call','get_desktop_state','{}','--socket',str(key)]) == 0
            output = capfd.readouterr().out
            assert 'screenshot_file_path' not in output
            assert 'inline' in output
            read_fd, write_fd = os.pipe()
            def mcp():
                os.close(write_fd)
                sys.stdin = os.fdopen(read_fd, 'r', encoding='utf-8')
                raise SystemExit(proxy.run(['mcp','--no-overlay','--embedded','--socket',str(key)]))
            mcp_child = multiprocessing.get_context('fork').Process(target=mcp)
            mcp_child.start()
            os.close(read_fd)
            try:
                os.write(write_fd, b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"get_desktop_state","arguments":{}}}\n')
                os.close(write_fd)
                mcp_child.join(timeout=6)
                assert mcp_child.exitcode == 0
                frame = json.loads(capfd.readouterr().out)
                assert frame['id'] == 7
                assert frame['result']['structuredContent'] == {'screenshot_png_b64':'inline-mcp'}
            finally:
                if mcp_child.is_alive(): mcp_child.kill()
                mcp_child.join(timeout=3)
            assert proxy.run(['stop','--socket',str(key)]) == 0
            child.join(timeout=5)
            assert child.exitcode == 0
            assert not list(guest_root.glob('*/d-*'))
            record['generation'] = 'c'*32
            with pytest.raises((ValueError, RuntimeError)):
                attestor()
            with pytest.raises((ValueError, RuntimeError)):
                proxy.run(['status','--socket',str(key)])
        finally:
            if child.is_alive(): child.terminate()
            child.join(timeout=8)
            if child.is_alive(): child.kill(); child.join()


def test_guest_driver_socket_inode_cannot_be_rebound_by_same_peer(tmp_path):
    import os
    import socket
    import tempfile
    guest = load('vm_cua_guest')
    with tempfile.TemporaryDirectory(prefix='hvc-', dir='/tmp') as short:
        runtime = Path(short)
        live = runtime / 'live.json'
        live.write_text(json.dumps({'pid': os.getpid(), 'start': guest.process_start(os.getpid())}))
        live.chmod(0o600)
        path = runtime / 'cua.sock'
        with socket.socket(socket.AF_UNIX) as first, socket.socket(socket.AF_UNIX) as second:
            first.bind(str(path))
            first.listen(8)
            initial = guest.driver_socket_identity(runtime)
            assert guest.driver_socket_identity(runtime) == initial
            path.unlink()
            second.bind(str(path))
            second.listen(8)
            with pytest.raises(ValueError, match='replaced'):
                guest.driver_socket_identity(runtime)


def test_mcp_peer_exit_leaves_no_blocked_host_stdin_reader(monkeypatch):
    import os
    import sys
    import threading
    cua = load('vm_cua')
    proxy = object.__new__(cua.VmDriverProxy)
    proxy.check = lambda: None
    read_fd, write_fd = os.pipe()
    before = set(threading.enumerate())
    stream = os.fdopen(read_fd, 'r', encoding='utf-8')
    monkeypatch.setattr(sys, 'stdin', stream)
    try:
        assert proxy._mcp([sys.executable, '-c', 'raise SystemExit(42)']) == 42
        assert not (set(threading.enumerate()) - before), 'MCP left a blocked stdin reader'
    finally:
        os.close(write_fd)
        for thread in set(threading.enumerate()) - before:
            thread.join(timeout=3)
        stream.close()


def test_guest_attestation_cannot_consume_parent_mcp_stdin(tmp_path):
    import subprocess
    import sys
    cua = load('vm_cua')
    binding = str(Path(cua.__file__).with_name('_binding.py'))
    script = tmp_path / 'attest-stdin.py'
    script.write_text('import runpy,sys\n'
        + f"c=runpy.run_path({binding!r})['load_runtime']('vm_cua')\n"
        + "c._guest_command=lambda *a: [sys.executable,'-c','import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())']\n"
        + "sys.stdout.buffer.write(c._remote(None,{},'fixture','attest'))\n")
    result = subprocess.run([sys.executable, str(script)], input=b'parent-private-mcp-frame\n',
                            capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b'', 'attestation inherited and consumed parent MCP input'
