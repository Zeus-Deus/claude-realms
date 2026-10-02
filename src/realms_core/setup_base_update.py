"""Profile-only commit admission within the existing setup ledger."""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from .config import Config, vm_data_path
from .vm_base import read_json, selected


def publication_evidence(record):
    """Inspect physical evidence, never roll back an uncertain publication."""
    base = vm_data_path(record["home"]) / "bases" / record["base_generation"]
    try:
        selection = selected(record["home"])
    except (OSError, ValueError, RuntimeError):
        return "unknown"
    try:
        receipt = read_json(base / "base.json")
        valid = (receipt.get("generation") == record["base_generation"]
                 and receipt.get("iso") == f"omarchy-{record['plan']['release']}.iso")
        if valid:
            return "selected" if selection == base else "retained"
        return "unknown"
    except FileNotFoundError:
        return "unknown" if base.exists() else "absent"
    except (OSError, ValueError, RuntimeError):
        return "unknown"


def failure(record):
    evidence = publication_evidence(record)
    if record.get("base_commit_started") or evidence != "absent":
        return {"state": "failed", "error": "publication_uncertain",
                "message": "Base publication outcome is uncertain (generation evidence: " + evidence +
                "). Existing bases and workspaces were retained; inspect current selection before retrying."}
    if record["state"] == "cancelling":
        return {"state": "cancelled", "message": "Base update cancelled before publication. Existing bases and workspaces were retained."}
    return {"state": "failed", "error": "setup_failed",
            "message": "Base update failed before publication. Review prerequisites and current selection before retrying."}


def completed(record):
    if not record.get("base_committed") or publication_evidence(record) not in {"selected", "retained"}:
        raise ValueError("Base publication could not be verified")


@contextmanager
def publication(home, job_id, generation, expected_selection, config, release):
    from . import setup_flow as flow
    from .vm_manager import VENDORED_SHA256

    service = SimpleNamespace(home=Path(home))
    with flow._job_guard(service, job_id):
        current = flow._read(service, job_id)
        flow._require_scope(current, "vm-base-update", None)
        flow._check_cancel(current)
        flow._require_active(service, job_id)
        plan = current["plan"]
        if (current["state"] != "running" or current.get("base_commit_started")
                or current.get("cancel_protocol") != 1
                or current["base_generation"] != generation
                or plan["expected_selection"] != expected_selection
                or plan["release"] != release or plan["config"] != config
                or plan["installer_revision"] != VENDORED_SHA256):
            raise PermissionError("Base update publication binding changed")

        def admit():
            # Called only after the registry lock and selection comparison.
            if asdict(Config.load(home)) != config:
                raise ValueError("Setup configuration changed before publication")
            current["base_commit_started"] = True
            flow.atomic_json(flow._path(service, job_id), current)

        yield admit
        # Keep running until the installer child and supervisor actually retire.
        current["base_committed"] = True
        flow.atomic_json(flow._path(service, job_id), current)
