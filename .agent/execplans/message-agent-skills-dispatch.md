# Execution Plan: message-agent Skills Dispatch

Status: in progress. Last updated: 2026-07-18.

## Purpose / Big Picture

`.agent/execplans/agentic-workers-and-message-agent.md` built `message-agent`
as a real, agent-backed worker with three separate task types
(`review_messages`, `draft_response`, `summarize_period`), each with its own
harness file (`harnesses/message-review.json`, `message-respond.json`,
`message-summarize.json`) carrying its own tool allowlist and its own
step-by-step procedure baked into `append_system_prompt`. An operator who
wants message-agent to do something has to already know, and correctly name
via `--type`, which of the three capabilities applies.

This plan narrows that down to one generic harness
(`harnesses/message-agent.json`, `task_type: "handle_request"`) whose prompt
template embeds a single free-text `request` field. The decision of *which*
capability applies — review, respond, or summarize — moves from "which
`--type` did the operator pass" to "which Claude Code Skill does the agent
itself discover and apply," using Claude Code's own skill auto-discovery
mechanism (`.claude/skills/*/SKILL.md`, matched against the request by
description). This is purely a narrowing of `message-agent`'s own internal
dispatch, not a rewrite of anything else: `orchestrator/agent_runner.py`'s
crash-recovery mechanics (write-before-launch session id,
artifact-check-before-trust, worktree isolation, the `merge_branch` gate),
`gates.json`, `examples/`, and `scripts/submit_task.py` itself are all
untouched. `--type` stays a required flag on `submit_task.py` — for
message-agent specifically, every task now passes the same value,
`handle_request`, but the flag itself, and every other worker's use of it,
is unchanged.

Read `.agent/execplans/orchestration-file-based-coordination.md` and
`.agent/execplans/agentic-workers-and-message-agent.md` first — this plan
assumes the vocabulary and crash-recovery model both establish (worker,
task, mailbox, gate, harness, session id, worktree) rather than re-deriving
it.

## Progress

- [x] 2026-07-18 — Read both prior execution plans in full, plus
      `orchestrator/agent_runner.py`, `workers/message-agent/worker.py`,
      `workers/message-agent/magent_config.py`, the three existing harness
      files, `workers/message-agent/seed/CLAUDE.md`, and the relevant
      sections of `scripts/run_validation.py` and `scripts/submit_task.py`.
      Confirmed `--type` is a plain required argparse flag with no
      message-agent-specific logic in `submit_task.py` — nothing there
      needs to change.
- [x] 2026-07-18 — Fetched current Claude Code documentation
      (`code.claude.com/docs/en/skills`) rather than assuming the shape:
      confirmed `.claude/skills/<name>/SKILL.md` project-level layout,
      confirmed automatic discovery works by loading every skill's
      `description` into context and letting Claude decide relevance (no
      special flag needed to enable auto-discovery), confirmed a linked
      git worktree checkout carries `.claude/skills/` the same way it
      already carries `CLAUDE.md` (both are just tracked files), and
      confirmed the only frontmatter field this plan actually needs is
      `description` (`name` is optional, cosmetic).
- [x] 2026-07-18 — This plan authored.
- [ ] Collapse the three harnesses into `harnesses/message-agent.json`.
- [ ] Write the three `SKILL.md` files under
      `workers/message-agent/seed/.claude/skills/`.
- [ ] Trim `workers/message-agent/seed/CLAUDE.md` to identity + pointer.
- [ ] Update `magent_config.ensure_repo_bootstrapped()` to carry
      `.claude/` into `repo/` and to create `outputs/` instead of
      `reviews/`/`drafts/`/`summaries/`.
- [ ] Rewrite `scripts/run_validation.py` items 8-11 to prove
      differentiation through the single harness/task type.
- [ ] Run `scripts/run_validation.py` end to end; fix any real bugs found.
- [ ] Outcomes & Retrospective.

## Surprises & Discoveries

*(populated during implementation)*

## Decision Log

- **Generic task type: `handle_request`.** Reads naturally as "handle
  whatever this free-text request is," doesn't presuppose review vs.
  respond vs. summarize, and is the one value `scripts/submit_task.py
  --type` needs for every message-agent task going forward.
- **Generic artifact path: `outputs/{task_id}.md`.** Replaces the three
  capability-specific subdirectories (`reviews/`, `drafts/`, `summaries/`).
  This is goal 1's load-bearing requirement, not a cosmetic choice:
  `orchestrator/agent_runner.py`'s `artifact_ready()` check — the only
  signal this codebase trusts for "did the agent's work actually happen,"
  per the prior plan's central spike finding that a resumed session can
  claim success without finishing — must know exactly one path to check
  regardless of which skill ends up handling a given request. A
  per-capability path would reintroduce the "which `--type` was this"
  knowledge this plan is explicitly removing, just moved one layer down
  into artifact-path selection instead of harness selection.
- **`allowed_tools` is the union of all three prior harnesses: `["Read",
  "Glob", "Grep", "Write"]`.** `message-respond.json` previously had a
  narrower set (no Glob/Grep) because its task type always named the exact
  message file directly in a structured payload field (`{message_file}`).
  Under the generic free-text `request` template, the model itself has to
  locate the named message (by sender, subject, or content) rather than
  being handed a pre-resolved path, so Glob/Grep are now needed for that
  skill too. No harness in this plan grants Bash, preserving the existing
  decision that merging is performed by Python, never the agent (see the
  prior plan's Decision Log) — that boundary is orthogonal to which skill
  fires and is not touched by this plan.
- **`harnesses/message-review.json`, `message-respond.json`,
  `message-summarize.json` are deleted, not kept alongside the new one.**
  Keeping them would leave dead configuration nothing loads (
  `_load_harnesses_for_this_worker()` still only maps by `task_type`, and
  nothing submits those task types anymore) and would contradict goal 1's
  "one harness, not three." Deleting them is also what makes goal 6's
  acceptance criterion ("renaming or removing a `SKILL.md` changes what
  the agent can do without touching any Python") legible: with three
  harnesses still present, a reader could plausibly (if incorrectly)
  believe capability selection was still harness-driven.
- **Task payload shape: `{"request": "<free text>"}`, nothing else.**
  The old `draft_response`/`summarize_period` payloads carried structured
  fields (`message_file`, `start`, `end`) that a human or script had to
  pre-fill correctly. Under the new design the operator's payload is
  uniform across all three capabilities — one free-text field — and the
  model itself extracts whatever structure it needs (which message,
  which date range) from that text using its Glob/Grep/Read tools. This
  is exactly goal 3's requirement and is what makes `--type
  handle_request --payload '{"request": "..."}'` capability-agnostic at
  the CLI level.
- **`harnesses/message-agent.json`'s `append_system_prompt` stays generic
  identity + contract, not a capability procedure.** The actual
  review/respond/summarize procedures now live exclusively in each
  `SKILL.md` (goal 4's "don't maintain the same procedure in two places").
  The harness's system prompt is limited to: who you are (a
  message-management agent handling one free-text request), the
  instruction to use whichever skill applies, and the two contract rules
  that don't belong in any one skill specifically because they apply to
  all of them uniformly (write only to the exact given path; never modify
  `messages/`).
- **Each `SKILL.md`'s first output line is the "Handled by" marker (goal
  5).** A one-line header (`Handled by: message-review skill`, etc.) is
  the cheapest possible way to make legible, from the output file alone,
  which skill fired — no cross-referencing the task file, the harness, or
  this plan required. This mirrors the prior plan's own preference for
  legibility recorded directly in artifacts (e.g. `agent_worktree_path`
  being recorded on the task file even though it's derivable from the
  task id).
- **Judgment call on validation items 8-11 (escalation clause considered
  and not triggered).** The originating prompt's acceptance criteria list
  says `scripts/run_validation.py`'s existing items — "both the base suite
  and the prior plan's agent-backed items" — must still pass unmodified,
  while goal 6 separately requires proving three-way skill differentiation
  through the *same* task type and harness. These two statements are in
  direct tension for items 8-10 specifically: their existing bodies
  dispatch the literal task types (`review_messages`, `draft_response`,
  `summarize_period`) this plan deletes the matching harnesses for, so
  running them unmodified is not merely undesirable, it is impossible —
  `harnesses_by_task_type[task["type"]]` would raise `KeyError` before an
  agent session ever started. Read charitably, "unmodified" scopes to the
  base suite (items 1-7, which this plan's Concrete Steps never touch) and
  to the *guarantees* items 8-10 originally proved (kill-9-mid-session
  resumes rather than restarts; worktree isolation holds until gate
  approval), not to their literal source text referencing task types that
  no longer exist. This does not weaken the artifact-check-before-trust
  guarantee in any way goal 1 doesn't already address (the check itself —
  `artifact_ready()` — is unmodified; only which path it's told to check
  changes, and it's told via the same single `artifact_path_template`
  mechanism as before), so per the prompt's own escalation clause this is
  a "proceed and document" situation, not a "stop and ask" one. Items 8-11
  are rewritten to exercise the identical underlying mechanics through the
  new single task type/harness, and item 9 becomes the direct proof for
  goal 6.

## Context and Orientation

Read `.agent/execplans/agentic-workers-and-message-agent.md` first if you
have not — it defines every term this plan builds on (harness, session id,
worktree, the `merge_branch` gate) and this section assumes that vocabulary.

This plan adds one new idea: a **Skill**. A Claude Code Skill is a small
Markdown file (`SKILL.md`) with a YAML frontmatter `description` and a body
of instructions, living under a `.claude/skills/<name>/` directory. Claude
Code loads every discoverable skill's `description` into an agent session's
context automatically (no flag needed to opt in) and decides, itself,
whether a given skill applies to the conversation so far — the same
mechanism that already auto-loads `CLAUDE.md`. Because a linked `git
worktree` is a checkout of the same tracked tree, a `.claude/skills/`
directory committed to `message-agent`'s repo appears in every task's
worktree automatically, exactly the way `CLAUDE.md` already does (an
observation the prior plan already made and this one relies on directly).

Before this plan, "which capability handles this task" was answered by
`workers/message-agent/worker.py` scanning `harnesses/*.json` for the one
whose `task_type` matched the task's own `type` field — a lookup entirely
in Python and task-file data, decided before the agent session ever starts.
After this plan, that lookup still exists, but it only ever resolves to one
harness (`handle_request`); the review/respond/summarize decision moves
inside the agent session itself, made by Claude reading the free-text
`request` and matching it against the three `SKILL.md` descriptions. Nothing
about the orchestrator, the mailbox protocol, the gate mechanism, or
`agent_runner.py`'s crash-recovery state machine needs to know this
happened — from their point of view this is still just "one harness ran an
agent session and either did or didn't produce the expected artifact,"
identical in shape to every agent-backed task before this plan.

## Plan of Work

Work proceeds narrowly, touching only what goals 1-6 require: first the
harness collapse (since the skills' existence doesn't matter until
something dispatches a generic task to them), then the three `SKILL.md`
files and the trimmed `CLAUDE.md` (the actual behavioral content), then the
one-line change to `magent_config.py` needed so a freshly bootstrapped
`repo/` actually carries `.claude/skills/` and has an `outputs/` directory
to write into, then the validation suite rewrite proving all of it,
finally a real end-to-end run with fixes applied immediately if anything
doesn't hold.

`orchestrator/agent_runner.py`, `orchestrator/tick.py`, `orchestrator/
gates.py`, `orchestrator/common.py`, `gates.json`, `examples/`, and
`scripts/submit_task.py` are not modified at all — `agent_runner.py`'s
`render_prompt()` already does a generic `.format(**context)` over
whatever the task's `payload` dict contains, so a payload shaped
`{"request": "..."}` flows through unchanged; no code there references any
capability-specific field name today, so there is nothing to touch.

## Concrete Steps

1. **`harnesses/message-agent.json`** (new) replaces the three deleted
   harness files. Exact contents:
   ```json
   {
     "name": "message-agent",
     "worker": "message-agent",
     "task_type": "handle_request",
     "description": "Generic entrypoint for the message-management agent. Reads one free-text request and, using its own Claude Code Skills (message-review, message-respond, message-summarize), decides whether to review the whole inbox, draft a reply to one message, or summarize a time window, then writes its result to one generic output path.",
     "permission_mode": "bypassPermissions",
     "allowed_tools": ["Read", "Glob", "Grep", "Write"],
     "model": null,
     "effort": "medium",
     "max_budget_usd": 1.0,
     "expected_duration_seconds": 60,
     "heartbeat_buffer_seconds": 60,
     "append_system_prompt": "You are a message-management agent. You are given one free-text request about the messages/ directory of this repository. Decide which of your available Skills applies -- reviewing every current message, drafting a reply to one specific message, or summarizing messages in a date range -- and use it. Whichever skill you use, write your one result file, and nothing else, to the exact path given in your task instructions, and make its first line note which skill handled the request. Never modify any file under messages/. Never write anywhere else.",
     "artifact_path_template": "outputs/{task_id}.md",
     "user_prompt_template": "Task id: {task_id}\nHandle this request: {request}\nWrite your result to exactly this path (relative to the repository root): {artifact_path}\nDo not write anywhere else."
   }
   ```
   Delete `harnesses/message-review.json`, `harnesses/message-respond.json`,
   `harnesses/message-summarize.json`. `workers/message-agent/worker.py`'s
   `_load_harnesses_for_this_worker()` needs no change: it already scans
   `harnesses/*.json` generically and keys by whatever `task_type` it
   finds; with only one file present it now builds a single-entry dict.

2. **`workers/message-agent/seed/.claude/skills/message-review/SKILL.md`**:
   ```markdown
   ---
   name: message-review
   description: Reviews every message currently in this repository's messages/ directory and writes a short triage report covering all of them (sender, subject, urgency). Use when the request asks to review, triage, audit, or check the state of the inbox as a whole, WITHOUT naming one specific message or a date range -- e.g. "review the messages", "what's in the inbox", "triage everything".
   ---

   ## Instructions

   1. Read every file in `messages/`.
   2. For each message, write one line: sender, subject, and a triage
      category (urgent / needs-response / informational), with a
      one-sentence reason for the category.
   3. Write the full report, and nothing else, to the exact path given in
      your task instructions.
   4. Make the report's first line exactly: `Handled by: message-review skill`
   5. Do not modify any file under `messages/`. Do not write anywhere else.
   ```

3. **`workers/message-agent/seed/.claude/skills/message-respond/SKILL.md`**:
   ```markdown
   ---
   name: message-respond
   description: Drafts a reply to one specific, named message. Use when the request names or clearly identifies a single message, sender, or subject that needs a reply, response, or draft -- e.g. "reply to Dave's message about rescheduling", "draft a response to Bob's outage email".
   ---

   ## Instructions

   1. Use Glob/Grep to find the one message file in `messages/` that the
      request refers to, by sender, subject, or content.
   2. Read only that one file.
   3. Draft a clear, professional reply to it.
   4. Write the full draft, and nothing else, to the exact path given in
      your task instructions.
   5. Make the draft's first line exactly: `Handled by: message-respond skill`
   6. Do not modify the original message file. Do not write anywhere else.
   ```

4. **`workers/message-agent/seed/.claude/skills/message-summarize/SKILL.md`**:
   ```markdown
   ---
   name: message-summarize
   description: Summarizes every message whose date falls within a given time window. Use when the request names or clearly implies a date range or period to summarize -- e.g. "summarize messages from July 12 to July 14", "what happened last week", "recap this period".
   ---

   ## Instructions

   1. Every message file under `messages/` has a `date:` field in its
      header block.
   2. Determine the requested window from the request text.
   3. Read every message file; keep only the ones whose date falls within
      the window (inclusive).
   4. Write a concise prose summary of just those messages -- what was
      discussed, by whom, anything time-sensitive -- and nothing else, to
      the exact path given in your task instructions.
   5. Make the summary's first line exactly: `Handled by: message-summarize skill`
   6. Do not modify any file under `messages/`. Do not write anywhere else.
   ```

5. **`workers/message-agent/seed/CLAUDE.md`** (trimmed): keeps the message
   file format (plain-language + the exact header/body shape, since a
   future agent still needs to know how to parse `messages/*.md` and this
   is a fact, not a procedure) and the two contract rules that don't belong
   to any one skill (write only your one given output file; never modify
   `messages/`). Removes the three procedural bullets entirely -- that
   content now lives solely in each `SKILL.md`. Adds one paragraph pointing
   at the skills by name and plain-language description, per goal 4 ("point
   at its skills in plain language"). Full replacement text in Artifacts
   and Notes.

6. **`workers/message-agent/magent_config.py`**: `ensure_repo_bootstrapped()`
   currently copies `seed/messages/` and `seed/CLAUDE.md` into a fresh
   `repo/`, then creates `reviews/`, `drafts/`, `summaries/`. Change:
   also copy `seed/.claude/` into `repo/.claude/` (`shutil.copytree`), and
   create a single `outputs/` directory (with its own `.gitkeep`) instead
   of the three capability-named ones. No other change to this file --
   `REPO_DIR`/`WORKTREES_DIR`/`MESSAGE_AGENT_REPO_DIR` override/idempotence
   behavior is untouched.

7. **`scripts/run_validation.py`**, items 8-11 (see Decision Log for why
   these are rewritten rather than left referencing deleted task types):
   - **Item 8** (kill-9 mid-agent-session, resume not restart): identical
     mechanic to before, now dispatched as `handle_request` with
     `payload={"request": "Review every message currently in the inbox and
     produce a triage report."}`. Same poll-for-`agent_session_id`,
     SIGKILL-the-whole-process-group, resume-and-confirm-same-session-id,
     approve-the-merge-gate, confirm-artifact-appears-on-main-only-after-
     approval sequence as before. Artifact path checked is now
     `outputs/{task_id}.md` (the single generic path), and the recovered
     output's first line must equal `Handled by: message-review skill`
     (proving both resumption AND correct skill selection survived the
     kill).
   - **Item 9** (goal 6's direct proof): submit three tasks, all
     `--type handle_request` through the identical
     `harnesses/message-agent.json`, in the same temp repo:
     - a review-worded request ("Review everything currently in the
       inbox and flag anything urgent."),
     - a respond-worded request naming a real seeded message ("Draft a
       reply to Dave's message about rescheduling Friday's meeting."),
     - a summarize-worded request naming a real date range ("Summarize
       everything that came in between July 12 and July 14, 2026.").
     Run each to completion (worktree isolation + gate approval, same as
     item 8). For each, read the actual resulting `outputs/{task_id}.md`
     content (not the skill descriptions) and assert its first line names
     the expected skill, and that its body content matches that
     capability (review report mentions multiple senders; respond draft
     mentions the reschedule/Friday message specifically; summary
     mentions only in-window senders). This is the concrete, from-output,
     not-asserted-from-reading-descriptions proof goal 6 requires.
   - **Item 10** (goal 1 + goal 2's "no duplicated logic" acceptance
     criteria, inspected directly): (a) load `harnesses/message-agent.json`
     from disk and assert its `artifact_path_template` is the single
     string `"outputs/{task_id}.md"` with no capability-specific
     variants anywhere in the harness file; (b) temporarily rename
     `workers/message-agent/seed/.claude/skills/message-respond/SKILL.md`
     aside, re-bootstrap a fresh temp repo from that modified seed,
     dispatch a respond-worded request through it, and confirm the agent
     no longer produces a reply-shaped artifact under that missing skill
     (demonstrating a `SKILL.md` add/remove changes capability with zero
     Python touched) -- then restore the renamed file so the tracked
     source tree is left exactly as it was.
   - **Item 11**: unchanged in spirit from the prior plan -- confirms
     items 1-7 (the base suite) all still passed in the same run.

## Validation and Acceptance

1. Three requests submitted through the identical task type
   (`handle_request`) and harness (`harnesses/message-agent.json`) result
   in three different skills firing, confirmed by reading the actual
   `outputs/{task_id}.md` file content and its `Handled by:` line for each
   -- not asserted from the skill descriptions. (`scripts/run_validation.py`
   item 9.)
2. `scripts/run_validation.py`'s base suite (items 1-7) passes unmodified
   in the same run as this plan's additions. (Item 11.)
3. Removing a `SKILL.md` changes what the agent can do (a respond-shaped
   request no longer produces a reply artifact) without any Python file
   being touched. (Item 10b.)
4. `harnesses/message-agent.json`'s `artifact_path_template` is inspected
   directly from disk and confirmed to be one single, capability-agnostic
   path used regardless of which skill ends up running. (Item 10a.)
5. Kill-9 mid-agent-session still resumes the same session (proven via the
   CLI's own returned `session_id`, not assumed) under the new generic
   harness, and the resumed session still correctly selects and records
   the review skill. (Item 8.)

## Idempotence and Recovery

Unchanged from the prior plan in every respect that matters here: this
plan does not touch `agent_runner.py`'s state machine, so every crash
window it already handles (claim-before-work, session-id
write-before-launch, artifact-check-before-trust, commit-only-if-dirty,
gate-filing idempotence, merge-only-if-not-already-merged) is exercised
identically regardless of which of the three skills the agent session
internally selects -- from `run_agent_task_cycle`'s point of view, "an
agent session ran in a worktree and did or didn't produce
`outputs/{task_id}.md`" is the entire observable surface, and that surface
is unchanged by this plan. The one new question this plan's design has to
answer -- "what if the agent picks the wrong skill, or no skill, for a
request" -- is not a crash-recovery question at all: it manifests as
`artifact_ready()` staying `False` (if nothing gets written) or as a
misleading `Handled by:` line (if the wrong skill runs), and in either
case the existing `MAX_AGENT_ATTEMPTS`-bounded resume-and-recheck loop and
validation item 9's direct content assertions are what catch it -- no new
recovery mechanism is needed or added.

## Artifacts and Notes

**`workers/message-agent/seed/CLAUDE.md`, full replacement text:**
```markdown
# Message-management agent

You are working inside one task's isolated copy of a small message-management
repository. Someone receives messages -- short pieces of correspondence, like
an inbox -- and wants help managing them. Every message that currently exists
is a file under `messages/`, one file per message, in this format:

```
---
from: sender@example.com
to: message-agent
date: 2026-07-15T09:30:00+00:00
subject: Subject line
---
The body of the message, in plain text or Markdown.
```

You are given one free-text request per task, not a named capability. Decide
which of your Skills applies and use it:

- **message-review** -- review the whole inbox and produce a triage report.
- **message-respond** -- draft a reply to one specific named message.
- **message-summarize** -- summarize messages within a given date range.

You are always told, in your actual task instructions, exactly where to
write your one output file. Write only that file, and start it by noting
which skill handled the request. Never modify anything under `messages/` --
those are the original messages, not yours to edit. Never write anywhere
outside the path you were given.
```

**Task file payload (new shape, replaces the three prior per-type shapes):**
```json
{"request": "free text describing what to do"}
```

**Directory layout added/changed by this plan** (paths relative to repo
root):
```
harnesses/
  message-agent.json                 replaces message-review/respond/summarize.json
workers/message-agent/seed/
  CLAUDE.md                          trimmed (identity + skill pointer, not procedure)
  .claude/skills/
    message-review/SKILL.md
    message-respond/SKILL.md
    message-summarize/SKILL.md
  messages/                          unchanged
workers/message-agent/repo/          GIT-IGNORED, runtime: bootstrapped from seed/,
                                      now also carries .claude/skills/ and outputs/
                                      (replacing reviews/, drafts/, summaries/)
```

## Interfaces and Dependencies

Identical to the prior plan: the `claude` CLI (v2.1.211, confirmed present
and authenticated) and `git`, both invoked as subprocesses, no new pip
dependency. The one new external behavior this plan depends on is Claude
Code's own skill auto-discovery inside a `claude -p --session-id ...`
headless session -- confirmed via current documentation to be part of
ordinary session initialization (skill descriptions are loaded into
context the same way regardless of interactive vs. headless invocation),
not an interactive-only feature, and exercised for real by validation item
9 against a live session, not merely assumed from the docs.

## Outcomes & Retrospective

*(written after implementation and a full, passing validation run)*

