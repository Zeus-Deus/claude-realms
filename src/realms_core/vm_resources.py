"""Read-only admission checks; never reclaim other guests to admit a new one."""
from pathlib import Path
import shutil

import psutil

MIB = 1024 * 1024
GIB = 1024 * MIB
HOST_MEMORY_HEADROOM = 512 * MIB
START_DISK_HEADROOM = 2 * GIB
ISO_DISK_HEADROOM = 6 * GIB


def inspect_resources(config, path, *, install=False):
    """Sparse clones need initial headroom, not a reservation of their disk cap."""
    required_memory = config.vm.memory * MIB + HOST_MEMORY_HEADROOM
    required_disk = (int(config.vm.disk_size[:-1]) * GIB + ISO_DISK_HEADROOM
                     if install else START_DISK_HEADROOM)
    blockers = []
    available_memory = available_disk = None
    try:
        available_memory = psutil.virtual_memory().available
    except (OSError, psutil.Error):
        blockers.append("Cannot inspect available host memory; retry before allocating a VM.")
    try:
        target = Path(path).resolve()
        while not target.exists():
            target = target.parent
        available_disk = shutil.disk_usage(target).free
    except OSError:
        blockers.append("Cannot inspect free host disk space; retry before allocating a VM.")
    if available_memory is not None and available_memory < required_memory:
        blockers.append(f"Insufficient host memory: {available_memory // MIB} MiB available; "
                        f"need {required_memory // MIB} MiB including host headroom.")
    if available_disk is not None and available_disk < required_disk:
        blockers.append(f"Insufficient host disk space: {available_disk // MIB} MiB available; "
                        f"need {required_disk // MIB} MiB before allocating this VM.")
    return {"ready": not blockers, "blockers": blockers,
            "available_memory_bytes": available_memory, "required_memory_bytes": required_memory,
            "available_disk_bytes": available_disk, "required_disk_bytes": required_disk}


def require_resources(config, path, *, install=False):
    from .vm_manager import VmError

    report = inspect_resources(config, path, install=install)
    if report["blockers"]:
        raise VmError(" ".join(report["blockers"]) + " No resources were reclaimed; host fallback is disabled.")


def require_install_resources(plan):
    # Starting an existing base may reuse a live guest. Admission belongs at
    # allocation in that case, not here before the manager resolves ownership.
    if plan["kind"] == "omarchy-vm" and plan["action"] == "install":
        from .config import Config, vm_data_path

        require_resources(Config(**plan["config"]), vm_data_path(plan["home"]), install=True)
