"""Shared scaffolding for the example fixture workers.

This is NOT production code -- it exists purely so examples/fast_worker.py
and examples/slow_worker.py can exercise the coordination system
end-to-end (mailbox claim/complete, crash-safe state transitions,
heartbeat re-stamping, human gates) without duplicating that logic in
each fixture. A real worker built on this system would follow the same
shape but is free to not use this exact module.

CONTRACT for task handlers registered here: a handler must be safe to run
again in full if the process crashes after claiming a task but before
finishing it. Every handler in this codebase (format a string, sleep N
seconds) already satisfies this; if you add a new one, it must too, or
the kill-and-recover guarantee this system exists to provide breaks for
that task type. See "Idempotence and Recovery" in
.agent/execplans/orchestration-file-based-coordination.md.
"""

import argparse
import time

from orchestrator import common, config, gates


def _read_task(task_id):
    if task_id is None:
        return None
    return common.read_json(config.TASKS_DIR / f"{task_id}.json")


def _find_inbox_message_for_task(worker, task_id):
    """Locate the still-pending dispatch message for task_id in worker's
    inbox, if any. Used to resume after a crash or after an operator
    resolves a gate -- in both cases the message was deliberately left in
    inbox/ (never moved) until the task is actually finished.
    """
    for path in common.read_inbox(worker):
        msg = common.read_json(path)
        if msg and (msg.get("payload") or {}).get("task_id") == task_id:
            return path
    return None


def _find_pending_id_for_task(worker, task_id):
    """Idempotence guard for the gate-filing step: if a pending (or
    already-resolved) item for this exact task already exists, reuse it
    instead of filing a duplicate. Needed because a crash could happen
    after gates.file_pending() succeeds but before state.json records the
    pointer to it.
    """
    for item in gates.list_pending():
        if item.get("requested_by") == worker and (item.get("payload") or {}).get("task_id") == task_id:
            return item["id"]
    resolved_dir = config.OPERATOR_PENDING_RESOLVED_DIR
    if resolved_dir.exists():
        for p in resolved_dir.glob("*.json"):
            item = common.read_json(p)
            if item and item.get("requested_by") == worker and (item.get("payload") or {}).get("task_id") == task_id:
                return item["id"]
    return None


class WorkerContext:
    """Passed to every task handler. Lets a handler re-stamp its own
    worker's heartbeat with a longer interval before doing something
    slow, so the 2.5x staleness window scales with the work instead of
    the worker being falsely flagged stalled mid-task.
    """

    def __init__(self, worker):
        self.worker = worker

    def heartbeat(self, interval_seconds, summary, ok=True):
        common.write_heartbeat(self.worker, ok, interval_seconds, summary)


def _default_handler(payload, ctx):
    return {"noop": True}


def _finish_task(worker, task_id, task, msg_path, task_handlers):
    """Perform the task's work, then record completion in the order that
    keeps recovery unambiguous: state -> idle FIRST, then move the
    message to done/, then report back to the orchestrator. If any step
    after "perform the work" is interrupted, a resume re-runs the whole
    handler (safe, by the handler contract above) rather than guessing
    whether it already ran.
    """
    handler = task_handlers.get(task.get("type"), _default_handler)
    ctx = WorkerContext(worker)
    handler(task.get("payload") or {}, ctx)

    common.write_worker_state(
        worker, phase="idle", current_task_id=None, waiting_on_pending_id=None,
        last_action={"type": "completed_task", "task_id": task_id, "ts": common.now_iso()},
    )
    if msg_path is not None and msg_path.exists():
        common.complete_inbox_message(worker, msg_path)
    common.send_mailbox_message("orchestrator", worker, "task_completed", {"task_id": task_id})


def _handle_task(worker, task_id, task, msg_path, task_handlers):
    """Either perform the task, or -- if its type is gated -- file an
    operator-pending item and stop, leaving the message in inbox/ until
    an operator resolves it.
    """
    action_type = task.get("type")
    gate = gates.is_gated(action_type)
    if gate is None:
        _finish_task(worker, task_id, task, msg_path, task_handlers)
        return

    pending_id = _find_pending_id_for_task(worker, task_id)
    if pending_id is None:
        pending_id = gates.file_pending(
            action_type=action_type,
            requested_by=worker,
            description=f"{worker} wants to perform gated action '{action_type}' for task {task_id}",
            payload={"task_id": task_id, "task_payload": task.get("payload")},
            category=gate.get("category"),
        )
    common.write_worker_state(
        worker, phase="waiting_on_operator", current_task_id=task_id, waiting_on_pending_id=pending_id,
        last_action={"type": "awaiting_operator", "task_id": task_id, "pending_id": pending_id, "ts": common.now_iso()},
    )


def run_cycle(worker: str, interval_seconds: float, task_handlers: dict) -> dict:
    """One worker cycle. See module docstring and the execution plan for
    why the operations within each branch happen in this exact order.
    """
    state = common.read_worker_state(worker)
    phase = state.get("phase")

    if phase == "waiting_on_operator":
        pending_id = state.get("waiting_on_pending_id")
        resolved = gates.get_resolved(pending_id) if pending_id else None
        if resolved is None:
            return {"action": "still_waiting_on_operator", "pending_id": pending_id}
        task_id = state.get("current_task_id")
        task = _read_task(task_id)
        msg_path = _find_inbox_message_for_task(worker, task_id)
        if resolved.get("status") == "approved":
            _finish_task(worker, task_id, task, msg_path, task_handlers)
            return {"action": "gate_approved_completed", "task_id": task_id}
        common.write_worker_state(
            worker, phase="idle", current_task_id=None, waiting_on_pending_id=None,
            last_action={"type": "task_denied", "task_id": task_id, "ts": common.now_iso()},
        )
        if msg_path is not None and msg_path.exists():
            common.complete_inbox_message(worker, msg_path)
        common.send_mailbox_message("orchestrator", worker, "task_failed", {"task_id": task_id, "reason": "operator denied gated action"})
        return {"action": "gate_denied", "task_id": task_id}

    if phase == "in_progress":
        task_id = state.get("current_task_id")
        msg_path = _find_inbox_message_for_task(worker, task_id)
        if msg_path is not None:
            task = _read_task(task_id)
            _handle_task(worker, task_id, task, msg_path, task_handlers)
            return {"action": "resumed_after_claim", "task_id": task_id}
        # The claimed message is gone (already moved to done/) with no
        # corresponding phase flip -- defensive fallback, shouldn't occur
        # under the write-before-flip ordering above, but don't get stuck.
        common.write_worker_state(worker, phase="idle", current_task_id=None, waiting_on_pending_id=None)
        phase = "idle"

    if phase in ("idle", None):
        inbox = common.read_inbox(worker)
        if not inbox:
            return {"action": "idle_no_work"}
        msg_path = inbox[0]
        msg = common.read_json(msg_path)
        if not msg or msg.get("type") != "dispatch_task":
            if msg_path.exists():
                common.complete_inbox_message(worker, msg_path)
            return {"action": "skipped_unknown_message"}
        task_id = (msg.get("payload") or {}).get("task_id")
        task = _read_task(task_id)
        # CLAIM: durably record the claim BEFORE any task work happens and
        # before the message is moved out of inbox -- this is the write
        # that makes kill -9 mid-task recoverable.
        common.write_worker_state(
            worker, phase="in_progress", current_task_id=task_id, waiting_on_pending_id=None,
            last_action={"type": "claimed_task", "task_id": task_id, "ts": common.now_iso()},
        )
        _handle_task(worker, task_id, task, msg_path, task_handlers)
        return {"action": "claimed_and_processed", "task_id": task_id}

    return {"action": "stood_down_noop"}


def run(worker: str, interval_seconds: float, task_handlers: dict, once: bool = False, stand_down: bool = False):
    if stand_down:
        common.write_heartbeat(worker, ok=True, interval_seconds=interval_seconds, summary="stood down intentionally", status="stood_down")
        common.write_worker_state(worker, phase="stood_down", current_task_id=None, waiting_on_pending_id=None,
                                   last_action={"type": "stood_down", "ts": common.now_iso()})
        return {"action": "stood_down"}

    if once:
        result = run_cycle(worker, interval_seconds, task_handlers)
        common.write_heartbeat(worker, ok=True, interval_seconds=interval_seconds, summary=f"cycle: {result['action']}")
        return result

    while True:
        result = run_cycle(worker, interval_seconds, task_handlers)
        common.write_heartbeat(worker, ok=True, interval_seconds=interval_seconds, summary=f"cycle: {result['action']}")
        time.sleep(interval_seconds)


def build_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run a single cycle and exit")
    mode.add_argument("--loop", action="store_true", help="run continuously")
    mode.add_argument("--stand-down", action="store_true", help="write a stood-down heartbeat/state and exit (intentional shutdown, not a crash)")
    parser.add_argument("--interval-seconds", type=float, default=config.DEFAULT_WORKER_INTERVAL_SECONDS)
    return parser
