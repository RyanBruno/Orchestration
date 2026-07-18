#!/usr/bin/env python3
"""FIXTURE WORKER -- not production code.

An example worker that simulates realistic long-running work (a
configurable sleep, done in short increments) so the heartbeat
re-stamping behavior and kill -9 mid-task recovery can actually be
exercised, not just argued about in prose. Also owns the one "heavy"
example task type (for the fan-out cap validation) and the one gated
example task type (for the human-gates validation).

Task duration is configurable via the SLOW_WORKER_TASK_SECONDS
environment variable (default 45s -- "meaningful" but not painfully long
for a demo; the validation suite overrides it to a few seconds so tests
don't take real minutes).

Run a single cycle:  python3 examples/slow_worker.py --once
Run continuously:     python3 examples/slow_worker.py --loop [--interval-seconds N]
Stand down on purpose: python3 examples/slow_worker.py --stand-down
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))       # for `import worker_common`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `from orchestrator import ...`

import worker_common  # noqa: E402

WORKER_NAME = "slow-worker"

# How much longer than the task itself the re-stamped heartbeat interval
# is: gives headroom so the 2.5x staleness multiplier doesn't false-flag
# right as the task finishes (e.g. if the tick that reads the heartbeat
# lands a moment after task completion but before the next heartbeat).
HEARTBEAT_BUFFER_SECONDS = 10

# Sleep in short chunks (rather than one long time.sleep) so a `kill -9`
# sent mid-task lands during an interruptible wait, matching how real
# long-running work would be checked on periodically rather than blocking
# uninterruptibly the whole time.
SLEEP_CHUNK_SECONDS = 2


def _task_seconds() -> float:
    return float(os.environ.get("SLOW_WORKER_TASK_SECONDS", "45"))


def handle_dummy_heavy_work(payload, ctx):
    task_seconds = _task_seconds()
    # Re-stamp BEFORE starting the long task, with an interval that
    # covers the whole task -- this is what keeps a legitimately busy
    # worker from being falsely flagged stalled (goal 4). Re-stamping
    # AFTER would leave a window, right at the start of the task, where
    # the OLD (short) interval is still on record while the worker is
    # about to go quiet for a long time.
    ctx.heartbeat(
        interval_seconds=task_seconds + HEARTBEAT_BUFFER_SECONDS,
        summary=f"starting long task, expect ~{task_seconds:.0f}s of work",
    )
    remaining = task_seconds
    while remaining > 0:
        chunk = min(SLEEP_CHUNK_SECONDS, remaining)
        time.sleep(chunk)
        remaining -= chunk
    return {"slept_seconds": task_seconds}


def handle_external_notify(payload, ctx):
    # This task type is listed in gates.json, so worker_common never
    # calls this handler until an operator has explicitly approved it.
    # It deliberately does not perform any real external call -- it's a
    # stand-in that proves the gate path end to end without this fixture
    # needing (or being trusted with) a real external integration.
    message = payload.get("message", "(no message provided)")
    return {"would_have_sent": message}


TASK_HANDLERS = {
    "dummy_heavy_work": handle_dummy_heavy_work,
    "external_notify": handle_external_notify,
}


def main():
    parser = worker_common.build_arg_parser("Fixture: long-running example worker")
    args = parser.parse_args()
    result = worker_common.run(
        WORKER_NAME,
        args.interval_seconds,
        TASK_HANDLERS,
        once=args.once,
        stand_down=args.stand_down,
    )
    print(result)


if __name__ == "__main__":
    main()
