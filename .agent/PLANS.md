# Execution plans: what they are and how to write one

This file defines the convention referenced by [`AGENTS.md`](../AGENTS.md).
It applies to any complex feature or significant refactor in this
repository. Read this whole file before writing a plan; it's short.

## What an execution plan is for

An execution plan (an "ExecPlan") is a single markdown document that is
both the design record and the working notes for a piece of work, from
before the first line of code is written through to the work being done.
It has one audience in mind: a capable stranger who has this repository
and this one file, and nothing else — no memory of the conversation that
produced it, no access to chat history, no assumed familiarity with the
codebase. If that stranger can pick up the plan mid-way through and
finish the work correctly, or debug a problem with it at 2am, the plan
has done its job. If they'd have to go ask someone what a term means or
what was decided and why, it hasn't.

This matters especially in this repository because the whole point of
the system being built is that state outlives any one process or
conversation. A plan that only makes sense to the person who just wrote
it violates the same principle the code is supposed to embody.

## Rules for writing one

- **Fully self-contained.** Every fact, term, and decision needed to
  understand and finish the work goes in the plan itself, in your own
  words. Don't say "as discussed" or "per the usual pattern" — write out
  what the pattern is.
- **A living document, not a proposal.** Update it the moment something
  changes: progress made, a surprise hit, a judgment call made between
  two reasonable options. A plan that reflects last week's understanding
  is worse than no plan, because it actively misleads.
- **Explain unfamiliar terms on first use**, tied to where they actually
  show up in this repo (a file path, a function name), not in the
  abstract.
- **Describe verifiable outcomes.** "Run `X`, expect to see `Y`" is a
  valid plan step. "The code compiles" or "this should work" is not —
  it doesn't tell the reader how to know if it's true.
- **Prose over bullets in narrative sections.** Bullet lists are for the
  Progress checklist, where a scannable list of done/not-done items is
  the actual point. Everywhere else, write sentences that connect ideas —
  a list of fragments forces the reader to reconstruct the reasoning
  themselves.
- **Independently verifiable milestones.** Break the work into chunks
  that can each be checked on their own, and say for each one what
  "checked" looks like.

## Required sections

Every plan must contain all of the following, in this order. A section
can be brief if the work genuinely doesn't need much said, but it must
be present — an empty or missing section is a sign the work wasn't
thought through, not a sign it wasn't needed.

1. **Purpose / Big Picture** — what this is, why it exists, in plain
   language, before any detail.
2. **Progress** — a timestamped checklist, updated as work happens. Not
   rewritten from scratch at the end; it's the running log.
3. **Surprises & Discoveries** — anything that didn't match the initial
   assumption, found while doing the work. If nothing surprised you,
   that's worth a one-line note too, since its absence is itself a claim.
4. **Decision Log** — every judgment call made where more than one
   reasonable option existed, with the reasoning, at the time it was
   made. This is what lets a future reader tell "considered and chosen"
   apart from "never occurred to anyone."
5. **Outcomes & Retrospective** — written at the end (and updated if the
   work resumes later): what actually got built, against what was
   planned, honestly.
6. **Context and Orientation** — what a newcomer needs to know about this
   repo and this problem space before the plan makes sense.
7. **Plan of Work** — the shape of the approach, at a level above
   individual steps.
8. **Concrete Steps** — the actual sequence of things to do, specific
   enough to execute without re-deriving the design.
9. **Validation and Acceptance** — how to know the work is actually
   correct, as concrete commands/actions and their expected results.
10. **Idempotence and Recovery** — for anything touching the
    coordination core specifically: what happens if this step is
    interrupted and retried, or run twice.
11. **Artifacts and Notes** — file paths created or changed, schemas
    introduced, anything a reader would otherwise have to go hunting for.
12. **Interfaces and Dependencies** — what this talks to, what talks to
    it, what it assumes about its environment.

## Where plans live

Plans live in `.agent/execplans/`, one file per major piece of work,
named descriptively (`kebab-case-topic.md`). This document itself is the
methodology, not a plan — it doesn't go in that directory.

## When a plan isn't needed

Small, self-contained fixes with no bearing on crash recovery, the
mailbox protocol, or any interacting invariant don't need this ceremony.
Use judgment; when genuinely unsure, write the plan — it's cheap insurance
against exactly the kind of subtle, hard-to-debug failure this repo
exists to prevent.
