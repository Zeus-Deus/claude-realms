"""Selected-copy conflict safety on real files; no VM or SSH required."""
import io
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys

import pytest
from realms_test_paths import PLUGIN_ROOT

MODULE = PLUGIN_ROOT / 'realms_core/vm_transfer_archive.py'
pytestmark = pytest.mark.platforms("linux")


def packed(api, source):
    stream = io.BytesIO()
    api['pack'](source, stream)
    stream.seek(0)
    return stream


def snapshot(root):
    result = {}
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        data = os.readlink(path) if stat.S_ISLNK(mode) else path.read_bytes() if stat.S_ISREG(mode) else None
        result[str(path.relative_to(root))] = (mode, data)
    return result


@pytest.mark.parametrize('kind', ['bytes', 'file-directory', 'directory-file', 'link'])
def test_conflict_preflight_preserves_entire_selected_copy(tmp_path, monkeypatch, kind):
    api = runpy.run_path(str(MODULE))
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'a-new').write_bytes(b'new\x00\xff')
    destination = tmp_path / 'destination'
    destination.mkdir()
    old = destination / 'z-conflict'
    incoming = source / 'z-conflict'
    if kind == 'directory-file':
        incoming.mkdir()
        (incoming / 'child').write_bytes(b'original source')
        old.write_bytes(b'original destination')
    elif kind == 'file-directory':
        incoming.write_bytes(b'original source')
        old.mkdir()
        (old / 'child').write_bytes(b'original destination')
    elif kind == 'link':
        incoming.symlink_to('a-new')
        old.symlink_to('retained')
        (destination / 'retained').write_bytes(b'original destination')
    else:
        incoming.write_bytes(b'original source')
        old.write_bytes(b'original destination')
    before_source = packed(api, source).getvalue()
    before_destination = snapshot(destination)
    publications = []
    publish = api['_publish']

    def observe(selected, fd, name):
        publications.append(name)
        return publish(selected, fd, name)

    monkeypatch.setitem(api['unpack'].__globals__, '_publish', observe)
    with pytest.raises(ValueError, match='conflict'):
        api['unpack'](packed(api, source), destination, directory_exact=True)
    assert publications == []
    assert packed(api, source).getvalue() == before_source
    assert snapshot(destination) == before_destination
    assert not (destination / 'a-new').exists()
    assert not list(tmp_path.rglob('.realms-transfer-*'))


@pytest.mark.parametrize('appearance', ['file', 'symlink', 'directory'])
def test_concurrent_destination_appearance_is_not_overwritten(tmp_path, monkeypatch, appearance):
    api = runpy.run_path(str(MODULE))
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'a-new').write_bytes(b'first addition')
    (source / 'z-race').write_bytes(b'selected bytes')
    destination = tmp_path / 'destination'
    destination.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'outside sentinel')
    original = api['_publish']
    witnessed = []

    def publish(selected, fd, name):
        if name == 'z-race':
            assert (destination / 'a-new').read_bytes() == b'first addition'
            subprocess.run([sys.executable, '-I', '-S', '-c',
                "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                "p.write_bytes(b'concurrent bytes') if sys.argv[2]=='file' else "
                "p.symlink_to(sys.argv[3]) if sys.argv[2]=='symlink' else p.mkdir()",
                str(destination / name), appearance, str(outside)], check=True)
            witnessed.append((destination / name).lstat().st_ino)
        return original(selected, fd, name)

    monkeypatch.setitem(api['unpack'].__globals__, '_publish', publish)
    with pytest.raises(ValueError, match='conflict'):
        api['unpack'](packed(api, source), destination, directory_exact=True)
    assert witnessed == [(destination / 'z-race').lstat().st_ino]
    assert not (destination / 'a-new').exists()
    assert outside.read_bytes() == b'outside sentinel'
    if appearance == 'file':
        assert (destination / 'z-race').read_bytes() == b'concurrent bytes'
    assert (source / 'z-race').read_bytes() == b'selected bytes'
    assert not list(tmp_path.rglob('.realms-transfer-*'))


@pytest.mark.parametrize('failure', ['io', 'cancel', 'permission'])
@pytest.mark.parametrize('existing', [False, True])
def test_failed_copy_removes_only_its_additions(tmp_path, monkeypatch, failure, existing):
    api = runpy.run_path(str(MODULE))
    source = tmp_path / 'source'
    (source / 'new-dir').mkdir(parents=True)
    (source / 'new-dir' / 'file').write_bytes(b'new\x00\xff')
    (source / 'z-link').symlink_to('new-dir/file')
    destination = tmp_path / 'destination'
    if existing:
        destination.mkdir()
        (destination / 'unrelated').write_bytes(b'keep')
    before = snapshot(destination) if existing else {}
    before_source = snapshot(source)
    publish = api['_publish']
    errors = {'io': OSError, 'cancel': KeyboardInterrupt, 'permission': PermissionError}

    def fail_on_link(selected, fd, name):
        if name == 'z-link':
            assert (destination / 'new-dir' / 'file').read_bytes() == b'new\x00\xff'
            if failure == 'permission':
                # Real non-root permission failure at the publication syscall.
                destination.chmod(0o500)
                try:
                    return publish(selected, fd, name)
                finally:
                    destination.chmod(0o700)
            raise errors[failure]('injected publication interruption')
        return publish(selected, fd, name)

    monkeypatch.setitem(api['unpack'].__globals__, '_publish', fail_on_link)
    with pytest.raises(errors[failure]):
        api['unpack'](packed(api, source), destination, directory_exact=True)
    assert snapshot(source) == before_source
    if existing:
        assert snapshot(destination) == before
    else:
        assert not destination.exists()
    assert not list(tmp_path.rglob('.realms-transfer-*'))


def test_directory_open_failure_does_not_leave_a_partial_tree(tmp_path, monkeypatch):
    import errno

    api = runpy.run_path(str(MODULE))
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_bytes(b'source preserved')
    destination = tmp_path / 'destination'
    stream = packed(api, source)
    original = os.open
    failures = []

    def exhausted(path, flags, *args, **kwargs):
        if path == destination.name and destination.exists() and not failures:
            failures.append(True)
            raise OSError(errno.EMFILE, 'injected descriptor exhaustion')
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', exhausted)
    with pytest.raises(OSError, match='descriptor exhaustion'):
        api['unpack'](stream, destination, directory_exact=True)
    assert failures
    assert not destination.exists()
    assert (source / 'file').read_bytes() == b'source preserved'
    assert not list(tmp_path.rglob('.realms-transfer-*'))


def test_rollback_preserves_a_concurrently_replaced_addition(tmp_path, monkeypatch):
    api = runpy.run_path(str(MODULE))
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'new').write_bytes(b'copied')
    (source / 'link').symlink_to('new')
    destination = tmp_path / 'destination'
    destination.mkdir()
    (destination / 'unrelated').write_bytes(b'existing work')
    outside = tmp_path / 'outside'
    outside.write_bytes(b'outside sentinel')
    original = api['_publish']

    def interrupt(selected, fd, name):
        if name == 'link':
            assert (destination / 'new').read_bytes() == b'copied'
            replacement = tmp_path / 'replacement'
            replacement.symlink_to(outside)
            os.replace(replacement, destination / 'new')
            raise OSError('injected failure after concurrent replacement')
        return original(selected, fd, name)

    monkeypatch.setitem(api['unpack'].__globals__, '_publish', interrupt)
    with pytest.raises(OSError, match='injected failure') as caught:
        api['unpack'](packed(api, source), destination, directory_exact=True)
    assert any('cleanup incomplete' in note for note in caught.value.__notes__)
    assert os.readlink(destination / 'new') == str(outside)
    assert (destination / 'unrelated').read_bytes() == b'existing work'
    assert outside.read_bytes() == b'outside sentinel'
    assert (source / 'new').read_bytes() == b'copied'
    assert not (destination / 'link').is_symlink()
    assert not list(tmp_path.rglob('.realms-transfer-*'))
