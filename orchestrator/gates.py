"""Human-gates: config-driven list of action types that always wait for a
person, and the durable operator-pending queue those waits land in.

Autonomy is the default. An action_type not present in gates.json is NOT
gated and runs unattended. gates.json is meant to be edited by the
operator directly -- this module only reads it, it never writes it (the
"policy_change" gate exists precisely so that editing this file itself is
a human act, not something a worker does to itself).
"""

import os
from pathlib import Path

from orchestrator import common, config


def load_gates(gates_path: Path = None) -> list:
    path = gates_path or config.GATES_CONFIG_PATH
    data = common.read_json(path, default={"gates": []})
    return data.get("gates", [])


def is_gated(action_type: str, gates_path: Path = None):
    """Return the matching gates.json entry for action_type, or None."""
    for entry in load_gates(gates_path):
        if entry.get("action_type") == action_type:
            return entry
    return None


def _pending_dir() -> Path:
    config.OPERATOR_PENDING_DIR.mkdir(parents=True, exist_ok=True)
    return config.OPERATOR_PENDING_DIR


def _resolved_dir() -> Path:
    config.OPERATOR_PENDING_RESOLVED_DIR.mkdir(parents=True, exist_ok=True)
    return config.OPERATOR_PENDING_RESOLVED_DIR


def file_pending(action_type: str, requested_by: str, description: str, payload: dict, category: str = None) -> str:
    """Create a new operator-pending item. Returns its id.

    Filename is timestamp-prefixed (same scheme as mailbox messages) so
    the dashboard and any listing always show items in the order they
    were raised.
    """
    ts_id = common.ts_compact() + "-" + common.rand6()
    pending_id = f"pending-{ts_id}"
    item = {
        "id": pending_id,
        "ts": common.now_iso(),
        "requested_by": requested_by,
        "action_type": action_type,
        "category": category,
        "description": description,
        "payload": payload,
        "status": "pending",
        "resolved_ts": None,
        "resolved_by": None,
        "note": None,
    }
    common.atomic_write_json(_pending_dir() / f"{pending_id}.json", item)
    return pending_id


def list_pending() -> list:
    """Every unresolved item, oldest first. Never expires, never silently
    disappears -- only moves to resolved/ via resolve_pending below.
    """
    d = _pending_dir()
    files = sorted(
        (p for p in d.iterdir() if p.is_file() and p.suffix == ".json"),
        key=lambda p: p.name,
    )
    return [common.read_json(p) for p in files]


def get_pending(pending_id: str):
    path = _pending_dir() / f"{pending_id}.json"
    return common.read_json(path)


def get_resolved(pending_id: str):
    """The archived (resolved) copy of a pending item, or None if it
    hasn't been resolved yet. Workers waiting on a gate poll THIS, not
    get_pending, since resolved/ is the authoritative post-decision copy.
    """
    path = _resolved_dir() / f"{pending_id}.json"
    return common.read_json(path)


def resolve_pending(pending_id: str, decision: str, resolved_by: str, note: str = "") -> dict:
    """Approve or deny a pending item, then archive it to resolved/.

    decision must be "approved" or "denied". Called by both
    scripts/resolve_pending.py (operator CLI) and the dashboard's
    Approve/Deny buttons -- one code path for both entry points.
    """
    if decision not in ("approved", "denied"):
        raise ValueError(f"decision must be 'approved' or 'denied', got {decision!r}")
    path = _pending_dir() / f"{pending_id}.json"
    item = common.read_json(path)
    if item is None:
        raise FileNotFoundError(f"no pending item {pending_id!r} (already resolved, or never existed)")
    item["status"] = decision
    item["resolved_ts"] = common.now_iso()
    item["resolved_by"] = resolved_by
    item["note"] = note
    dest = _resolved_dir() / f"{pending_id}.json"
    common.atomic_write_json(dest, item)
    os.remove(path)
    return item
