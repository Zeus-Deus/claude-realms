"""Durable human holds, live input leases and independent revocation epochs."""

import contextlib
import os
from pathlib import Path
import sqlite3
import stat
import time

from .lifecycle import alive, identity


class _StoreLocks(dict):
    """One re-entrant lock per authority store within this process.

    Parallel subagents in one backend create and write this store while their
    siblings read it. The read refuses a store that is mid-write (and must), so
    same-process writers and readers are serialized instead.
    """

    def __missing__(self, path):
        import threading
        return self.setdefault(path, threading.RLock())


_store_locks = _StoreLocks()


class ControlAuthority:
    TTL = 3.0

    def __init__(self, directory):
        with _store_locks[str(Path(directory).absolute())]:
            self._create(directory)

    def _create(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()  # windows-footgun: ok — runtime package rejects non-Linux hosts
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("Viewer state must be a private owned directory")
        self.path = self.directory / "authority.sqlite"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._connection() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS leases(realm TEXT PRIMARY KEY, generation TEXT NOT NULL, lease TEXT NOT NULL, pid INTEGER NOT NULL, started INTEGER NOT NULL, expires REAL NOT NULL); CREATE TABLE IF NOT EXISTS epochs(realm TEXT PRIMARY KEY, value INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS control_epochs(realm TEXT PRIMARY KEY, value INTEGER NOT NULL);"
            )
            db.execute("BEGIN IMMEDIATE")
            if "established" not in {row[1] for row in db.execute("PRAGMA table_info(leases)")}:
                # Pre-upgrade holds may already contain an interrupted login.
                db.execute("ALTER TABLE leases ADD COLUMN established INTEGER NOT NULL DEFAULT 1")

    @contextlib.contextmanager
    def _connection(self):
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()  # windows-footgun: ok — runtime package rejects non-Linux hosts
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Viewer authority ownership changed")
        with _store_locks[str(self.directory.absolute())]:
            db = sqlite3.connect(self.path, timeout=3)
            try:
                with db:
                    yield db
            finally:
                db.close()

    @classmethod
    def read_agent_epoch(cls, directory, realm):
        """Observe control without constructing a viewer or upgrading its store.

        Only a never-created viewer directory is an initial cold state. An
        existing directory with no DB, a legacy schema, or corrupt data refuses.
        """
        from .setup_plan import confined
        directory = Path(directory)
        path = confined(directory.parent.parent, directory / "authority.sqlite")
        if not directory.exists():
            return None
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:  # windows-footgun: ok — runtime package rejects non-Linux hosts
            raise ValueError("Viewer state must be a private owned directory")
        from .selection_store import read_snapshot
        with _store_locks[str(directory.absolute())], read_snapshot(path, mode=0o600) as db:
            db.execute("BEGIN")
            if "established" not in {row[1] for row in db.execute("PRAGMA table_info(leases)")}:
                raise ValueError("Viewer authority requires an explicit upgrade")
            if db.execute("SELECT 1 FROM leases WHERE realm=?", (realm,)).fetchone():
                raise PermissionError("Human control is held; explicitly hand back or recover the viewer")
            row = db.execute("SELECT value FROM control_epochs WHERE realm=?", (realm,)).fetchone()
            epoch = row[0] if row else 0
            if type(epoch) is not int or epoch < 0:
                raise ValueError("Invalid viewer control epoch")
            return epoch

    @staticmethod
    def _live(row):
        return (
            row is not None
            and row[2] > time.time()
            and alive({"pid": row[0], "start_time": row[1]})
        )

    def acquire(self, realm, generation, lease, *, pending=False):
        own = identity(os.getpid())
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT pid,started,expires,established FROM leases WHERE realm=?", (realm,)
            ).fetchone()
            if self._live(row):
                return False
            db.execute(
                "INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?,?)",
                (
                    realm,
                    generation,
                    lease,
                    own["pid"],
                    own["start_time"],
                    time.time() + self.TTL,
                    int(not pending or bool(row and row[3])),
                ),
            )
            self._advance_control_epoch(db, realm)
            return True

    def attach(self, realm, lease):
        with self._connection() as db:
            db.execute(
                "UPDATE leases SET established=1 WHERE realm=? AND lease=?", (realm, lease)
            )

    def abort(self, realm, lease):
        """Undo failed initial attach, but never erase an earlier human hold."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "DELETE FROM leases WHERE realm=? AND lease=? AND established=0", (realm, lease)
            ).rowcount
            if changed:
                self._advance_control_epoch(db, realm)
            else:
                db.execute(
                    "UPDATE leases SET expires=0 WHERE realm=? AND lease=?", (realm, lease)
                )

    def controlled(self, realm):
        return self.status(realm)["controlled"]

    def status(self, realm):
        """Heartbeat/process loss disconnects input, never hands back to the agent."""
        with self._connection() as db:
            row = db.execute(
                "SELECT pid,started,expires FROM leases WHERE realm=?", (realm,)
            ).fetchone()
            return {"controlled": row is not None, "connected": self._live(row)}

    def disconnect(self, realm, lease):
        """Retain exclusion while making this exact holder manually recoverable."""
        with self._connection() as db:
            db.execute(
                "UPDATE leases SET expires=0 WHERE realm=? AND lease=?", (realm, lease)
            )

    @staticmethod
    def _advance_control_epoch(db, realm):
        db.execute(
            "INSERT INTO control_epochs VALUES(?,1) ON CONFLICT(realm) DO UPDATE SET value=value+1",
            (realm,),
        )

    def agent_epoch(self, realm):
        """Atomically admit agent access and snapshot its full-handback fence.

        This is NOT the VNC ticket epoch: normal human acquire/release must not
        invalidate Watch tickets. Callers recheck this value before disclosure.
        """
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM leases WHERE realm=?", (realm,)).fetchone():
                raise PermissionError("Human control is held; explicitly hand back or recover the viewer")
            row = db.execute(
                "SELECT value FROM control_epochs WHERE realm=?", (realm,)
            ).fetchone()
            return row[0] if row else 0

    def refresh(self, realm, lease):
        with self._connection() as db:
            changed = db.execute(
                "UPDATE leases SET expires=? WHERE realm=? AND lease=? AND expires>?",
                (time.time() + self.TTL, realm, lease, time.time()),
            ).rowcount
            return changed == 1

    @contextlib.contextmanager
    def forwarding(self, realm, generation, lease=None):
        """Serialize the synchronous socket write with takeover and revocation.

        Never await inside this guard: the transaction is held only through the
        final ticket check and transport write, not network backpressure.
        """
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = db.execute(
                "SELECT value FROM epochs WHERE realm=?", (realm,)
            ).fetchone()
            valid = generation.rpartition(":")[2] == str(epoch[0] if epoch else 0)
            if lease is not None:
                row = db.execute(
                    "SELECT pid,started,expires,generation,lease FROM leases WHERE realm=?",
                    (realm,),
                ).fetchone()
                valid = (
                    valid
                    and self._live(row[:3] if row else None)
                    and row[3:] == (generation, lease)
                    and identity(os.getpid()) == {"pid": row[0], "start_time": row[1]}
                )
                if valid:
                    db.execute(
                        "UPDATE leases SET expires=? WHERE realm=? AND lease=?",
                        (time.time() + self.TTL, realm, lease),
                    )
            yield bool(valid)

    def release(self, realm, lease):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "DELETE FROM leases WHERE realm=? AND lease=?", (realm, lease)
            ).rowcount
            if changed:
                self._advance_control_epoch(db, realm)

    def epoch(self, realm):
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM epochs WHERE realm=?", (realm,)
            ).fetchone()
            return row[0] if row else 0

    def revoke(self, realm):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO epochs VALUES(?,1) ON CONFLICT(realm) DO UPDATE SET value=value+1",
                (realm,),
            )
            self._advance_control_epoch(db, realm)
