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
