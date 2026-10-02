"""Out-of-scope sentinel: stopping the scope also kills orphaned descendants."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

if __package__ in (None, ""):
    import runpy

    __package__ = runpy.run_path(str(Path(__file__).resolve().with_name("_binding.py")))["load_runtime"]().__name__

from .lifecycle import Registry, alive, atomic_json, stop_scope, remove_runtime


def cleanup(home, realm_id, generation=None):
    registry = Registry(home)
    with registry.lock():
        if not registry.path(realm_id).exists():
            return
        record = registry.get(realm_id)
        if generation is not None and record["generation"] != generation:
            return
        if record["status"] in ("stopped", "deleting"):
            return
        stop_scope(record)
        remove_runtime(registry, record)


def run(home, realm_id):
    from .lifecycle import identity
    from .units import backend_of, become_subreaper, publish_direct, scope_command

    registry = Registry(home)
    record = registry.get(realm_id)
    runtime = Path(record["runtime_dir"])
    direct = backend_of(record) == "direct"
    me = identity(os.getpid())
    if direct:
        # Orphans of the desktop reparent here, so cleanup still reaches them.
        become_subreaper()
        publish_direct(home, record["guardian_unit"], me, "Claude realm guardian " + record["generation"])
    atomic_json(runtime / "supervisor.json", me)
    command = scope_command(
        record,
        [sys.executable, "-P", "-m", "realms_core.bootstrap", "worker", str(runtime)],
        description="Claude realm " + record["generation"],
        inhibit_who="Claude Realms",
        inhibit_why="Background realm is active",
    )
    env = dict(os.environ)
    if direct:
        env["REALMS_SUBREAPER"] = "1"
    runner = None
    try:
        runner = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True, env=env,
                                  start_new_session=direct)
        if direct:
            publish_direct(home, record["scope"], identity(runner.pid), "Claude realm " + record["generation"])
        deadline = time.monotonic() + 25
        ready = runtime / "ready.json"
        while not ready.exists():
            if runner.poll() is not None:
                raise RuntimeError("realm scope exited during startup")
            if time.monotonic() > deadline:
                raise RuntimeError("realm startup timed out")
            time.sleep(0.1)
        startup = json.loads(ready.read_text(encoding="utf-8"))
        while all(alive(process) for process in startup["processes"].values()):
            with registry.lock():
                current = registry.get(realm_id)
                abandoned_start = (
                    current["status"] == "starting"
                    and time.time() - current["created_at"] >= 35
                )
                if (
                    abandoned_start
                    or time.time() - current["last_activity"] >= current["idle_ttl"]
                ):
                    # Linearize expiry with env/reuse renewal. Cleanup may wait
                    # for the lock again, but no caller can renew this lease.
                    current["status"] = "stopping"
                    registry.put(current)
                    print("realm idle lease expired", flush=True)
                    break
                if current["status"] in ("stopping", "stopped"):
                    break
                owner = current.get("owner_process")
                if owner and not alive(owner):
                    # The host process that owned this desktop is gone.
                    current["status"] = "stopping"
                    registry.put(current)
                    print("realm owner exited", flush=True)
                    break
            time.sleep(0.2)
        dead = {
            name: process
            for name, process in startup["processes"].items()
            if not alive(process)
        }
        if dead:
            print("realm components exited: " + json.dumps(dead), flush=True)
    except Exception as exc:
        if runtime.exists():
            atomic_json(runtime / "error.json", {"error": str(exc)})
        print(str(exc), file=sys.stderr, flush=True)
    finally:
        try:
            cleanup(home, realm_id, record["generation"])
        finally:
            if runner is not None:
                try:
                    runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if direct:
                _reap_leftovers(home, record)


def _reap_leftovers(home, record):
    """Direct backend: kill reparented stragglers, then retire this guardian."""
    import signal
    from .units import _receipt, descendants, identity

    me = identity(os.getpid())
    for number in (signal.SIGTERM, signal.SIGKILL):
        stragglers = [pid for pid in descendants(me) if pid != me["pid"]]
        for pid in stragglers:
            try:
                os.kill(pid, number)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if all(pid == me["pid"] for pid in descendants(me)):
                break
            time.sleep(0.05)
    _receipt(home, record["guardian_unit"]).unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1] == "cleanup":
        cleanup(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    else:
        run(sys.argv[1], sys.argv[2])
