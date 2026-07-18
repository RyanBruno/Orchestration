# Execution Plan: Agent-Backed Workers and the Message-Management Agent

Status: implementation complete, all validation passing, including a real
`kill -9` mid-agent-session recovery test against a live Claude Code
session. Last updated: 2026-07-18.

## Purpose / Big Picture

The base system (`.agent/execplans/orchestration-file-based-coordination.md`)
coordinates workers whose task handlers are local Python functions: format a
string, sleep for a while, and so on. Those handlers are safe to re-run in
full from scratch if the process crashes mid-task, because their work is
trivial and side-effect-free on retry.

This plan extends that system so a task handler can instead drive a **real
Claude Code agent session** — an actual LLM agent that reads files, thinks,
and writes files or runs commands — to do the actual work. An LLM agent
session is not safe to blindly re-run from scratch: it has already taken
actions (maybe written a file, maybe started a git commit) by the time a
crash happens, and starting a fresh session with no memory of that would
either duplicate the work or produce inconsistent output. So the core new
idea this plan introduces is: **a task handler that drives an agent must
capture that agent's session id and write it durably to disk before the
agent takes any further action, so that a crash at any point can be
recovered by resuming the same session — which has its own memory of what
it already did — rather than starting over blind.**

Everything else in this plan exists to make that idea concrete and to prove
it works:

- A **harness** is a small JSON config file describing how one agent session
  should behave: what tools it may use, its permission mode, its system
  prompt, its cost cap. Harnesses are read by shared plumbing code, not
  hardcoded per worker — swapping the harness a task type points at changes
  the agent's behavior with zero Python changes, the same way `gates.json`
  already lets an operator change policy without touching code.
- Any agent session whose job is to edit files in a real git repository does
  that editing in its own isolated `git worktree` on its own branch, so a
  resumed or retried session, or a second concurrent task, can never step on
  another task's in-flight edits. Nothing from that worktree reaches the
  shared `main` branch except through this repo's existing `merge_branch`
  human gate (already defined in `gates.json`) — the same gate mechanism the
  base system built, not a new one.
- The concrete worker built to prove all of this is a **message-management
  agent**: a real (not fixture) worker whose job is to review, respond to,
  and summarize "messages" — plain files living in that worker's own git
  repository, a distinct thing from this system's `mailboxes/` protocol,
  which is for worker-to-worker coordination messages, not business content.
  This worker is dispatched exactly like `fast-worker` or `slow-worker`: via
  `scripts/submit_task.py`, picked up by the orchestrator's normal tick loop.

Nothing about the orchestrator's tick loop, the mailbox protocol, the
heartbeat mechanism, or `gates.json` changes. This plan is purely additive:
new shared plumbing, new harness configs, one new worker.

## Progress

- [x] 2026-07-18 — Read `AGENTS.md`, `.agent/PLANS.md`, the base execution
      plan in full, `orchestrator/common.py`, `orchestrator/tick.py`,
      `orchestrator/config.py`, `orchestrator/gates.py`,
      `examples/worker_common.py`, and `gates.json`.
- [x] 2026-07-18 — Confirmed the environment: `claude` CLI v2.1.211 is
      installed and already authenticated (no `ANTHROPIC_API_KEY` needed —
      billed a live call successfully). `claude_agent_sdk` is NOT installed
      in Python. This determined the SDK-vs-CLI decision below.
- [x] 2026-07-18 — Ran live spike tests against the real `claude` CLI (not
      simulated) to determine the exact plumbing shape before designing it:
      pre-chosen `--session-id`, `--resume` with real memory continuity, and
      a real `kill -9` of a live tool-using session. See Surprises &
      Discoveries — these spikes surfaced two real behaviors (orphaned child
      processes, and the agent's tendency to claim completion without
      finishing) that directly shaped the recovery design below.
- [x] 2026-07-18 — This plan authored.
- [x] 2026-07-18 — `orchestrator/agent_runner.py` built: CLI invocation
      (own-process-group subprocess launch, JSON result parsing) and the
      agent-backed worker cycle scaffolding (claim, session-id
      write-before-launch, worktree lifecycle, artifact-check-before-trust,
      merge_branch gate filing/resolution, finalize). Smoke-tested directly.
- [x] 2026-07-18 — `harnesses/message-review.json`,
      `harnesses/message-respond.json`, `harnesses/message-summarize.json`
      written.
- [x] 2026-07-18 — `workers/message-agent/` built: git-initialized `repo/`
      with `messages/`, `reviews/`, `drafts/`, `summaries/`, `CLAUDE.md`;
      `worker.py` entrypoint.
- [x] 2026-07-18 — Real message files created for testing; all three task
      types (`review_messages`, `draft_response`, `summarize_period`)
      dispatched through `scripts/submit_task.py` → orchestrator tick →
      message-agent worker cycle, and produced real, inspected output files
      merged to `main` after gate approval.
- [x] 2026-07-18 — Real `kill -9` mid-agent-session test added to
      `scripts/run_validation.py` and run against a live session: confirmed
      session id resumption, not restart.
- [x] 2026-07-18 — Two-harness differing-behavior test added and run:
      `message-respond` (Read+Write only) vs `message-review`/
      `message-summarize` (Read+Write+Glob+Grep), different system prompts,
      demonstrably different output artifacts, same handler code path.
- [x] 2026-07-18 — Worktree isolation + `merge_branch` gate end-to-end test
      added and run: edits confirmed absent from `main` until
      `scripts/resolve_pending.py <id> approve` is run.
- [x] 2026-07-18 — `scripts/run_validation.py`'s original 7 items (now
      renumbered 1–7) still pass unmodified; 4 new items appended, all
      passing.
- [x] 2026-07-18 — Outcomes & Retrospective written.

## Surprises & Discoveries

- **Pre-chosen `--session-id` is better than "capture after start."** The
  originating prompt describes capturing a session id "the moment an agent
  session starts... before the agent takes any further action." The
  `claude` CLI's `--session-id <uuid>` flag (verified: `claude -p "..."
  --session-id "$UUID"` returns that exact same id as `session_id` in its
  JSON result) lets the caller choose the id *before* the process is even
  launched. This is strictly better than capturing an id the agent
  generates: there is no window at all between "session exists" and "we
  know its id," because we write the id to the task file before running
  `claude` at all, not after. This became the core of the write-before-flip
  step in `orchestrator/agent_runner.py`.
- **`kill -9` on the `claude` CLI process does not kill its child tool
  processes.** Spike test: started `claude -p` with a prompt instructing it
  to run `sleep 15 && echo one > marker1.txt` via the Bash tool, then run
  the Write tool to create `marker2.txt`, then reply. Waited 6 seconds
  (mid-sleep), `kill -9`'d the `claude` process's PID directly. The `claude`
  process died immediately (confirmed via `ps -p <pid>`), but the orphaned
  `sleep 15 && echo one > marker1.txt` bash subprocess it had spawned kept
  running independently and created `marker1.txt` *after* the parent was
  already dead — `marker2.txt` (the step that required the now-dead `claude`
  process to issue a Write tool call) correctly never appeared. This proves
  the crash was real (no further LLM-driven action happened) but also
  proves that a naive `kill -9` on just the reported PID is not sufficient
  to guarantee "nothing from this task's tool use is still running" in
  general. **Consequence for the design:** `agent_runner.py` launches the
  `claude` subprocess with `start_new_session=True` (its own process group),
  and the kill-9 validation test kills the whole process group
  (`os.killpg`), not just the leader PID, so the test actually exercises
  "everything this task was doing stops," matching the spirit of the base
  system's `slow_worker.py` kill test. In production use this codebase does
  not attempt to force-kill an agent's in-flight tool subprocesses on a
  normal crash-and-resume cycle (there is no supervisor killing workers on
  purpose here) — this only matters for constructing a faithful test.
- **A resumed session can claim success without having actually finished.**
  Continuing the spike above: after the kill, `claude -p "Continue exactly
  where you left off and finish the task." --resume "$UUID" ...` returned
  `result: "FINISHED"` with `is_error: false` — but `marker2.txt` was never
  actually created. The model appears to have seen `marker1.txt` already
  existed (from the orphaned child process finishing on its own) and
  concluded the whole task was done, skipping the Write step. **This is the
  single most important finding for goal 2's recovery design.** It means a
  task handler must never treat the CLI's textual "success" result as proof
  the task's real work happened. `orchestrator/agent_runner.py` therefore
  never trusts `result`/`is_error` as the completion signal for
  agent-backed tasks — it checks for the actual expected artifact (a
  specific output file, non-empty, in the task's worktree) before treating
  the agent's work as done, and re-prompts (up to a small retry cap) if the
  artifact is missing. This same artifact-first check is also what makes
  crash-window recovery clean (see Idempotence and Recovery): "is the
  artifact already there?" is a single question that correctly handles both
  "the agent already finished before we crashed" and "the agent is still
  mid-work," without needing to distinguish those cases by any other means.
- **Empty `--resume` prompts are rejected.** `claude -p "" --resume <id>`
  fails with "No deferred tool marker found in the resumed session... Provide
  a prompt to continue the conversation." A resume call must always carry a
  short, real continuation instruction. `agent_runner.py`'s resume path
  always sends `"Continue exactly where you left off and finish the task
  described in your original instructions. If you already produced the
  expected output file, verify it exists and is complete; if not, produce
  it now."` — a fixed, harness-independent string, since by definition a
  resume has no new information to add beyond "keep going."
- **`git worktree` checkouts include tracked files like `CLAUDE.md`
  automatically**, since a linked worktree is a checkout of the same
  tracked tree at a given commit — no special handling needed for the
  agent's auto-loaded project context to be present inside a worktree.
- **A directory containing its own `.git` cannot be tracked as plain files
  by an outer repository, even with `--separate-git-dir`.** Hit during
  implementation, not anticipated in the original design: the first
  attempt committed `workers/message-agent/repo/` (already `git init`'d,
  with its seed commits) directly into this repository. `git add` silently
  represented the entire directory as a single opaque gitlink (mode
  `160000`) instead of tracking its files — the CLAUDE.md and message file
  *content* were never actually recorded in this repository's history,
  only a dangling reference to a commit hash nothing else has. Verified
  the obvious-seeming fix (`git init --separate-git-dir=<sibling>`, which
  moves the `.git` metadata out but leaves a small `.git` *file* pointing
  to it) does NOT solve this either: a live empirical test showed the
  outer repo still detects that path as an embedded repository and still
  produces a gitlink, because git's detection is "does this path resolve
  to a repository root," not "does a `.git` directory literally sit here."
  **Fix:** split the concern. `workers/message-agent/seed/` (no nested
  `.git`) holds the actual tracked, human-readable content — `CLAUDE.md`
  and the real test message files — exactly the human-editable artifacts
  goal 6 and the legibility principle care about. `workers/message-agent/
  repo/`, the *live* git repository the worker actually runs worktree/
  branch/commit/merge operations against, is git-ignored entirely (added
  to `.gitignore` alongside `worktrees/`) and materialized from `seed/` by
  `magent_config.ensure_repo_bootstrapped()` the first time it's needed —
  idempotent, a no-op once `repo/.git` already exists, so it never
  overwrites accumulated task history. This is the same "runtime state is
  regenerable from tracked inputs, not itself committed" principle the
  base system already applies to `state/`, `mailboxes/`, `heartbeats/`,
  and `operator-pending/`, just applied one level deeper because this
  particular runtime state happens to itself be a git repository. Caught
  by actually running `git add` and reading its own warning output and
  the resulting index entry, not by reasoning about it in the abstract —
  the same "describe outcomes a person can observe and verify" discipline
  the base plan's own Surprises & Discoveries section models.
- **Two stale-in-memory-dict bugs, caught by re-reading the code before
  ever running it, not by a test failure.** While writing
  `run_agent_task_cycle`, two spots reused a Python `dict` variable after
  the disk state it was read from had already changed: (1) after the
  claim write flips `state.json`'s phase to `in_progress`, the code
  originally kept reading `current_task_id` from the *pre-claim* `state`
  dict captured at the top of the function, which would always be empty
  on a task's very first cycle; (2) after generating a brand-new
  `agent_session_id` and writing it to the task file, the code returned
  `task.get("agent_session_id")` from the in-memory `task` dict, which
  still reflected the pre-write value, so the very first cycle's result
  would always report no session id even though one had just been
  created and used. Both are exactly the kind of "which copy of the state
  is this variable actually holding" mistake this codebase's own
  discipline (re-read or re-bind after every write, never trust an
  in-memory copy across a write it doesn't know about) exists to prevent;
  fixed by re-binding `state` to `write_worker_state`'s own return value
  and by updating `task["agent_session_id"]` immediately after the write
  that sets it, before either variable was next read. Neither bug was
  seen to fail at runtime — they were caught by re-reading the function
  end to end and asking "is every variable read here actually current" —
  but both would have surfaced immediately and confusingly in validation
  item 8, which specifically depends on the returned `agent_session_id`
  being accurate.
- With those two fixes applied, the first real (non-spike) run of the
  full dispatch → agent session → worktree isolation → merge gate →
  approve → merge → cleanup pipeline succeeded on its first attempt.

## Decision Log

- **Headless CLI subprocess (`claude -p`), not the Claude Agent SDK.** The
  environment has the `claude` CLI (v2.1.211) already installed and
  authenticated, verified with a live call. `claude_agent_sdk` is not
  installed in Python, and adding it would be a new third-party dependency
  for a repository whose Decision Log already chose "standard library
  only" for the base system. The CLI's `-p`/`--output-format json` mode
  gives a single, parseable JSON result using only `subprocess` and `json`
  from the standard library; its `--session-id` and `--resume` flags give
  exactly the durable-session-id primitive goal 2 needs, with no SDK-level
  session object to keep alive across a process restart (which would
  defeat the whole point — nothing here can depend on anything living only
  in a running process's memory). The CLI was sufficient for every
  requirement in this plan; the SDK was never needed.
- **One shared module, `orchestrator/agent_runner.py`**, not two. The
  originating prompt's suggested layout names this one file. Rather than
  splitting "invoke the CLI" and "run one agent-backed worker cycle" into
  two modules, both live here: a low-level `invoke_agent(...)` /
  `resume_agent(...)` pair (pure CLI plumbing, no task/worker knowledge),
  and a higher-level `run_agent_task_cycle(...)` (the claim/session-id/
  worktree/artifact-check/gate/finalize state machine, mirroring the role
  `examples/worker_common.py` plays for the toy fixtures, but for
  agent-backed handlers specifically). Splitting further would be premature
  structure for one worker's worth of code; if a second agent-backed worker
  is added later and the two concerns need independent evolution, split
  then.
- **Not reusing `examples/worker_common.py`'s `run_cycle` directly.** That
  function's contract is "safe to re-run the handler in full" and its gate
  check is "is this task's `type` itself a gated action." Neither holds for
  agent-backed handlers: goal 2 explicitly replaces the re-run contract
  with session resumption, and the gated action here is not the task type
  but the *merge to main* that happens after the agent's work is already
  committed to a branch. `orchestrator/agent_runner.py`'s
  `run_agent_task_cycle` mirrors `worker_common.run_cycle`'s ordering
  discipline (claim before work, write before flip, gate-and-wait, then
  idempotent finalize) but is its own state machine built for this
  different contract. `examples/worker_common.py` itself is untouched.
- **Merge is performed by Python, not by the agent.** Early design
  considered giving the agent's harness `Bash(git *)` so it could stage and
  commit its own output. Rejected after the spike finding that a resumed
  agent can claim success without finishing: relying on the LLM to
  correctly run `git add`/`git commit` is one more place that exact failure
  mode could hide a no-op as a success. Instead, no harness in this plan
  grants Bash access at all; the agent's only job is to read message files
  and write its one expected output file via the Read/Write (and, for
  harnesses that need to scan many files, Glob/Grep) tools. After the
  session ends, `orchestrator/agent_runner.py` deterministically checks for
  that specific expected file, and if present, performs `git add` +
  `git commit` itself. This is both simpler to reason about and closes a
  real failure mode found during testing, not a hypothetical one.
- **No new gate category; `merge_branch` (already in `gates.json`) covers
  this.** The originating prompt asks not to add a new gate category if the
  existing `merge_branch` entry already covers "merging a worktree's
  changes back," and to say why if it doesn't. It does: `merge_branch`'s
  existing reason ("merging changes history other people build on top of")
  applies exactly to merging a task's worktree branch into the message-agent
  repo's `main`. `gates.json` is unmodified by this plan.
- **The task types themselves are not gated.** Reviewing, drafting, and
  summarizing messages are not irreversible, trust-boundary-crossing, or
  spend/scope/policy actions in themselves — only the resulting merge to
  `main` is, and that already goes through `merge_branch`. This means
  `examples/worker_common.py`'s per-task-type gate pattern (checking
  `gates.is_gated(task.type)`) does not apply here at all; the gate check
  in this plan's design is a separate, later step tied to "the agent
  produced a commit," not to the task type.
- **Message file format: Markdown with a small key: value header block**,
  not JSON. Chosen for two reasons: (1) it is what a human — or an agent
  whose job is literally to read and reason about message content — reads
  and writes most naturally, unlike the mailbox protocol's JSON, which is
  read by code, not by an LLM trying to understand a conversation; (2) it
  diffs and merges cleanly under git, which JSON with reordered keys does
  not always do as legibly. This is a deliberate, explicit break from the
  base system's "everything is JSON" convention, scoped narrowly to this
  one new concept — the base system's actual coordination files (task
  files, heartbeats, mailbox messages, operator-pending items) are
  untouched and remain JSON.
- **Messages live in `workers/message-agent/repo/messages/`, a git
  repository the message-agent worker owns — a different thing from
  `mailboxes/`, on purpose.** `mailboxes/<worker>/inbox|done/` is this
  system's internal worker-to-worker coordination protocol (dispatch
  messages, completion reports) — plumbing no task's business logic ever
  reads as content. The message files this worker reviews/responds to/
  summarizes are business content: the actual "messages" a human sent it to
  manage, analogous to an inbox of real correspondence. Conflating the two
  would mean either polluting the coordination mailbox with business
  content and breaking its "one small JSON envelope per protocol event"
  shape, or making the orchestrator's dispatch protocol depend on
  Markdown. Keeping them as two directories with two formats and two
  purposes, both documented here explicitly, means a future reader is never
  tempted to merge them.
- **The message-agent's repo, not just its message directory, is
  git-tracked**, and every agent-backed task type (not only ones that "edit
  a shared repo" in some more limited sense) goes through worktree
  isolation and the `merge_branch` gate. All three task types
  (review/respond/summarize) write an output file into this same repo
  (`reviews/`, `drafts/`, `summaries/`), so per the letter of goal 4 ("any
  agent-backed handler whose work involves editing files in a git-tracked
  repository"), all three qualify, and unifying them under one code path is
  simpler than special-casing "which task types count." This also means
  goal 4 (worktree isolation) and goal 6 (the message-agent worker) are
  proven by the same real worker rather than needing a second, separate,
  contrived example.
- **Branch/worktree naming: `task/<task_id>` / `worktrees/<task_id>/`.**
  Deterministic from the task id alone, so recovery can always find (or
  know it must create) the exact same worktree/branch for a given task
  without needing to store the path anywhere else — though it is also
  recorded on the task file directly (`agent_worktree_path`), per the
  "legibility" principle: a human reading the task file should see where
  the work happened without having to know the naming convention.
  Recorded anyway for auditability.
- **Harness config format: JSON, not YAML** — same reasoning the base plan
  already gave for `gates.json` over `gates.yaml` (no stdlib YAML parser;
  adding PyYAML for config files conflicts with the minimal-dependency
  stance more than it helps). Consistent with the one config format this
  repository already uses everywhere else.
- **`--permission-mode bypassPermissions` for every harness, with the tool
  allowlist as the actual safety boundary.** These agent sessions run fully
  unattended (`-p`, no human present to answer an interactive permission
  prompt). Any permission mode that can block waiting for approval
  (`acceptEdits`, `manual`, `dontAsk`) would hang forever in this context —
  verified conceptually from the CLI's own mode descriptions, not
  something to discover by hanging a real process. The actual safety
  boundary is each harness's `allowed_tools` list (verified empirically:
  `--allowedTools "Bash,Write"` combined with a prompt that only uses those
  two tools worked as expected in the spike test) plus running inside an
  isolated worktree directory the agent cannot escape via any granted tool.
- **`workers/message-agent/repo/CLAUDE.md` for the worker's general
  identity; each harness's `append_system_prompt` for task-type-specific
  instructions.** Claude Code auto-discovers `CLAUDE.md` upward from the
  session's working directory (confirmed by design of the tool itself, and
  by the general-purpose base-system pattern of putting durable
  human-readable policy in a plain file) — since each task's worktree is a
  checkout of the same tracked tree, `CLAUDE.md` is present in every
  worktree automatically. This is the natural place for "what kind of
  worker is this, in plain language" (goal 6's requirement), while the
  per-task-type instructions ("produce a triage report," "draft a reply to
  this specific message," "summarize this time window") belong in the
  harness, since they differ by task type where the worker's identity does
  not.
- **No Python mapping of task type → harness.** Each harness JSON file
  carries its own `worker` and `task_type` fields; `workers/message-agent/
  worker.py` discovers its task-type → harness mapping by scanning
  `harnesses/*.json` at startup and indexing the ones whose `worker` field
  matches its own name. This means adding a fourth task type to this
  worker, or changing which harness an existing task type uses, never
  touches `worker.py` at all — only a harness file changes, satisfying goal
  3's "no duplicated Python" as literally as possible: there isn't even a
  task-type-to-harness dict in Python to edit.
- **Prompt construction is also declarative, via a `user_prompt_template`
  string in each harness with `{field}` placeholders**, filled in generically
  by `worker.py` from the task's payload plus two computed fields
  (`task_id`, `artifact_path`) — never a per-task-type Python branch. This
  keeps the last remaining piece of "how do we ask the agent to do this
  task" out of Python too.
- **`workers/message-agent/repo/` and `workers/message-agent/worktrees/` are
  siblings, not nested.** A linked `git worktree` cannot be created inside
  the main worktree's own tracked path without confusing which `.git` a
  given file belongs to; keeping `worktrees/<task_id>/` alongside `repo/`
  (both direct children of `workers/message-agent/`) avoids that entirely
  and matches how the base system already separates concerns by directory.
- **`workers/message-agent/seed/` is tracked; `workers/message-agent/repo/`
  and `worktrees/` are git-ignored and regenerated from it.** Found only
  after actually trying to commit a live nested git repository into this
  one and watching git silently reduce it to a dangling gitlink (see
  Surprises & Discoveries for the full empirical finding, including why
  `--separate-git-dir` doesn't help). The human-authored content this plan
  actually needs tracked — `CLAUDE.md`, the real test message files — lives
  in `seed/`, which has no nested `.git` and is therefore just plain files
  like everything else in this repository. `repo/` is bootstrapped from
  `seed/` on first use (`magent_config.ensure_repo_bootstrapped()`,
  idempotent) and then accumulates real task branches and merge commits
  over time — that accumulated history is exactly the kind of "runtime
  state, not source" this repository already declines to commit for
  `state/`, `mailboxes/`, `heartbeats/`, and `operator-pending/`.
- **A worker-specific env var, `MESSAGE_AGENT_REPO_DIR`, overrides where
  this worker's git repo lives**, exactly mirroring how `ORCH_BASE_DIR`
  overrides the base system's state directories for tests. Without it,
  `scripts/run_validation.py`'s new agent-backed items would create real
  branches, worktrees, and commits inside the actual
  `workers/message-agent/repo/` on every run, accumulating test cruft
  forever — the same reason `ORCH_BASE_DIR` exists for the base suite.
  `harnesses/*.json` is deliberately NOT affected by this override, for the
  same reason `gates.json` is always read from the real repository
  regardless of `ORCH_BASE_DIR`: harness configs are policy, not runtime
  state.
- **`MAX_AGENT_ATTEMPTS = 3`**, a constant in `orchestrator/agent_runner.py`.
  Chosen directly in response to the "claims done without finishing" spike
  finding: if the expected artifact is still missing after this many
  resume-and-nudge cycles, the task is marked `failed` with a clear reason
  rather than looping forever. Three was chosen as "one honest try plus two
  nudges" — enough to absorb the kind of single-step skip seen in the
  spike, not so many that a genuinely broken harness burns unbounded real
  API cost before surfacing as a failure.

## Outcomes & Retrospective

Written after implementation and a full, passing validation run (see
Validation and Acceptance). Assessed against this plan's own
self-reflection bar: if a process running an agent-backed handler is
`kill -9`'d mid-session, does a fresh process resume the same session
rather than starting over blind; does swapping a harness config actually
change agent behavior with zero Python changes; does any agent-backed
handler that edits a real repo do so only inside an isolated worktree; does
the message-management worker's review/respond/summarize behavior actually
run against real message files, not just read plausibly from the code.

1. **Agent-invocation handler pattern — fully implemented.**
   `orchestrator/agent_runner.py` is the one shared module every
   agent-backed handler goes through; `workers/message-agent/worker.py` is
   the only caller today, but nothing about the module is specific to that
   worker — a second agent-backed worker would call the exact same
   `invoke_agent` / `resume_agent` / `run_agent_task_cycle` functions with
   its own harnesses. Headless CLI subprocess, not the SDK, per the
   Decision Log; zero new pip dependencies.
2. **Session resumption replacing full-rerun — fully implemented and
   proven against a live `kill -9`.** Validation item 8 kills a real
   worker process AND the real `claude` subprocess it spawned (its own
   process group, per the orphaned-child spike finding) mid-agent-session,
   starts a fresh worker process, and confirms via the CLI's own returned
   `session_id` — not an assumption — that the fresh process resumed the
   identical session rather than starting a new one. This is not asserted
   from reading the code; it is asserted from a real subprocess's real
   JSON output.
3. **Harness as declarative configuration — fully implemented.** Three
   harness files exist; two (`message-respond` vs. `message-review`/
   `message-summarize`) have genuinely different `allowed_tools` (no
   Glob/Grep for the single-message drafting task, which doesn't need to
   search) and entirely different `append_system_prompt` /
   `user_prompt_template` content, and produce visibly different output
   (a triage report vs. a drafted reply vs. a period summary, in three
   different subdirectories) through the exact same
   `run_agent_task_cycle` code path. Validation item 9 proves this by
   actually running both, not by comparing the JSON files.
4. **Worktree isolation for repo edits — fully implemented.** Every
   agent-backed task in this worker runs in its own `git worktree` on its
   own branch; validation items 8-10 all confirm the new file is absent
   from `main` immediately after the agent session ends and present only
   after `scripts/resolve_pending.py <id> approve` — the same
   `merge_branch` gate the base system already defined, unmodified.
5. **Heartbeat discipline extended to agent calls — fully implemented.**
   `run_agent_task_cycle` re-stamps the worker's heartbeat with
   `expected_duration_seconds + heartbeat_buffer_seconds` (both from the
   harness) immediately before every `invoke_agent`/`resume_agent` call,
   before not after, exactly mirroring `examples/slow_worker.py`'s existing
   pattern for its simulated long task.
6. **The message-management worker — fully implemented and exercised
   against real files.** `review_messages`, `draft_response`, and
   `summarize_period` were each dispatched at least once through
   `scripts/submit_task.py` → a real orchestrator tick → this worker's
   normal cycle, against message files created for this validation run
   (not fixtures read from the code), and each produced a real, inspected
   output file that was merged to `main` only after gate approval.

**What was actually tested against a live Claude Code session, and what
was not:** every claim above involving an actual agent session (goals 1,
2, 3, 5, 6, and the worktree/gate mechanics in goal 4) was run against the
real `claude` CLI with real API calls and real billed cost — this was true
starting from the very first spike test in Surprises & Discoveries, through
every item in Validation and Acceptance. Nothing in this plan's live
validation was stubbed pending credentials, because credentials were
already available in this environment (confirmed at the very start of
context-gathering). The only thing not exercised is a *second* real
agent-backed worker built on the same `orchestrator/agent_runner.py`
plumbing — this plan proves the plumbing is generic by construction
(nothing in it names "message-agent"), but only one concrete worker was
asked for and built.

No stop condition was hit: no credential beyond what was already present
was needed, nothing destructive against real external state was required
(git operations were confined to this worker's own repository and
disposable temp copies of it), and the required tool (`claude` CLI) was
already installed and working. The CLI-vs-SDK choice, the harness config
format, the message file format and location, the exact task-type names,
and the retry cap were all made unilaterally and recorded in the Decision
Log, per this plan's explicit instruction not to come back and ask about
those.

## Context and Orientation

Read `.agent/execplans/orchestration-file-based-coordination.md` first if
you have not — it defines every term this plan builds on (worker, task,
mailbox, gate, heartbeat, tick) and this section assumes that vocabulary
rather than redefining it.

This plan adds one new idea to that vocabulary: an **agent-backed
handler**. Where a normal task handler in this system is a Python function
that does some work directly (format a string, sleep), an agent-backed
handler instead starts (or resumes) a real Claude Code session — a
separate OS process, `claude -p ...`, running an actual LLM agent loop that
can read and write files and think about what to do — and waits for it to
finish one piece of work. The task dispatch mechanism around it (the
orchestrator's tick loop, the mailbox message that tells the worker to
start, the worker's own `state.json`) is completely unchanged from the base
system; only what happens *inside* the worker's handling of one task is
new.

A **harness** is a small JSON file under `harnesses/` describing exactly
one such session's allowed behavior: which tools it may call
(`allowed_tools`), how permission prompts are handled (always
`bypassPermissions` in this plan, since these sessions are unattended), an
appended system prompt describing the specific job, a prompt template for
the one instruction it's actually given, an expected duration (for
heartbeat re-stamping), and a cost cap (`max_budget_usd`, enforced by the
CLI itself). A harness is identified by which `worker` and `task_type` it's
for; a worker discovers all of its own harnesses by scanning the
`harnesses/` directory, so adding or changing a harness never requires a
Python change.

A **session id** is a UUID that identifies one continuous Claude Code
conversation. This plan's central trick is choosing that UUID *before*
starting the session (`claude -p ... --session-id <uuid>`) and writing it
to the task's own file on disk before the `claude` process is even
launched. If the process running the worker is killed at any point after
that write — whether the agent session was one second in or nearly
finished — a fresh process can look at the task file, see the recorded
session id, and resume that exact conversation
(`claude -p "..." --resume <uuid>`) rather than starting a new one that
remembers nothing.

A **worktree** here means a `git worktree` — a second, independent working
directory backed by the same repository, checked out to its own branch.
This plan gives every agent-backed task its own worktree and branch so
that whatever the agent reads or writes there can never collide with
another task's in-flight edits, and so that nothing it does is visible on
the repository's `main` branch until a human explicitly approves merging
it — reusing the `merge_branch` entry the base system's `gates.json`
already defines, not a new gate.

The **message-management worker** (`workers/message-agent/`) is the one
concrete agent-backed worker this plan builds, to prove all of the above
actually works rather than just describing it. It owns a small git
repository (`workers/message-agent/repo/`) containing "messages" — plain
Markdown files representing pieces of correspondence someone wants
reviewed, replied to, or summarized. **This is a different concept from
`mailboxes/`**, this system's existing worker-to-worker coordination
protocol: `mailboxes/<worker>/inbox|done/` holds small JSON envelopes like
"dispatch this task" or "this task is done," read and written only by this
codebase's own Python, never by an LLM reasoning about their content. The
message files under `workers/message-agent/repo/messages/` are business
content — the actual correspondence a person asked this worker to manage —
written in Markdown specifically because an LLM agent (and a human) reads
and writes that shape of text far more naturally than JSON. A future reader
should never assume these are the same thing: one is internal plumbing, the
other is the worker's actual job.

## Plan of Work

Work proceeds in the same bottom-up order the base system's plan used:
first the low-level, worker-agnostic plumbing for invoking and resuming a
`claude` session (`orchestrator/agent_runner.py`'s `invoke_agent` /
`resume_agent`), then the higher-level per-cycle state machine that uses
that plumbing correctly under crash recovery (`run_agent_task_cycle`, in
the same module), then the harness configs (pure data, no code depends on
their contents beyond generic field lookups), then the message-management
worker itself (`workers/message-agent/`, both its git repository skeleton
and its `worker.py` entrypoint), then real message files created
specifically to exercise it, and finally an extension of the existing
validation suite proving every acceptance criterion — including the live
`kill -9` test — against real subprocesses and a real `claude` CLI, with
fixes applied immediately if a criterion doesn't actually hold.

The `orchestrator/tick.py`, `orchestrator/gates.py`, `orchestrator/
common.py`, `orchestrator/config.py`, and `gates.json` files from the base
system are not modified at all. This worker is dispatched to, and reports
back, using exactly the existing dispatch/mailbox/gate machinery; the only
new code is what happens inside this one worker's own cycle.

## Concrete Steps

1. **`orchestrator/agent_runner.py`** — the shared plumbing module. Two
   layers:

   - **CLI invocation layer** (no knowledge of tasks or workers):
     - `AgentResult`: a small dataclass — `session_id: str`,
       `ok: bool` (subprocess exited 0 and stdout parsed as JSON),
       `is_error: bool` (the CLI's own `is_error` field), `result_text: str`,
       `raw: dict` (the full parsed JSON), `stderr_tail: str` (last part of
       stderr, for diagnostics on failure).
     - `_run_claude(argv, cwd) -> AgentResult`: launches
       `subprocess.Popen(argv, cwd=cwd, stdout=PIPE, stderr=PIPE,
       start_new_session=True)` — `start_new_session=True` is the direct
       response to the orphaned-child spike finding: it puts the `claude`
       process in its own OS process group so that (a) a future timeout or
       crash-cleanup mechanism could kill the whole group with
       `os.killpg`, and (b) this plan's own kill-9 validation test can
       reliably kill the entire session, not just the reported PID, to
       faithfully simulate a real crash. Waits via `communicate()` (this
       whole call is meant to block for the duration of one real agent
       session — the same "one long synchronous step per cycle" shape
       `examples/slow_worker.py` already uses for its simulated long task).
       Parses stdout as JSON; a parse failure or non-zero exit produces an
       `AgentResult` with `ok=False` rather than raising, so callers can
       treat "the CLI itself failed" as just another retryable condition.
     - `invoke_agent(harness, prompt, cwd, session_id) -> AgentResult`:
       builds argv with `--session-id <session_id>` (the id was already
       decided and durably written by the caller before this function is
       even called — this function never generates or chooses an id
       itself) plus every harness-derived flag (`--permission-mode`,
       `--allowedTools`, `--append-system-prompt`, `--model` if set,
       `--effort` if set, `--max-budget-usd` if set), `--output-format
       json`, and the prompt as the positional argument.
     - `resume_agent(harness, cwd, session_id) -> AgentResult`: same
       flags, but `--resume <session_id>` instead of `--session-id`, and a
       fixed continuation prompt (the exact string from the "Empty
       `--resume` prompts are rejected" spike finding) instead of a
       harness-built one, since a resume has nothing new to say beyond
       "keep going."

   - **Per-cycle state machine layer** (knows about tasks, workers,
     worktrees, gates):
     - `MAX_AGENT_ATTEMPTS = 3` (module constant, see Decision Log).
     - `ensure_worktree(repo_dir, worktrees_dir, task_id) -> Path`: if
       `worktrees_dir / task_id` already exists, return it unchanged
       (idempotent — this is the resume case). Otherwise run `git -C
       <repo_dir> worktree add <worktrees_dir>/<task_id> -b task/<task_id>
       main` and return the new path. Never recreates a worktree that
       already exists, which is exactly what makes this safe to call again
       after any crash.
     - `render_prompt(harness, task) -> str`: `.format(**context)` on the
       harness's `user_prompt_template`, where `context` is the task's
       `payload` dict merged with `{"task_id": task["id"], "artifact_path":
       harness["artifact_path_template"].format(task_id=task["id"])}`.
     - `artifact_ready(worktree, artifact_path) -> bool`: the relative
       path exists under the worktree and its file size is greater than
       zero. This is the *only* signal `run_agent_task_cycle` trusts for
       "did the agent's work actually happen" — never the CLI's own
       `result`/`is_error` fields, per the "claims done without finishing"
       spike finding.
     - `commit_artifact_if_needed(worktree) -> bool`: runs `git -C
       <worktree> status --porcelain`; if empty (nothing to commit —
       either nothing changed, or an earlier crashed attempt already
       committed), returns `False` and does nothing further. Otherwise
       runs `git -C <worktree> add -A` then `git -C <worktree> commit -m
       "task <task_id>: <task_type>"` and returns `True`. Committing is
       always done by this Python function, never by the agent (see
       Decision Log) — no harness in this plan grants any git-capable
       tool.
     - `file_merge_gate_if_needed(worker, task) -> str`: mirrors
       `examples/worker_common.py`'s `_find_pending_id_for_task` idempotence
       guard exactly (scan `gates.list_pending()` and
       `OPERATOR_PENDING_RESOLVED_DIR` for an existing item whose
       `payload.task_id` matches before filing a new one), but always with
       `action_type="merge_branch"` — reusing the base system's existing
       gate entry, not a new one — and a `payload` containing the task id,
       branch name, and worktree path so an operator reading the pending
       item on the dashboard can see exactly what would be merged.
     - `perform_merge_or_discard(repo_dir, worktree_path, branch, decision)`:
       if `decision == "approved"`: checks `git -C <repo_dir> merge-base
       --is-ancestor <branch> main` first (idempotent — if the branch is
       already merged, from a prior crashed attempt that got this far
       before dying, skip the merge itself) and if not already merged,
       runs `git -C <repo_dir> checkout main && git -C <repo_dir> merge
       --no-ff <branch> -m "merge task <task_id>"`. Either way, then runs
       `git -C <repo_dir> worktree remove <worktree_path>` (idempotent:
       no-op, caught and ignored, if the path is already gone) and deletes
       the branch. If `decision == "denied"`: runs `git worktree remove
       --force` and deletes the branch without merging, discarding the
       work entirely — this is the deliberate, expected outcome of a
       denial, not an error.
     - `run_agent_task_cycle(worker, repo_dir, worktrees_dir,
       harnesses_by_task_type) -> dict`: the actual state machine. See
       Idempotence and Recovery below for the exact branch-by-branch
       reasoning; the shape mirrors `examples/worker_common.py`'s
       `run_cycle` (same claim-before-work discipline, same "leave the
       inbox message in place until truly done" discipline, same
       `WorkerContext.heartbeat()` re-stamping-before-slow-work pattern)
       but implements the different agent-backed contract end to end.

2. **`harnesses/message-review.json`, `harnesses/message-respond.json`,
   `harnesses/message-summarize.json`** — see Artifacts and Notes for the
   full schema and exact contents of each. `message-respond` is the one
   with the narrower `allowed_tools` (`["Read", "Write"]`, no Glob/Grep,
   since its prompt already names the one message file to read); the other
   two get `["Read", "Glob", "Grep", "Write"]` since they need to discover
   or scan across many message files. All three share
   `permission_mode: "bypassPermissions"` and have no git-capable tool at
   all (see Decision Log).

3. **`workers/message-agent/seed/`** — tracked, plain files, no git repo of
   its own: `CLAUDE.md` (plain-language description of the worker's job,
   goal 6) and `messages/` containing the real test message files (see
   step 6). This is the human-readable source of truth.

4. **`workers/message-agent/magent_config.py`** — this worker's own small
   config module, parallel in spirit to `orchestrator/config.py` but scoped
   to this one worker: `SEED_DIR` (`workers/message-agent/seed`),
   `REPO_DIR` (default `workers/message-agent/repo`, overridable via
   `MESSAGE_AGENT_REPO_DIR` for tests, exactly like `ORCH_BASE_DIR`),
   `WORKTREES_DIR` (`REPO_DIR`'s parent `/ "worktrees"`), `WORKER_NAME =
   "message-agent"`, and `ensure_repo_bootstrapped()` — if `REPO_DIR`
   doesn't already have a `.git`, copy `SEED_DIR`'s content in, create the
   `reviews/`/`drafts/`/`summaries/` subdirectories, `git init -b main`,
   one initial commit; a no-op if `REPO_DIR/.git` already exists (see
   Decision Log and Surprises & Discoveries for why `REPO_DIR` is
   git-ignored rather than tracked directly). `worker.py` calls this once
   at the top of every invocation, before scanning harnesses or running a
   cycle.

5. **`workers/message-agent/worker.py`** — the entrypoint script:
   - At startup, scans `harnesses/*.json`, keeps the ones with `worker ==
     "message-agent"`, and builds `{task_type: harness_dict}`.
   - `--once` / `--loop [--interval-seconds N]` / `--stand-down` CLI, reusing
     `examples/worker_common.build_arg_parser` (pure argument-parsing
     boilerplate, no task-execution logic, so reusing it is not the kind of
     duplication this plan is meant to avoid).
   - Each cycle calls `agent_runner.run_agent_task_cycle(worker="message-agent",
     repo_dir=magent_config.REPO_DIR, worktrees_dir=magent_config.WORKTREES_DIR,
     harnesses_by_task_type=<the scanned dict>)` and prints the resulting
     dict as JSON (matching `orchestrator/tick.py`'s existing
     `print(json.dumps(result))` convention), so both a human operator and
     the validation suite can see exactly what a cycle did, including the
     `agent_session_id` actually used — this is what lets validation item 8
     prove resumption happened by comparing session ids directly rather
     than inferring it.

6. **Real message files** — created directly (not generated by an agent)
   in `workers/message-agent/seed/messages/`, tracked normally: four
   messages spanning 2026-07-10 to 2026-07-16, so `summarize_period` has a
   real time window to filter on and `draft_response` has a real, named
   message to reply to. Exact content documented in Artifacts and Notes.

7. **`scripts/run_validation.py`** — extended with four new items (8-11),
   sharing one `_setup_message_agent_env()` helper that points
   `MESSAGE_AGENT_REPO_DIR` at a fresh disposable temp path (not yet
   existing) and lets `ensure_repo_bootstrapped()` materialize it from the
   real, tracked `seed/` on first use — no copying of an existing `.git`
   needed, since the bootstrap step already does exactly that
   deterministically. Sets `ORCH_BASE_DIR` the same way, exactly mirroring
   the existing `isolated_env` pattern. Items run in sequence against the
   same temp repo (so branch/merge history accumulates realistically
   across them, and so bootstrap only runs once): item 8 (kill-9 recovery via
   `review_messages`), item 9 (harness-difference via `draft_response`,
   also re-confirming worktree isolation on a second task type), item 10
   (`summarize_period`, the third task type, completing "all three task
   types exercised"), item 11 (confirm items 1-7, the base suite, still
   pass unmodified). See Validation and Acceptance for the exact
   assertions.

## Validation and Acceptance

All of the following are real, scripted items in `scripts/run_validation.py`
(items 8-11), run against real subprocesses (a real worker process, a real
`claude` CLI process, real `git` commands) — not read from the code and not
argued in prose. Items 1-7 (the base system's own suite) are unchanged and
must still pass, proving nothing here weakened the base system's
guarantees.

8. **Kill -9 mid-agent-session, resume not restart (goal 2).** Submit a
   `review_messages` task to `message-agent` in a fresh temp copy of its
   repo (real message files already committed to `main`). Run
   `orchestrator/tick.py --once` to dispatch it. Start
   `workers/message-agent/worker.py --once` in the background — this call
   blocks for the duration of a real agent session. After a few seconds
   (well before a real review of 3 short files finishes), read the task
   file directly and confirm `agent_session_id` is already recorded and
   `state.json`'s phase is `in_progress` — proving the id was written
   before the session could finish, per the write-before-flip discipline.
   Then `kill -9` **both** the worker process and (found via `pgrep -f
   <recorded-session-id>`, since the argv literally contains
   `--session-id <uuid>`) the `claude` process it spawned, killing that
   process's entire group (`os.killpg`) — per the orphaned-child spike
   finding, killing only the worker's own PID would not be a faithful
   crash simulation. Confirm both processes are actually gone. Start a
   **fresh** `worker.py --once` process; expect its printed JSON result's
   `agent_session_id` to equal the originally recorded session id (proof
   of resumption, not restart — read directly from the CLI's own output,
   never assumed), and expect the task to end up `waiting_on_operator`
   with a `merge_branch` pending item filed (the review did complete on
   this second, resumed call). Approve it via `scripts/resolve_pending.py
   <id> approve`, run `worker.py --once` once more, and confirm
   `reviews/<task_id>.md` now exists on `main` in the temp repo, is
   non-empty, and was **not** present on `main` before the approval step
   (proving worktree isolation: the file existed only on the task's branch
   until the gate was explicitly cleared).
9. **Two harnesses, demonstrably different behavior, same code path (goal
   3, and goal 4's isolation again on a second task type).** Submit a
   `draft_response` task (payload naming one of the seeded message files)
   to the same worker, same temp repo. Run it to completion through the
   identical `run_agent_task_cycle` path used in item 8. Expect: the
   harness actually used (`harnesses/message-respond.json`) has a strictly
   smaller `allowed_tools` list than `message-review.json`'s (no Glob, no
   Grep — checked by reading both harness files, not by inference), the
   produced artifact lands under `drafts/`, not `reviews/`, and its content
   is a drafted reply (contains a reference to the named message), visibly
   different in kind from the triage report item 8 produced. Confirm again
   that the new file is absent from `main` until
   `scripts/resolve_pending.py` approves this task's own `merge_branch`
   pending item.
10. **Third task type, `summarize_period`, completing "all three task
    types exercised" (goal 6).** Submit a `summarize_period` task (payload
    naming a `start`/`end` window covering the seeded messages) to the same
    worker and temp repo, run it to completion and through gate approval
    the same way, and confirm `summaries/<task_id>.md` exists on `main`
    afterward, non-empty, and mentions messages actually inside the given
    window.
11. **Existing validation suite unaffected.** `python3
    scripts/run_validation.py`'s original items 1-7 (the base system's own
    suite) all still pass, unmodified, in the same run — confirming this
    plan's additions did not touch, and did not weaken, any of the base
    system's crash-recovery, mailbox, heartbeat, or gate guarantees.

## Idempotence and Recovery

This section is the direct answer to goal 2, worked out branch by branch —
every crash window a `run_agent_task_cycle` call can be interrupted at, and
why the next cycle recovers correctly from disk alone. As in the base
plan, "correctly" means: never silently duplicate a real side effect
(never a duplicate commit, never two separate git branches for the same
task, never two separate merges), and never get stuck unable to make
progress.

- **Crash before the claim write.** The dispatch message is still
  untouched in `mailboxes/message-agent/inbox/`, nothing has been claimed.
  A fresh process's `idle` branch sees the same unclaimed message it always
  would have and proceeds exactly as if this were the first attempt,
  identical to the base system's own claim-before-work discipline.
- **Crash after the claim write (`state.json` phase → `in_progress`,
  `current_task_id` set), before a worktree exists.** A fresh process sees
  `phase == "in_progress"`; it looks at the task file and finds no
  `agent_worktree_path` recorded yet. `ensure_worktree` is safe to call
  here for the first time — nothing has happened yet that a fresh worktree
  creation could collide with.
- **Crash after the worktree is created and after `agent_session_id` is
  durably written to the task file (write-before-flip, the core of goal
  2), but during the `claude -p --session-id ...` call itself.** This is
  the exact window validation item 8 targets. A fresh process sees
  `phase == "in_progress"`, `agent_session_id` present, and
  `artifact_ready()` false (the artifact wasn't written yet, or the
  process died before finishing). It calls `resume_agent`, not
  `invoke_agent` — the same session id, so the agent's own transcript
  supplies whatever memory of partial progress exists; the handler itself
  does not need to know or reconstruct what the agent had already done.
- **Crash after the agent session ends (successfully or not) but before
  the handler notices.** Functionally identical to the window above from
  the recovery code's point of view: `artifact_ready()` is checked fresh
  on every cycle regardless of why the previous cycle didn't get further,
  so "the agent already fully finished, we just crashed before checking"
  and "the agent is still working" are both handled by the same
  `resume_agent`-then-recheck path — the *spike-discovered flakiness* case
  (agent claims done, artifact missing) and the *real crash* case are
  handled by the identical retry-with-a-cap logic, which is deliberate: the
  handler cannot tell these apart from the outside, and doesn't need to.
- **Crash after the artifact exists but before it's committed.**
  `commit_artifact_if_needed` checks `git status --porcelain` fresh each
  time; if the previous attempt already committed (clean worktree), this
  is a no-op — no double commit.
- **Crash after the commit but before the `merge_branch` gate is filed.**
  `file_merge_gate_if_needed` scans existing pending *and resolved* items
  for one already tied to this task id before filing a new one — the exact
  same idempotence guard `examples/worker_common.py` already uses for its
  own gate-filing step, applied here.
- **Crash after the gate is filed but before `state.json` records
  `waiting_on_pending_id`.** A fresh process re-derives the same pending id
  via the same lookup in `file_merge_gate_if_needed` (it searches by task
  id, not by trusting `state.json`) and writes the state pointer then,
  rather than filing a second pending item.
- **Crash after an operator approves, but before the merge is performed.**
  `perform_merge_or_discard` checks `git merge-base --is-ancestor <branch>
  main` before merging; if a prior attempt got as far as merging before
  dying, this is `True` and the merge step is skipped, going straight to
  worktree cleanup and task finalization.
- **Crash after the merge but before the task is marked `done` and
  reported.** Finalization (`write_task(status="done", ...)`, move the
  original dispatch message to `done/`, send `task_completed` to the
  orchestrator's inbox) is the same three-step sequence
  `examples/worker_common.py`'s `_finish_task` already uses, in the same
  order, for the same reason: each step is safe to redo (writing the same
  status again, or finding the message already moved and skipping that
  step, or — since `send_mailbox_message` always creates a fresh uniquely
  named file — sending a second `task_completed` message in the rare case
  this exact step is retried, which the orchestrator's `process_inbox`
  already handles safely since re-marking an already-`done` task `done`
  again is harmless).
- **Denial path.** A denial is symmetric to approval: `perform_merge_or_
  discard` with `decision == "denied"` discards the branch and worktree
  instead of merging, and finalization reports `task_failed` instead of
  `task_completed` — same idempotent redo-safety reasoning throughout.

## Artifacts and Notes

**Harness schema** (`harnesses/<name>.json`, all three follow this shape):
```json
{
  "name": "message-review",
  "worker": "message-agent",
  "task_type": "review_messages",
  "description": "Reads every message currently in messages/ and writes a short triage report.",
  "permission_mode": "bypassPermissions",
  "allowed_tools": ["Read", "Glob", "Grep", "Write"],
  "model": null,
  "effort": "medium",
  "max_budget_usd": 1.0,
  "expected_duration_seconds": 60,
  "heartbeat_buffer_seconds": 60,
  "append_system_prompt": "You are the review pass of a message-management agent. Read every file in messages/. For each message, write one line noting its sender, subject, and a triage category (urgent / needs-response / informational). Write the full report, and nothing else, to the exact path given in your instructions. Do not modify any file under messages/.",
  "artifact_path_template": "reviews/{task_id}.md",
  "user_prompt_template": "Task id: {task_id}\nRead every message file in the messages/ directory of this repository.\nWrite your triage report to exactly this path (relative to the repository root): {artifact_path}\nDo not write anywhere else."
}
```
`harnesses/message-respond.json` differs in: `task_type: "draft_response"`,
`allowed_tools: ["Read", "Write"]` (no Glob/Grep), `artifact_path_template:
"drafts/{task_id}.md"`, a system prompt describing drafting one reply, and
a `user_prompt_template` that includes `{message_file}` (filled from the
task's own payload).
`harnesses/message-summarize.json` differs in: `task_type:
"summarize_period"`, `allowed_tools: ["Read", "Glob", "Grep", "Write"]`,
`artifact_path_template: "summaries/{task_id}.md"`, a system prompt
describing summarizing a time window, and a `user_prompt_template`
including `{start}`/`{end}`.

**Message file format** (`workers/message-agent/seed/messages/
<ISO-compact-ts>-<from-slug>-<subject-slug>.md`, copied into
`workers/message-agent/repo/messages/` by `ensure_repo_bootstrapped()`):
```
---
from: alice@example.com
to: message-agent
date: 2026-07-15T09:30:00+00:00
subject: Question about Q3 roadmap
---
Body text of the message, in plain Markdown, goes here.
```
A simple key: value header block delimited by `---` lines, then a blank
line, then a plain-text/Markdown body — deliberately not JSON (see
Decision Log) and deliberately not the mailbox message schema.

**Task file additions** (new optional fields on the existing
`state/orchestrator/tasks/<task_id>.json` schema; unused by non-agent-
backed workers):
```json
{
  "agent_session_id": "uuid string, or null before an agent session ever starts",
  "agent_worktree_path": "workers/message-agent/worktrees/<task_id> (relative), or null",
  "agent_branch": "task/<task_id>, or null",
  "agent_harness": "message-review | message-respond | message-summarize",
  "agent_started_ts": "ISO timestamp of the first invoke_agent call, or null",
  "agent_attempt": "integer, incremented once per resume-due-to-missing-artifact",
  "agent_merge_pending_id": "the merge_branch operator-pending id, or null"
}
```

**Directory layout added by this plan** (all paths relative to the
repository root):
```
orchestrator/agent_runner.py       CLI invocation + agent-backed worker cycle state machine
harnesses/
  message-review.json               harness for review_messages
  message-respond.json              harness for draft_response
  message-summarize.json            harness for summarize_period
workers/message-agent/
  magent_config.py                   SEED_DIR / REPO_DIR / WORKTREES_DIR, MESSAGE_AGENT_REPO_DIR override, ensure_repo_bootstrapped()
  worker.py                          entrypoint: --once / --loop / --stand-down
  seed/                                TRACKED: human-authored source of truth, no nested .git
    CLAUDE.md                          plain-language description of the worker's job
    messages/                          the real test message files (see format above) — NOT mailboxes/
  repo/                                GIT-IGNORED, runtime: bootstrapped from seed/, this worker's own
                                        live git repository, branch main (see Decision Log/Surprises for why
                                        this can't be tracked directly -- a nested .git can't be committed
                                        as plain files by this outer repo)
    CLAUDE.md, messages/                copied in from seed/ by ensure_repo_bootstrapped()
    reviews/                           review_messages output, one file per task
    drafts/                            draft_response output, one file per task
    summaries/                         summarize_period output, one file per task
  worktrees/                          GIT-IGNORED, runtime: sibling of repo/; linked worktrees, one per in-flight task
```

## Interfaces and Dependencies

- **New external dependency: the `claude` CLI itself**, invoked as a
  subprocess (`subprocess.Popen`, standard library — no new pip package).
  Verified present and authenticated in this environment at v2.1.211
  before any of this plan's design was finalized. This is the one thing
  this extension needs that the base system did not: the base system made
  zero network calls; every agent-backed task in this plan makes a real
  network call to Anthropic's API through the `claude` CLI's own
  authentication (whatever the CLI is configured to use in this
  environment — this plan does not manage or assume any specific auth
  mechanism beyond "the `claude` CLI works when invoked"). A reader running
  this in an environment without a working `claude` CLI login will see
  every agent-backed task fail at the `invoke_agent`/`resume_agent` call;
  nothing in this plan degrades gracefully to a no-op in that case, by
  design, since silently skipping real work would be worse than a loud
  failure.
- **`git` (any reasonably current version; developed against 2.50.1)**,
  invoked as a subprocess for `worktree add`, `worktree remove`, `add`,
  `commit`, `checkout`, `merge`, `merge-base`. No Python git library is
  used, consistent with the minimal-dependencies stance.
- **Everything else this plan depends on** — the orchestrator's tick loop,
  the mailbox protocol, `gates.json` and `orchestrator/gates.py`, the
  `operator-pending/` queue, `scripts/submit_task.py`,
  `scripts/resolve_pending.py` — is the base system, completely unmodified,
  imported and called exactly as any other worker would.
- **What depends on this plan's new code:** nothing in the base system
  does. `orchestrator/tick.py` dispatches to `message-agent` exactly as it
  would to any worker name a task file names — it has no special knowledge
  of agent-backed handlers at all. This is by design: the orchestrator's
  job is to deliver a message to a worker's inbox, not to know what kind of
  handler that worker runs internally.
