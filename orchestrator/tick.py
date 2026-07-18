#!/usr/bin/env python3
"""The orchestrator: one deterministic Gather / Process-inbox / Advance /
Render tick, run on a fixed interval.

Gather and the staleness/eligibility math are pure functions of what's on
disk -- no model call, no judgment, only code. The only "decision" made
here is which single eligible task to dispatch next, and that decision is
entirely mechanical (oldest eligible task by creation time).

Run a single tick:      python3 orchestrator/tick.py --once
Run continuously:        python3 orchestrator/tick.py --loop [--interval-seconds N]

See .agent/execplans/orchestration-file-based-coordination.md for the
full design and the Idempotence and Recovery section in particular, which
this file's dispatch/recovery logic implements directly.
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

# Make `from orchestrator import ...` work whether this file is run
# directly (python3 orchestrator/tick.py, which sets sys.path[0] to the
# orchestrator/ directory itself, not the repo root) or imported as part
# of the orchestrator package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import common, config, gates

ORCHESTRATOR = "orchestrator"


# --- Task queue -----------------------------------------------------------

def task_path(task_id: str) -> Path:
    return config.TASKS_DIR / f"{task_id}.json"


def read_task(task_id: str):
    return common.read_json(task_path(task_id))


def write_task(task_id: str, **fields) -> dict:
    task = read_task(task_id) or {"id": task_id}
    task.update(fields)
    common.atomic_write_json(task_path(task_id), task)
    return task


def list_tasks() -> list:
    if not config.TASKS_DIR.exists():
        return []
    files = sorted(config.TASKS_DIR.glob("*.json"))
    return [common.read_json(p) for p in files]


def create_task(worker: str, task_type: str, heavy: bool = False, payload: dict = None) -> str:
    """Enqueue a new task. This is the one sanctioned way to add work --
    used by scripts/submit_task.py and by tests. A task starts life as
    "pending" and is picked up by the next tick's Advance step once
    eligible.
    """
    task_id = f"task-{common.ts_compact()}-{common.rand6()}"
    write_task(
        task_id,
        id=task_id,
        worker=worker,
        type=task_type,
        heavy=heavy,
        status="pending",
        payload=payload or {},
        created_ts=common.now_iso(),
        dispatched_ts=None,
        completed_ts=None,
    )
    return task_id


def _heavy_in_flight(tasks) -> int:
    return sum(
        1 for t in tasks
        if t.get("heavy") and t.get("status") in ("dispatched", "in_progress")
    )


def gather() -> dict:
    """Pure computation over disk state: what's eligible to dispatch next,
    plus liveness info for the dashboard. No side effects.
    """
    tasks = list_tasks()
    heavy_in_flight = _heavy_in_flight(tasks)
    eligible = [
        t for t in tasks
        if t.get("status") == "pending"
        and (not t.get("heavy") or heavy_in_flight < config.MAX_CONCURRENT_HEAVY)
    ]
    eligible.sort(key=lambda t: t.get("created_ts") or "")
    workers = common.list_known_workers()
    heartbeats = {w: common.heartbeat_status(w) for w in workers}
    return {
        "tasks": tasks,
        "eligible": eligible,
        "heavy_in_flight": heavy_in_flight,
        "heartbeats": heartbeats,
        "pending_operator_items": gates.list_pending(),
    }


# --- Process inbox ----------------------------------------------------------

def process_inbox() -> int:
    """Handle every message in the orchestrator's own inbox, oldest first.
    Returns the number of messages processed.
    """
    handled = 0
    for msg_path in common.read_inbox(ORCHESTRATOR):
        msg = common.read_json(msg_path)
        msg_type = msg.get("type")
        payload = msg.get("payload") or {}
        if msg_type == "task_completed":
            write_task(payload["task_id"], status="done", completed_ts=common.now_iso())
        elif msg_type == "task_failed":
            write_task(
                payload["task_id"],
                status="failed",
                completed_ts=common.now_iso(),
                failure_reason=payload.get("reason"),
            )
        elif msg_type == "escalation":
            gates.file_pending(
                action_type="worker_escalation",
                requested_by=msg.get("from"),
                description=payload.get("description", "worker escalation"),
                payload=payload,
                category="ambiguous_authorization",
            )
        # Unknown message types are intentionally not an error: forward
        # compatibility for message types this version doesn't know about
        # yet. They're still archived to done/ below so they aren't
        # reprocessed forever.
        common.complete_inbox_message(ORCHESTRATOR, msg_path)
        handled += 1
    return handled


# --- Advance (dispatch), with write-before-flip crash recovery ------------

def _recover_uncommitted_dispatches() -> int:
    """Find any dispatch whose journal shows "intent" with no matching
    "committed" entry -- meaning a previous tick was interrupted between
    deciding to dispatch and finishing the flip -- and complete it
    idempotently. See Idempotence and Recovery in the execution plan.
    """
    entries = common.read_jsonl(config.DISPATCH_LOG_PATH)
    last_entry_for_task = {}
    for e in entries:
        last_entry_for_task[e["task_id"]] = e  # later lines overwrite earlier -> last entry wins

    recovered = 0
    for task_id, entry in last_entry_for_task.items():
        if entry["phase"] != "intent":
            continue  # already committed, nothing to recover
        task = read_task(task_id)
        if task is None:
            continue  # nothing to recover onto (shouldn't normally happen)
        worker = entry["worker"]
        message_id = entry["message_id"]

        if task.get("status") in ("dispatched", "in_progress", "done", "failed"):
            # The status flip already happened; only the commit journal
            # line is missing. Just close the journal -- no new send.
            common.append_jsonl(config.DISPATCH_LOG_PATH, {**entry, "phase": "committed", "ts": common.now_iso()})
            recovered += 1
            continue

        already_sent = common.message_exists(worker, message_id, "inbox") or common.message_exists(worker, message_id, "done")
        if not already_sent:
            # Crash happened before the send -- perform it now, for the
            # first and only time, using the SAME message id that was
            # already durably logged as the intent.
            common.write_mailbox_message_with_id(message_id, worker, ORCHESTRATOR, "dispatch_task", {"task_id": task_id})
        write_task(task_id, status="dispatched", dispatched_ts=common.now_iso())
        common.append_jsonl(config.DISPATCH_LOG_PATH, {**entry, "phase": "committed", "ts": common.now_iso()})
        recovered += 1
    return recovered


def _dispatch_one(task: dict) -> str:
    """Dispatch a single freshly-eligible task: write intent, send, flip
    status, write commit -- in that exact order. Returns the message id.
    """
    message_id = f"{common.ts_compact()}-{common.rand6()}-dispatch_task"
    task_id = task["id"]
    worker = task["worker"]
    common.append_jsonl(config.DISPATCH_LOG_PATH, {
        "ts": common.now_iso(), "phase": "intent",
        "task_id": task_id, "worker": worker, "message_id": message_id,
    })
    common.write_mailbox_message_with_id(message_id, worker, ORCHESTRATOR, "dispatch_task", {"task_id": task_id})
    write_task(task_id, status="dispatched", dispatched_ts=common.now_iso())
    common.append_jsonl(config.DISPATCH_LOG_PATH, {
        "ts": common.now_iso(), "phase": "committed",
        "task_id": task_id, "worker": worker, "message_id": message_id,
    })
    return message_id


def advance(gathered: dict) -> dict:
    """Recover any interrupted dispatch first, then dispatch at most one
    freshly-eligible task (mechanical choice: oldest by created_ts).
    Returns a small summary of what happened, for Render.
    """
    recovered = _recover_uncommitted_dispatches()

    # Re-gather if recovery changed anything, so eligibility (esp. the
    # fan-out cap count) reflects the now-consistent state before picking
    # a new task.
    g = gather() if recovered else gathered
    dispatched_task_id = None
    if g["eligible"]:
        candidate = g["eligible"][0]
        _dispatch_one(candidate)
        dispatched_task_id = candidate["id"]

    return {"recovered": recovered, "dispatched_task_id": dispatched_task_id}


# --- Render -----------------------------------------------------------------

def _summary_for_render(gathered: dict) -> dict:
    counts = {}
    for t in gathered["tasks"]:
        counts[t.get("status", "?")] = counts.get(t.get("status", "?"), 0) + 1
    stalled = [w for w, hb in gathered["heartbeats"].items() if hb["is_stalled"]]
    return {
        "task_counts": counts,
        "heavy_in_flight": gathered["heavy_in_flight"],
        "stalled_workers": sorted(stalled),
        "operator_pending_count": len(gathered["pending_operator_items"]),
    }


def render(gathered: dict, advance_result: dict) -> bool:
    """Write a status update only if something actually changed since the
    last tick. Returns True if a render actually happened (non-quiet
    tick), False if the tick was quiet and nothing was written.
    """
    summary = _summary_for_render(gathered)
    summary_json = json.dumps(summary, sort_keys=True)
    new_hash = hashlib.sha256(summary_json.encode()).hexdigest()

    previous = common.read_json(config.LAST_RENDER_SUMMARY_PATH, default=None)
    previous_hash = previous.get("hash") if previous else None

    changed = (new_hash != previous_hash) or advance_result.get("dispatched_task_id") or advance_result.get("recovered")
    if not changed:
        return False

    common.atomic_write_json(config.LAST_RENDER_SUMMARY_PATH, {"hash": new_hash, "summary": summary, "ts": common.now_iso()})
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    line_parts = [common.now_iso(), json.dumps(summary, sort_keys=True)]
    if advance_result.get("dispatched_task_id"):
        line_parts.append(f"dispatched={advance_result['dispatched_task_id']}")
    if advance_result.get("recovered"):
        line_parts.append(f"recovered_dispatches={advance_result['recovered']}")
    with open(config.ORCHESTRATOR_LOG_PATH, "a") as f:
        f.write(" | ".join(line_parts) + "\n")
    return True


# --- Tick + CLI ---------------------------------------------------------

def run_tick(interval_seconds: float) -> dict:
    process_inbox()
    gathered = gather()
    advance_result = advance(gathered)
    # Re-gather after advance so Render sees post-dispatch counts.
    gathered_after = gather()
    rendered = render(gathered_after, advance_result)
    common.write_worker_state(
        ORCHESTRATOR,
        phase="idle",
        current_task_id=None,
        last_action={"type": "tick_completed", "ts": common.now_iso(), "dispatched_task_id": advance_result.get("dispatched_task_id")},
    )
    common.write_heartbeat(
        ORCHESTRATOR, ok=True, interval_seconds=interval_seconds,
        summary=f"tick ok, dispatched={advance_result.get('dispatched_task_id')}, rendered={rendered}",
    )
    return {"advance": advance_result, "rendered": rendered}


def main():
    parser = argparse.ArgumentParser(description="Orchestrator tick loop")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run a single tick and exit")
    mode.add_argument("--loop", action="store_true", help="run continuously on --interval-seconds")
    parser.add_argument("--interval-seconds", type=float, default=config.DEFAULT_TICK_INTERVAL_SECONDS)
    args = parser.parse_args()

    if args.once:
        result = run_tick(args.interval_seconds)
        print(json.dumps(result))
        return

    while True:
        run_tick(args.interval_seconds)
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
