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
