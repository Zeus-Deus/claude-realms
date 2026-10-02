"""Namespace semantics at source capture and destination merge boundaries."""
import io
import os
from pathlib import Path
import runpy
import stat

import pytest
from realms_test_paths import PLUGIN_ROOT

MODULE=PLUGIN_ROOT / 'realms_core/vm_transfer_archive.py'
pytestmark=pytest.mark.platforms("linux")


def packed(api, source):
    stream=io.BytesIO()
    api['pack'](source,stream)
    stream.seek(0)
    return stream


def test_root_exchange_cannot_export_the_new_symlink_target(tmp_path, monkeypatch):
    api=runpy.run_path(str(MODULE))
    source=tmp_path/'selected'; source.mkdir()
    (source/'ordinary').write_bytes(b'allowed')
    outside=tmp_path/'outside'; outside.mkdir()
    sentinel=b'fixture-only-unselected-root-content'
    (outside/'sentinel').write_bytes(sentinel)
    original=Path.is_symlink
    swapped=False
    def exchange(path):
        nonlocal swapped
        result=original(path)
        if path==source and not swapped:
            swapped=True
            source.rename(tmp_path/'original')
            source.symlink_to(outside,target_is_directory=True)
        return result
    monkeypatch.setattr(Path,'is_symlink',exchange)
    stream=io.BytesIO()
    try:
        api['pack'](source,stream)
    except (ValueError,OSError):
        pass
    assert swapped
    assert sentinel not in stream.getvalue()


@pytest.mark.parametrize('pivot,target,referent',[
    ('deep/inside','pivot/../file','deep/file'),
    ('deep/inside','pivot/../../file','file'),
    ('a/b/c','pivot/../../../file','file'),
])
def test_relative_link_preserves_literal_target_and_meaning(tmp_path,pivot,target,referent):
    api=runpy.run_path(str(MODULE))
    source=tmp_path/'source'; (source/pivot).mkdir(parents=True)
    (source/'file').write_bytes(b'wrong')
    (source/referent).write_bytes(b'expected')
    (source/'pivot').symlink_to(pivot,target_is_directory=True)
    (source/'link').symlink_to(target)
    assert (source/'link').read_bytes()==b'expected'
    destination=tmp_path/'destination'
    api['unpack'](packed(api,source),destination,directory_exact=True)
    assert os.readlink(destination/'link')==target
    assert (destination/'link').read_bytes()==b'expected'


def test_retained_link_cannot_turn_incoming_link_into_an_escape(tmp_path):
    api=runpy.run_path(str(MODULE))
    source=tmp_path/'source'; source.mkdir()
    (source/'leak').symlink_to('bridge/sentinel')
    outside=tmp_path/'outside'; outside.mkdir()
    (outside/'sentinel').write_bytes(b'untouched')
    destination=tmp_path/'destination'; destination.mkdir()
    (destination/'bridge').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match='link'):
        api['unpack'](packed(api,source),destination,directory_exact=True)
    assert not (destination/'leak').is_symlink()
    assert (outside/'sentinel').read_bytes()==b'untouched'
    # An unrelated retained link must not block an ordinary file merge.
    (source/'leak').unlink()
    (source/'ordinary').write_bytes(b'copied')
    api['unpack'](packed(api,source),destination,directory_exact=True)
    assert (destination/'ordinary').read_bytes()==b'copied'
    assert (destination/'bridge').is_symlink()


@pytest.mark.parametrize('directory_exact',[False,True])
def test_writable_destination_does_not_require_writable_parent(tmp_path,directory_exact):
    if os.geteuid()==0:
        pytest.skip('Requires a non-root account to exercise actual directory permissions')
    api=runpy.run_path(str(MODULE))
    source=tmp_path/'source'
    if directory_exact:
        source.mkdir()
        (source/'file').write_bytes(b'copied')
    else:
        source.write_bytes(b'copied')
    parent=tmp_path/'readonly-parent'; destination=parent/'destination'
    destination.mkdir(parents=True)
    (destination/'unrelated').write_bytes(b'unchanged')
    parent.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            (parent/'write-control').write_bytes(b'must fail')
        api['unpack'](packed(api,source),destination,directory_exact=directory_exact)
        assert (destination/('file' if directory_exact else source.name)).read_bytes()==b'copied'
        assert (destination/'unrelated').read_bytes()==b'unchanged'
        assert not list(destination.glob('.realms-transfer-*'))
        assert not list(parent.glob('.realms-transfer-*'))
    finally:
        parent.chmod(0o755)


def test_new_directory_keeps_private_access_without_chmod_existing_dirs(tmp_path):
    api=runpy.run_path(str(MODULE))
    source=tmp_path/'source'; source.mkdir(mode=0o700)
    (source/'file').write_bytes(b'private')
    (source/'file').chmod(0o644)
    destination=tmp_path/'destination'
    previous=os.umask(0o022)
    try:
        api['unpack'](packed(api,source),destination,directory_exact=True)
        assert stat.S_IMODE(destination.stat().st_mode)&0o077==0
        destination.chmod(0o750)
        api['unpack'](packed(api,source),destination,directory_exact=True)
        assert stat.S_IMODE(destination.stat().st_mode)==0o750
    finally:
        os.umask(previous)
