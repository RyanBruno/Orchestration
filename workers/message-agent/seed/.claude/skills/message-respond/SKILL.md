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
