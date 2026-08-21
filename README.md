# Adjourn — the meeting is the to-do.

A meeting ends. Someone says they will open a PR, someone reverses a decision
made three weeks ago, someone promises to email the client. Then everyone closes
the laptop and most of it does not happen.

Adjourn records the call on the machine it is running on, transcribes it there,
works out what was actually decided and actually promised, and does those things
— files the issue comment, moves the ticket, opens the draft PR, holds the time,
writes the recap. Not a summary with action items in it. The actions.

It runs on a Mac, against a local recorder and a local graph. The transcript does
not leave the machine.

## What it does, exactly

| | |
| --- | --- |
| `github_update` | Comments what changed on the issue the decision was about, with the verbatim sentence and a before/after table. |
| `linear_create` | Files the ticket somebody asked for, assigned to whoever asked. |
| `linear_move` | Moves an existing ticket to the state an update implies — "I'm picking up SHA-6" moves it to In Progress. |
| `pull_request_stub` | Opens a draft PR carrying the decision and the quote behind it. |
| `pr_review_suggestion` | Leaves an inline ```suggestion on an open PR when a review comment in the room names a concrete change. |
| `slack_send` | Posts the message somebody promised, after a visible 60-second countdown. |
| `email_send` | Sends the email somebody promised, after the same countdown. |
| `calendar_hold` | Holds the time the meeting agreed to. |
| `recap_page` | Writes the recap: decisions, commitments, and the questions nobody answered. |

Every fired action writes a receipt and can be undone from the board that shows
it — undo lives next to the thing that fired, not in a settings page.

## What it refuses to do

This is the harder half and most of the code is here.

A meeting is full of sentences that sound like commitments and are not.
Negations — *"don't email the client yet"*. Ideas that were raised and killed in
the same breath. Hypotheticals. And reported speech: *"Alex said he'd send the
deck"* is somebody else's commitment, mentioned in passing, and firing on it
means sending mail on behalf of a person who was not in the room. None of these
fire anything. Reported speech reaches the recap as a third-party note, clearly
attributed, and stops there.

The planner that decides what fires is a deterministic table from statement kind
to action kind. It imports no model and opens no socket. The model's job is to
read the transcript and produce statements; choosing the verb is not its job.

## How it fits together

```
  microphone + system audio
            │
            ▼
   MeetingScribe (local)          recording, diarisation, transcript
            │  transcript, on disk
            ▼
       extraction                 Statements: kind, claim, quote, refs
            │                     (the one model call; sandboxed, no tools)
            ▼
        memory                    FalkorDB graph — who said what, about which
            │                     issue, in which meeting, superseding what
            ▼
        planner                   ROUTING_TABLE: statement kind -> action kind
            │                     deterministic; no model, no network
            ▼
       executors                  github · linear · slack · email · calendar · recap
            │                     one live/sim fork at the bottom of each module
            ▼
     journal + board              receipts, undo, a 60s window on the irreversible
            │
            └────────► cloud mirror ────► read-only web surface
```

Sim and live are the same code path. Every executor builds the exact payload it
would send, and forks to the wire only at the very bottom of the module. A
missing credential is not an error, it is sim mode: the card renders the real
request, badged SIM, unsent. That is why the demo works on venue wifi and why
the badge on a card can be trusted — nothing renders LIVE unless something left
the machine.

Live writes are bounded by allowlists that are checked in code rather than
promised in a README: one GitHub repository, one Slack channel, one Linear team.

## Running it

Requires macOS, Python 3.12+, an authenticated `gh`, and
[MeetingScribe](https://github.com/sharique2004/MeetingScribe) on `:5005`.
FalkorDB is optional — memory falls back to SQLite.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r adjourn/requirements.txt
cp adjourn/.env.example adjourn/.env      # ADJOURN_SIM=1 by default

python -m adjourn.board_server            # the follow-through board, :5117
python -m adjourn.watcher                 # watches for a finished recording
```

Nothing goes live until a credential exists for it. Put credentials in the
Keychain, not in the .env:

```bash
security add-generic-password -a "$USER" -s adjourn.slack_bot_token -w
```

Tests are plain modules, no framework:

```bash
python -m adjourn.tests.test_skeleton_contracts
python -m adjourn.tests.test_lane_a_spine
```

## The web surface

`web/` is a Next.js app that reads the cloud mirror and nothing else. It cannot
fire an action and it cannot undo one. With no cloud credentials it runs from a
bundled snapshot and says so in the footer.

## Licence

MIT. See LICENSE.
