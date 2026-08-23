# adjourn/config-loader

Placeholder committed by Adjourn so this branch differs from its base and a
draft pull request can exist. Delete this file in your first real commit.

> **Sharique:** Okay, cache layer first. Bad news in Redis is overkill. I profiled the ingestion path last night and the hotkey is 60 megabytes. So we're dropping Redis on issue 2 and going with an in-process LRU instead. Priya's taking the cache work off Sam. She's the one who found the 60MB. So she should finish it. Push the ship date to the 25th. She needs the weekend to rip the Redis client out. We keep talking about webhook signature verification and it's live and it lives nowhere. We need a ticket for webhook signature verification on the ingestion endpoint. SHA-5, the streaming adapter, I'm about 80% done with it. Should be in review tomorrow. And I'll open a PR for the config loader tonight. It's draft ready. I just want eyes on the interface before I go further. Issue 1, the auth migration. It's been running clean in dev all week. Nothing to report, which is the report. Do we still need the rate limit ticket open or is SHA-5 the same piece of work? I'll look after this. I'll slack the channel, the summary, once we're done here. Let's review Friday and see where the cache actually landed On Priya's join button PR pull 6 That green is wrong it should be our slate blue 6B7 F99 She can turn that around before Friday without breaking a sweat. That's all I had

_From: Meeting 22 Aug 2026, 23:05_
