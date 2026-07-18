"""Shared plumbing for task handlers that drive a real Claude Code agent
session instead of running local Python logic.

Two layers live here:

1. CLI invocation (invoke_agent / resume_agent / the AgentResult they
   return): no knowledge of tasks, workers, or git -- just "run this
   harness-configured session against this prompt in this directory and
   tell me what happened."
2. The per-cycle state machine for an agent-backed worker
   (run_agent_task_cycle and its helpers): claim-before-work, durable
   session-id capture before the agent takes any further action,
   worktree lifecycle, and the merge_branch gate -- the agent-backed
   analogue of what examples/worker_common.py's run_cycle does for the
   toy fixtures.

See .agent/execplans/agentic-workers-and-message-agent.md for the full
design, the live spike findings that shaped it (in particular: a killed
`claude` process can leave orphaned child tool processes running, and a
resumed session can claim a task is finished without actually having
produced its output), and the Idempotence and Recovery section, which
this module's crash-recovery logic implements directly.

CONTRACT for agent-backed task handlers built on this module: unlike the
local-Python handlers in examples/worker_common.py, an agent-backed
handler is NOT safe to blindly re-run in full after a crash. Recovery
instead means: capture the agent session's id durably BEFORE invoking it,
and on any restart, resume that same session id rather than starting a
new one. See run_agent_task_cycle for exactly how this is implemented.
"""

import json
import os
import signal
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from orchestrator import common, gates

MAX_AGENT_ATTEMPTS = 3

# Fixed continuation prompt used for every --resume call, regardless of
# harness. A resume has nothing new to say beyond "keep going" -- and an
# empty prompt is rejected by the CLI (see Surprises & Discoveries in the
# execution plan: "No deferred tool marker found in the resumed session...
# Provide a prompt to continue the conversation.").
RESUME_PROMPT = (
    "Continue exactly where you left off and finish the task described in "
    "your original instructions. If you already produced the expected "
    "output file, verify it exists and is complete; if not, produce it now."
)


# --- CLI invocation layer ---------------------------------------------

@dataclass
class AgentResult:
    session_id: str | None
    ok: bool           # subprocess exited 0 and stdout parsed as JSON
    is_error: bool      # the CLI's own "is_error" field (only meaningful if ok)
    result_text: str
    raw: dict
    stderr_tail: str

    @property
    def session_never_persisted(self) -> bool:
        """True if a --resume call failed because the CLI never actually
        wrote anything for this session id -- possible if a crash lands in
        the narrow window between "we chose and durably recorded this
        session id" and "Claude Code's own on-disk session store has
        persisted its first turn" (found via a live test: killing at the
        earliest possible instant produced exactly this CLI error: "No
        conversation found with session ID: ..."). Safe to treat as
        equivalent to "never started": if the session was never
        persisted, by definition no action was ever taken under it, so
        starting fresh with a new id cannot duplicate any side effect.
        """
        return not self.ok and "No conversation found" in self.stderr_tail


def _harness_argv(harness: dict) -> list:
    """Flags derived from a harness config, shared by fresh-start and
    resume invocations alike.
    """
    argv = [
        "claude",
        "--output-format", "json",
        "--permission-mode", harness.get("permission_mode", "bypassPermissions"),
    ]
    allowed_tools = harness.get("allowed_tools")
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    if harness.get("model"):
        argv += ["--model", harness["model"]]
    if harness.get("effort"):
        argv += ["--effort", harness["effort"]]
    if harness.get("max_budget_usd") is not None:
        argv += ["--max-budget-usd", str(harness["max_budget_usd"])]
    return argv


def _run_claude(argv: list, cwd: Path) -> AgentResult:
    """Launch `claude` as a subprocess in its OWN process group
    (start_new_session=True) and wait for it to finish.

    Its own process group matters because a killed `claude` process does
    NOT kill tool subprocesses it already spawned (confirmed by a live
    spike test: an orphaned `sleep ... && echo one > marker` bash child
    kept running and completed after its parent `claude` process was
    already SIGKILL'd). Launching in a fresh process group means a caller
    that needs to guarantee "nothing from this session is still running"
    (this module's own kill-9 validation test, and any future
    timeout/cleanup logic) can kill the whole group with os.killpg,
    rather than just the one PID that was reported.
    """
    proc = subprocess.Popen(
        argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    stdout, stderr = proc.communicate()
    stderr_tail = stderr[-2000:] if stderr else ""

    if proc.returncode != 0:
        return AgentResult(session_id=None, ok=False, is_error=True, result_text="", raw={}, stderr_tail=stderr_tail)

    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError:
        return AgentResult(session_id=None, ok=False, is_error=True, result_text="", raw={}, stderr_tail=stderr_tail or stdout[-2000:])

    return AgentResult(
        session_id=raw.get("session_id"),
        ok=True,
        is_error=bool(raw.get("is_error")),
        result_text=raw.get("result", ""),
        raw=raw,
        stderr_tail=stderr_tail,
    )


def invoke_agent(harness: dict, prompt: str, cwd: Path, session_id: str) -> AgentResult:
    """Start a fresh session with a CALLER-CHOSEN session id.

    The id must already be durably written to the task's own state (via
    atomic_write_json, same discipline as everywhere else in this
    codebase) BEFORE this function is called -- this function does not
    generate or choose an id itself, precisely so there is no window
    where a session exists but its id isn't yet known to a fresh process
    reading disk after a crash.
    """
    argv = _harness_argv(harness) + ["--session-id", session_id]
    if harness.get("append_system_prompt"):
        argv += ["--append-system-prompt", harness["append_system_prompt"]]
    argv += ["--print", prompt]
    return _run_claude(argv, cwd)


def resume_agent(harness: dict, cwd: Path, session_id: str) -> AgentResult:
    """Resume an existing session by id, rather than starting a new one.

    Used both for genuine crash recovery (the previous process died
    mid-session) and for the "claimed done but artifact missing" retry
    case found in testing -- from this function's caller's point of view
    those are the same situation: an in-progress task with a recorded
    session id and no artifact yet.
    """
    argv = _harness_argv(harness) + ["--resume", session_id]
    if harness.get("append_system_prompt"):
        argv += ["--append-system-prompt", harness["append_system_prompt"]]
    argv += ["--print", RESUME_PROMPT]
    return _run_claude(argv, cwd)


def kill_agent_session(session_id: str) -> int:
    """Find any live `claude` process invoked with this exact session id
    (its argv literally contains "--session-id <id>" or "--resume <id>")
    and SIGKILL its whole process group. Used only by the validation
    suite's kill-9 test, to faithfully simulate a crash that takes down
    the agent session too, not just the worker process driving it -- per
    the orphaned-child spike finding, killing only the worker's PID is
    not sufficient to prove the session itself is really gone. Returns
    the number of processes signaled.
    """
    try:
        out = subprocess.run(["pgrep", "-f", session_id], capture_output=True, text=True)
    except FileNotFoundError:
        return 0
    pids = [int(p) for p in out.stdout.split() if p.strip()]
    killed = 0
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            continue
        except PermissionError:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                continue
    return killed


# --- Per-cycle state machine layer -------------------------------------

def _git(repo_or_worktree: Path, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo_or_worktree), *args],
        capture_output=True, text=True, check=check,
    )


def ensure_worktree(repo_dir: Path, worktrees_dir: Path, task_id: str, branch: str) -> Path:
    """Idempotent: if the worktree already exists (a resume after a
    crash that happened after creation), return it unchanged. Otherwise
    create a fresh linked worktree on a fresh branch off main. Safe to
    call again after any crash, because it never touches an
    already-existing worktree.
    """
    path = worktrees_dir / task_id
    if path.exists():
        return path
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "worktree", "add", str(path), "-b", branch, "main")
    return path


def render_prompt(harness: dict, task: dict) -> str:
    context = dict(task.get("payload") or {})
    context["task_id"] = task["id"]
    context["artifact_path"] = harness["artifact_path_template"].format(task_id=task["id"])
    return harness["user_prompt_template"].format(**context)


def artifact_path_for(harness: dict, task: dict) -> str:
    return harness["artifact_path_template"].format(task_id=task["id"])


def artifact_ready(worktree: Path, artifact_rel_path: str) -> bool:
    """The ONLY signal this module trusts for "did the agent's work
    actually happen." Never the CLI's own result/is_error fields -- a
    live spike test showed a resumed session can reply with a confident
    "FINISHED" while never having actually written its expected output
    file (it saw a side effect from an orphaned child process and
    concluded, incorrectly, that the whole task was done).
    """
    p = worktree / artifact_rel_path
    return p.exists() and p.stat().st_size > 0


def commit_artifact_if_needed(worktree: Path, task_id: str, task_type: str) -> bool:
    """Commit whatever the agent wrote, but only if there's something to
    commit. A clean worktree means an earlier crashed attempt already
    committed this -- returns False and does nothing, so recovery never
    produces a second, duplicate commit for the same task.
    """
    status = _git(worktree, "status", "--porcelain").stdout
    if not status.strip():
        return False
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", f"task {task_id}: {task_type}")
    return True


def _find_pending_id_for_task(worker: str, task_id: str):
    """Idempotence guard for merge-gate filing, mirroring
    examples/worker_common.py's _find_pending_id_for_task exactly: if a
    pending (or already-resolved) merge_branch item for this exact task
    already exists, reuse it instead of filing a duplicate. Needed
    because a crash could happen after gates.file_pending() succeeds but
    before state.json records the pointer to it.
    """
    for item in gates.list_pending():
        if item.get("requested_by") == worker and (item.get("payload") or {}).get("task_id") == task_id:
            return item["id"]
    from orchestrator import config
    resolved_dir = config.OPERATOR_PENDING_RESOLVED_DIR
    if resolved_dir.exists():
        for p in resolved_dir.glob("*.json"):
            item = common.read_json(p)
            if item and item.get("requested_by") == worker and (item.get("payload") or {}).get("task_id") == task_id:
                return item["id"]
    return None


def file_merge_gate_if_needed(worker: str, task: dict) -> str:
    """File (or find the already-filed) merge_branch operator-pending
    item for this task's branch. Reuses the base system's existing
    merge_branch gate entry in gates.json -- no new gate category.
    """
    existing = _find_pending_id_for_task(worker, task["id"])
    if existing:
        return existing
    return gates.file_pending(
        action_type="merge_branch",
        requested_by=worker,
        description=(
            f"{worker} wants to merge branch {task.get('agent_branch')} "
            f"(task {task['id']}, type {task.get('type')}) into main."
        ),
        payload={"task_id": task["id"], "branch": task.get("agent_branch"), "worktree": task.get("agent_worktree_path")},
        category="irreversible",
    )


def perform_merge_or_discard(repo_dir: Path, worktree_path: Path, branch: str, decision: str) -> None:
    """Approved: merge the branch into main (idempotent -- skips the
    merge itself if already merged, e.g. from a prior crashed attempt
    that got this far before dying) then clean up the worktree. Denied:
    discard the worktree and branch without merging -- the deliberate,
    expected outcome of a denial, not an error.
    """
    if decision == "approved":
        already_merged = _git(repo_dir, "merge-base", "--is-ancestor", branch, "main", check=False).returncode == 0
        if not already_merged:
            _git(repo_dir, "checkout", "main")
            _git(repo_dir, "merge", "--no-ff", branch, "-m", f"merge branch {branch}")
        _git(repo_dir, "worktree", "remove", str(worktree_path), check=False)
        _git(repo_dir, "branch", "-D", branch, check=False)
    else:
        _git(repo_dir, "worktree", "remove", "--force", str(worktree_path), check=False)
        _git(repo_dir, "branch", "-D", branch, check=False)


def _find_inbox_message_for_task(worker: str, task_id: str):
    for path in common.read_inbox(worker):
        msg = common.read_json(path)
        if msg and (msg.get("payload") or {}).get("task_id") == task_id:
            return path
    return None


def _finalize(worker: str, task_id: str, msg_path, outcome: str, reason: str = None) -> None:
    """Same three-step completion order examples/worker_common.py's
    _finish_task uses, for the same crash-safety reason: state -> idle
    first, then move the message to done/, then report to the
    orchestrator -- each step safe to redo if interrupted and retried.
    """
    common.write_worker_state(
        worker, phase="idle", current_task_id=None, waiting_on_pending_id=None,
        last_action={"type": f"agent_task_{outcome}", "task_id": task_id, "ts": common.now_iso()},
    )
    if msg_path is not None and msg_path.exists():
        common.complete_inbox_message(worker, msg_path)
    if outcome == "completed":
        common.send_mailbox_message("orchestrator", worker, "task_completed", {"task_id": task_id})
    else:
        common.send_mailbox_message("orchestrator", worker, "task_failed", {"task_id": task_id, "reason": reason})


class WorkerContext:
    """Passed nowhere externally -- kept internal to run_agent_task_cycle,
    but factored out as its own small object so the heartbeat re-stamp
    call reads the same way examples/worker_common.py's WorkerContext
    does: re-stamp BEFORE starting the slow call, with a buffer, exactly
    mirroring examples/slow_worker.py's existing pattern.
    """

    def __init__(self, worker):
        self.worker = worker

    def heartbeat(self, interval_seconds, summary):
        common.write_heartbeat(self.worker, True, interval_seconds, summary)


def run_agent_task_cycle(worker: str, repo_dir: Path, worktrees_dir: Path, harnesses_by_task_type: dict) -> dict:
    """One agent-backed worker cycle. See the execution plan's
    Idempotence and Recovery section for the exhaustive crash-window
    reasoning behind this exact branch structure.
    """
    ctx = WorkerContext(worker)
    state = common.read_worker_state(worker)
    phase = state.get("phase")

    # --- waiting on the merge_branch gate -------------------------------
    if phase == "waiting_on_operator":
        task_id = state.get("current_task_id")
        pending_id = state.get("waiting_on_pending_id")
        resolved = gates.get_resolved(pending_id) if pending_id else None
        if resolved is None:
            return {"action": "still_waiting_on_merge_gate", "task_id": task_id, "pending_id": pending_id}

        from orchestrator import tick
        task = tick.read_task(task_id)
        msg_path = _find_inbox_message_for_task(worker, task_id)
        worktree_path = Path(task["agent_worktree_path"])
        decision = resolved.get("status")
        perform_merge_or_discard(repo_dir, worktree_path, task["agent_branch"], decision)

        if decision == "approved":
            tick.write_task(task_id, status="done", completed_ts=common.now_iso())
            _finalize(worker, task_id, msg_path, "completed")
            return {"action": "merge_approved_completed", "task_id": task_id, "agent_session_id": task.get("agent_session_id")}
        tick.write_task(task_id, status="failed", completed_ts=common.now_iso(), failure_reason="operator denied merge_branch")
        _finalize(worker, task_id, msg_path, "failed", reason="operator denied merge_branch")
        return {"action": "merge_denied_discarded", "task_id": task_id, "agent_session_id": task.get("agent_session_id")}

    # --- claim a fresh dispatch, if idle ---------------------------------
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
        # CLAIM: durably record BEFORE any agent work happens and before
        # the message is moved out of inbox -- this is the write that
        # makes kill -9 mid-session recoverable. Re-bind `state` to the
        # dict write_worker_state returns (not re-read from disk) so the
        # in_progress branch below sees the just-written current_task_id
        # instead of the stale pre-claim snapshot read at the top.
        state = common.write_worker_state(
            worker, phase="in_progress", current_task_id=task_id, waiting_on_pending_id=None,
            last_action={"type": "claimed_agent_task", "task_id": task_id, "ts": common.now_iso()},
        )
        phase = "in_progress"

    # --- in flight: either just claimed, or resuming after a crash ------
    if phase == "in_progress":
        task_id = state.get("current_task_id")
        from orchestrator import tick
        task = tick.read_task(task_id)
        msg_path = _find_inbox_message_for_task(worker, task_id)
        harness = harnesses_by_task_type[task["type"]]

        worktree_path = ensure_worktree(
            repo_dir, worktrees_dir, task_id, task.get("agent_branch") or f"task/{task_id}",
        )
        artifact_rel = artifact_path_for(harness, task)

        if not task.get("agent_worktree_path"):
            tick.write_task(
                task_id,
                agent_worktree_path=str(worktree_path),
                agent_branch=f"task/{task_id}",
                agent_harness=harness["name"],
                agent_attempt=task.get("agent_attempt") or 0,
            )
            task = tick.read_task(task_id)

        if not artifact_ready(worktree_path, artifact_rel):
            attempt = task.get("agent_attempt") or 0
            if task.get("agent_session_id") is None:
                # Never started -- generate and durably write the
                # session id BEFORE invoking the CLI at all, so there is
                # no window where a session exists but its id is unknown.
                session_id = str(uuid.uuid4())
                tick.write_task(task_id, agent_session_id=session_id, agent_started_ts=common.now_iso())
                task["agent_session_id"] = session_id  # keep the in-memory copy current for the result dict below
                ctx.heartbeat(harness["expected_duration_seconds"] + harness["heartbeat_buffer_seconds"], f"running agent session for {task_id}")
                result = invoke_agent(harness, render_prompt(harness, task), worktree_path, session_id)
            else:
                if attempt >= MAX_AGENT_ATTEMPTS:
                    tick.write_task(task_id, status="failed", completed_ts=common.now_iso(), failure_reason=f"agent did not produce expected artifact after {attempt} attempts")
                    _finalize(worker, task_id, msg_path, "failed", reason="agent did not produce expected artifact")
                    return {"action": "agent_gave_up", "task_id": task_id, "agent_session_id": task.get("agent_session_id")}
                ctx.heartbeat(harness["expected_duration_seconds"] + harness["heartbeat_buffer_seconds"], f"resuming agent session for {task_id}")
                result = resume_agent(harness, worktree_path, task["agent_session_id"])
                if result.session_never_persisted:
                    # The crash landed before Claude Code's own on-disk
                    # session store had persisted anything for this id --
                    # by definition no action was ever taken under it, so
                    # starting fresh with a NEW id is safe (nothing to
                    # duplicate) and is the only way to make progress,
                    # since resuming a session that was never created can
                    # never succeed no matter how many times it's retried.
                    # Does not count against MAX_AGENT_ATTEMPTS -- nothing
                    # actually ran.
                    session_id = str(uuid.uuid4())
                    tick.write_task(task_id, agent_session_id=session_id, agent_started_ts=common.now_iso())
                    task["agent_session_id"] = session_id
                    result = invoke_agent(harness, render_prompt(harness, task), worktree_path, session_id)
                else:
                    tick.write_task(task_id, agent_attempt=attempt + 1)

            if not artifact_ready(worktree_path, artifact_rel):
                return {"action": "agent_ran_no_artifact_yet", "task_id": task_id, "agent_session_id": result.session_id or task.get("agent_session_id")}

        # Artifact exists (just produced, or already there from an
        # earlier crashed attempt) -- commit, file the gate, and wait.
        commit_artifact_if_needed(worktree_path, task_id, task["type"])
        pending_id = file_merge_gate_if_needed(worker, task)
        common.write_worker_state(
            worker, phase="waiting_on_operator", current_task_id=task_id, waiting_on_pending_id=pending_id,
            last_action={"type": "awaiting_merge_gate", "task_id": task_id, "pending_id": pending_id, "ts": common.now_iso()},
        )
        return {"action": "agent_completed_awaiting_merge", "task_id": task_id, "agent_session_id": task.get("agent_session_id"), "pending_id": pending_id}

    return {"action": "stood_down_noop"}
