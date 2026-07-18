"""Small config module for the message-agent worker, parallel in spirit to
orchestrator/config.py but scoped to this one worker's own git repository.

MESSAGE_AGENT_REPO_DIR overrides where that repository lives, exactly the
way ORCH_BASE_DIR overrides the base system's state directories -- so
scripts/run_validation.py's agent-backed items can point this worker at a
disposable temp path instead of accumulating real branches, worktrees, and
commits in the repository's own workers/message-agent/repo/ on every run.

REPO_DIR is a live git repository (its own .git, its own branches, its own
commit history from every task's worktree work) -- a nested .git cannot be
tracked as plain files by THIS repository's own git history (git always
represents a directory containing a .git as one opaque gitlink, regardless
of --separate-git-dir tricks; confirmed empirically before settling on this
design -- see the execution plan's Decision Log). So REPO_DIR itself is
git-ignored by this repository (see .gitignore), and its human-authored
seed content -- CLAUDE.md and the real test message files -- instead lives
in SEED_DIR, which IS tracked normally, since it contains no nested .git.
ensure_repo_bootstrapped() materializes REPO_DIR from SEED_DIR the first
time it's needed, once, idempotently.
"""

import os
import shutil
import subprocess
from pathlib import Path

WORKER_NAME = "message-agent"

SEED_DIR = Path(__file__).resolve().parent / "seed"

_DEFAULT_REPO_DIR = Path(__file__).resolve().parent / "repo"

REPO_DIR = Path(os.environ.get("MESSAGE_AGENT_REPO_DIR", str(_DEFAULT_REPO_DIR))).resolve()

# Sibling of REPO_DIR, not nested inside it -- a linked git worktree cannot
# live inside the main worktree's own tracked path.
WORKTREES_DIR = REPO_DIR.parent / "worktrees"


def ensure_repo_bootstrapped(repo_dir: Path = None, seed_dir: Path = None) -> None:
    """If repo_dir doesn't already exist as a git repository, create it:
    copy the tracked seed content in, git init, one initial commit on
    main. A no-op (checked first, does nothing further) if repo_dir
    already has a .git -- so this is safe to call at the top of every
    worker cycle without ever re-seeding over real accumulated history.
    """
    repo_dir = repo_dir or REPO_DIR
    seed_dir = seed_dir or SEED_DIR
    if (repo_dir / ".git").exists():
        return
    repo_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(seed_dir / "messages", repo_dir / "messages")
    shutil.copy(seed_dir / "CLAUDE.md", repo_dir / "CLAUDE.md")
    for sub in ("reviews", "drafts", "summaries"):
        (repo_dir / sub).mkdir(exist_ok=True)
        (repo_dir / sub / ".gitkeep").touch()

    def _git(*args):
        subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True, text=True)

    _git("init", "-q", "-b", "main")
    _git("add", "-A")
    subprocess.run(
        ["git", "-C", str(repo_dir), "-c", "user.email=message-agent@example.com", "-c", "user.name=message-agent",
         "commit", "-q", "-m", "Bootstrap from workers/message-agent/seed/"],
        check=True, capture_output=True, text=True,
    )
