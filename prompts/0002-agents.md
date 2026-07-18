## Goal

Extend the file-based orchestration system already built in this repository so
that a worker's task handler can drive a real Claude Code agent session —
not a local Python function — to do the actual work, with the specific agent
behavior for each worker/task type controlled by a declarative harness
configuration rather than hardcoded logic. Prove this end to end by building
one real (non-fixture) production worker: a message-management agent whose
task types are reviewing messages, drafting a response to a message, and
summarizing a time period of messages, where "messages" are files that live
in that worker's own working directory.

This is an extension of the existing system, not a from-scratch build. Read
`AGENTS.md`, `.agent/PLANS.md`, and
`.agent/execplans/orchestration-file-based-coordination.md` in full before
writing anything — the crash-recovery invariants, mailbox protocol,
heartbeat rules, and gate mechanism they describe are the foundation this
work sits on, and nothing here should weaken any of them.

## Goals this plan must achieve

1. **Agent-invocation handler pattern.** Define how a task handler in this
   system invokes a real Claude Code agent instead of running local Python
   logic — pick either the Claude Agent SDK or headless CLI (`claude -p`)
   as the mechanism, and document your reasoning against this repo's
   existing "minimal dependencies, standard library where possible" stance
   from the Decision Log. Build this as shared plumbing (a new module
   alongside `orchestrator/common.py` and `examples/worker_common.py`, not
   duplicated per worker) so every agent-backed worker calls through the
   same interface.

2. **Idempotence via session resumption, not full re-run.** The existing
   contract — "a task handler must be safe to run again in full if the
   process crashes mid-task" — does not hold for a handler that drives an
   LLM agent editing files or running commands; re-running from scratch is
   not deterministic and can duplicate effects. Replace that contract for
   agent-backed handlers specifically: the moment an agent session starts,
   its session id must be captured and durably written into the task's own
   state (`atomic_write_json`, same as everywhere else in this codebase)
   *before* the agent takes any further action — this is the same
   write-before-flip discipline the dispatch journal already uses, applied
   one layer up. On recovery, a handler with a recorded session id must
   resume that session rather than starting a new one, so the agent has
   its own memory of what it already did. Prove this with an actual
   `kill -9` mid-session test, the same way the existing validation suite
   proves dispatch-journal recovery — don't accept an argument in prose in
   place of a real test.

3. **Harness as declarative configuration.** A worker's or task type's
   behavior — which tools it may use, its permission mode, its system
   prompt or CLAUDE.md, which MCP servers it can reach, its working
   directory — must live in an editable config artifact per harness, not
   be hardcoded per worker in Python, following the same principle
   `gates.json` already establishes ("policy is an editable config, not
   logic baked into code"). Two different task types dispatched to
   agent-backed handlers should be able to behave completely differently
   by pointing at different harness configs, with no duplicated Python.

4. **Worktree isolation for anything that touches a real repository.** Any
   agent-backed handler whose work involves editing files in a
   git-tracked repository must do that work in its own isolated
   worktree/branch, never directly on a shared checkout, so a resumed or
   retried session can't collide with another task's in-flight edits.
   Nothing merges a worktree's changes back without going through this
   repo's existing `merge_branch` gate in `gates.json` — do not add a new
   gate category for this if the existing one already covers it; if you
   decide it doesn't, say why in the Decision Log before adding one.

5. **Heartbeat discipline extended to agent calls.** Before invoking a
   (potentially long) agent session, a handler must re-stamp its worker's
   heartbeat with an interval realistic for that call, using the same
   before-not-after ordering and buffer pattern `examples/slow_worker.py`
   already uses for its simulated long task — an agent call is exactly
   the kind of legitimately-slow work that pattern exists for.

6. **Build one real production worker: a message-management agent.** This
   is not a fixture under `examples/` — it is real functionality, and it
   is also how goals 1–5 above get proven end to end, the same way the
   original fixtures proved the base system. Requirements:
   - Messages are files that live in this worker's own working directory.
     This is a distinct concept from the orchestrator's `mailboxes/`
     protocol (which is for worker-to-worker coordination messages) — pick
     a location and file format for these message files, document the
     choice in the Decision Log, and be explicit in the plan about how
     the two are different so a future reader never conflates them.
   - The worker must handle at least these task types, dispatched the same
     way any other task is (via `scripts/submit_task.py`, processed by the
     orchestrator's normal tick loop): reviewing the messages currently
     present (read and triage them), generating a response to a specific
     message, and summarizing all messages within a given time period.
   - Give this worker its own harness (per goal 3): tools and permissions
     scoped to what a message-management job actually needs, and a system
     prompt / CLAUDE.md describing its job in plain language.
   - Exercise it with real files you create for testing purposes — don't
     just argue it works from reading the code.

## A note on effort and verbosity

Use a high reasoning effort if your environment exposes that setting — goal
2 in particular (session-resumption recovery replacing full-handler-rerun)
is exactly the kind of interacting-invariant change to a crash-recovery
system that's easy to get subtly wrong under fast pattern-matching, and it
changes a guarantee the rest of this codebase currently depends on. Keep
narration concise; keep the plan's Concrete Steps, the harness config
schema, and the message file format fully explicit.

## How you will work: one continuous ExecPlan pass

This repository already has the ExecPlan convention established —
`AGENTS.md` and `.agent/PLANS.md` define it, and
`.agent/execplans/orchestration-file-based-coordination.md` is a worked
example of the whole system being built this way. Don't re-derive the
methodology; follow it as written. Author the plan at
`.agent/execplans/agentic-workers-and-message-agent.md`, then implement it
end-to-end in this same session. Do not pause after writing the plan to ask
whether to proceed.

<persistence>
- Work through all six goals end to end; don't stop because the first few
  look done — an agent-invocation pattern with no working recovery story,
  or a harness system that's still secretly hardcoded per worker, doesn't
  give you what this plan is for.
- Don't come back to ask which of several reasonable choices to make — SDK
  vs. CLI, harness config file format, message file format and location,
  exact task-type names. Pick one, document your reasoning in the Decision
  Log, and continue.
- The only reasons to stop and ask are the stop conditions listed later in
  this prompt.
</persistence>

<context_gathering>
Goal: before writing anything, understand exactly how the existing system
works and where this plan's new pieces attach to it — this is an
extension, not a from-scratch build, so context here means reading the
existing code and plan closely, not making foundational choices in a
vacuum.
Method: read `AGENTS.md`, `.agent/PLANS.md`, the existing execution plan in
full, `orchestrator/common.py`, `orchestrator/tick.py`,
`examples/worker_common.py`, and `gates.json` before writing your own plan.
Check whether the environment has `ANTHROPIC_API_KEY` or another working
Claude Code auth method available — this determines whether you can
validation-test a live agent invocation or need to build the
agent-invocation call behind an interface you can stub for testing, with a
documented note that live end-to-end testing is still needed once
credentials are available.
Early stop: once you can name exactly which existing files each new piece
extends or sits alongside, stop gathering context and move to the plan.
Escalate once: only if you find an existing invariant this plan would need
to violate to succeed — otherwise proceed and document the judgment call.
</context_gathering>

<code_editing_rules>
<guiding_principles>
- Everything in this repo's existing AGENTS.md still applies without
  exception: the filesystem is the source of truth, writes to anything
  representing current state are atomic-replace, intent is written before
  effects, and a `kill -9` at the worst plausible moment must still allow
  correct recovery from disk alone.
- Idempotence is redefined, not abandoned, for agent-backed handlers:
  "safe to recover from a crash" now means "resumable via session id," not
  "safe to blindly re-run in full." Say explicitly, wherever this applies,
  which contract a given handler follows.
- Harnesses are configuration, not code: the same principle that makes
  `gates.json` reviewable by a non-programmer should make a harness config
  reviewable the same way.
- Minimal dependencies: prefer the standard library and this repo's
  existing patterns over new frameworks; if you add the Agent SDK as a
  dependency, say why the CLI-subprocess alternative wasn't sufficient.
- Legibility: a human should be able to read a harness config, a message
  file, or a task's recorded session id and understand what happened
  without running anything.
</guiding_principles>
<layout>
Extend, don't restructure, the existing layout:
orchestrator/agent_runner.py (or similar) — shared plumbing for invoking a
  Claude Code session from a handler, mirroring the role worker_common.py
  plays for the toy fixtures
harnesses/<name>.json (or equivalent) — one declarative harness config per
  worker/task-type role
workers/message-agent/ — the new production worker: its own working
  directory, its message files, its task handlers, its harness
Keep examples/ as-is — those fixtures still validate the base system and
should keep passing.
</layout>
</code_editing_rules>

<tool_preambles>
- Before starting, restate the goal in a sentence or two in your own
  words.
- Once the plan's Progress section exists, share the milestone list.
- Give brief sequential narration as you work — one or two sentences per
  milestone, not per file edit.
- Keep the repo in a working, committed state at logical checkpoints,
  matching this repo's existing commit conventions.
- When you finish, summarize what was built, and call out explicitly
  whether live agent-invocation was actually tested against real
  credentials or only stubbed, so I know which validation still needs to
  happen on my end.
</tool_preambles>

<self_reflection>
Before marking any milestone done, check it against this: if the process
running an agent-backed handler is `kill -9`'d mid-session, does a fresh
process resume the same Claude Code session rather than starting over
blind; does swapping a harness config actually change agent behavior with
zero Python changes; does any agent-backed handler that edits a real repo
do so only inside an isolated worktree; does the message-management
worker's review/respond/summarize behavior actually run against real
message files you created, not just read plausibly from the code. If a
milestone doesn't clear this, fix it before moving on rather than noting
it as a caveat.
</self_reflection>

## Stop conditions (the only reasons to pause and ask me)

- You need a credential, API key, or account access only I have, and no
  stub/interface approach lets you make verifiable progress without it.
- An action would be destructive or irreversible against real state (not
  just a reversible change to files in this repo).
- A required tool or runtime is genuinely unavailable in this environment.

Everything else — including SDK vs. CLI, harness config format, message
file format — make the most reasonable decision, record it with your
reasoning in the Decision Log, and continue.

## Validation and acceptance

- Killing a process running an agent-backed handler mid-session, then
  starting a replacement, results in that replacement resuming the same
  Claude Code session (verified by session id, not just assumed) rather
  than starting a new one.
- Two task types dispatched to agent-backed handlers with different
  harness configs demonstrably behave differently (different allowed
  tools or different instructions produce visibly different agent
  behavior), using the same handler-invocation code path for both.
- Any agent-backed handler that edits files in a real git repository does
  so in an isolated worktree; those edits do not appear on the shared
  branch until the existing `merge_branch` gate is explicitly approved.
- The message-management worker, given real message files you create for
  the test, correctly handles at least one review task, one
  response-generation task, and one summarize-a-time-period task,
  dispatched through the normal `scripts/submit_task.py` → orchestrator
  tick → worker cycle path — not called directly, bypassing the
  coordination system.
- The existing validation suite (`scripts/run_validation.py`) still passes
  unmodified, proving none of this weakened the base system's guarantees.

## Wrap-up

Finish with an Outcomes & Retrospective entry stating, for each of the six
goals above, whether it was fully implemented, partially implemented, or
hit a real constraint worth documenting — run the result against the
`<self_reflection>` bar one more time — and separate clearly what was
actually tested against a live Claude Code session from what was stubbed
pending credentials.
