"""Independent pidfd guardian; systemd BindsTo retires even starting guests."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import select
import socket
import sys

if __package__ in (None, ""):
    import runpy
    __package__ = runpy.run_path(
        str(Path(__file__).resolve().with_name("_binding.py"))
    )["load_runtime"]().__name__

from .lifecycle import OwnershipError, atomic_json
from .vm_owner_lifetime import live_owner_fd, owner_lock, read_receipt, receipt_path


def _notify_ready():
    address = os.environ["NOTIFY_SOCKET"]
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
        notifier.connect(address)
        notifier.sendall(b"READY=1")


def _retire(record, receipt):
    receipt["state"] = "retiring"
    atomic_json(receipt_path(record), receipt)


def _watch(record, owner, fd, invocation):
    watcher = select.poll()
    watcher.register(fd, select.POLLIN)
    while True:
        exited = bool(watcher.poll(100))
        with owner_lock(record):
            receipt = read_receipt(record)
            if receipt.get("guard_invocation_id") != invocation:
                raise OwnershipError("VM guardian receipt invocation changed")
            if receipt["state"] == "retiring":
                return True
            if receipt["owner"] != owner:
                # Handoff won the lock. Re-open and validate the new pidfd;
                # observing the old pidfd's exit must not retire its successor.
                return False
            if exited:
                _retire(record, receipt)
                return True


def run(record):
    invocation = os.environ["INVOCATION_ID"]
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise OwnershipError("VM guardian lacks a systemd invocation")
    notified = False
    while True:
        with ExitStack() as descriptors:
            with owner_lock(record):
                receipt = read_receipt(record)
                if receipt["state"] == "retiring":
                    return
                if receipt.get("guard_invocation_id") not in (None, invocation):
                    raise OwnershipError("refusing to restart a retired VM guardian")
                owner = receipt.get("owner")
                try:
                    fd = descriptors.enter_context(live_owner_fd(owner))
                except OwnershipError:
                    _retire(record, receipt)
                    return
                receipt.update(guard_invocation_id=invocation, watched_owner=owner)
                atomic_json(receipt_path(record), receipt)
            if not notified:
                _notify_ready()
                notified = True
            if _watch(record, owner, fd, invocation):
                return


if __name__ == "__main__":
    run(json.loads(sys.argv[1]))
