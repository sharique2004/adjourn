# adjourn/config-loader

Placeholder committed by Adjourn so this branch differs from its base and a
draft pull request can exist. Delete this file in your first real commit.

> **Sharique:** Okay, cache layer first. Bad news in Redis is overkill. I profiled the ingestion path last night and the hot set is 60 megabytes. So we're dropping Redis on issue 2 and going with an in-process LRU instead. Priya is taking the cache of Sam. She's the one who found the 60MB, so she should finish it. Push the ship date to the 25th. She needs the weekend to rip the Redis client out. We keep talking about webhook signature verification and it lives nowhere. We need a ticket for webhook signature verification on the ingestion endpoint. SHA-5, the stream adapter, I'm about 80% done with it. Should be in review tomorrow. And I'll open a PR for the config loader tonight. It's draft ready. I just want eyes on the interface before I go further. Issue 1, the auth migration. It's been running clean in dev all week. Nothing to report, which is actually the report. I'll slack the channel, the summary, once we are done here. I'll email Alex the deck after this. He asked me for it twice and I keep forgetting. Let's review Friday and see where the cache actually landed. On Priya's join button PR, pull 6, the green is wrong. It should be our slate blue. 6B7F99. That's all I had.

_From: Meeting 22 Aug 2026, 23:20_
