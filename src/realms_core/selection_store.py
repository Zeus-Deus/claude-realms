"""Non-mutating snapshots of the plugin's small rollback-journal stores."""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat


@contextmanager
def read_snapshot(path, *, mode=None):
    """Read the validated object, never let SQLite reopen its filename.

    These stores default to DELETE journaling. WAL/hot journals require an
    explicit writer/recovery; a selector must neither create SHM nor ignore a
    committed WAL. Metadata and sidecar checks bracket both copying and use so
    concurrent writes/replacement refuse rather than authorize a torn snapshot.
    """
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()  # windows-footgun: ok — runtime package rejects non-Linux hosts
                or (mode is not None and stat.S_IMODE(opened.st_mode) != mode)
                or not 100 <= opened.st_size <= 16 * 1024 * 1024):
            raise ValueError("Selection store cannot be safely read")

        def stamp(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        def unchanged():
            if (stamp(os.fstat(fd)) != stamp(opened)
                    or stamp(path.lstat()) != stamp(opened)):
                raise ValueError("Selection store changed while reading")
            for suffix in ('-journal', '-wal', '-shm'):
                if os.path.lexists(str(path) + suffix):
                    raise ValueError("Selection store journal requires explicit recovery")

        unchanged()
        data = bytearray()
        while len(data) < opened.st_size:
            chunk = os.read(fd, min(65536, opened.st_size - len(data)))
            if not chunk:
                raise ValueError("Selection store was truncated")
            data.extend(chunk)
        unchanged()
        if data[:16] != b'SQLite format 3\0' or data[18:20] != b'\1\1':
            raise ValueError("Selection requires a rollback-journal SQLite store")
        db = sqlite3.connect(':memory:')
        try:
            # Python builds without deserialize cannot safely perform this read.
            if not hasattr(db, 'deserialize'):
                raise ValueError("SQLite snapshot reads are unavailable")
            db.deserialize(bytes(data))
            db.execute('PRAGMA query_only=ON')
            yield db
            unchanged()
        finally:
            db.close()
    finally:
        os.close(fd)
