"""Human terminal output: readable realm tables and a linkable live viewer.

Machine output stays JSON. Tables are for a person at a terminal; scripts that
pipe ``list`` keep receiving the exact JSON they always did.
"""

import getpass
import ipaddress
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

STATES = {
    "running": "running",
    "starting": "starting",
    "stopping": "stopping",
    "stopped": "stopped",
    "deleting": "deleting",
    "recovery-required": "needs recovery",
    "cleanup_failed": "cleanup failed",
}
# Lower sorts first: what is live (and costs memory) leads the table.
_ORDER = {"running": 0, "starting": 1, "stopping": 1, "needs recovery": 2,
          "needs export": 2, "cleanup failed": 2, "deleting": 3, "stopped": 4}
_COLORS = {"running": "32", "starting": "36", "stopping": "36", "needs recovery": "33",
           "needs export": "33", "cleanup failed": "31"}


def state(record):
    if record.get("cleanup_note") and "workspace_dir" not in record:
        return "needs export"
    return STATES.get(record.get("status"), str(record.get("status") or "unknown"))


def ago(timestamp, now):
    if not isinstance(timestamp, (int, float)):
        return "-"
    seconds = max(0, int(now - timestamp))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s" if seconds else "now"


def _size(record):
    if record.get("kind") == "omarchy-vm" or str(record.get("id", "")).startswith("v-"):
        memory = record.get("memory")
        if isinstance(memory, int):
            return f"{memory / 1024:g} GB RAM" if memory % 1024 == 0 else f"{memory} MB RAM"
        return "-"
    return str(record.get("size") or "-")


def _clip(text, width):
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def table(records, *, now=None, width=None, color=False):
    """Render records as an aligned table, running first, then most recently used."""
    now = time.time() if now is None else now
    if not records:
        return "No realms in this profile."
    rows = []
    for record in records:
        rows.append({
            "ID": str(record.get("id", "?")),
            "KIND": "vm" if record.get("kind") == "omarchy-vm" else "realm",
            "STATE": state(record),
            "SIZE": _size(record),
            "SESSION": str(record.get("session_id") or "-"),
            "AGE": ago(record.get("created_at"), now),
            "ACTIVE": ago(record.get("last_activity"), now),
            "_sort": (_ORDER.get(state(record), 5), -(record.get("last_activity") or 0)),
        })
    rows.sort(key=lambda row: row["_sort"])
    columns = ["ID", "KIND", "STATE", "SIZE", "SESSION", "AGE", "ACTIVE"]
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in columns}
    width = width or shutil.get_terminal_size((120, 24)).columns
    fixed = sum(widths[c] for c in columns if c != "SESSION") + 2 * (len(columns) - 1)
    widths["SESSION"] = max(12, min(widths["SESSION"], width - fixed))
    lines = ["  ".join(c.ljust(widths[c]) for c in columns).rstrip()]
    for row in rows:
        cells = []
        for column in columns:
            cell = _clip(row[column], widths[column]).ljust(widths[column])
            if color and column == "STATE" and row["STATE"] in _COLORS:
                cell = f"\033[{_COLORS[row['STATE']]}m{cell}\033[0m"
            cells.append(cell)
        lines.append("  ".join(cells).rstrip())
    counts = {}
    for row in rows:
        counts[row["STATE"]] = counts.get(row["STATE"], 0) + 1
    lines.append("")
    lines.append(", ".join(f"{n} {s}" for s, n in sorted(
        counts.items(), key=lambda item: _ORDER.get(item[0], 5))))
    reasons = {}
    for record in records:
        reason = record.get("recovery_reason") if state(record) == "needs recovery" else None
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
        lines.append(f"  needs recovery ({count}): {reason}")
    if any(row["STATE"] == "running" for row in rows):
        lines.append("Watch one: realms view ID   ·   Full records: --json")
    else:
        lines.append("Full records: --json")
    return "\n".join(lines)


def print_records(records, args, stream=None):
    """JSON for scripts and pipes (unchanged contract); a table for people."""
    import json

    stream = stream or sys.stdout
    as_table = getattr(args, "table", False) or (
        not getattr(args, "json", False) and stream.isatty())
    if as_table:
        print(table(records, color=stream.isatty() and not os.environ.get("NO_COLOR")),
              file=stream)
    else:
        print(json.dumps(records, sort_keys=True), file=stream)


# --- live viewer -----------------------------------------------------------

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")


def tailnet_address(run=subprocess.run):
    """This machine's Tailscale IPv4 address, or an explanatory error."""
    try:
        result = run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"--tailnet needs a running Tailscale: {exc}") from None
    for line in (result.stdout or "").split():
        try:
            address = ipaddress.ip_address(line.strip())
        except ValueError:
            continue
        if address in TAILNET_V4:
            return str(address)
    raise ValueError("--tailnet: no Tailscale IPv4 address (100.64.0.0/10) on this machine")


def hyperlink(url, stream):
    """OSC 8 link when a terminal reads the output; the bare URL is always the text."""
    if stream.isatty() and os.environ.get("TERM") != "dumb":
        return f"\033]8;;{url}\033\\{url}\033]8;;\033\\"
    return url


def ssh_hint(port, environ=None):
    """Exact tunnel command for the device this SSH session comes from."""
    environ = os.environ if environ is None else environ
    connection = environ.get("SSH_CONNECTION", "").split()
    if len(connection) != 4:
        return None
    server, server_port = connection[2], connection[3]
    option = "" if server_port == "22" else f" -p {server_port}"
    return (f"ssh -N -L {port}:127.0.0.1:{port}{option} "
            f"{getpass.getuser()}@{server}")


TTL = 300


def _alive(home, record):
    """Read-only liveness: never reconciles, renews idle time or writes records.

    The same generation and compute invocation must still be running; a
    restarted realm is a new screen the printed link was never issued for.
    The bridge separately proves the VNC peer belongs to the realm.
    """
    from .lifecycle import RealmError, scope_info, validate_live
    from .manager import Manager
    from .vm_manager import VmManager, validate_vm_record

    vm = record["id"].startswith("v-")
    manager = VmManager(home) if vm else Manager(home)
    try:
        current = manager.registry.get(record["id"])
        if (current.get("status") != "running"
                or current.get("generation") != record.get("generation")
                or current.get("invocation_id") != record.get("invocation_id")):
            return None
        if vm:
            validate_vm_record(current)
            info = scope_info(current["unit"])
            if (info.get("ActiveState") != "active"
                    or info.get("InvocationID") != current.get("invocation_id")):
                return None
        else:
            validate_live(current)
        return current
    except (RealmError, OSError, KeyError, ValueError):
        return None


def _hangup_watch(stream):
    """True once the reader of *stream* (terminal, pipe, SSH channel) is gone."""
    import select

    try:
        poller = select.poll()
        poller.register(stream.fileno(), select.POLLERR | select.POLLHUP)
    except (AttributeError, OSError, ValueError):
        return lambda: False
    return lambda: any(event & (select.POLLERR | select.POLLHUP)
                       for _, event in poller.poll(0))


def _find(home, realm_id, kind=None):
    """Record lookup across both kinds without reconciling (view is read-only)."""
    from .manager import Manager
    from .vm_manager import VmManager

    managers = {"realm": [Manager], "vm": [VmManager]}.get(kind, [Manager, VmManager])
    if realm_id is not None and kind == "vm" and not realm_id.startswith("v-"):
        raise ValueError(f"{realm_id} is not an Omarchy VM ID (v-…); use realms view")
    if realm_id is None:
        running = [r for m in managers for r in m(home).registry.records()
                   if r.get("status") == "running"]
        if len(running) != 1:
            raise ValueError(
                "No running realm to view" if not running else
                "Several realms are running; pass one ID: "
                + ", ".join(sorted(r["id"] for r in running)))
        return running[0]
    manager = VmManager(home) if realm_id.startswith("v-") else Manager(home)
    with manager.registry.lock():
        return manager.registry.get(realm_id)


def view(home, realm_id=None, *, control=False, tailnet=False, port=0, stream=None,
         stop=None, viewer=None, environ=None, renew_every=60.0, alive=None, kind=None):
    """Serve the realm's screen until Ctrl-C or until the realm stops.

    Reuses the desktop app's viewer: the same loopback listener, vendored
    noVNC page, capability tickets and server-side view-only RFB filter.
    """
    from pathlib import Path

    from .bridge import ViewerServer
    from .config import effective_home

    stream = stream or sys.stdout
    environ = os.environ if environ is None else environ
    home = effective_home(home)
    record = _find(home, realm_id, kind)
    realm_id = record["id"]
    kind = "Omarchy VM" if realm_id.startswith("v-") else "Realm"
    print(f"{kind} {realm_id} · {state(record)} · {_size(record)} · "
          f"session {record.get('session_id') or '-'}", file=stream)
    if record.get("status") != "running":
        raise ValueError(f"{realm_id} is {state(record)}; only a running realm can be viewed")
    alive = alive or _alive
    if alive(home, record) is None:
        raise ValueError(f"{realm_id} is not a live, owned realm right now")
    host = tailnet_address() if tailnet else "127.0.0.1"
    # Its own listener, but the profile's shared takeover authority: a
    # terminal --control holds the agent off exactly like Watch → Take over.
    cache = [0.0, None]

    def resolve(rid):
        # The bridge re-resolves on every forwarded frame and four times a
        # second; a short cache keeps that from spawning systemctl per frame.
        if rid != realm_id:
            return None
        if time.monotonic() - cache[0] > 0.5:
            cache[:] = [time.monotonic(), alive(home, record)]
        return cache[1]

    viewer = viewer or ViewerServer(resolve, state_dir=Path(home) / "realms/viewer")
    viewer.start(host=host, port=port)
    try:
        token = viewer.issue(realm_id, can_control=control, ttl=TTL)
        url = f"{viewer.origin}/realms/{realm_id}/view#ticket={token}"
        if not control:
            url += "&view=1"
        bound = int(viewer.origin.rsplit(":", 1)[1])
        mode = "you may take over input" if control else "view-only"
        print(f"Live screen ({mode}). Open:", file=stream)
        print(f"  {hyperlink(url, stream)}", file=stream)
        hint = None if tailnet else ssh_hint(bound, environ)
        if hint:
            print("You are on SSH: this link works on the device you connected from once "
                  "it forwards the port. Run there, then open the link:", file=stream)
            print(f"  {hint}", file=stream)
        elif tailnet:
            print("Reachable from your tailnet devices only; the link's ticket is required.",
                  file=stream)
        print("The link is private and works only while this command runs. "
              "Ctrl-C to stop.", file=stream, flush=True)
        stop = stop or threading.Event()
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
        last_renew = time.monotonic()
        hangup = _hangup_watch(stream)
        try:
            while not stop.wait(1.0):
                if hangup():
                    # The terminal or SSH session went away without a signal
                    # (e.g. `ssh host realms view` with no pty).
                    return 0
                current = alive(home, record)
                if current is None:
                    print(f"{realm_id} stopped; viewer closed.", file=stream, flush=True)
                    return 0
                if time.monotonic() - last_renew >= renew_every:
                    # Same generation-bound renewal as the desktop's owner route.
                    if not viewer.tickets.renew(token, realm_id,
                                                viewer._ticket_generation(current), ttl=TTL):
                        print("Viewer authorization ended (realm restarted or access "
                              "was revoked); viewer closed.", file=stream, flush=True)
                        return 0
                    last_renew = time.monotonic()
        except KeyboardInterrupt:
            pass
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        print("Viewer closed.", file=stream, flush=True)
        if control and viewer.is_controlled(realm_id):
            print("You still hold human control, so the agent stays paused. Hand back from "
                  "a viewer (the Realm pane, or realms view --control).", file=stream)
        return 0
    finally:
        # Process-local tickets only: the profile-wide revoke would also cut
        # off the desktop app's viewers of this realm.
        viewer.tickets.revoke(realm_id)
        viewer.stop()
