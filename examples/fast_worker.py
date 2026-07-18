#!/usr/bin/env python3
"""FIXTURE WORKER -- not production code.

A near-instant example worker, used to prove the baseline plumbing
(dispatch -> claim -> complete -> report) works end to end quickly, and
as a control case against examples/slow_worker.py for the fan-out cap
and heartbeat-staleness validations.

Run a single cycle:  python3 examples/fast_worker.py --once
Run continuously:     python3 examples/fast_worker.py --loop [--interval-seconds N]
Stand down on purpose: python3 examples/fast_worker.py --stand-down
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))       # for `import worker_common`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `from orchestrator import ...`

import worker_common  # noqa: E402

WORKER_NAME = "fast-worker"


def handle_dummy_work(payload, ctx):
    # Deliberately trivial and side-effect-free-on-repeat: satisfies the
    # "safe to redo in full" contract for task handlers with no extra
    # bookkeeping needed.
    label = payload.get("label", "unlabeled")
    return {"formatted": f"fast-worker did: {label}"}


TASK_HANDLERS = {
    "dummy_work": handle_dummy_work,
}


def main():
    parser = worker_common.build_arg_parser("Fixture: near-instant example worker")
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
