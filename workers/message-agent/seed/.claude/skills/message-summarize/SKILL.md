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
