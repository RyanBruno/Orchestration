# Execution Plan: File-Based Worker Orchestration System

Status: in progress. Last updated: 2026-07-18 (initial authoring).

## Purpose / Big Picture

This repository is building a system for coordinating multiple independent
"worker" processes — programs that each do some unit of work — without
relying on any of them staying alive, without a database, and without a
message broker. Everything that matters is a file on disk. The reason this
matters: if a worker process or the orchestrator itself is killed at any
moment (crash, machine restart, `kill -9`, whatever), a fresh process
started in its place must be able to figure out exactly where things stood
and continue correctly — never redoing a completed action, never losing
one that was in flight, and never getting confused about whether an action
already happened. Nothing that matters is allowed to live only in a
running process's memory or in a language model's conversational context.

Concretely, this plan delivers: a **mailbox** protocol for workers to send
each other messages as individual files; a **state.json** convention so
every worker's current situation is reconstructable from disk alone; an
**orchestrator** that runs a fixed, fully-deterministic four-step cycle
(Gather, Process inbox, Advance, Render) on an interval and decides what
work to dispatch next, with a hard cap on how much "heavy" work can run
at once; a **heartbeat** mechanism so staleness/liveness is computed from
timestamps on disk, not from trusting silence; a **human-gates** config
that names a short, editable list of action categories that always stop
and wait for a person, plus a durable **operator-pending** queue for those
stops; two toy **example workers** (`examples/fast_worker.py`,
`examples/slow_worker.py`) that exist purely to prove all of the above
actually works, not as production workloads; and an **HTML dashboard**
that shows the live state of everything by reading the same disk files,
recomputed fresh on every request.

## Progress

- [x] 2026-07-18 — Repo scaffolding: `AGENTS.md`, `.agent/PLANS.md` written and committed.
- [x] 2026-07-18 — This plan authored.
- [x] 2026-07-18 — Shared library (`orchestrator/common.py`, `orchestrator/config.py`): atomic writes, mailbox send/receive, heartbeat read/staleness, ISO timestamp helpers. Smoke-tested directly (heartbeat write/read, mailbox send/read/complete, worker state round-trip, jsonl append/read).
- [x] 2026-07-18 — Gates config (`gates.json`) and operator-pending helpers (`orchestrator/gates.py`). Smoke-tested (is_gated hit/miss, file/list/resolve pending, get_resolved).
- [x] 2026-07-18 — Orchestrator tick loop (`orchestrator/tick.py`): Gather/Process-inbox/Advance/Render, dispatch journal, fan-out cap. Directly exercised all three dispatch-journal crash windows (never sent, sent-not-flipped, flipped-not-committed) and the fan-out cap — all recovered/enforced correctly; see Surprises & Discoveries.
- [x] 2026-07-18 — Example workers (`examples/worker_common.py`, `examples/fast_worker.py`, `examples/slow_worker.py`). Directly exercised: baseline dispatch/claim/complete, the gate path end to end (file pending -> approve -> resume -> complete), and `kill -9` mid-heavy-task recovery (state showed `in_progress` before and after the kill; fresh process resumed and completed exactly once, verified via `done/` containing exactly one file per task). Also found and fixed a real bug here — see Surprises & Discoveries.
- [x] 2026-07-18 — Dashboard (`dashboard/server.py`, `dashboard/index.html`). Verified live in an actual browser against seeded disposable data, not just by reading the code: worker table correctly showed real staleness (orchestrator/fast-worker genuinely stalled relative to their declared interval, slow-worker correctly "no heartbeat yet"), fan-out cap and task tables matched disk state, and the Approve flow was driven end to end (typed a note, clicked Approve, confirmed the pending item moved to `operator-pending/resolved/` on disk with that exact note). Found and fixed two real bugs in the process — see Surprises & Discoveries.
- [ ] Operator CLI scripts (`scripts/submit_task.py`, `scripts/resolve_pending.py`).
- [ ] Validation suite (`scripts/run_validation.sh`) covering every item in Validation and Acceptance, run and passing.
- [ ] Outcomes & Retrospective written; final summary delivered; gates config flagged for operator review.

## Surprises & Discoveries

*(Anticipated pitfalls noted during planning, before implementation
started — kept here rather than deleted so the reasoning is visible, but
distinct from things actually hit during implementation, which will be
appended below as they occur.)*

- macOS `SIGKILL` (`kill -9`) recovery testing needs the worker to be
  mid-sleep (inside the simulated long task) when killed, which means the
  validation script has to background the process, sleep a shorter
  interval than the worker's task duration, then send the signal — this
  is straightforward but timing-sensitive; documented in the validation
  script's comments so a future reader isn't surprised by the sleeps.
- `os.replace()` is atomic only within the same filesystem/directory.
  This is fine everywhere in this design because every atomic write
  writes its temp file into the *same directory* as the final file (e.g.
  `inbox/.tmp-x` → `inbox/x`, never across directories), but it's worth
  stating explicitly since it's the one thing that would silently break
  the whole crash-safety story if violated.
- **Real bug found and fixed**: the documented direct-invocation style
  (`python3 orchestrator/tick.py --once`) initially failed with
  `ModuleNotFoundError: No module named 'orchestrator'`. Python sets
  `sys.path[0]` to the *script's own directory* when run this way (here,
  `orchestrator/` itself), not the repository root and not the caller's
  cwd -- so `from orchestrator import common` inside `tick.py` couldn't
  resolve. Fixed by inserting the repo root into `sys.path` at the top of
  every entry-point script (`orchestrator/tick.py`,
  `examples/fast_worker.py`, `examples/slow_worker.py`, and later
  `dashboard/server.py` and the `scripts/*.py` CLIs) before importing
  anything from the `orchestrator` package. Caught by actually running
  the documented command, not by reading the code -- a reminder that
  "the code compiles" and "the documented command works" are genuinely
  different claims.
- **Real bug found and fixed (dashboard, blocking dialog)**: the Approve/
  Deny buttons originally used a synchronous `prompt()` for the optional
  resolution note. In the automated browser used to test this, `prompt()`
  hung indefinitely with no error and no network request ever fired --
  and independent of that environment quirk, a blocking native dialog on
  every approve/deny click is poor dashboard UX regardless. Replaced with
  a plain inline `<input>` per pending item; the button reads its value
  directly, no blocking call at all.
- **Real bug found and fixed (dashboard, clobbered input)**: the
  operator-pending section rebuilt its `innerHTML` unconditionally on
  every 3-second poll, which meant a human mid-way through typing a note
  would have it silently erased by the next poll. Fixed by only rebuilding
  that section when the *set* of pending item ids actually changed
  (an item appeared or got resolved), leaving the DOM -- and any
  in-progress note text -- untouched on quiet polls. Same "only act on
  actual change" principle the orchestrator's own Render step uses,
  applied client-side; found only by actually typing into the field
  in a live browser and watching it vanish, not by reading the code.
- Python's `time.time()`-based ISO timestamps need explicit UTC and
  microsecond precision for mailbox filename ordering to be unambiguous
  under rapid sends; using `datetime.now(timezone.utc)` with
  `.strftime("%Y%m%dT%H%M%S%f")` plus a 6-hex-character random suffix
  (not a shared counter) avoids needing any coordination file for
  uniqueness, which would otherwise be its own crash-safety problem.

## Decision Log

- **Language/runtime: Python 3, standard library only.** No external
  runtime constraint existed (Python 3.14 and Node 25 were both already
  present), so the choice was made purely on fit: JSON handling and
  atomic file replacement (`os.replace`) are both in the standard
  library, there's no framework hiding control flow that a reader would
  need to learn in order to reason about crash points, and it keeps the
  dependency tree at zero for infrastructure meant to be trustworthy and
  long-lived. A human can read any script here top to bottom and know
  what it does.
- **`gates.json` instead of `gates.yaml`.** The prompt suggested YAML "or
  equivalent." Parsing YAML correctly from the standard library isn't
  possible (no stdlib YAML parser), and adding PyYAML as a dependency
  for one config file conflicts with the "minimal dependencies"
  principle more than it helps readability. JSON with a `"reason"` field
  on every entry gets the same operator-legibility goal (a human can
  read *why* each gate exists) without the dependency or a hand-rolled
  parser. This is a deliberate deviation, called out per the "or
  equivalent" latitude in the prompt.
- **Tick interval: 5 minutes (300s) default for the orchestrator loop.**
  Matches the prompt's suggested default. Reasoning: the work units this
  system coordinates are expected to be minutes-to-hours long, so a
  5-minute cycle surfaces new dispatches and operator-pending items
  promptly relative to that timescale without busy-polling or producing
  constant log noise on quiet ticks. Both the orchestrator and example
  workers accept a `--interval-seconds` override so tests don't have to
  wait 5 real minutes per tick.
- **Fan-out cap: `MAX_CONCURRENT_HEAVY = 1`**, a constant in
  `orchestrator/config.py`. Chosen as the smallest non-trivial value that
  still lets the system demonstrate "cap enforced, second heavy task
  queues instead of dispatching" without needing more than two heavy
  tasks in flight to prove it. It's a plain module-level constant read
  by the Gather step's eligibility computation — not a convention the
  model or an operator has to remember, the dispatch code itself refuses
  to advance a second heavy task while one is outstanding.
- **Staleness multiplier: 2.5x** a worker's own last-declared
  `interval_seconds`, exactly as specified in the prompt. Applied as
  `now - heartbeat.ts > 2.5 * heartbeat.interval_seconds`. Because the
  multiplier is applied to the worker's *own* self-declared interval
  (not a global constant), a worker that re-stamps a longer interval
  before starting slow work raises its own staleness threshold before
  the risk period begins, exactly matching the requirement in goal 4.
- **Task/dispatch model.** The orchestrator treats "a unit of work" as a
  task file under `state/orchestrator/tasks/<task_id>.json` with a
  `status` field. This is not stated explicitly in the prompt but is
  necessary infrastructure to have something concrete for Gather to
  compute eligibility over and for Advance to dispatch — documented
  fully in Artifacts and Notes below.
- **Runtime data directories are git-ignored in content, tracked in
  structure.** Goal 1 says state must be "git-tracked or otherwise
  backed up" — read as an "or," durability is satisfied by ordinary
  filesystem persistence (atomic writes already survive process death;
  that's the whole point of atomic writes). Committing a `heartbeat.json`
  on every tick would produce constant, meaningless commit noise, so
  `state/`, `mailboxes/`, `heartbeats/`, `operator-pending/`, and `logs/`
  contents are `.gitignore`d after their directory skeletons are
  established via `.gitkeep`, while every script that produces or
  consumes them is fully tracked.
- **Dashboard reads disk fresh on every HTTP request, no caching.** This
  directly satisfies "the dashboard should reflect all of the above
  live... without needing anything held in a running process to render
  correctly" — the dashboard server process holds no state between
  requests; restarting it has zero effect on correctness, only on
  availability. All staleness math (2.5x check) is computed at request
  time from the heartbeat's `ts` and `interval_seconds`, never cached.
- **Human-gates default list.** See `gates.json` and the callout in the
  final summary — this is the one item in this plan that is a proposal
  for the operator to review, not a finalized decision, per the
  prompt's explicit instruction.

## Outcomes & Retrospective

**Not yet written.** This section is filled in honestly at the end of
implementation, against what actually happened — not against what the
plan predicted. If you are reading this line, implementation is still in
progress; check the Progress checklist above for current state.

## Context and Orientation

If you have never seen this repository before, here is what you need to
know before the rest of this plan makes sense.

The system has no central database and no long-running message broker.
Instead, coordination state is plain files under version-controlled
directories, and "sending a message" means "atomically creating a file in
someone else's inbox directory." Every *worker* — including the
orchestrator itself, which this plan treats as a worker with a special
job — follows the same shape: it has a durable `state.json` describing
what it's doing right now, a `heartbeat.json` proving it's alive (or
that it stood down on purpose), and an `inbox/`/`done/` pair of
directories for receiving messages.

A **task** is the unit of work the orchestrator hands to a worker. Tasks
are themselves files (`state/orchestrator/tasks/<task_id>.json`) so the
orchestrator's own queue survives its own restarts. When the
orchestrator decides a task is ready to run, it writes a mailbox message
into the target worker's `inbox/` describing the task; the worker reads
it, does the work, and reports back (also via a mailbox message, into
the orchestrator's own inbox) when done.

A **gate** is a category of action (defined in `gates.json`) that must
never happen without a human's explicit approval, no matter how
confident the code or a worker is that it's fine. When a worker is about
to perform a gated action, instead of performing it, it creates a file
in `operator-pending/` describing what it wants to do and why, sets its
own `state.json` phase to `waiting_on_operator`, and stops — it does not
guess, escalate its own authority, or time out and proceed anyway.

The **dashboard** is a small local web page. It does not hold any state
of its own; every time it's loaded or polled, its backing server
(`dashboard/server.py`) walks the same directories described above and
computes the current picture fresh.

Directory layout (created by the code in this plan, all paths relative
to the repository root):

```
orchestrator/            orchestrator + shared library code (Python)
  common.py               atomic writes, mailbox, heartbeats, timestamps
  config.py                constants: intervals, fan-out cap, paths
  gates.py                 gate lookup + operator-pending queue helpers
  tick.py                   the Gather/Process/Advance/Render loop, CLI
examples/                 fixture workers — NOT production code
  worker_common.py          shared scaffolding used by both example workers
  fast_worker.py             near-instant example worker
  slow_worker.py             long-simulated-task example worker
dashboard/
  server.py                 stateless stdlib HTTP server
  index.html                 polls the server, renders the live picture
scripts/                  operator-facing and test-facing CLI helpers
  submit_task.py             enqueue a task for a worker (testing/ops)
  resolve_pending.py         approve/deny an operator-pending item
  run_validation.sh          exercises every Validation & Acceptance item
gates.json                the editable, reviewable human-gates config
state/<worker>/state.json               per-worker durable state
state/orchestrator/tasks/<id>.json      durable task queue entries
state/orchestrator/dispatch_log.jsonl   append-only dispatch journal
mailboxes/<worker>/inbox/               pending messages, oldest-first
mailboxes/<worker>/done/                 permanent processed-message record
heartbeats/<worker>/heartbeat.json      liveness proof, per worker
operator-pending/                       queue of items awaiting a human
operator-pending/resolved/               archived after resolution
logs/orchestrator.log                   human-readable tick log (quiet on quiet ticks)
```

## Plan of Work

Work proceeds bottom-up: first the primitives every other piece depends
on (atomic writes, mailbox send/receive, heartbeat read/write — all in
`orchestrator/common.py`), then the human-gates mechanism (since both the
orchestrator and the example workers need to be able to check gates and
file pending items), then the orchestrator tick loop itself, then the two
example workers (which exist to exercise everything built so far), then
the dashboard (a pure read-only consumer of the same files), then the
small operator CLI scripts, and finally a validation pass that walks
every acceptance criterion in the originating prompt end to end using the
example workers, with fixes applied immediately if any criterion doesn't
actually hold rather than being noted as a caveat.

Every module is deliberately small and readable in one sitting — this is
infrastructure meant to be debugged by a human reading source, not by
tracing through abstractions.

## Concrete Steps

1. **`orchestrator/config.py`** — module-level constants: repository-root-
   relative `Path` objects for each top-level directory in the layout
   above, `DEFAULT_TICK_INTERVAL_SECONDS = 300`, `MAX_CONCURRENT_HEAVY =
   1`, `STALENESS_MULTIPLIER = 2.5`. No logic, just named values so
   nothing important is a magic number scattered across files.

2. **`orchestrator/common.py`** — the shared primitives:
   - `now_iso() -> str`: current UTC time as an ISO-8601 string with
     microsecond precision and an explicit `+00:00` offset.
   - `atomic_write_json(path, data)`: creates parent directories if
     needed, writes `json.dumps(data, indent=2, sort_keys=True)` to a
     temp file in the *same directory*, then `os.replace()`s it into
     place. This is the only way any `state.json`, `heartbeat.json`, or
     task file is ever written in this codebase.
   - `read_json(path, default=None)`: reads and parses a JSON file,
     returning `default` if the file doesn't exist yet (never raises for
     "not created yet," since that's a normal state for a fresh worker).
   - `send_mailbox_message(to_worker, from_worker, msg_type, payload)`:
     builds a filename `f"{ts}-{rand6}-{msg_type}.json"`, writes the
     message body via the same temp-then-rename pattern directly into
     `mailboxes/<to_worker>/inbox/`, and returns the message id (the
     filename without extension) so callers can log it in the dispatch
     journal.
   - `read_inbox(worker) -> list[Path]`: returns every file in
     `mailboxes/<worker>/inbox/` sorted by filename (lexicographic sort
     is chronological, since the filename is timestamp-prefixed) —
     oldest first.
   - `complete_inbox_message(worker, path)`: moves (via `os.replace`,
     which is an atomic rename within the same filesystem) a message
     from that worker's `inbox/` to its `done/`.
   - `write_heartbeat(worker, ok, interval_seconds, summary, status="running")`:
     atomic-writes `heartbeats/<worker>/heartbeat.json`.
   - `heartbeat_status(worker) -> dict`: reads a worker's heartbeat and
     returns a computed dict including `is_stalled` (bool, always
     `False` if `status == "stood_down"` regardless of age) and
     `age_seconds`, so both the orchestrator's Gather step and the
     dashboard share one staleness computation instead of two
     potentially-diverging ones.

3. **`gates.json`** — the default, human-editable list of gated action
   types. Each entry: `{"action_type": ..., "category": one of the four
   named in the prompt, "reason": a one-sentence human explanation}`.
   Default proposed entries (flagged for operator review in the final
   summary, not treated as finished policy):
   - `delete_data` (irreversible)
   - `merge_branch` (irreversible)
   - `send_external_message` (irreversible + trust-boundary — anything
     leaving the system to a real external recipient)
   - `publish_release` (irreversible)
   - `write_unauthorized_repo` (trust boundary — writing anywhere not
     already explicitly pre-approved)
   - `worker_escalation` (ambiguous authorization — a worker's own
     signal that it can't tell if it's allowed to do something; always
     present in the default list so the "escalate rather than assume"
     rule in goal 5 has a concrete landing spot)
   - `spend_above_threshold` (spend/scope/policy)
   - `policy_change` (spend/scope/policy — includes edits to this very
     file, the fan-out cap, or the pre-approved-repo list)
   - `external_notify` (trust boundary — the example-worker demo action;
     see step 6 below. This entry exists to give the example workers
     something concrete to trip on, not because it's inherently more
     important than the others.)

4. **`orchestrator/gates.py`**:
   - `is_gated(action_type) -> dict | None`: looks up `action_type` in
     `gates.json`; returns the matching entry or `None`. Unlisted action
     types are **not** gated — autonomy is the default, exactly as
     specified.
   - `file_pending(action_type, requested_by, description, payload) ->
     str`: atomic-writes a new file into `operator-pending/` (filename
     timestamp-prefixed like mailbox messages, for the same ordering
     reason) with `status: "pending"`, and returns its id.
   - `list_pending() -> list[dict]`: every file currently in
     `operator-pending/` (not `resolved/`), for the dashboard and the
     orchestrator's own Gather step to resurface every tick.
   - `resolve_pending(pending_id, decision, resolved_by, note="") ->
     dict`: decision is `"approved"` or `"denied"`; atomic-updates the
     pending item's `status`, then moves it to
     `operator-pending/resolved/`. Used by both
     `scripts/resolve_pending.py` and the dashboard's Approve/Deny
     buttons — one code path, two entry points.

5. **`orchestrator/tick.py`** — the orchestrator's own state lives at
   `state/orchestrator/state.json` and `heartbeats/orchestrator/heartbeat.json`,
   exactly like any other worker; its "inbox" is `mailboxes/orchestrator/inbox/`,
   where task-completion / task-failure / escalation reports from workers
   arrive. One `run_tick()` function performs, in strict order:
   - **Gather**: read every task file under `state/orchestrator/tasks/`,
     every worker's heartbeat (for the dashboard's benefit, not for
     dispatch decisions — a worker being stale doesn't block dispatch by
     itself in this design, since a message left in its inbox is safe to
     wait in), and compute the set of `status == "pending"` tasks that
     are eligible: eligible means (task is not `heavy`) **or** (count of
     tasks currently `status in {"dispatched","in_progress"}` with
     `heavy == true` is `< MAX_CONCURRENT_HEAVY`). Purely computed from
     disk contents; no judgment calls.
   - **Process inbox**: `read_inbox("orchestrator")`, oldest first. For
     each message: if `type == "task_completed"` or `"task_failed"`,
     atomic-update the corresponding task file's `status`; if
     `type == "escalation"`, call `gates.file_pending(...)` for a
     `worker_escalation` gate. Then `complete_inbox_message`.
   - **Advance**: from the eligible set computed in Gather, pick the
     single oldest-by-`created_ts` eligible task (if any). Before
     touching its status: append a `{"phase": "intent", ...}` line to
     `state/orchestrator/dispatch_log.jsonl` (append-only, `open(...,
     "a")` plus `f.flush(); os.fsync(f.fileno())` — this file's entire
     purpose is surviving a crash between "decided" and "did," so it is
     the one place in this codebase where append, not replace, is
     correct). Then call `send_mailbox_message` to the target worker
     with `type="dispatch_task"`. Then atomic-write the task file's
     `status` to `"dispatched"`. Then append a `{"phase": "committed",
     ...}` line to the same journal. See Idempotence and Recovery below
     for exactly how a crash between any two of these steps is resolved
     on the next tick.
   - **Render**: compute a summary dict (counts of tasks by status, any
     stalled workers, any unresolved operator-pending items, last
     dispatch). Compare its hash against
     `state/orchestrator/last_render_summary.json`; if unchanged,
     nothing is written (a quiet tick stays quiet). If changed,
     atomic-write the new summary there and append one line to
     `logs/orchestrator.log` describing what changed. The dashboard does
     **not** read this summary file — it computes its own view directly,
     per the "no cached state" decision above; this render output is the
     tick loop's own quiet/noisy log discipline, a separate concern.
   - CLI: `python3 orchestrator/tick.py --once` runs a single tick and
     exits (used by tests and by anyone who'd rather drive this from an
     external cron than an internal loop); `--loop [--interval-seconds N]`
     runs `run_tick()` in a `while True` with a heartbeat write before
     each sleep, default interval from `config.py`.

6. **`examples/worker_common.py`** — scaffolding shared by both example
   workers, parameterized by worker name:
   - On each cycle: `write_heartbeat(...)` first (see step 4a's ordering
     note about re-stamping *before* long work — the slow worker calls
     this scaffolding function directly around its long-task branch so
     it controls exactly when the longer interval is stamped).
   - Read own `state.json`. If `phase == "in_progress"` with a
     `current_task_id` that still has a matching (not-yet-`done`)
     message in this worker's own `inbox/`, treat this as a resume:
     redo the (idempotent, side-effect-free-if-repeated) task work, then
     proceed to completion exactly as if freshly claimed. This is what
     makes `kill -9` mid-task safe: the claim was already durably
     recorded before the crash, so the restart doesn't need to guess.
   - Otherwise, if `phase == "idle"`, `read_inbox(own_name)`; if empty,
     nothing to do this cycle. If non-empty, take the oldest message.
   - **Claim-before-work ordering** (the write-before-flip rule applied
     to workers): atomic-write `state.json` to `phase="in_progress"`,
     `current_task_id=<id from message>` **before** doing any task work
     and before moving the message out of `inbox/`. Do the work. If the
     task's `type` matches a `gates.json` entry, call
     `gates.file_pending(...)` instead of performing it, set
     `phase="waiting_on_operator"` with a pointer to the pending id, and
     stop this cycle without completing the task or moving the message
     (it will be picked back up once the pending item resolves — see
     next bullet). Otherwise, after the work completes: atomic-write
     `state.json` to `phase="idle"`, `last_action={"type":
     "completed_task", ...}`; **then** `complete_inbox_message` (move to
     `done/`); **then** `send_mailbox_message` to `"orchestrator"` with
     `type="task_completed"`.
   - If `phase == "waiting_on_operator"`: check whether the referenced
     pending item has moved to `operator-pending/resolved/` with
     `status == "approved"` or `"denied"`. If not yet resolved, do
     nothing this cycle (still waiting — no timeout, matches the mailbox
     "sits until read" philosophy applied to human gates). If approved,
     perform the previously-deferred action, then complete exactly as
     above. If denied, mark the task `failed`, report
     `type="task_failed"` with the denial reason, and return to `idle`.
   - `--once` (process at most one cycle then exit — used by the kill/
     recovery validation script to get precise control) and `--loop
     [--interval-seconds N]` CLI modes, same shape as the orchestrator.

7. **`examples/fast_worker.py`** — thin wrapper around
   `worker_common.run(worker_name="fast-worker", ...)` with a task
   implementation that does effectively nothing (e.g. formats a string)
   and a short default interval (`5` seconds), to prove the baseline
   plumbing (dispatch → claim → complete → report) end to end quickly.

8. **`examples/slow_worker.py`** — wrapper around
   `worker_common.run(worker_name="slow-worker", ...)` whose task
   implementation sleeps for `SLOW_WORKER_TASK_SECONDS` (env var,
   default `45`, overridden to a small value like `8` by the validation
   script so tests don't take real minutes) in short increments,
   simulating "realistic long-running work." Immediately before starting
   that sleep, it calls `write_heartbeat(..., interval_seconds=task_seconds
   + buffer)` so the 2.5x staleness window covers the whole task; after
   the task finishes it re-stamps back to its normal short interval. Its
   task list includes the one `heavy: true` example task type
   (`dummy_heavy_work`) used to exercise the fan-out cap, and its inbox
   is also where an `external_notify`-typed task is sent during
   validation to exercise the human-gate path end to end.

9. **`dashboard/server.py`** — `http.server.BaseHTTPRequestHandler`
   subclass, stdlib only, no threading needed for this scale. Two routes:
   `GET /api/snapshot` walks every directory in the layout fresh on each
   call (worker states, heartbeats with `heartbeat_status()` staleness
   computed at request time, task counts by status, inbox/done counts
   per worker, unresolved `operator-pending` items) and returns it as
   JSON; `POST /api/resolve` accepts `{pending_id, decision}` and calls
   `gates.resolve_pending(...)`. `GET /` serves `dashboard/index.html`
   as a static file. Run via `python3 dashboard/server.py [--port 8765]`.

10. **`dashboard/index.html`** — single static file, vanilla JS
    (`fetch('/api/snapshot')` on load and every 3 seconds), no build
    step, no framework. Renders: a worker table (name, phase, heartbeat
    age vs. threshold with a clear stalled/ok/stood-down indicator), a
    task table (id, worker, status, heavy flag), an operator-pending
    list with inline Approve/Deny buttons that `POST /api/resolve`, and
    a raw view of `gates.json`'s entries so an operator can see the
    active policy without leaving the page.

11. **`scripts/submit_task.py`** — CLI: `python3 scripts/submit_task.py
    --worker slow-worker --type dummy_heavy_work --heavy` (etc.) —
    atomic-writes a new `state/orchestrator/tasks/<id>.json` with
    `status="pending"`. Exists so tests and a real operator have one
    sanctioned way to enqueue work rather than hand-crafting task files.

12. **`scripts/resolve_pending.py`** — CLI: `python3
    scripts/resolve_pending.py <pending_id> approve|deny [--note ...]` —
    thin wrapper over `gates.resolve_pending`, the non-dashboard way for
    an operator to clear a gate.

13. **`scripts/run_validation.sh`** — drives every item in Validation and
    Acceptance below in sequence, printing `PASS`/`FAIL` per item and
    exiting non-zero on any failure. This is what actually gets run to
    call goal 6 and the self-reflection bar satisfied, not a description
    of what *could* be run.

## Validation and Acceptance

Each item below is both an acceptance criterion from the prompt and a
concrete, scripted step in `scripts/run_validation.sh`. All commands are
run from the repository root.

1. **Kill-and-recover mid-task, no double-execution, no lost work.**
   Submit a task to `slow-worker` with a short `SLOW_WORKER_TASK_SECONDS`,
   start it with `--once` in the background, wait until its `state.json`
   shows `phase="in_progress"` (proving the claim was durably written),
   `kill -9` the process mid-sleep, then start a fresh `--once` process.
   Expect: the task's mailbox message is still correctly handled exactly
   once (the fresh process resumes and completes it — verified by
   checking `mailboxes/slow-worker/done/` contains exactly one file for
   that task id, and the orchestrator inbox receives exactly one
   `task_completed` message for it, not two).
2. **Concurrent inbox writes, no corruption, order preserved.** Two
   Python processes started simultaneously each write 25 distinct
   messages into the same worker's `inbox/` (using
   `send_mailbox_message`). Expect: 50 well-formed, individually valid
   JSON files exist afterward (no partial/corrupted file, since writes
   are temp-then-rename), and reading the inbox back in filename order
   matches timestamp order for messages from each sender.
3. **Orchestrator crash between dispatch-intent and status-flip.** A
   test hook in the validation script calls the Advance step's pieces
   directly (append intent → simulate crash by stopping before the
   status write) then runs a normal tick. Expect: the next tick detects
   the uncommitted intent (matching a task still `"pending"` whose
   `task_id` has a journal `"intent"` entry with no matching
   `"committed"` entry) and completes it exactly once — checked by
   confirming the target worker's inbox has exactly one message for that
   task id (not zero, not two) and the task ends up `"dispatched"`.
4. Heartbeat behavior, three sub-cases:
   - **4a.** Run `slow-worker` through a task with `SLOW_WORKER_TASK_SECONDS`
     larger than the default staleness window would allow; confirm
     `heartbeat_status("slow-worker")["is_stalled"]` is `False`
     throughout the task, because the re-stamped `interval_seconds`
     scaled the 2.5x window.
   - **4b.** Start a worker, let it heartbeat once, then stop touching
     it (no further cycles) past `2.5 * interval_seconds`; confirm
     `is_stalled` becomes `True`.
   - **4c.** Send a worker a deliberate stop signal (or call its stand-
     down heartbeat write directly); confirm its heartbeat's `status`
     field is `"stood_down"` and `is_stalled` reads `False` regardless
     of subsequent age, and that the dashboard snapshot reflects this as
     idle, not stalled.
5. **Fan-out cap actually enforced.** Submit two `heavy: true` tasks to
   `slow-worker` (`MAX_CONCURRENT_HEAVY = 1`). Run a tick; expect exactly
   one becomes `"dispatched"` and the second remains `"pending"` (visible
   in the Gather eligibility computation and in the resulting task
   files) until the first reaches a terminal status.
6. **Gated action lands in operator-pending and resurfaces; resolves
   correctly.** Submit an `external_notify`-typed task to `slow-worker`.
   Expect: the worker does not perform the notify, `state.json` shows
   `phase="waiting_on_operator"`, a file appears in `operator-pending/`,
   and `GET /api/snapshot` lists it on every poll until resolved.
   Resolve it with `scripts/resolve_pending.py <id> approve`; expect the
   next worker cycle completes the task and the pending item moves to
   `operator-pending/resolved/`.
7. **Dashboard reflects live disk state without relying on a running
   process.** Start `dashboard/server.py`, fetch `/api/snapshot`, mutate
   state on disk directly (e.g. touch a heartbeat file's timestamp
   backward to simulate staleness) without restarting the server or
   touching any orchestrator/worker process, fetch again; expect the
   second response reflects the mutation, proving the server holds no
   stale in-memory cache.

`scripts/run_validation.sh` runs all seven in sequence against a
disposable validation data root (via an env var overriding
`orchestrator/config.py`'s base paths) so it never touches the example
fixtures' real state between runs, and prints a final `ALL PASS` or lists
which item(s) failed.

## Idempotence and Recovery

This section exists because "what happens if this exact step runs twice,
or is interrupted halfway" is the central question this whole system is
built to answer correctly, for every operation that mutates durable
state. Each bullet names the operation, the crash window, and why the
recovery is unambiguous.

- **`atomic_write_json`**: either the temp file exists (nothing has
  changed yet from a reader's point of view — the original file, if any,
  is untouched) or `os.replace` completed and the new content is fully
  visible. There is no window where a reader can observe a partial
  write. A crash mid-write just leaves an orphaned temp file, which is
  harmless and can be ignored (temp filenames include the PID and a
  random suffix, so they never collide with a retry).
- **`send_mailbox_message`**: same atomic-write guarantee, applied to a
  filename that is itself unique (timestamp + random suffix) — so even
  if the *sender* crashes and is restarted and ends up calling this
  again for "the same" logical event, it produces a second distinct
  message rather than corrupting or duplicating a file. Whether that
  results in the receiver doing the work twice is therefore a question
  for the *caller's* idempotence (see dispatch journal below), not for
  the mailbox primitive itself.
- **Dispatch journal (`state/orchestrator/dispatch_log.jsonl`)**: the
  only append-only file in this codebase, by design — it is the record
  of intent that lets a restart distinguish "decided to dispatch but
  hadn't yet" from "already sent." Recovery rule, checked at the start
  of every Advance step: for each task whose `dispatch_log.jsonl` has an
  `"intent"` entry with no matching `"committed"` entry, check whether a
  `dispatch_task` message for that task id already exists in the target
  worker's `inbox/` or `done/`. If yes, the send already happened before
  the crash — just write the missing `"committed"` entry and flip the
  task's status, without sending again. If no, the crash happened before
  the send — perform the send now (safe, since it's the first send),
  then commit. Either path converges to exactly one dispatch message per
  task, regardless of exactly where the crash landed.
- **Worker claim-before-work (`state.json` phase transitions)**: the
  claim (`phase="in_progress"`, `current_task_id=X`) is written before
  any task-specific work happens and before the triggering message is
  moved out of `inbox/`. A crash before the claim write means the
  message is still untouched and unclaimed — a fresh process just
  proceeds normally, nothing was ever considered started. A crash after
  the claim write but before the message is moved to `done/` means a
  fresh process sees `phase="in_progress"` for a task whose message is
  still sitting in `inbox/` — by construction this can only mean "was
  claimed, wasn't finished," so the fresh process safely resumes/redoes
  the (idempotent) task work and then proceeds to completion normally.
  Task implementations in this codebase are required to be safe to redo
  in full (the example tasks are: formatting a string, sleeping a fixed
  duration — neither has a side effect that duplicates on retry), which
  is documented directly in `examples/worker_common.py`'s module
  docstring as a contract any future real task type must also satisfy.
- **Operator-pending items**: never expire, never get deleted except by
  moving to `resolved/` as part of an explicit resolution call. A crash
  at any point leaves the item exactly where it was — `pending/` or
  `resolved/` — with no in-between state to recover from, since the
  resolve operation is itself a single atomic status update followed by
  a move, and a worker re-checking an item's status after a crash simply
  re-reads whichever state it's actually in.

## Artifacts and Notes

Schemas (all JSON, all written via `atomic_write_json` unless noted
append-only):

**`state/<worker>/state.json`**
```json
{
  "worker": "slow-worker",
  "phase": "idle | in_progress | waiting_on_operator | stood_down",
  "current_task_id": "task-... | null",
  "waiting_on_pending_id": "pending-... | null",
  "last_action": {"type": "completed_task", "task_id": "...", "ts": "..."},
  "updated_ts": "2026-07-18T19:30:45.123456+00:00"
}
```

**`state/orchestrator/tasks/<task_id>.json`**
```json
{
  "id": "task-20260718T193000000000Z-abc123",
  "worker": "slow-worker",
  "type": "dummy_heavy_work",
  "heavy": true,
  "status": "pending | dispatched | in_progress | done | failed | waiting_on_operator",
  "payload": {},
  "created_ts": "...",
  "dispatched_ts": null,
  "completed_ts": null
}
```

**Mailbox message** (`mailboxes/<worker>/inbox|done/<ts>-<rand6>-<type>.json`)
```json
{
  "id": "20260718T193000000000Z-abc123-dispatch_task",
  "from": "orchestrator",
  "to": "slow-worker",
  "type": "dispatch_task | task_completed | task_failed | escalation",
  "ts": "...",
  "payload": {"task_id": "task-..."}
}
```

**`heartbeats/<worker>/heartbeat.json`**
```json
{
  "worker": "slow-worker",
  "ts": "...",
  "ok": true,
  "interval_seconds": 300,
  "summary": "idle, watching inbox",
  "status": "running | stood_down"
}
```

**Dispatch journal line** (`state/orchestrator/dispatch_log.jsonl`, one JSON object per line, append-only)
```json
{"ts": "...", "phase": "intent | committed", "task_id": "...", "worker": "...", "message_id": "..."}
```

**`operator-pending/<id>.json`**
```json
{
  "id": "pending-...",
  "ts": "...",
  "requested_by": "slow-worker",
  "action_type": "external_notify",
  "category": "trust_boundary",
  "description": "...",
  "payload": {},
  "status": "pending | approved | denied",
  "resolved_ts": null,
  "resolved_by": null,
  "note": null
}
```

**`gates.json`** — top-level array of entries shaped as
`{"action_type", "category", "reason"}` — see Concrete Steps item 3 for
the proposed default contents.

## Interfaces and Dependencies

- **Runtime**: Python 3 (developed against 3.14, no version-specific
  features used beyond what's been standard for years — should run on
  any reasonably current Python 3). No third-party packages; standard
  library only (`json`, `os`, `pathlib`, `datetime`, `uuid`, `argparse`,
  `http.server`, `hashlib`, `time`).
- **Filesystem assumptions**: `os.replace` must be atomic on the target
  filesystem, which holds for any local POSIX filesystem (and NTFS on
  Windows) but would *not* hold across a network filesystem boundary if
  the temp file and destination ended up on different mounts — this
  codebase never does that (every temp file is written into the exact
  directory of its final destination), so the assumption holds as long
  as the whole `state/`, `mailboxes/`, `heartbeats/`, `operator-pending/`
  tree stays on one filesystem.
- **Process model**: workers and the orchestrator are independent OS
  processes, started and stopped by whatever starts them (a human, a
  process supervisor, or — for the example fixtures — the validation
  script). Nothing in this design requires any process to be started by
  any other process; the orchestrator dispatches by writing a mailbox
  message, not by spawning a worker subprocess, which is what makes "a
  message to an offline recipient just sits there" true rather than
  aspirational.
- **Dashboard**: consumes the same on-disk files as everything else;
  its only "interface" is reading `state/`, `heartbeats/`, `mailboxes/`,
  `operator-pending/`, and `gates.json`, and writing to
  `operator-pending/` (moves only, via the same `gates.resolve_pending`
  function CLI use). Runs on `localhost` only by default.
- **No network calls, no external services** anywhere in this codebase.
  The `external_notify` example task type deliberately does not actually
  send anything externally even once approved — it's a stand-in that
  logs what it *would* send, since actually wiring a real external
  notification integration is out of scope for a coordination-system
  fixture and would violate the "trust boundary" gate it's meant to
  demonstrate if it ran unattended by default.
