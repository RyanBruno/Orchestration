"""Shared primitives every part of this system is built on.

Everything here exists to make one guarantee hold: a process can be
killed at any point, and a fresh process reading only these files back
from disk can tell exactly what state things were left in. Read
.agent/execplans/orchestration-file-based-coordination.md (Idempotence
and Recovery section) for why each function is shaped the way it is.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from orchestrator import config


def now_iso() -> str:
    """Current UTC time, microsecond precision, explicit offset."""
    return datetime.now(timezone.utc).isoformat()


def _ts_compact() -> str:
    """Filename-safe UTC timestamp with microsecond precision.

    Lexicographic sort of strings produced by this function equals
    chronological order, which is the entire trick mailbox filenames and
    operator-pending filenames rely on for "oldest first" processing
    without needing a shared counter or lock file.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def _rand6() -> str:
    return uuid.uuid4().hex[:6]


# Public aliases: callers outside this module (tick.py, gates.py) need
# these to pre-compute ids for the write-before-flip dispatch pattern,
# where the id must be decided before the journal "intent" line is
# written and then reused for the actual send.
def ts_compact() -> str:
    return _ts_compact()


def rand6() -> str:
    return _rand6()


def atomic_write_json(path: Path, data) -> None:
    """Write JSON to path such that a reader never observes a partial file.

    Writes to a temp file in the SAME directory as the destination, then
    os.replace()s it into place. os.replace is atomic only within one
    filesystem/directory, which is why the temp file is never written
    anywhere else. This is the only way state.json, heartbeat.json, and
    task files are ever written in this codebase -- never open(path, "w")
    directly for anything that represents "current state."
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".tmp-{os.getpid()}-{_rand6()}-{path.name}"
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_path, path)


def read_json(path: Path, default=None):
    """Read+parse a JSON file, or return `default` if it doesn't exist yet.

    A missing file is a normal, expected state (a worker that has never
    run yet has no state.json) -- this is not an error condition.
    """
    path = Path(path)
    if not path.exists():
        return default
    with open(path, "r") as f:
        return json.load(f)


def append_jsonl(path: Path, obj) -> None:
    """Append one JSON object as a line, fsync'd, to an append-only log.

    This is used ONLY for the dispatch journal, which is deliberately the
    one file in this codebase where append (not atomic-replace) is
    correct -- its whole purpose is to durably record intent before an
    effect happens, so a crash between "decided" and "did" is
    unambiguous on restart.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path):
    """Read every line of an append-only JSONL file as a list of dicts."""
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# --- Mailbox -----------------------------------------------------------

def _mailbox_dir(worker: str, sub: str) -> Path:
    d = config.MAILBOX_DIR / worker / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_mailbox_message_with_id(msg_id: str, to_worker: str, from_worker: str, msg_type: str, payload: dict) -> str:
    """Write a message using a CALLER-CHOSEN id rather than generating one.

    Used by the dispatch journal's write-before-flip pattern: the id is
    decided and durably logged as "intent" before the send happens, so
    that if the process crashes between logging intent and sending, a
    restart can check whether a message with that exact id already
    exists (already sent) or not (never sent) and act accordingly without
    ever sending a duplicate. Idempotent: writing the same id twice with
    the same content just overwrites the temp file and replaces the same
    final path, which is safe because the content is deterministic given
    the id.
    """
    message = {
        "id": msg_id,
        "from": from_worker,
        "to": to_worker,
        "type": msg_type,
        "ts": now_iso(),
        "payload": payload,
    }
    inbox = _mailbox_dir(to_worker, "inbox")
    final_path = inbox / f"{msg_id}.json"
    tmp_path = inbox / f".tmp-{os.getpid()}-{_rand6()}-{msg_id}.json"
    tmp_path.write_text(json.dumps(message, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_path, final_path)
    return msg_id


def send_mailbox_message(to_worker: str, from_worker: str, msg_type: str, payload: dict) -> str:
    """Write one message file into to_worker's inbox, generating a fresh
    id. Returns the message id.

    The filename is timestamp-prefixed plus a random suffix, so it is
    both unique (no shared counter needed, hence no extra coordination
    file to keep safe) and orders correctly under lexicographic sort. The
    write itself is temp-then-rename into the inbox directory so a
    concurrent reader of that directory never sees a half-written file.

    Use write_mailbox_message_with_id instead when the caller needs to
    durably pre-commit to an id before sending (the dispatch journal
    pattern) -- this function is for everything else (task_completed,
    task_failed, escalation reports), where no crash-recovery journal is
    tracking the send.
    """
    msg_id = f"{_ts_compact()}-{_rand6()}-{msg_type}"
    return write_mailbox_message_with_id(msg_id, to_worker, from_worker, msg_type, payload)


def read_inbox(worker: str):
    """Every message file in worker's inbox, oldest first (by filename)."""
    inbox = _mailbox_dir(worker, "inbox")
    files = [p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".tmp-")]
    return sorted(files, key=lambda p: p.name)


def message_exists(worker: str, msg_id_prefix: str, where: str = "inbox") -> bool:
    """Whether a message whose id starts with msg_id_prefix exists in inbox or done.

    Used by dispatch-journal recovery to tell "already sent before the
    crash" apart from "never sent."
    """
    d = _mailbox_dir(worker, where)
    return any(p.name.startswith(msg_id_prefix) for p in d.iterdir() if p.is_file())


def complete_inbox_message(worker: str, path: Path) -> Path:
    """Move a handled message from inbox/ to done/. done/ is never pruned."""
    done = _mailbox_dir(worker, "done")
    dest = done / path.name
    os.replace(path, dest)
    return dest


# --- Heartbeats ----------------------------------------------------------

def write_heartbeat(worker: str, ok: bool, interval_seconds: float, summary: str, status: str = "running") -> None:
    """Overwrite worker's heartbeat.json. Called at the end of every cycle,
    and also BEFORE starting any unusually long piece of work with a
    correspondingly longer interval_seconds, so the staleness window
    scales with the work instead of falsely flagging a busy worker.
    """
    path = config.HEARTBEAT_DIR / worker / "heartbeat.json"
    atomic_write_json(path, {
        "worker": worker,
        "ts": now_iso(),
        "ok": ok,
        "interval_seconds": interval_seconds,
        "summary": summary,
        "status": status,
    })


def heartbeat_status(worker: str) -> dict:
    """Read a worker's heartbeat and compute liveness.

    Shared by the orchestrator's Gather step and the dashboard so both
    use exactly one staleness computation, never two that could diverge.
    A worker that stood down on purpose is never considered stalled,
    regardless of how old its heartbeat gets -- intentional shutdown must
    read as idle, not as a crash.
    """
    hb = read_json(config.HEARTBEAT_DIR / worker / "heartbeat.json")
    if hb is None:
        return {
            "worker": worker,
            "found": False,
            "is_stalled": False,
            "age_seconds": None,
            "heartbeat": None,
        }
    ts = datetime.fromisoformat(hb["ts"])
    age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    stood_down = hb.get("status") == "stood_down"
    threshold = config.STALENESS_MULTIPLIER * float(hb.get("interval_seconds") or 0)
    is_stalled = (not stood_down) and (threshold > 0) and (age_seconds > threshold)
    return {
        "worker": worker,
        "found": True,
        "is_stalled": is_stalled,
        "age_seconds": age_seconds,
        "threshold_seconds": threshold,
        "heartbeat": hb,
    }


# --- Worker state.json ----------------------------------------------------

def worker_state_path(worker: str) -> Path:
    return config.STATE_DIR / worker / "state.json"


def read_worker_state(worker: str) -> dict:
    return read_json(worker_state_path(worker), default={
        "worker": worker,
        "phase": "idle",
        "current_task_id": None,
        "waiting_on_pending_id": None,
        "last_action": None,
        "updated_ts": None,
    })


def write_worker_state(worker: str, **fields) -> dict:
    state = read_worker_state(worker)
    state.update(fields)
    state["worker"] = worker
    state["updated_ts"] = now_iso()
    atomic_write_json(worker_state_path(worker), state)
    return state


def list_known_workers():
    """Every worker with either a state or heartbeat directory, for the
    dashboard's benefit. Purely observational -- the orchestrator itself
    never needs a global worker list, only per-task target names.
    """
    names = set()
    for base in (config.STATE_DIR, config.HEARTBEAT_DIR, config.MAILBOX_DIR):
        if base.exists():
            for p in base.iterdir():
                if p.is_dir():
                    names.add(p.name)
    names.discard("orchestrator")
    return sorted(names)
