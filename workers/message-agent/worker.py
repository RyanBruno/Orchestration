#!/usr/bin/env python3
"""The message-management agent: a real (not fixture) worker whose task
handler drives an actual Claude Code session to review, respond to, and
summarize "messages" -- plain Markdown files living in this worker's own
git repository (workers/message-agent/repo/), a different thing entirely
from this system's mailboxes/ worker-to-worker coordination protocol.

Dispatched exactly like examples/fast_worker.py or examples/slow_worker.py:
via scripts/submit_task.py, processed by the orchestrator's normal tick
loop. This file only wires together the generic
orchestrator/agent_runner.py plumbing with this worker's own name and
repository paths -- it contains no task-type-specific logic at all. Which
harness handles which task type is discovered by scanning harnesses/*.json
for entries whose "worker" field is "message-agent", not by any mapping
written here.

Run a single cycle:  python3 workers/message-agent/worker.py --once
Run continuously:     python3 workers/message-agent/worker.py --loop [--interval-seconds N]

See .agent/execplans/agentic-workers-and-message-agent.md for the full
design.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
# workers/message-agent has a hyphen, so it cannot be a dotted package path
# (`workers.message-agent` is not valid Python) -- add this one directory
# directly to sys.path instead, the same accommodation
# .agent/execplans/orchestration-file-based-coordination.md documents for
# every other entry-point script in this repo needing sys.path.insert.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import agent_runner, common  # noqa: E402
from examples import worker_common  # noqa: E402
import magent_config  # noqa: E402

HARNESSES_DIR = REPO_ROOT / "harnesses"


def _load_harnesses_for_this_worker() -> dict:
    """Scan harnesses/*.json for entries belonging to this worker, keyed
    by task_type. No Python mapping of task type -> harness exists
    anywhere in this codebase; this is the one place that mapping is
    derived, purely from reading config files.
    """
    by_task_type = {}
    for path in sorted(HARNESSES_DIR.glob("*.json")):
        harness = common.read_json(path)
        if harness and harness.get("worker") == magent_config.WORKER_NAME:
            by_task_type[harness["task_type"]] = harness
    return by_task_type


def main():
    parser = worker_common.build_arg_parser("Message-management agent worker")
    args = parser.parse_args()

    if args.stand_down:
        common.write_heartbeat(magent_config.WORKER_NAME, ok=True, interval_seconds=args.interval_seconds, summary="stood down intentionally", status="stood_down")
        common.write_worker_state(magent_config.WORKER_NAME, phase="stood_down", current_task_id=None, waiting_on_pending_id=None,
                                   last_action={"type": "stood_down", "ts": common.now_iso()})
        print(json.dumps({"action": "stood_down"}))
        return

    magent_config.ensure_repo_bootstrapped()
    harnesses_by_task_type = _load_harnesses_for_this_worker()

    def _cycle():
        return agent_runner.run_agent_task_cycle(
            worker=magent_config.WORKER_NAME,
            repo_dir=magent_config.REPO_DIR,
            worktrees_dir=magent_config.WORKTREES_DIR,
            harnesses_by_task_type=harnesses_by_task_type,
        )

    if args.once:
        result = _cycle()
        common.write_heartbeat(magent_config.WORKER_NAME, ok=True, interval_seconds=args.interval_seconds, summary=f"cycle: {result['action']}")
        print(json.dumps(result))
        return

    import time
    while True:
        result = _cycle()
        common.write_heartbeat(magent_config.WORKER_NAME, ok=True, interval_seconds=args.interval_seconds, summary=f"cycle: {result['action']}")
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
