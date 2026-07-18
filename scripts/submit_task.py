#!/usr/bin/env python3
"""Enqueue a new task for a worker. The one sanctioned way to add work to
the durable task queue, used by operators and by the validation suite --
neither should hand-craft task files directly.

Example:
  python3 scripts/submit_task.py --worker slow-worker --type dummy_heavy_work --heavy
  python3 scripts/submit_task.py --worker fast-worker --type dummy_work --payload '{"label":"demo"}'
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import tick  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Enqueue a task for a worker")
    parser.add_argument("--worker", required=True, help="target worker name, e.g. slow-worker")
    parser.add_argument("--type", required=True, help="task type, e.g. dummy_work")
    parser.add_argument("--heavy", action="store_true", help="counts against the fan-out cap")
    parser.add_argument("--payload", default="{}", help="JSON object, e.g. '{\"label\":\"demo\"}'")
    args = parser.parse_args()

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        parser.error(f"--payload must be valid JSON: {e}")

    task_id = tick.create_task(args.worker, args.type, heavy=args.heavy, payload=payload)
    print(task_id)


if __name__ == "__main__":
    main()
