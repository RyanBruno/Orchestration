#!/usr/bin/env python3
"""Operator CLI to approve or deny an item in the operator-pending queue.
Thin wrapper over orchestrator.gates.resolve_pending -- the non-dashboard
way to clear a human gate. Same underlying function the dashboard's
Approve/Deny buttons call, so behavior is identical either way.

Example:
  python3 scripts/resolve_pending.py pending-20260718T170520797404-09cb6a approve
  python3 scripts/resolve_pending.py pending-20260718T170520797404-09cb6a deny --note "not authorized yet"
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import gates  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Approve or deny an operator-pending item")
    parser.add_argument("pending_id")
    parser.add_argument("decision", choices=["approve", "deny"])
    parser.add_argument("--note", default="")
    parser.add_argument("--resolved-by", default="operator-cli")
    args = parser.parse_args()

    decision = "approved" if args.decision == "approve" else "denied"
    try:
        item = gates.resolve_pending(args.pending_id, decision, args.resolved_by, args.note)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"{item['id']}: {item['status']}")


if __name__ == "__main__":
    main()
