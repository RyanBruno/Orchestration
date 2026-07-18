# AGENTS.md

This repository is a file-based worker orchestration system. Its entire
value proposition is that state lives on disk and survives process death —
which means changes to it are easy to get subtly wrong in ways that only
show up when something crashes at an inconvenient moment.

## Execution plans are mandatory for non-trivial work

Any complex feature or significant refactor in this repo — anything that
touches crash recovery, the mailbox protocol, the tick loop, heartbeat
staleness rules, or the human-gates mechanism, or that spans multiple
files with interacting invariants — goes through an **execution plan**
before and during implementation, not after.

The methodology for what an execution plan is, how to write one, and how
to keep it updated lives in [`.agent/PLANS.md`](.agent/PLANS.md). Read
that file before starting this kind of work. Plans themselves live in
`.agent/execplans/`.

Small, self-contained fixes (a typo, a one-line bug fix with no crash-path
implications, a comment) do not need a plan. If you are unsure whether
your change qualifies, err toward writing one — the cost of a plan you
didn't strictly need is much lower than the cost of a subtle recovery bug
shipped without one.

## Ground rules specific to this codebase

- **The filesystem is the source of truth.** Never make a design decision
  that requires a running process to remember something across a restart.
  If you catch yourself writing "the orchestrator keeps track of X in
  memory," stop and put X in a state file instead.
- **Writes are atomic-replace, never append**, for anything that
  represents "current state" (`state.json`, `heartbeat.json`, task files).
  Write to a temp file in the same directory, then `os.replace` it into
  place. The only intentionally append-only files are logs and the
  dispatch journal, and those are append-only by design, not by accident.
- **Mailbox messages are one file per message**, named so lexicographic
  sort equals chronological order, written by temp-then-rename so a
  reader never sees a half-written message, and moved (not copied, not
  deleted) from `inbox/` to `done/` once handled.
- **Write intent before flipping status.** Anywhere a crash between two
  steps could leave the system unable to tell what happened, log the
  intent first, perform the effect, then record completion — and make
  sure a restart can look at that sequence and know unambiguously what to
  do next.
- Before calling any change to the coordination core "done," ask: if this
  process were `kill -9`'d at the worst plausible moment, could a fresh
  process recover correctly from disk alone? If the answer isn't a clear
  yes, it isn't done.
