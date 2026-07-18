---
from: bob@example.com
to: message-agent
date: 2026-07-12T14:30:00+00:00
subject: Server outage last night
---
We had about 40 minutes of downtime on the primary API server starting
around 2:10am. Looks like it was an out-of-memory kill on the ingest
worker. I've restarted it and it's stable now, but we should look at
adding a memory limit alert before this happens again during business
hours. Flagging as urgent since it's customer-facing.
