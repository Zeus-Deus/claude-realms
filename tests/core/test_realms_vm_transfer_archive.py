"""Archive transfer contracts use real files without SSH or guest dependencies."""
import io
from pathlib import Path
import runpy
import tarfile

import pytest
from realms_test_paths import PLUGIN_ROOT

MODULE = PLUGIN_ROOT / 'realms_core/vm_transfer_archive.py'
pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize('name', ['a[1].txt','*','?','back\\slash.txt'," project's 日本語 [1]*?\\.txt "])
def test_literal_files_and_repeat_directory_merge(tmp_path, name):
    api = runpy.run_path(str(MODULE))
    source = tmp_path/'source'
    source.mkdir()
    (source/name).write_bytes(b'first')
    (source/'internal-link').symlink_to(name)
    destination = tmp_path/'destination'
    for payload in (b'first', b'first'):
        (source/name).write_bytes(payload)
        stream = io.BytesIO()
        api['pack'](source,stream)
        stream.seek(0)
        assert api['unpack'](stream,destination,directory_exact=True) == str(destination)
        assert (destination/name).read_bytes() == payload
        assert (destination/'internal-link').is_symlink()
        assert not (destination/'source').exists()
    incoming = io.BytesIO()
    api['pack'](source/name,incoming)
    incoming.seek(0)
    target = tmp_path/'files'
    target.mkdir()
    (target/'control').write_bytes(b'untouched')
    assert api['unpack'](incoming,target) == str(target/name)
    assert (target/name).read_bytes() == b'first'
    assert (target/'control').read_bytes() == b'untouched'


@pytest.mark.parametrize('attack', ['source_link','source_root_link','traversal','destination_link','device'])
def test_transfer_refuses_escape_without_changing_outside_data(tmp_path, attack):
    api = runpy.run_path(str(MODULE))
    outside = tmp_path/'outside'
    outside.mkdir()
    sentinel = outside/'sentinel'
    sentinel.write_bytes(b'do not export or overwrite')
    source = tmp_path/'source'
    source.mkdir()
    (source/'sentinel').write_bytes(b'new contents')
    destination = tmp_path/'destination'
    stream = io.BytesIO()
    if attack in ('source_link','source_root_link'):
        if attack == 'source_root_link':
            source = tmp_path/'root-link'
            source.symlink_to(sentinel)
        else:
            (source/'external').symlink_to(sentinel)
        with pytest.raises(ValueError,match='link'):
            api['pack'](source,stream)
    else:
        if attack in ('traversal','device'):
            with tarfile.open(fileobj=stream,mode='w') as archive:
                root = tarfile.TarInfo('source'); root.type = tarfile.DIRTYPE
                archive.addfile(root)
                entry = tarfile.TarInfo('source/../../outside/sentinel' if attack=='traversal' else 'source/device')
                if attack=='device':
                    entry.type=tarfile.CHRTYPE
                archive.addfile(entry)
        else:
            api['pack'](source,stream)
            destination.mkdir()
            (destination/'sentinel').symlink_to(sentinel)
        stream.seek(0)
        with pytest.raises(ValueError):
            api['unpack'](stream,destination,directory_exact=True)
    assert sentinel.read_bytes() == b'do not export or overwrite'
