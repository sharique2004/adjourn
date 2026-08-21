"""A fabricated journal shaped exactly like the dress rehearsal's real one. (Lane 2)

Every regression in this lane came from a shape the fixtures did not have:

  * a LIVE github_update writes a FLAT undo_payload whose label key is
    `labels_added` — the board read only the simulated `payload.labels` shape and
    so never pinned the star card;
  * a recap_page is written MORE THAN ONCE per meeting, and one undo click has
    to unwind every generation;
  * a cancelled countdown is a journal record of its own, not a card.

So this module builds a journal with all three, matching what the dress
rehearsal measured on 2026-08-20 and re-measured on 2026-08-21: TWELVE actions
from living-room-standup, the two what-changed cards live with real labels, a
linear_move, an inline PR review suggestion, a cancelled email.

WHY pr_review_suggestion IS IN HERE. The tape merged the PR-review beat into the
demo meeting, so one bare `--replay` fires five kinds the room is watching for —
linear_create, linear_move, slack_send, github_update and pr_review_suggestion.
This journal is the stand-in for that run, so a kind missing here is a card
missing from the rehearsal that is supposed to prove the real one.

TWO USES, ONE FILE. As a library it writes into a directory a caller names, and
touches nothing else — that is what the suites use. As a command it dresses the
REAL journal, which is the pre-demo one-liner:

    python -m adjourn.tests.dress_journal

It writes only `executions.jsonl`, and only where `config` says the journal
lives; `--into DIR` sends it somewhere harmless instead.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

MEETING_ID = "living-room-standup"
#: What a masthead prints for this meeting. NEVER a slug, and never an
#: organisation's name — the board is read from across a room by people who did
#: not choose either.
MEETING_TITLE = "Living room standup"
REPO = "sharique2004/adjourn"

#: The prop PR the review suggestion lands on, and the colour it suggests. Kept
#: as constants because DEMO.md, the fixture ground truth and this file all have
#: to agree about them or the rehearsal stops being evidence.
PROP_PULL_NUMBER = 6
SUGGESTED_COLOUR = "#6b7f99"


def _moment(now: datetime, seconds_ago: int) -> str:
    return (now - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")


def build_records(now: datetime | None = None) -> list[dict]:
    """The twelve executions plus one cancellation, in fire order."""
    now = now or datetime.now(UTC)
    at = lambda seconds: _moment(now, seconds)  # noqa: E731 — local shorthand, read once

    def execution(**fields) -> dict:
        record = {
            "record_type": "execution",
            "ok": True,
            "mode": "sim",
            "meeting_id": MEETING_ID,
            "quote": "",
            "speaker": "You",
            "url": "",
            "external_id": "",
            "undo_payload": {},
        }
        record.update(fields)
        record.setdefault("written_at", record["fired_at"])
        return record

    return [
        # --- the star card: a LIVE what-changed comment. FLAT undo_payload. ---
        execution(
            kind="github_update",
            mode="live",
            human_summary="Comment on #2: approach Redis->in-process LRU cache",
            quote="We are not using Redis for the ingestion cache — it is an in-process LRU now.",
            url=f"https://github.com/{REPO}/issues/2#issuecomment-5365537026",
            external_id="5365537026",
            dedup_key="github_update:living-room-standup:issue-2:decision",
            undo_payload={
                "operation": "comment",
                "repo": REPO,
                "issue_number": 2,
                "comment_id": "5365537026",
                "labels_added": ["from-meeting", "decision-changed"],
            },
            fired_at=at(300),
        ),
        # --- the second what-changed card, also live and flat ---
        execution(
            kind="github_update",
            mode="live",
            human_summary="Comment on #2: owner Priya -> Alex",
            quote="Alex is picking up the cache work, not Priya.",
            speaker="Travis",
            url=f"https://github.com/{REPO}/issues/2#issuecomment-5365538019",
            external_id="5365538019",
            dedup_key="github_update:living-room-standup:issue-2:reassignment",
            undo_payload={
                "operation": "comment",
                "repo": REPO,
                "issue_number": 2,
                "comment_id": "5365538019",
                "labels_added": ["from-meeting", "decision-changed"],
            },
            fired_at=at(295),
        ),
        execution(
            kind="calendar_hold",
            human_summary="Ship: cache layer — Tue 25 Aug 09:00",
            quote="Ship it by the twenty-fifth.",
            external_id="adjourn-hold-ship",
            dedup_key="calendar_hold:living-room-standup:2026-08-25",
            undo_payload={"path": "state/holds/ship-cache-layer.ics"},
            fired_at=at(290),
        ),
        execution(
            kind="linear_create",
            human_summary="Created SHA-12 'Benchmark the LRU cache'",
            quote="Someone should benchmark it before Friday.",
            url="https://linear.app/sha/issue/SHA-12",
            external_id="SHA-12",
            dedup_key="linear_create:living-room-standup:benchmark-lru",
            undo_payload={"simulated": True, "payload": {"team": "SHA"}},
            fired_at=at(285),
        ),
        # --- the second-biggest beat: a state transition, pinned by KIND ---
        execution(
            kind="linear_move",
            human_summary="Moved SHA-5 to In Review — about eighty percent done",
            quote="The streaming adapter is about eighty percent done, should be in review tomorrow.",
            url="https://linear.app/sha/issue/SHA-5",
            external_id="SHA-5",
            dedup_key="linear_move:living-room-standup:sha-5:in-review",
            undo_payload={"simulated": True, "payload": {"previous_state": "In Progress"}},
            fired_at=at(280),
        ),
        execution(
            kind="pull_request_stub",
            mode="live",
            human_summary="Opened draft PR #19 'WIP: config loader'",
            quote="I'll put up a draft for the config loader tonight.",
            url=f"https://github.com/{REPO}/pull/19",
            external_id="19",
            dedup_key="pull_request_stub:living-room-standup:config-loader",
            undo_payload={"repo": REPO, "number": 19, "branch": "adjourn/config-loader"},
            fired_at=at(275),
        ),
        execution(
            kind="github_update",
            mode="live",
            human_summary="Comment on #1: auth migration is unblocked",
            quote="Auth migration is unblocked, the SSO piece landed.",
            url=f"https://github.com/{REPO}/issues/1#issuecomment-5365732010",
            external_id="5365732010",
            dedup_key="github_update:living-room-standup:issue-1:update",
            undo_payload={
                "operation": "comment",
                "repo": REPO,
                "issue_number": 1,
                "comment_id": "5365732010",
                "labels_added": ["from-meeting"],
            },
            fired_at=at(270),
        ),
        execution(
            kind="calendar_hold",
            human_summary="Review: cache layer — Fri 28 Aug 15:00",
            quote="Let's review it Friday.",
            external_id="adjourn-hold-review",
            dedup_key="calendar_hold:living-room-standup:2026-08-28",
            undo_payload={"path": "state/holds/review-cache-layer.ics"},
            fired_at=at(265),
        ),
        # --- the beat that is a SUGGESTION, never a push. On the diff of the
        # standing prop PR, reversible, and the one card that proves a meeting
        # can correct code without a meeting being allowed to commit code.
        execution(
            kind="pr_review_suggestion",
            human_summary=(
                f"Suggested change on PR #{PROP_PULL_NUMBER}: "
                f"join button green #2ecc71 → {SUGGESTED_COLOUR}"
            ),
            quote=(
                "The join button on pull six is that harsh green — it should be "
                "the slate blue, six b seven f nine nine."
            ),
            speaker="Travis",
            url=f"https://github.com/{REPO}/pull/{PROP_PULL_NUMBER}",
            external_id=str(PROP_PULL_NUMBER),
            dedup_key=f"pr_review_suggestion:{MEETING_ID}:pull-{PROP_PULL_NUMBER}:join-button",
            undo_payload={
                "simulated": True,
                "payload": {
                    "endpoint": f"repos/{REPO}/pulls/{PROP_PULL_NUMBER}/reviews",
                    "method": "POST",
                    "body": {
                        "event": "COMMENT",
                        "path": "index.html",
                        "line": 42,
                    },
                },
            },
            fired_at=at(268),
        ),
        execution(
            kind="slack_send",
            human_summary="Posted the decision change to #all-test",
            quote="I'll slack the team the notes so everyone knows the Redis thing changed.",
            url="https://sharique.slack.com/archives/C0BSHPBMTH6/p1787292112393999",
            external_id="1787292112.393999",
            dedup_key="slack_send:living-room-standup:decision-change",
            undo_payload={"simulated": True, "payload": {"channel": "#all-test"}},
            fired_at=at(120),
        ),
        # --- rewritten in place: three generations of the same page ---
        execution(
            kind="recap_page",
            mode="live",
            human_summary=f"Wrote the recap for {MEETING_TITLE} — 10 actions",
            external_id=f"{MEETING_ID}.html",
            url=f"file:///Users/x/adjourn/recaps/{MEETING_ID}.html",
            dedup_key=f"recap_page:{MEETING_ID}",
            undo_payload={
                "path": f"/Users/x/adjourn/recaps/{MEETING_ID}.html",
                "had_previous_version": False,
            },
            fired_at=at(260),
        ),
        execution(
            kind="recap_page",
            mode="live",
            human_summary=f"Wrote the recap for {MEETING_TITLE} — 11 actions",
            external_id=f"{MEETING_ID}.html",
            url=f"file:///Users/x/adjourn/recaps/{MEETING_ID}.html",
            dedup_key=f"recap_page:{MEETING_ID}",
            undo_payload={
                "path": f"/Users/x/adjourn/recaps/{MEETING_ID}.html",
                "had_previous_version": True,
            },
            fired_at=at(110),
        ),
        {
            "record_type": "cancellation",
            "kind": "email_send",
            "meeting_id": MEETING_ID,
            "human_summary": "Would email alex@example.com — Follow-up: demo deck",
            "quote": "I'll email Alex the deck after this.",
            "speaker": "You",
            "dedup_key": "email_send:living-room-standup:demo-deck",
            "cancelled_at": at(200),
            "written_at": at(200),
        },
    ]


def write(directory: Path, now: datetime | None = None) -> Path:
    """Write the journal into `directory` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    journal = directory / "executions.jsonl"
    with journal.open("w", encoding="utf-8") as handle:
        for record in build_records(now):
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return journal


# --- the pre-demo one-liner --------------------------------------------------
#
# WHY A COMMAND AND NOT A SECOND SCRIPT. The board reads one file, and the fastest
# honest way to have a dressed board in front of a room is to put that file where
# the board looks. The records are the same objects the suites assert on, so the
# rehearsal board and the tested board cannot drift; a separate fabricator would
# be a second truth by tomorrow. Every card it writes is stamped `sim` except the
# ones the dress rehearsal genuinely fired live, so nothing here claims a write
# that did not happen.


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .. import config

    parser = argparse.ArgumentParser(
        prog="adjourn.tests.dress_journal",
        description=(
            "Dress the follow-through board with the rehearsal's twelve actions "
            "(the five demo kinds included). Writes executions.jsonl and nothing else."
        ),
    )
    parser.add_argument(
        "--into",
        metavar="DIR",
        default="",
        help="write into DIR instead of the real journal's directory",
    )
    options = parser.parse_args(argv)

    directory = (
        Path(options.into).expanduser()
        if options.into
        else config.executions_journal_path().parent
    )
    journal = write(directory)
    records = build_records()
    kinds = sorted({record.get("kind", "") for record in records if record.get("kind")})
    print(f"[dress] wrote {len(records)} record(s) to {journal}")
    print(f"[dress] kinds: {', '.join(kinds)}")
    print(f"[dress] meeting: {MEETING_TITLE}  (id {MEETING_ID})")
    print("[dress] reload http://127.0.0.1:5117/ — no server restart needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
