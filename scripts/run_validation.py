#!/usr/bin/env python3
"""Validation suite: exercises every item in "Validation and Acceptance"
in .agent/execplans/orchestration-file-based-coordination.md, end to end,
using the real example workers and orchestrator as separate OS processes
(not just in-process function calls) wherever the criterion is actually
about surviving a real process boundary.

Each item runs against its own disposable, isolated ORCH_BASE_DIR (a
fresh temp directory), so items never interfere with each other or with
the repository's own example-fixture state under state/, mailboxes/,
heartbeats/, operator-pending/.

Run: python3 scripts/run_validation.py
Exits 0 if every item passes, 1 otherwise, and prints PASS/FAIL per item.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

results = []  # list of (name, ok, detail)


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f"\n      {detail}" if detail else ""))


def env_for(base, extra=None):
    import os
    e = os.environ.copy()
    e["ORCH_BASE_DIR"] = str(base)
    e.setdefault("SLOW_WORKER_TASK_SECONDS", "6")
    if extra:
        e.update(extra)
    return e


@contextmanager
def isolated_env(extra_env=None):
    base = Path(tempfile.mkdtemp(prefix="orch_validate_"))
    try:
        yield base, env_for(base, extra_env)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run(args, env, timeout=60, check=True):
    p = subprocess.run([PYTHON, *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args} failed (exit {p.returncode}): {p.stderr}")
    return p


def py_eval(code, env, timeout=30):
    """Run inline Python in a fresh subprocess with the given env (so
    ORCH_BASE_DIR is read correctly by orchestrator/config.py at import
    time) and return stdout. Used for every state read/write this script
    needs that doesn't already have a dedicated CLI, since importing
    `orchestrator` directly into THIS long-running process would only
    ever see the ORCH_BASE_DIR from the first import (module-level
    constants are computed once), which would silently break isolation
    between items.
    """
    full_code = f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); {code}"
    p = subprocess.run([PYTHON, "-c", full_code], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"py_eval failed: {p.stderr}")
    return p.stdout.strip()


def read_task_status(task_id, env):
    return py_eval(f"from orchestrator import tick; print(tick.read_task({task_id!r})['status'])", env)


def read_worker_state(worker, env):
    return json.loads(py_eval(f"import json; from orchestrator import common; print(json.dumps(common.read_worker_state({worker!r})))", env))


def read_heartbeat_status(worker, env):
    return json.loads(py_eval(f"import json; from orchestrator import common; print(json.dumps(common.heartbeat_status({worker!r})))", env))


def list_pending(env):
    return json.loads(py_eval("import json; from orchestrator import gates; print(json.dumps(gates.list_pending()))", env))


def get_resolved(pending_id, env):
    return json.loads(py_eval(f"import json; from orchestrator import gates; print(json.dumps(gates.get_resolved({pending_id!r})))", env))


def create_task(worker, task_type, env, heavy=False, payload=None):
    payload = payload or {}
    return py_eval(
        f"from orchestrator import tick; print(tick.create_task({worker!r}, {task_type!r}, heavy={heavy!r}, payload={payload!r}))",
        env,
    )


# --- Item 1: kill -9 mid-task, no double-execution, no lost work -------

def item1_kill_recover():
    with isolated_env({"SLOW_WORKER_TASK_SECONDS": "8"}) as (base, env):
        task_id = create_task("slow-worker", "dummy_heavy_work", env, heavy=True)
        run(["orchestrator/tick.py", "--once"], env)

        proc = subprocess.Popen([PYTHON, "examples/slow_worker.py", "--once"], cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)  # let it claim the task and get partway into the sleep
        mid_state = read_worker_state("slow-worker", env)

        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)

        run(["examples/slow_worker.py", "--once"], env)  # fresh process, should resume+complete
        run(["orchestrator/tick.py", "--once"], env)      # process the task_completed report

        done_files = list((base / "mailboxes" / "slow-worker" / "done").glob("*dispatch_task.json"))
        task_status = read_task_status(task_id, env)

        ok = (
            mid_state.get("phase") == "in_progress"
            and mid_state.get("current_task_id") == task_id
            and len(done_files) == 1
            and task_status == "done"
        )
        record(
            "1. kill -9 mid-task: no double-execution, no lost work",
            ok,
            f"mid-kill phase={mid_state.get('phase')!r}, done_files={len(done_files)}, final task status={task_status!r}",
        )


# --- Item 2: concurrent inbox writers, no corruption, order preserved --

def item2_concurrent_writes():
    with isolated_env() as (base, env):
        sender_code = (
            "import sys; sys.path.insert(0, {repo!r}); from orchestrator import common\n"
            "for i in range(25):\n"
            "    common.send_mailbox_message('inbox-target', {sender!r}, 'ping', {{'sender': {sender!r}, 'seq': i}})\n"
        )
        procs = []
        for sender in ("sender-a", "sender-b"):
            code = sender_code.format(repo=str(REPO_ROOT), sender=sender)
            procs.append(subprocess.Popen([PYTHON, "-c", code], cwd=REPO_ROOT, env=env))
        for p in procs:
            rc = p.wait(timeout=30)
            if rc != 0:
                raise RuntimeError(f"concurrent sender exited {rc}")

        inbox_dir = base / "mailboxes" / "inbox-target" / "inbox"
        files = sorted(inbox_dir.glob("*.json"))
        corrupt = []
        parsed = []
        for f in files:
            try:
                parsed.append(json.loads(f.read_text()))
            except Exception:
                corrupt.append(f.name)

        order_ok = True
        for sender in ("sender-a", "sender-b"):
            seqs = [m["payload"]["seq"] for m in parsed if m["payload"]["sender"] == sender]
            if seqs != sorted(seqs) or len(seqs) != 25:
                order_ok = False

        ok = len(files) == 50 and not corrupt and order_ok
        record(
            "2. concurrent inbox writes: no corruption, order preserved",
            ok,
            f"files={len(files)}/50, corrupt={corrupt}, order_ok={order_ok}",
        )


# --- Item 3: orchestrator crash between dispatch-intent and status-flip

def item3_dispatch_crash_recovery():
    with isolated_env() as (base, env):
        task_id = create_task("fast-worker", "dummy_work", env)
        # Simulate a crash immediately after logging intent, before the
        # send or the status flip -- write the intent line directly and
        # stop, exactly what a killed orchestrator would leave behind.
        msg_id = py_eval(
            "from orchestrator import common, config\n"
            f"msg_id = common.ts_compact() + '-' + common.rand6() + '-dispatch_task'\n"
            f"common.append_jsonl(config.DISPATCH_LOG_PATH, {{'ts': common.now_iso(), 'phase': 'intent', "
            f"'task_id': {task_id!r}, 'worker': 'fast-worker', 'message_id': msg_id}})\n"
            "print(msg_id)",
            env,
        )
        before_status = read_task_status(task_id, env)

        run(["orchestrator/tick.py", "--once"], env)  # a normal, un-crashed tick should recover this

        after_status = read_task_status(task_id, env)
        matching = list((base / "mailboxes" / "fast-worker" / "inbox").glob(f"*{msg_id}*"))
        matching += list((base / "mailboxes" / "fast-worker" / "done").glob(f"*{msg_id}*"))

        ok = before_status == "pending" and after_status == "dispatched" and len(matching) == 1
        record(
            "3. orchestrator crash between dispatch-intent and flip: recovers exactly once",
            ok,
            f"before={before_status!r}, after={after_status!r}, matching dispatch messages={len(matching)} (want 1)",
        )


# --- Item 4: heartbeat behavior -----------------------------------------

def item4a_restamp_no_false_stall():
    task_secs = 8
    with isolated_env({"SLOW_WORKER_TASK_SECONDS": str(task_secs)}) as (base, env):
        create_task("slow-worker", "dummy_heavy_work", env, heavy=True)
        run(["orchestrator/tick.py", "--once"], env)

        proc = subprocess.Popen([PYTHON, "examples/slow_worker.py", "--once"], cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(task_secs / 2)
        hb = read_heartbeat_status("slow-worker", env)
        proc.wait(timeout=30)

        interval = (hb.get("heartbeat") or {}).get("interval_seconds") or 0
        ok = hb.get("found") and hb.get("is_stalled") is False and interval >= task_secs
        record(
            "4a. heartbeat re-stamped before long task is not falsely flagged stalled mid-task",
            ok,
            f"is_stalled={hb.get('is_stalled')}, re-stamped interval_seconds={interval} (task took {task_secs}s)",
        )


def item4b_silence_detected_stalled():
    with isolated_env() as (base, env):
        py_eval("from orchestrator import common; common.write_heartbeat('quiet-worker', True, 2, 'one heartbeat then silence')", env)
        time.sleep(2.5 * 2 + 1.5)  # past the 2.5x threshold for a 2s declared interval
        hb = read_heartbeat_status("quiet-worker", env)
        ok = hb.get("is_stalled") is True
        record("4b. silence past 2.5x declared interval is flagged stalled", ok, f"hb={hb}")


def item4c_stand_down_not_stalled():
    with isolated_env() as (base, env):
        run(["examples/fast_worker.py", "--stand-down"], env)
        hb = read_heartbeat_status("fast-worker", env)
        ok = (hb.get("heartbeat") or {}).get("status") == "stood_down" and hb.get("is_stalled") is False
        record("4c. intentional stand-down reads as idle, not stalled", ok, f"hb={hb}")


# --- Item 5: fan-out cap enforced by code --------------------------------

def item5_fanout_cap():
    with isolated_env({"SLOW_WORKER_TASK_SECONDS": "6"}) as (base, env):
        t1 = create_task("slow-worker", "dummy_heavy_work", env, heavy=True)
        t2 = create_task("slow-worker", "dummy_heavy_work", env, heavy=True)
        run(["orchestrator/tick.py", "--once"], env)
        s1, s2 = read_task_status(t1, env), read_task_status(t2, env)
        ok = {s1, s2} == {"dispatched", "pending"}
        record(
            "5. fan-out cap actually refuses a second concurrent heavy dispatch",
            ok,
            f"task1={s1!r}, task2={s2!r} (want exactly one dispatched, one still pending)",
        )


# --- Item 6: gated action -> operator-pending -> resolve -> proceed ----

def item6_gate_end_to_end():
    with isolated_env({"SLOW_WORKER_TASK_SECONDS": "3"}) as (base, env):
        task_id = create_task("slow-worker", "external_notify", env, payload={"message": "validation demo"})
        run(["orchestrator/tick.py", "--once"], env)
        run(["examples/slow_worker.py", "--once"], env)

        state = read_worker_state("slow-worker", env)
        pending = list_pending(env)
        gated_correctly = (
            state.get("phase") == "waiting_on_operator"
            and len(pending) == 1
            and pending[0]["action_type"] == "external_notify"
            and pending[0]["payload"]["task_id"] == task_id
        )
        pending_id = pending[0]["id"] if pending else None

        if pending_id:
            run(["scripts/resolve_pending.py", pending_id, "approve", "--note", "validation suite approval"], env)
        run(["examples/slow_worker.py", "--once"], env)  # should notice the resolution and complete

        state_after = read_worker_state("slow-worker", env)
        resolved = get_resolved(pending_id, env) if pending_id else None
        pending_after = list_pending(env)

        ok = (
            gated_correctly
            and state_after.get("phase") == "idle"
            and resolved is not None
            and resolved.get("status") == "approved"
            and pending_after == []
        )
        record(
            "6. gated action lands in operator-pending, resurfaces, resolves and proceeds",
            ok,
            f"gated_correctly={gated_correctly}, phase_after_approval={state_after.get('phase')!r}, "
            f"still_pending_after_resolve={len(pending_after)}",
        )


# --- Item 7: dashboard reflects live disk state, no server-side cache --

def item7_dashboard_live():
    with isolated_env() as (base, env):
        py_eval("from orchestrator import common; common.write_heartbeat('probe-worker', True, 5, 'fresh')", env)
        port = 8799
        server = subprocess.Popen(
            [PYTHON, "dashboard/server.py", "--port", str(port)],
            cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(1)

            def fetch():
                with urllib.request.urlopen(f"http://localhost:{port}/api/snapshot", timeout=5) as r:
                    return json.loads(r.read())

            snap1 = fetch()
            w1 = next((w for w in snap1["workers"] if w["worker"] == "probe-worker"), None)
            ok1 = w1 is not None and w1["heartbeat"]["is_stalled"] is False

            # Mutate disk DIRECTLY -- the dashboard server process is left
            # completely alone, not restarted, not signaled.
            py_eval(
                "import json\n"
                "from orchestrator import config\n"
                "p = config.HEARTBEAT_DIR / 'probe-worker' / 'heartbeat.json'\n"
                "data = json.loads(p.read_text())\n"
                "data['ts'] = '2020-01-01T00:00:00+00:00'\n"
                "p.write_text(json.dumps(data))",
                env,
            )
            snap2 = fetch()
            w2 = next((w for w in snap2["workers"] if w["worker"] == "probe-worker"), None)
            ok2 = w2 is not None and w2["heartbeat"]["is_stalled"] is True

            ok = ok1 and ok2
            record(
                "7. dashboard reflects a direct disk mutation without any server restart",
                ok,
                f"before mutation is_stalled={w1['heartbeat']['is_stalled'] if w1 else None} (want False), "
                f"after mutation is_stalled={w2['heartbeat']['is_stalled'] if w2 else None} (want True)",
            )
        finally:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


# --- Items 8-11: agent-backed workers (.agent/execplans/agentic-workers-
# and-message-agent.md). These items invoke a real `claude` CLI session
# with real API calls -- unlike items 1-7 above, they cost real money and
# take real wall-clock time (seconds, not milliseconds). Every assertion
# below is checked against real subprocess output, never read from the
# code or assumed.

WORKER_PY = "workers/message-agent/worker.py"


@contextmanager
def message_agent_env():
    """Like isolated_env, but also points MESSAGE_AGENT_REPO_DIR at a
    fresh, not-yet-existing temp path. No repo needs to be pre-copied --
    magent_config.ensure_repo_bootstrapped() materializes it from the
    real, tracked workers/message-agent/seed/ the first time the worker
    runs, which is exactly the same bootstrap path production use takes.
    """
    base = Path(tempfile.mkdtemp(prefix="orch_validate_magent_"))
    msg_repo = base / "message-agent-repo"
    try:
        yield base, msg_repo, env_for(base, {"MESSAGE_AGENT_REPO_DIR": str(msg_repo)})
    finally:
        shutil.rmtree(base, ignore_errors=True)


def read_task(task_id, env):
    return json.loads(py_eval(f"import json; from orchestrator import tick; print(json.dumps(tick.read_task({task_id!r})))", env))


def _kill_agent_session(session_id: str):
    """SIGKILL the whole process group of any live `claude` process whose
    argv contains this exact session id. Reimplemented here (rather than
    importing orchestrator.agent_runner.kill_agent_session) for the same
    reason every other state read in this script goes through a fresh
    subprocess via py_eval: importing an orchestrator module directly into
    this long-running validation process would permanently cache whatever
    ORCH_BASE_DIR/MESSAGE_AGENT_REPO_DIR was set at the moment of that
    first import, for the rest of the process's life, silently breaking
    isolation between items.
    """
    out = subprocess.run(["pgrep", "-f", session_id], capture_output=True, text=True)
    pids = [int(p) for p in out.stdout.split() if p.strip()]
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return pids


def _session_alive(session_id: str) -> bool:
    out = subprocess.run(["pgrep", "-f", session_id], capture_output=True, text=True)
    return bool(out.stdout.strip())


def _run_worker_bg(env):
    return subprocess.Popen(
        [PYTHON, WORKER_PY, "--once"], cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _run_worker_once(env, timeout=120):
    p = run([WORKER_PY, "--once"], env, timeout=timeout)
    return json.loads(p.stdout.strip().splitlines()[-1])


def _run_worker_until_terminal(env, max_calls=4, timeout=120):
    """Keep calling `worker.py --once` until a cycle reaches a terminal-
    ish action (awaiting merge, gave up, or nothing to do) or max_calls is
    exhausted. A single external cycle is not guaranteed to finish a
    multi-turn agent task -- a real deployment runs --loop, cycling
    continuously, so this mirrors that instead of assuming one call
    suffices. Returns the last result.
    """
    result = {}
    for _ in range(max_calls):
        result = _run_worker_once(env, timeout=timeout)
        if result.get("action") in ("agent_completed_awaiting_merge", "agent_gave_up", "idle_no_work"):
            break
    return result


def _item8_kill_recover(msg_repo, env):
    """Goal 2: kill -9 mid-agent-session, resume not restart -- proven
    against a live session, not argued in prose. Dispatched through the
    single generic handle_request task type/harness introduced by
    .agent/execplans/message-agent-skills-dispatch.md; the recovered
    session must still have correctly selected the review skill.
    """
    task_id = create_task(
        "message-agent", "handle_request", env,
        payload={"request": "Review every message currently in the inbox and produce a triage report."},
    )
    run(["orchestrator/tick.py", "--once"], env)

    proc = _run_worker_bg(env)
    # Poll for the write-before-flip moment: agent_session_id durably
    # recorded on the task file.
    session_id = None
    for _ in range(40):  # up to ~20s
        t = read_task(task_id, env)
        if t.get("agent_session_id"):
            session_id = t["agent_session_id"]
            mid_state = read_worker_state("message-agent", env)
            break
        time.sleep(0.5)
    else:
        mid_state = read_worker_state("message-agent", env)

    session_captured_before_finish = bool(session_id) and mid_state.get("phase") == "in_progress"

    # Give the CLI a couple of real seconds to actually persist the
    # session's first turn before killing it. Killing at the very
    # instant the id is written can land BEFORE Claude Code's own
    # on-disk session store has written anything for that id at all,
    # which makes --resume correctly (and unavoidably) fail with "No
    # conversation found" -- a real, narrower crash window this plan's
    # agent_runner.py separately handles via a safe fresh-restart
    # fallback (see its session_never_persisted property), but a
    # different thing than what THIS test is trying to prove. This test
    # is specifically about the goal 2 window that matters: an agent
    # session that has already started doing real work, killed and then
    # truly resumed -- not "a session that was never created."
    time.sleep(2)

    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    if session_id:
        _kill_agent_session(session_id)
    time.sleep(0.5)
    session_confirmed_dead = not _session_alive(session_id) if session_id else False

    # This one call is the actual proof of resumption -- captured
    # separately from any further cycles below, since it's the call
    # whose argv is --resume <session_id>, not --session-id.
    first_resume_result = _run_worker_once(env) if session_id else {}
    resumed_same_session = bool(session_id) and first_resume_result.get("agent_session_id") == session_id

    # A single resume call is not guaranteed to finish the whole task --
    # this kill test deliberately kills at the earliest possible moment
    # (the instant the session id is observed), which can leave the
    # resumed agent needing more than one turn to actually produce the
    # artifact. Keep cycling (a real deployment runs --loop, not one
    # external call) up to the same MAX_AGENT_ATTEMPTS-driven cap the
    # handler itself uses.
    latest_result = first_resume_result
    for _ in range(3):
        if latest_result.get("action") in ("agent_completed_awaiting_merge", "agent_gave_up"):
            break
        latest_result = _run_worker_once(env)
    reached_merge_wait = latest_result.get("action") == "agent_completed_awaiting_merge"

    pending = list_pending(env)
    pending_id = pending[0]["id"] if pending else None
    on_main_before_approval = (msg_repo / "outputs" / f"{task_id}.md").exists()

    if pending_id:
        run(["scripts/resolve_pending.py", pending_id, "approve", "--note", "validation suite approval"], env)
    _run_worker_until_terminal(env)
    run(["orchestrator/tick.py", "--once"], env)

    artifact = msg_repo / "outputs" / f"{task_id}.md"
    on_main_after_approval = artifact.exists() and artifact.stat().st_size > 0
    first_line = artifact.read_text().splitlines()[0] if on_main_after_approval else ""
    recovered_correct_skill = first_line == "Handled by: message-review skill"
    final_status = read_task_status(task_id, env)

    ok = (
        session_captured_before_finish
        and session_confirmed_dead
        and resumed_same_session
        and reached_merge_wait
        and not on_main_before_approval
        and on_main_after_approval
        and recovered_correct_skill
        and final_status == "done"
    )
    record(
        "8. agent-backed kill -9 mid-session: resumes the same session, not a restart",
        ok,
        f"session_captured_before_finish={session_captured_before_finish}, session_confirmed_dead={session_confirmed_dead}, "
        f"resumed_same_session={resumed_same_session} (orig={session_id!r}, first_resume={first_resume_result.get('agent_session_id')!r}), "
        f"reached_merge_wait={reached_merge_wait} (last_action={latest_result.get('action')!r}), "
        f"on_main_before={on_main_before_approval} (want False), on_main_after={on_main_after_approval} (want True), "
        f"recovered_correct_skill={recovered_correct_skill} (first_line={first_line!r}), final_status={final_status!r}",
    )


def _item9_three_skills_fire(msg_repo, env):
    """Goal 6, the direct proof this plan exists to deliver: three
    free-text requests, all dispatched through the IDENTICAL task type
    (handle_request) and the IDENTICAL harness
    (harnesses/message-agent.json), must each be handled by a different
    skill -- confirmed from the actual outputs/<task_id>.md content and
    its "Handled by:" line for each, never asserted from reading the
    skill descriptions.
    """
    cases = [
        ("review", "Review everything currently in the inbox and flag anything urgent.",
         "Handled by: message-review skill"),
        ("respond", "Draft a reply to Dave's message about rescheduling Friday's meeting.",
         "Handled by: message-respond skill"),
        ("summarize", "Summarize everything that came in between July 12 and July 14, 2026.",
         "Handled by: message-summarize skill"),
    ]
    content_checks = {
        "review": lambda c: sum(name in c.lower() for name in ("alice", "bob", "carol", "dave")) >= 2,
        "respond": lambda c: "reschedul" in c.lower() or "friday" in c.lower(),
        "summarize": lambda c: ("bob" in c.lower() or "carol" in c.lower() or "outage" in c.lower() or "newsletter" in c.lower())
        and "dave" not in c.lower() and "alice" not in c.lower(),
    }

    results = {}
    for label, request_text, expected_header in cases:
        task_id = create_task("message-agent", "handle_request", env, payload={"request": request_text})
        run(["orchestrator/tick.py", "--once"], env)
        cycle_result = _run_worker_until_terminal(env)

        pending = list_pending(env)
        pending_id = pending[0]["id"] if pending else None
        on_main_before_approval = (msg_repo / "outputs" / f"{task_id}.md").exists()
        if pending_id:
            run(["scripts/resolve_pending.py", pending_id, "approve", "--note", "validation suite approval"], env)
        _run_worker_until_terminal(env)
        run(["orchestrator/tick.py", "--once"], env)

        artifact = msg_repo / "outputs" / f"{task_id}.md"
        exists_nonempty = artifact.exists() and artifact.stat().st_size > 0
        content = artifact.read_text() if exists_nonempty else ""
        first_line = content.splitlines()[0] if content else ""
        results[label] = {
            "reached_merge_wait": cycle_result.get("action") == "agent_completed_awaiting_merge",
            "on_main_before_approval": on_main_before_approval,
            "exists_nonempty": exists_nonempty,
            "content": content,
            "header_matches": first_line == expected_header,
            "first_line": first_line,
            "expected_header": expected_header,
            "content_ok": exists_nonempty and content_checks[label](content),
            "final_status": read_task_status(task_id, env),
        }

    ok = all(
        r["reached_merge_wait"] and not r["on_main_before_approval"] and r["exists_nonempty"]
        and r["header_matches"] and r["content_ok"] and r["final_status"] == "done"
        for r in results.values()
    )
    record(
        "9. three free-text requests through the identical task type/harness fire three different skills",
        ok,
        "; ".join(
            f"{label}: header={r['first_line']!r} (want {r['expected_header']!r}), "
            f"content_ok={r['content_ok']}, on_main_before={r['on_main_before_approval']} (want False), "
            f"status={r['final_status']!r}"
            for label, r in results.items()
        ),
    )


def _item10_harness_and_skill_removal():
    """Goal 1 (a single, capability-agnostic artifact_path_template,
    inspected directly rather than assumed) and the acceptance criterion
    for goal 2's "no duplicated Python" property.

    This item originally tried to prove "removing a SKILL.md changes what
    the agent can do" by actually removing message-respond's SKILL.md and
    running a live agent session against a respond-worded request. That
    live test found a real problem (see the execution plan's Surprises &
    Discoveries): the agent still produced a "Handled by: message-respond
    skill" header and a perfectly good draft reply even with the skill
    file gone, because CLAUDE.md's own plain-language paragraph already
    names all three capabilities, and drafting a reply is well within a
    general-purpose model's native ability -- it doesn't actually need
    the skill's specific step-by-step instructions to do a recognizable
    version of the task. That makes "did behavior visibly change"
    fundamentally unreliable as a test of "is the procedure duplicated in
    Python," for a sufficiently capable model.

    The claim goal 2 actually makes ("adding a fourth capability should
    mean adding one new SKILL.md, not touching Python") is a claim about
    WHERE the procedure text lives, not about what the model can improvise
    without it -- and that is a deterministic, static, non-flaky thing to
    check directly: each skill's own distinctive procedural phrase must
    exist in its SKILL.md and must not be duplicated into any tracked
    Python file anywhere in the repository.
    """
    harness = json.loads((REPO_ROOT / "harnesses" / "message-agent.json").read_text())
    single_generic_path = harness.get("artifact_path_template") == "outputs/{task_id}.md"
    only_one_harness_file = sorted(p.name for p in (REPO_ROOT / "harnesses").glob("*.json")) == ["message-agent.json"]

    skills_dir = REPO_ROOT / "workers" / "message-agent" / "seed" / ".claude" / "skills"
    distinctive_phrases = {
        "message-review": "urgent / needs-response / informational",
        "message-respond": "professional reply",
        "message-summarize": "concise prose summary",
    }
    py_files = [
        p for p in REPO_ROOT.rglob("*.py")
        if ".git" not in p.parts and "worktrees" not in p.parts
        and "message-agent" + os.sep + "repo" not in str(p)
        # this validation script itself necessarily quotes each phrase to
        # check for it -- excluded so the check isn't trivially self-defeating.
        and p.resolve() != Path(__file__).resolve()
    ]

    no_duplicated_logic = True
    detail_bits = []
    for skill_name, phrase in distinctive_phrases.items():
        skill_md_text = (skills_dir / skill_name / "SKILL.md").read_text()
        phrase_in_skill = phrase in skill_md_text
        py_hits = [str(p.relative_to(REPO_ROOT)) for p in py_files if phrase in p.read_text(errors="ignore")]
        this_ok = phrase_in_skill and not py_hits
        no_duplicated_logic = no_duplicated_logic and this_ok
        detail_bits.append(f"{skill_name}: phrase_in_its_SKILL.md={phrase_in_skill}, found_in_py_files={py_hits}")

    ok = single_generic_path and only_one_harness_file and no_duplicated_logic
    record(
        "10. single artifact path inspected directly, and each skill's procedure lives only in its SKILL.md (no duplicated Python)",
        ok,
        f"artifact_path_template={harness.get('artifact_path_template')!r} (want 'outputs/{{task_id}}.md'), "
        f"only_one_harness_file={only_one_harness_file}; " + "; ".join(detail_bits),
    )


def item8_10_message_agent_suite():
    with message_agent_env() as (base, msg_repo, env):
        try:
            _item8_kill_recover(msg_repo, env)
        except Exception as e:
            record("8. agent-backed kill -9 mid-session: resumes the same session, not a restart", False, f"raised {type(e).__name__}: {e}")
        try:
            _item9_three_skills_fire(msg_repo, env)
        except Exception as e:
            record("9. three free-text requests through the identical task type/harness fire three different skills", False, f"raised {type(e).__name__}: {e}")
    try:
        _item10_harness_and_skill_removal()
    except Exception as e:
        record("10. single artifact path inspected directly, and each skill's procedure lives only in its SKILL.md (no duplicated Python)", False, f"raised {type(e).__name__}: {e}")


def item11_base_suite_unaffected():
    """Not a new subprocess run -- items 1-7 already ran earlier in this
    same process and their results are already in `results`. This item
    just checks that none of them failed, which is the acceptance
    criterion: this plan's additions must not have weakened anything the
    base system already guaranteed.
    """
    base_names = {name for name, _, _ in results if (m := re.match(r"\d+", name)) and int(m.group()) <= 7}
    base_failed = [name for name, ok, _ in results if name in base_names and not ok]
    # 7 distinct base acceptance items (1, 2, 3, 4a, 4b, 4c, 5, 6, 7) --
    # item 4 is split into three sub-checks, so 9 result rows map to 7
    # numbered items.
    ok = len(base_names) == 9 and not base_failed
    record(
        "11. existing validation suite (items 1-7) still passes unmodified",
        ok,
        f"base_result_rows_seen={len(base_names)}/9 (7 numbered items, 4 split into 4a/4b/4c), failed={base_failed}",
    )


def main():
    print(f"=== Orchestration validation suite ===\npython: {PYTHON}\nrepo: {REPO_ROOT}\n")
    items = [
        item1_kill_recover,
        item2_concurrent_writes,
        item3_dispatch_crash_recovery,
        item4a_restamp_no_false_stall,
        item4b_silence_detected_stalled,
        item4c_stand_down_not_stalled,
        item5_fanout_cap,
        item6_gate_end_to_end,
        item7_dashboard_live,
        item8_10_message_agent_suite,
        item11_base_suite_unaffected,
    ]
    for fn in items:
        try:
            fn()
        except Exception as e:
            record(fn.__name__, False, f"raised {type(e).__name__}: {e}")

    print()
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"{len(failed)} FAILED / {len(results)} total:")
        for name in failed:
            print(f"  - {name}")
        sys.exit(1)
    print(f"ALL PASS ({len(results)}/{len(results)})")
    sys.exit(0)


if __name__ == "__main__":
    main()
