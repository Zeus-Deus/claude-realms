"""Durable VM data receipts, independent of ephemeral compute ownership.

These detect accidental replacement, not a hostile same-UID administrator.
Mutable overlay/firmware are inode-bound; immutable spec/base metadata and the
SSH pin are sealed. Incomplete legacy work is retained, never auto-recreated.
"""
import hashlib
import json
import os
from pathlib import Path
import stat

from .config import Config
from .lifecycle import OwnershipError


def _file(path, *, mutable=False, large=False):
    if path.resolve() != path:
        raise OwnershipError("VM workspace path redirects through a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not info.st_size:  # windows-footgun: ok — runtime package rejects non-Linux hosts
            raise OwnershipError("VM workspace file is missing, empty or foreign")
        result: dict[str, int | str] = {"device": info.st_dev, "inode": info.st_ino}
        if not mutable:
            result.update(size=info.st_size, mtime_ns=info.st_mtime_ns)
            if not large:
                if info.st_size > 2 * 1024 * 1024:
                    raise OwnershipError("VM workspace metadata is too large")
                result["sha256"] = hashlib.sha256(os.read(fd, info.st_size + 1)).hexdigest()
        return result
    finally:
        os.close(fd)


def _binding(record):
    from .vm_manager import validate_vm_record
    validate_vm_record(record)
    from .vm_base import dependency
    dependency(Path(record["home"]), record.get("base_disk", ""))
    session = Path(record["session_dir"])
    info = session.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:  # windows-footgun: ok — runtime package rejects non-Linux hosts
        raise OwnershipError("VM workspace directory ownership changed")
    return {key: record[key] for key in ("id", "generation", "uid", "home", "session_id", "session_dir", "base_disk")}


def create(record):
    binding = _binding(record)
    session = Path(record["session_dir"])
    base = Path(record["base_disk"])
    record["workspace"] = {
        "version": 1, "binding": binding,
        "files": {name: _file(session / name, mutable=name != "spec.json")
                  for name in ("disk.qcow2", "OVMF_VARS.4m.fd", "spec.json")},
        "base": {"disk": _file(base, large=True), "metadata": _file(base.parent / "base.json")},
    }


def retain_pin(record):
    from .vm_ssh import require_host_key
    source = require_host_key(record)
    pin = _file(source)
    destination = Path(record["session_dir"]) / "ssh_known_hosts"
    # Exclusive creation: never overwrite a previous enrolled identity.
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _file(destination)["sha256"] != pin["sha256"]:
            raise OwnershipError("VM retained SSH pin differs from the enrolled key")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if isinstance(record.get("workspace"), dict):
        record["workspace"]["pin"] = _file(destination)


def validate(record, *, allow_unlaunched=False):
    try:
        binding = _binding(record)
        receipt = record.get("workspace")
        if not isinstance(receipt, dict) or receipt.get("version") != 1 or receipt.get("binding") != binding:
            raise OwnershipError("VM workspace has no complete bound recovery receipt")
        session = Path(record["session_dir"])
        for name in ("disk.qcow2", "OVMF_VARS.4m.fd", "spec.json"):
            if _file(session / name, mutable=name != "spec.json") != receipt["files"][name]:
                raise OwnershipError("VM retained workspace file changed: " + name)
        base = Path(record["base_disk"])
        if {"disk": _file(base, large=True), "metadata": _file(base.parent / "base.json")} != receipt["base"]:
            raise OwnershipError("VM retained base dependency changed")
        if not allow_unlaunched:
            if _file(session / "ssh_known_hosts") != receipt.get("pin"):
                raise OwnershipError("VM retained SSH pin changed or was never enrolled")
            if stat.S_IMODE((session / "ssh_known_hosts").stat().st_mode) != 0o600:
                raise OwnershipError("VM retained SSH pin permissions changed")
        elif record.get("ssh_host_key_pinned") or receipt.get("pin"):
            raise OwnershipError("A previously enrolled VM cannot be treated as unlaunched")
        config = Config(**json.loads((session / "spec.json").read_text(encoding="utf-8")))
        if (config.vm.memory, config.vm.network) != (record["memory"], record["network"]):
            raise OwnershipError("VM retained resource receipt differs from frozen spec")
        return config
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OwnershipError("VM workspace metadata is incomplete; recovery required") from exc


def _directory(path):
    if path.resolve() != path:
        raise OwnershipError("VM deletion directory redirects through a symlink")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:  # windows-footgun: ok — runtime package rejects non-Linux hosts
        raise OwnershipError("VM deletion directory ownership changed")
    return {"device": info.st_dev, "inode": info.st_ino}


def begin_deletion(record):
    validate(record)
    record["deletion"] = {
        "version": 1,
        "workspace_sha256": hashlib.sha256(json.dumps(record["workspace"], sort_keys=True).encode()).hexdigest(),
        "directories": {key: _directory(Path(record[key])) for key in ("runtime_dir", "session_dir")},
    }


def validate_deletion(record):
    """Only already-authorized missing files are tolerated, never replacements."""
    from .vm_manager import validate_vm_record
    validate_vm_record(record)
    try:
        intent, receipt = record["deletion"], record["workspace"]
        if (intent["version"] != 1
                or intent["workspace_sha256"] != hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
                or receipt["binding"] != {key: record[key] for key in receipt["binding"]}):
            raise OwnershipError("VM deletion receipt binding changed")
        for key in ("runtime_dir", "session_dir"):
            current = _directory(Path(record[key]))
            if current is not None and current != intent["directories"][key]:
                raise OwnershipError("VM deletion directory was replaced")
        session = Path(record["session_dir"])
        for name, expected in {**receipt["files"], "ssh_known_hosts": receipt["pin"]}.items():
            if name not in {"disk.qcow2", "OVMF_VARS.4m.fd", "spec.json", "ssh_known_hosts"}:
                raise OwnershipError("VM deletion file receipt changed")
            try:
                current = _file(session / name, mutable=name in {"disk.qcow2", "OVMF_VARS.4m.fd"})
            except FileNotFoundError:
                continue
            if current != expected:
                raise OwnershipError("VM deletion file was replaced: " + name)
            if name == "ssh_known_hosts" and stat.S_IMODE((session / name).stat().st_mode) != 0o600:
                raise OwnershipError("VM retained SSH pin permissions changed")
        base = Path(record["base_disk"])
        if {"disk": _file(base, large=True), "metadata": _file(base.parent / "base.json")} != receipt["base"]:
            raise OwnershipError("VM retained base dependency changed")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OwnershipError("VM deletion receipt is incomplete; explicit recovery required") from exc


def restore_pin(record):
    validate(record)
    source = Path(record["session_dir"]) / "ssh_known_hosts"
    destination = Path(record["runtime_dir"]) / "ssh_known_hosts"
    with destination.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(source.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
