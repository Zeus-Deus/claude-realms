"""Real cross-filesystem publication in private unprivileged mount namespaces."""
from pathlib import Path
import subprocess
import sys

import pytest
from realms_test_paths import PLUGIN_ROOT

MODULE = PLUGIN_ROOT / 'realms_core/vm_transfer_archive.py'


@pytest.mark.platforms("linux")
@pytest.mark.integration
@pytest.mark.parametrize('placement', ['root', 'nested', 'full', 'nested-full', 'nested-race'])
def test_transfer_publishes_atomically_on_destination_filesystem(tmp_path, placement):
    code = r'''
import errno,io,os
from pathlib import Path
import runpy,subprocess,sys
api=runpy.run_path(sys.argv[1])
root=Path(sys.argv[2]); placement=sys.argv[3]
nested=placement.startswith('nested')
destination=root/'destination'; destination.mkdir()
mount=destination/'sub' if nested else destination
if nested: mount.mkdir()
subprocess.run(['mount','-t','tmpfs','-o','size=16m,mode=700,nosuid,nodev','tmpfs',str(mount)],check=True)
try:
    source=root/'source'
    if not nested:
        source.write_bytes(b'new file bytes')
        selected=source
        target=destination/source.name
    else:
        (source/'sub').mkdir(parents=True)
        (source/'a-new').write_bytes(b'first addition')
        selected=source/'sub'/'file'
        selected.write_bytes(b'new file bytes')
        (source/'sub'/'link').symlink_to('file')
        target=destination/'sub'/'file'
    sentinel=mount/'retained'
    sentinel.write_bytes(b'previous contents')
    if placement.endswith('full'):
        with selected.open('wb') as stream: stream.truncate(17*1024**2)
    assert root.stat().st_dev != mount.stat().st_dev
    def packed():
        stream=io.BytesIO(); api['pack'](source,stream); stream.seek(0)
        return stream
    if placement=='nested-race':
        original=api['_link_new']
        raced=[]
        def race(source,parent_fd,name,*,source_fd=None):
            if source_fd is not None and name=='file':
                assert not raced
                target.write_bytes(b'concurrent destination')
                raced.append(target.stat().st_ino)
            return original(source,parent_fd,name,source_fd=source_fd)
        api['unpack'].__globals__['_link_new']=race
        try:
            api['unpack'](packed(),destination,directory_exact=True)
        except ValueError as error:
            assert 'conflict' in str(error)
        else:
            raise AssertionError('Cross-device publication overwrote a raced destination')
        assert raced==[target.stat().st_ino]
        assert target.read_bytes()==b'concurrent destination'
        assert not (destination/'a-new').exists()
    elif placement.endswith('full'):
        try:
            api['unpack'](packed(),destination,directory_exact=nested)
        except OSError as error:
            assert error.errno==errno.ENOSPC,error
        else:
            raise AssertionError('Constrained destination accepted an oversized file')
        assert not target.exists()
        assert not (destination/'a-new').exists()
    else:
        api['unpack'](packed(),destination,directory_exact=nested)
        assert target.read_bytes()==b'new file bytes'
        if nested:
            assert os.readlink(destination/'sub'/'link')=='file'
            assert (destination/'sub'/'link').read_bytes()==b'new file bytes'
        selected.write_bytes(b'different selected bytes')
        try:
            api['unpack'](packed(),destination,directory_exact=nested)
        except ValueError as error:
            assert 'conflict' in str(error)
        else:
            raise AssertionError('Existing different destination was replaced')
        assert target.read_bytes()==b'new file bytes'
        assert selected.read_bytes()==b'different selected bytes'
    assert sentinel.read_bytes()==b'previous contents'
    assert not list(destination.rglob('.realms-transfer-*'))
finally:
    subprocess.run(['umount',str(mount)],check=True)
assert root.stat().st_dev == mount.stat().st_dev
'''
    result = subprocess.run(['unshare', '-Urnm', '--propagation', 'private', sys.executable, '-I', '-S', '-c',
                             code, str(MODULE), str(tmp_path), placement], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
