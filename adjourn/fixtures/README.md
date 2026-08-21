# fixtures — the demo meeting, and the floor under it

Two things live here: the **transcripts** the demo replays, and the
**ground truth** that says what a correct reading of them looks like.

## Files

| File | What it is |
| --- | --- |
| `prior-standup.jsonl` | Last week's standup (2026-08-14). The meeting the demo contradicts. |
| `prior-standup.fixtures.json` | Hand-written ground truth for it — 5 statements. |
| `agi-living-room.jsonl` | **The demo meeting** (2026-08-21), ~3 minutes, 28 segments, two speakers. |
| `agi-living-room.fixtures.json` | Hand-written ground truth — 11 statements covering all ten statement kinds. |
| `demo_statements.json` | Generated copy of the demo ground truth. The last-resort floor. |

## Transcript format

Drift's `.jsonl`, extended to the segment shape `meetingscribe_source` produces —
one `meta` line, then one line per segment:

```json
{"type":"meta","meeting_id":"agi-living-room","title":"Adjourn standup — living room","date":"2026-08-21"}
{"type":"segment","segment_id":"agi-s05","speaker":"Them","track":"system","start":14.3,"end":31.8,"text":"..."}
```

`extraction.load_fixture_transcript(id)` returns `(segments, meta)`. This is what
lets a rehearsal replay the demo through the **exact same extraction path** a live
recording takes — there is no fixtures-only branch, so nothing about the demo is
only true in rehearsal.

## Ground-truth format

`{"meeting": {...}, "statements": [...]}`, each statement a
`extraction.Statement` dict. `extraction.load_fixture_statements(id)` also accepts
a bare list, and looks in this order:

```
<id>.fixtures.json  ->  <id>.json  ->  demo_statements.json
```

Two rules the ground truth holds itself to, both checked by
`tests/test_understanding_extraction.py`:

- **Every `quote` is verbatim** from the transcript segment. A quote is what the
  board shows under an action card, so a paraphrase there would make the board
  unauditable.
- **`entity_refs` only carries handles spoken *in that segment*.** `agi-s21` has
  empty refs even though it is about issue #1, because "issue one" is spoken in
  `agi-s20`. Inferring across segments is how a comment lands on the wrong issue.

## What the demo meeting exercises

Every one of the ten statement kinds, and between them every one of the eight
action kinds:

| Beat | Segment | Kind | Fires |
| --- | --- | --- | --- |
| Redis dropped for an in-process LRU on issue #2 | `agi-s05` | `decision` | `github_update` (conflict vs. last week) |
| Priya takes the cache work off Sam | `agi-s09` | `assignment` | `github_update` (reassignment on #2) |
| Ship date moves to the twenty-fifth | `agi-s11` | `deadline` | `calendar_hold` |
| "We need a ticket for webhook signature verification" | `agi-s13` | `ticket_request` | `linear_create` |
| "SHA-5 is about eighty percent done" | `agi-s15` | `progress_report` | `linear_move` (In Progress -> In Review) |
| "I'll open a PR for the config loader tonight" | `agi-s17` | `pr_intent` | `pull_request_stub` |
| Auth migration running clean | `agi-s21` | `update` | `github_update` |
| Is SHA-5 the same as the rate-limit ticket? | `agi-s22` | `question` | — (recap only) |
| "I'll Slack the channel the summary" | `agi-s24` | `message_commitment` | `slack_send` (60s regret window) |
| "I'll email Div the deck" | `agi-s25` | `email_commitment` | `email_send` (60s regret window) |
| "Let's review Friday" | `agi-s26` | `deadline` | `calendar_hold` |

`recap_page` fires once per meeting from the orchestrator, not from a statement.

## The conflict the demo turns on

`prior-standup` settles **Redis / owner Sam / ship the twenty-second** on issue #2.
`agi-living-room` reverses all three. Seed the prior meeting into memory before
the demo and the judge returns `conflict=true` with
`what_changed: "approach Redis->in-process LRU"`.

## Why these names and not others

Two beats are worded around the REAL Linear board, and changing them back would
break the demo rather than just reword it:

* `agi-s15` says **SHA-5** verbatim, because SHA-5 ("Migrate transcript ingestion
  to the streaming adapter") really sits in **In Progress** on the seeded board.
  That is the ticket the demo moves. `prior-standup` names SHA-5 *and* the words
  "streaming adapter" in the SAME segment, because that statement is what settles
  the ticket's topic in memory — an earlier wording mentioned "the interface" and
  seeding christened SHA-5 "interface design".
* `agi-s13` asks for a ticket for **webhook signature verification**, NOT for
  retry logic, because SHA-7 on that board is already "Retry logic for executor
  failures". A ticket_request that duplicates an existing ticket is the one thing
  this beat must not demonstrate.

## Regenerating `demo_statements.json`

It is a copy of `agi-living-room.fixtures.json`. If the demo meeting changes,
regenerate it rather than editing it by hand — otherwise the floor and the
ground truth drift apart and the cold-machine demo stops matching the rehearsed one.
