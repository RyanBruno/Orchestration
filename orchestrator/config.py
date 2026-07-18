"""Shared constants for the orchestration system.

Every path and tunable number used anywhere in this codebase is defined
here and imported, rather than repeated as a literal. See
.agent/execplans/orchestration-file-based-coordination.md for the
reasoning behind each value (Decision Log section).
"""

import os
from pathlib import Path

# The repository root is this file's grandparent directory
# (orchestrator/config.py -> orchestrator/ -> repo root).
REPO_ROOT = Path(__file__).resolve().parent.parent

# ORCH_BASE_DIR lets tests/validation point every durable-state directory
# at a disposable location instead of the repository's real state, without
# changing a single line of orchestrator/worker/dashboard code.
BASE_DIR = Path(os.environ.get("ORCH_BASE_DIR", str(REPO_ROOT))).resolve()

STATE_DIR = BASE_DIR / "state"
MAILBOX_DIR = BASE_DIR / "mailboxes"
HEARTBEAT_DIR = BASE_DIR / "heartbeats"
OPERATOR_PENDING_DIR = BASE_DIR / "operator-pending"
OPERATOR_PENDING_RESOLVED_DIR = OPERATOR_PENDING_DIR / "resolved"
LOG_DIR = BASE_DIR / "logs"

ORCHESTRATOR_STATE_DIR = STATE_DIR / "orchestrator"
TASKS_DIR = ORCHESTRATOR_STATE_DIR / "tasks"
DISPATCH_LOG_PATH = ORCHESTRATOR_STATE_DIR / "dispatch_log.jsonl"
LAST_RENDER_SUMMARY_PATH = ORCHESTRATOR_STATE_DIR / "last_render_summary.json"
ORCHESTRATOR_LOG_PATH = LOG_DIR / "orchestrator.log"

# gates.json is policy configuration, not runtime state, so it always
# lives in the real repository regardless of ORCH_BASE_DIR overrides,
# UNLESS a test explicitly wants to point at a different one (rare; tests
# that need a custom gates file pass gates_path explicitly to
# orchestrator.gates functions instead of relying on this default).
GATES_CONFIG_PATH = REPO_ROOT / "gates.json"

# Orchestrator tick interval, in seconds. 5 minutes: the work this system
# coordinates is expected to run minutes-to-hours, so this cadence
# surfaces new dispatches and operator-pending items promptly relative to
# that timescale without busy-polling or constant log churn on quiet
# ticks. Overridable per-invocation via --interval-seconds.
DEFAULT_TICK_INTERVAL_SECONDS = 300

# Hard, code-enforced cap on how many "heavy" tasks may be
# dispatched/in_progress system-wide at once. A plain constant the
# dispatch code itself refuses to exceed -- not a convention anyone has
# to remember.
MAX_CONCURRENT_HEAVY = 1

# A heartbeat older than this multiple of the worker's own last-declared
# interval_seconds is considered stalled. Applied to the worker's own
# self-declared interval (not a global number) so a worker that re-stamps
# a longer interval before starting slow work raises its own threshold
# ahead of the risk period, per goal 4 of the originating spec.
STALENESS_MULTIPLIER = 2.5

# Default worker cycle interval for the example fixtures when not
# overridden on the command line.
DEFAULT_WORKER_INTERVAL_SECONDS = 5
