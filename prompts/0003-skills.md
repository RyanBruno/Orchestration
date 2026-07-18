## Goal

Replace `message-agent`'s three separate, task-type-specific harnesses
(`message-review`, `message-respond`, `message-summarize`) with a single
generic harness plus three Claude Code Skills, so an operator submitting a
task no longer has to know or name which capability applies — they submit
one generic task with a free-text request, and Claude Code's own
skill-discovery mechanism (`.claude/skills/*/SKILL.md`, matched
automatically against the request) decides whether to review, respond, or
summarize.

This is a narrow, surgical change, not a rewrite. `orchestrator/
agent_runner.py`'s crash-recovery mechanics (write-before-launch session
id, artifact-check-before-trust, worktree isolation, the `merge_branch`
gate), `gates.json`, `examples/`, and `scripts/submit_task.py` itself are
all untouched by this plan. `--type` stays a required flag on
`submit_task.py` — every worker, including the fixtures, still uses it to
pick a handler or harness. What changes is scoped entirely to
`message-agent`: it collapses from three task types to one, and the
review-vs-respond-vs-summarize decision moves from "which `--type` did the
operator pass" to "which skill does Claude itself pick."

Read `.agent/execplans/orchestration-file-based-coordination.md` and
`.agent/execplans/agentic-workers-and-message-agent.md` in full before
writing anything. This plan does not re-derive the crash-recovery model
either of those established — it narrows one piece of the second one.

## Goals this plan must achieve

1. **One generic harness for message-agent, not three.** Replace
   `harnesses/message-review.json`, `harnesses/message-respond.json`, and
   `harnesses/message-summarize.json` with a single harness (e.g.
   `harnesses/message-agent.json`) whose `task_type` is one generic value
   (e.g. `handle_request`), whose `allowed_tools` is the union actually
   needed across all three capabilities (Read, Write, Glob, Grep — no
   Bash, preserving the existing decision that merging is performed by
   Python, never the agent), and whose `artifact_path_template` is a
   single, capability-agnostic path (e.g. `outputs/{task_id}.md`). This
   uniformity is load-bearing, not cosmetic: `orchestrator/
   agent_runner.py`'s crash-recovery correctness depends on always
   knowing, deterministically, exactly one path to check for "did the
   work actually happen," and that must keep working regardless of which
   skill ends up handling a given request.

2. **Three Skills carrying the capability-specific instructions.** Move
   what's currently baked into each harness's `append_system_prompt` (the
   review/respond/summarize procedures) into three separate `SKILL.md`
   files under the message-agent's own `.claude/skills/` directory
   (`message-review/`, `message-respond/`, `message-summarize/`), each
   with a `description` written specifically enough that Claude's
   automatic skill-discovery can correctly distinguish which applies to a
   given free-text request. Since every task's worktree is a checkout of
   the same tracked tree, these load automatically no matter which
   worktree a session runs in, the same way `CLAUDE.md` already does.
   Verify how Claude Code's skill auto-discovery and `SKILL.md` frontmatter
   actually work against current documentation before writing these —
   don't assume the shape from general knowledge.

3. **A generic, free-text prompt template.** Replace the type-specific
   `user_prompt_template` fields (which currently reference `{message_file}`
   or `{start}`/`{end}` directly) with one generic template that embeds a
   single free-text `request` field from the task payload — e.g. `"Task
   id: {task_id}\nHandle this request: {request}\nWrite your result to
   exactly this path: {artifact_path}"` — so the operator's payload going
   forward is just `{"request": "..."}` regardless of which capability
   ends up handling it.

4. **`CLAUDE.md` trimmed to identity, not procedure.** Update `workers/
   message-agent/seed/CLAUDE.md` (and therefore what gets bootstrapped into
   `repo/CLAUDE.md`) to describe the worker's identity and point at its
   skills in plain language, removing the now-duplicated step-by-step
   review/respond/summarize instructions that belong in each `SKILL.md`
   instead. Don't maintain the same procedure in two places.

5. **Legibility: the output records which skill handled it.** Each
   `SKILL.md` must instruct the agent to note, in the output file itself
   (a one-line header is enough), which capability it exercised. Since the
   harness and task file no longer name the capability explicitly the way
   `harnesses/message-review.json` used to, this is now the only place a
   human reading a result can tell which skill fired.

6. **Prove the differentiation actually works.** Submit at least three
   tasks through the exact same task type and harness — one worded so the
   review skill should apply, one naming a specific real message so the
   respond skill should apply, one naming a real date range so the
   summarize skill should apply — and confirm, from the actual output
   files (not from reading the skill descriptions and assuming), that the
   right skill fired each time.

## A note on effort and verbosity

Use a high reasoning effort if your environment exposes that setting —
goal 1's constraint (one deterministic artifact path must keep the
recovery guarantee correct regardless of which skill runs) is exactly the
kind of detail that's easy to lose sight of while focused on the
skills mechanism itself. Keep narration concise; keep the plan's Concrete
Steps, the new harness schema, and each `SKILL.md`'s content fully
explicit.

## How you will work: one continuous ExecPlan pass

This repository already has the ExecPlan convention established, and two
prior plans exist as worked examples — `AGENTS.md` and `.agent/PLANS.md`
define the methodology; don't re-derive it. Author this plan at
`.agent/execplans/message-agent-skills-dispatch.md`, then implement it
end-to-end in this same session. Do not pause after writing the plan to
ask whether to proceed.

<persistence>
- Work through all six goals end to end; don't stop because the harness
  and skills exist but the differentiation was never actually proven with
  three real requests — an unproven claim is exactly what goal 6 exists to
  rule out.
- Don't come back to ask which of several reasonable choices to make —
  the generic task-type name, the generic artifact path, exact skill
  descriptions and boundaries between them. Pick, document in the Decision
  Log, and continue.
- The only reasons to stop and ask are the stop conditions listed later in
  this prompt.
</persistence>

<context_gathering>
Goal: understand exactly how the current three-harness dispatch works
before narrowing it, and confirm precisely which files this plan must
leave alone.
Method: read both prior execution plans, `orchestrator/agent_runner.py`
(especially `render_prompt`, `artifact_path_for`, and how
`harnesses_by_task_type` is used), `workers/message-agent/worker.py`,
`workers/message-agent/magent_config.py`, and the three existing harness
files before writing your own plan. Confirm current documentation on how
Claude Code's skill auto-discovery and `SKILL.md` format actually work
rather than assuming.
Early stop: once you can point at exactly which existing files this plan
touches and which it must leave alone, stop gathering context and move to
the plan.
Escalate once: only if collapsing to one harness would actually weaken
the artifact-check-before-trust guarantee in some way not already
addressed by goal 1 — otherwise proceed and document the judgment call.
</context_gathering>

<code_editing_rules>
<guiding_principles>
- Everything both prior execution plans established still applies without
  exception: write-before-launch session id, artifact-check-before-trust
  (never the CLI's own result/is_error field), worktree isolation, the
  merge_branch gate, heartbeat re-stamp before invoking.
- This is a narrowing, not a rewrite: `orchestrator/agent_runner.py`'s core
  cycle logic, `gates.json`, `examples/`, and `scripts/submit_task.py`
  itself should need zero or near-zero changes. If you find yourself
  editing `agent_runner.py`'s state machine, stop and reconsider whether
  this plan's scope has crept.
- Skills are configuration in the same spirit `gates.json` and the
  harnesses already are: adding a fourth capability to message-agent later
  should mean adding one new `SKILL.md`, not touching Python.
- Legibility: a human should be able to read an output file and tell which
  skill produced it without cross-referencing anything else.
</guiding_principles>
<layout>
harnesses/message-agent.json -- the one replacement harness (delete the
  three it replaces, or say in the Decision Log why you kept them instead)
workers/message-agent/seed/.claude/skills/message-review/SKILL.md
workers/message-agent/seed/.claude/skills/message-respond/SKILL.md
workers/message-agent/seed/.claude/skills/message-summarize/SKILL.md
  (tracked in seed/ alongside CLAUDE.md, so
  magent_config.ensure_repo_bootstrapped() carries them into repo/ the
  same way it already carries CLAUDE.md)
workers/message-agent/seed/CLAUDE.md -- trimmed per goal 4
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
- When you finish, summarize what was built, and explicitly confirm that
  `scripts/submit_task.py --type` is unchanged and still required — this
  plan only narrows what message-agent itself does with it.
</tool_preambles>

<self_reflection>
Before marking any milestone done, check it against this: does submitting
three differently-worded requests through the exact same task type
actually produce three differently-handled outputs; does the harness still
have exactly one artifact path regardless of which skill fires; does every
existing base-suite and prior-plan validation item still pass unmodified;
is the review/respond/summarize procedure now written in exactly one
place (its `SKILL.md`) rather than duplicated in `CLAUDE.md` or anywhere
else. If a milestone doesn't clear this, fix it before moving on rather
than noting it as a caveat.
</self_reflection>

## Stop conditions (the only reasons to pause and ask me)

- You need a credential, API key, or account access only I have.
- An action would be destructive or irreversible against real state (not
  just a reversible change to files in this repo).
- A required tool or runtime is genuinely unavailable in this environment.

Everything else — the generic task-type name, the generic artifact path,
exact skill boundaries and descriptions — make the most reasonable
decision, record it with your reasoning in the Decision Log, and continue.

## Validation and acceptance

- Three requests submitted through the same task type and harness result
  in three different skills firing, confirmed by actual output content and
  the recorded skill note from goal 5 — not asserted from reading the
  skill descriptions.
- `scripts/run_validation.py`'s existing items — both the base suite and
  the prior plan's agent-backed items — still all pass unmodified.
- Renaming or removing a `SKILL.md` changes what the agent can do without
  touching any Python, demonstrating the "no duplicated logic" property
  goal 2 requires.
- The harness's `artifact_path_template` is inspected directly and
  confirmed to be a single, capability-agnostic path used regardless of
  which skill runs.

## Wrap-up

Finish with an Outcomes & Retrospective entry stating, for each of the six
goals above, whether it was fully implemented, partially implemented, or
hit a real constraint worth documenting — run the result against the
`<self_reflection>` bar one more time — and confirm explicitly that
`scripts/submit_task.py --type`'s requiredness was not touched, only
message-agent's own use of it.
