"""A fabricated journal shaped exactly like the dress rehearsal's real one. (Lane 2)

Every regression in this lane came from a shape the fixtures did not have:

  * a LIVE github_update writes a FLAT undo_payload whose label key is
    `labels_added` — the board read only the simulated `payload.labels` shape and
    so never pinned the star card;
  * a recap_page is written MORE THAN ONCE per meeting, and one undo click has
    to unwind every generation;
  * a cancelled countdown is a journal record of its own, not a card.

So this module builds a journal with all three, matching what the dress
rehearsal measured on 2026-08-20: eleven actions from agi-living-room, the two
what-changed cards live with real labels, a linear_move, a cancelled email.

Nothing here touches the real journal. Callers pass a directory.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

MEETING_ID = "agi-living-room"
MEETING_TITLE = "AGI Inc. living room"
REPO = "sharique2004/adjourn"


def _moment(now: datetime, seconds_ago: int) -> str:
    return (now - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")


def build_records(now: datetime | None = None) -> list[dict]:
    """The eleven executions plus one cancellation, in fire order."""
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
            dedup_key="github_update:agi-living-room:issue-2:decision",
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
            human_summary="Comment on #2: owner Priya -> Div",
            quote="Div is picking up the cache work, not Priya.",
            speaker="Travis",
            url=f"https://github.com/{REPO}/issues/2#issuecomment-5365538019",
            external_id="5365538019",
            dedup_key="github_update:agi-living-room:issue-2:reassignment",
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
            dedup_key="calendar_hold:agi-living-room:2026-08-25",
            undo_payload={"path": "state/holds/ship-cache-layer.ics"},
            fired_at=at(290),
        ),
        execution(
            kind="linear_create",
            human_summary="Created SHA-12 'Benchmark the LRU cache'",
            quote="Someone should benchmark it before Friday.",
            url="https://linear.app/sha/issue/SHA-12",
            external_id="SHA-12",
            dedup_key="linear_create:agi-living-room:benchmark-lru",
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
            dedup_key="linear_move:agi-living-room:sha-5:in-review",
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
            dedup_key="pull_request_stub:agi-living-room:config-loader",
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
            dedup_key="github_update:agi-living-room:issue-1:update",
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
            dedup_key="calendar_hold:agi-living-room:2026-08-28",
            undo_payload={"path": "state/holds/review-cache-layer.ics"},
            fired_at=at(265),
        ),
        execution(
            kind="slack_send",
            human_summary="Posted the decision change to #all-test",
            quote="I'll slack the team the notes so everyone knows the Redis thing changed.",
            url="https://sharique.slack.com/archives/C0BSHPBMTH6/p1787292112393999",
            external_id="1787292112.393999",
            dedup_key="slack_send:agi-living-room:decision-change",
            undo_payload={"simulated": True, "payload": {"channel": "#all-test"}},
            fired_at=at(120),
        ),
        # --- rewritten in place: three generations of the same page ---
        execution(
            kind="recap_page",
            mode="live",
            human_summary="Wrote the recap for AGI Inc. living room — 9 actions",
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
            human_summary="Wrote the recap for AGI Inc. living room — 10 actions",
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
            "human_summary": "Would email div@example.com — Follow-up: demo deck",
            "quote": "I'll email Div the deck after this.",
            "speaker": "You",
            "dedup_key": "email_send:agi-living-room:demo-deck",
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
