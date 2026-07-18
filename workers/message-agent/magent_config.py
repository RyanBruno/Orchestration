"""Small config module for the message-agent worker, parallel in spirit to
orchestrator/config.py but scoped to this one worker's own git repository.

MESSAGE_AGENT_REPO_DIR overrides where that repository lives, exactly the
way ORCH_BASE_DIR overrides the base system's state directories -- so
scripts/run_validation.py's agent-backed items can point this worker at a
disposable temp copy instead of accumulating real branches, worktrees, and
commits in the repository's own workers/message-agent/repo/ on every run.
"""

import os
from pathlib import Path

WORKER_NAME = "message-agent"

_DEFAULT_REPO_DIR = Path(__file__).resolve().parent / "repo"

REPO_DIR = Path(os.environ.get("MESSAGE_AGENT_REPO_DIR", str(_DEFAULT_REPO_DIR))).resolve()

# Sibling of REPO_DIR, not nested inside it -- a linked git worktree cannot
# live inside the main worktree's own tracked path.
WORKTREES_DIR = REPO_DIR.parent / "worktrees"
