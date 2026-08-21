"""The follow-through board — Adjourn's demo surface. (Lane E)

A small Flask app on 127.0.0.1 that renders what Adjourn actually did after a
meeting ended: one card per fired action, a Ready to send box for Slack and
email (edit, then Send), a countdown ring for anything still inside a regret
window, and an honest live/sim badge on every card.

READ-MOSTLY BY DESIGN. The board owns no state. It reads two files:

    ~/.meetingscribe/executions.jsonl   (results.append_execution writes it)
    adjourn/state/pending.json          (orchestrator writes it)

and the only writes are undo, cancel, and send — each performed by asking the
orchestrator/executor, never by editing either file itself.

WHY POLLING AND NOT SSE. One 1s fetch of a small JSON document has no
reconnect logic, no half-open connections, no proxy buffering, and no way to
wedge itself thirty seconds into a demo. The board is the one component that
must not be clever.

MARKUP LIVES IN ONE PLACE. The page is server-rendered from Jinja macros in
templates/board.html; the poll endpoint re-renders those same macros and hands
back HTML fragments. There is no second copy of the card markup in JavaScript,
so the curl-able page and the live page can never drift apart.

Run:
    cd /path/to/adjourn
    python -m adjourn.board_server            # 127.0.0.1:5117
    python -m adjourn.board_server --demo     # with fabricated data
    python -m adjourn.board_server --port 5100

ONE APP, FIVE TABS. The Meetings and Live tabs are a Blueprint that lives in
meetings_ui.py and is registered here; Follow-through, Ledger and Connections are
this module's own routes. Every page carries the same tab bar, rendered from the
same Jinja macro, so the shell is one thing and not five.

Routes:
    GET  /                      the board (Follow-through)
    GET  /ledger                per-person commitment ledger across meetings
    GET  /connections           read-only status of every executor and backend
    GET  /meeting/<id>          one meeting's actions, filtered from the journal
    GET  /recap/<id>            the recap page over HTTP (same origin as the board)
    GET  /meetings/…            the Meetings + Live tabs (meetings_ui Blueprint)
    GET  /api/board             full board state as JSON (includes pipeline)
    GET  /api/fragment          {version, header_html, pending_html, cards_html, guts_html}
    GET  /api/pipeline          watcher / extraction / planner / executor phases
    GET  /api/ledger            ledger as JSON
    GET  /api/connections       the connections page as JSON
    POST /undo/<card_id>        cancel a draft or countdown, or undo a fired action
    POST /send/<card_id>        send a Slack or email draft (after optional edits)
    GET  /healthz               liveness, for the integration pass
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets as secrets_module
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    get_template_attribute,
    jsonify,
    render_template,
    request,
)

from . import config, orchestrator, planner, results

# --- port -------------------------------------------------------------------
#
# 5117 everywhere now: adjourn/.env ships BOARD_PORT=5117, config.DEFAULT_BOARD_PORT
# is 5117, and DEMO.md says 5117. For one evening this file and .env disagreed
# (5117 vs 5100), which is exactly the kind of thing that eats five minutes in
# front of an audience. ADJOURN_BOARD_PORT or --port still overrides.
LANE_BOARD_PORT = 5117
BOARD_HOST = "127.0.0.1"

# --- kind presentation ------------------------------------------------------
#
# Label only. The accent colour for each kind lives in static/board.css keyed on
# [data-kind], so a designer can retune the palette without touching Python.
KIND_LABELS: dict[str, str] = {
    "github_update": "GitHub",
    "linear_create": "Linear",
    "linear_move": "Linear",
    "pull_request_stub": "Pull Request",
    "pr_review_suggestion": "PR Review",
    "slack_send": "Slack",
    "email_send": "Email",
    "calendar_hold": "Calendar",
    "recap_page": "Recap",
}

KIND_VERBS: dict[str, str] = {
    "github_update": "commented on an issue",
    "linear_create": "created a ticket",
    "linear_move": "moved a ticket",
    "pull_request_stub": "opened a draft PR",
    "pr_review_suggestion": "suggested a change on a PR",
    "slack_send": "sent a message",
    "email_send": "sent an email",
    "calendar_hold": "placed a hold",
    "recap_page": "wrote the recap",
}

LINK_LABELS: dict[str, str] = {
    "github_update": "View on GitHub",
    "linear_create": "Open in Linear",
    "linear_move": "Open in Linear",
    "pull_request_stub": "View pull request",
    "pr_review_suggestion": "View the suggestion",
    "slack_send": "Open in Slack",
    "email_send": "View message",
    "calendar_hold": "Open in Calendar",
    "recap_page": "Open recap",
}

QUIET_HEADER_LINE = "The meeting is the to-do."
EMPTY_STATE_LINE = "Adjourned. Waiting for the next meeting."

# TRACING A QUOTE BACK TO WHAT WAS SAID. Every card carries the words that caused
# it; the demo beat is that those words are not decoration — you click them and
# land on the transcript line they came out of. The transcript lives on the
# Meetings blueprint (/meetings/<meeting_id>), so a card can only offer the link
# when that blueprint actually mounted. create_board_application sets this; the
# default is True because build_card is also called by tests and by the archive
# reader, neither of which builds an app.
MEETINGS_TAB_MOUNTED = True


def display_path(path: Path | str) -> str:
    """A filesystem path as a human reads it: '~/…' rather than '/Users/<name>/…'.

    The Connections tab is the honest-preflight page, so it names the real files
    Adjourn reads and writes — but a projected absolute path leads with the
    operator's account name and eats the row. Collapsing $HOME shortens it
    without changing which file it points at.
    """
    text = str(path)
    home = str(Path.home())
    if home and text.startswith(home):
        return "~" + text[len(home) :]
    return text


def display_journal_path(path: Path | str) -> str:
    """The journal, named on Connections without printing a filesystem path.

    NO PATHS ON A PROJECTOR. Connections must say truthfully what is written and
    where it stays, and it does — the file has a name, and "local" is the whole
    claim being made about it. What it no longer prints is a directory: an
    absolute path leads with the operator's account name, eats the row from 1.5m,
    and on the engine's own dot-directory it is also the one place on 5117 where
    a careful reader learns a second product exists.

    A journal moved elsewhere (EXECUTIONS_JOURNAL) reads the same way. The point
    of the row is "one line per action, on this Mac", and that is what it says.
    """
    return f"{Path(str(path)).name} · local"


def display_directory_label(path: Path | str) -> str:
    """A directory Adjourn writes into: its own name plus 'local'. No path."""
    name = Path(str(path)).name or "local"
    return f"{name}/ · local"


# Words that are shouted rather than spelled. Anything not in here is written the
# way a person writes it in a sentence — lower case unless it starts the title.
#
# NO COMPANY NAMES IN HERE. This table used to hold an organisation prefix,
# which is how the slug of the demo tape came out of the fallback as a
# three-letter shout in the 28px masthead — a name nobody typed, minted by a
# title-caser, on the one frame the room reads first. A slug segment that
# happens to spell an organisation is not an acronym this function is allowed
# to invent; the only members are the ones that are unreadable any other way
# (PR, API, SHA).
_MEETING_ID_ACRONYMS = frozenset({
    "pr", "prs", "ai", "api", "ui", "ux", "sha", "cli", "llm", "qa", "kpi",
})

# THE ONE NAME A KNOWN TAPE ANSWERS TO. A fixture is not a recording, so there is
# no meeting.json to carry a title; the tape's own `meta.title` is read first
# (see _read_meeting_document -> fixture_library), and this is the belt to that
# brace for any path that only ever holds an id — the archived-journal reader and
# the memory reader both do. Keyed on the internal id so the internal id is never
# the thing a reader sees.
_MEETING_DISPLAY_TITLES: dict[str, str] = {
    "living-room-standup": "Living room standup",
    "agi-living-room": "Living room standup",
    "agi-living-room-standup": "Living room standup",
    "prior-standup": "Adjourn standup — 14 Aug",
    "pr-review-beat": "Adjourn standup — PR review beat",
}

# A leftover organisation prefix on a slug. Stripped so it can never be the
# first word of a masthead, even for an id that is not in the table above.
_MEETING_ID_DROPPED_TOKENS = frozenset({"agi"})


def humanize_meeting_id(meeting_id: str) -> str:
    """'living-room-standup' -> 'Living room standup'. A title when there is no title.

    A SLUG IS AN ADDRESS, NOT A NAME. It is right in a URL, in the journal and in
    the small monospace `external-id` chip at the foot of a card — all places a
    reader understands they are looking at an identifier. It is wrong as the
    28px headline of the opening frame, which is where it was landing whenever a
    recording folder had no title on it: the first thing in the room read like a
    filename. This is the fallback for that one job, and only that one.

    Returns "" for an empty id so callers can keep their own last-resort phrase.
    """
    raw = str(meeting_id or "").strip()
    known = _MEETING_DISPLAY_TITLES.get(raw)
    if known:
        return known
    words = [word for word in re.split(r"[-_\s]+", raw) if word]
    words = [word for word in words if word.lower() not in _MEETING_ID_DROPPED_TOKENS]
    stripped_key = "-".join(word.lower() for word in words)
    known = _MEETING_DISPLAY_TITLES.get(stripped_key)
    if known:
        return known
    # A trailing capture stamp ("…-20260821", "…-041329") is machine bookkeeping
    # and never part of what the meeting was called.
    while words and words[-1].isdigit() and len(words[-1]) >= 6:
        words.pop()
    if not words:
        return ""
    spoken = [
        word.upper() if word.lower() in _MEETING_ID_ACRONYMS else word.lower()
        for word in words
    ]
    if spoken[0].lower() not in _MEETING_ID_ACRONYMS:
        spoken[0] = spoken[0][:1].upper() + spoken[0][1:]
    return " ".join(spoken)


# A CARD LINKS TO A TRANSCRIPT ONLY IF THERE IS ONE. The default `--replay` tape
# is a FIXTURE (living-room-standup): it produces real cards with real quotes, but no
# recording was ever made, so /meetings/living-room-standup is a 404. Linking anyway
# would put a dead door under the quote on the demo's own default path. So the
# board asks the library first — cheaply, because the answer is cached: a hit is
# permanent (a recording does not un-record), a miss expires in seconds so that a
# meeting recorded live on stage starts linking as soon as its folder lands.
_TRANSCRIPT_PRESENT: dict[str, bool] = {}
_TRANSCRIPT_MISSES: dict[str, float] = {}
TRANSCRIPT_MISS_TTL_SECONDS = 5.0


def meeting_has_transcript(meeting_id: str) -> bool:
    """Is there a readable recording behind this meeting id? Never raises."""
    meeting_id = (meeting_id or "").strip()
    if not meeting_id:
        return False
    if _TRANSCRIPT_PRESENT.get(meeting_id):
        return True
    missed_at = _TRANSCRIPT_MISSES.get(meeting_id)
    if missed_at is not None and (time.monotonic() - missed_at) < TRANSCRIPT_MISS_TTL_SECONDS:
        return False
    try:
        # TRUTHY, not `is not None`. meetingscribe_source.read_meeting_json answers
        # an EMPTY DICT for an id it does not hold, which is a dict and therefore
        # passed an `is not None` test — that is exactly how the fixture tape
        # (living-room-standup) got a link to a page that 404s. An empty document is
        # not a transcript.
        found = bool(_read_meeting_document(meeting_id))
    except Exception:  # noqa: BLE001 — an unreadable library is "no link", not a 500
        found = False
    if found:
        _TRANSCRIPT_PRESENT[meeting_id] = True
        _TRANSCRIPT_MISSES.pop(meeting_id, None)
    else:
        _TRANSCRIPT_MISSES[meeting_id] = time.monotonic()
    return found


def card_transcript_url(meeting_id: str) -> str:
    """Where a card's quote traces back to. Empty when there is nowhere to go."""
    meeting_id = (meeting_id or "").strip()
    if not meeting_id or not MEETINGS_TAB_MOUNTED:
        return ""
    if not meeting_has_transcript(meeting_id):
        return ""
    return f"/meetings/{meeting_id}"

# THE SETTLED PRIVACY CLAIM. One string, used everywhere the board makes the
# local-first argument, because the older absolute wording ("nothing ever leaves
# this machine") is falsified by the model calls and by the cloud mirror the
# Connections tab itself lists. This sentence is true with the mirror on and true
# with it off.
PRIVACY_LINE = (
    "audio and transcripts never leave the Mac — "
    "only the receipts you see on this board are mirrored"
)

# --- the live rail ----------------------------------------------------------
#
# How many rows of thinking the rail holds. The orchestrator caps its own feed at
# 80 (see scratchpad/pipeline-feed-spec.md); this is the board's own ceiling for
# the DERIVED feed, and the trim applied to whatever the orchestrator sent.
FEED_MAX_ROWS = 80

# Longest text a rail row renders. The rail is read from across a room on a
# projector; anything longer is a paragraph pretending to be a log line.
FEED_TEXT_LIMIT = 140


# --- path resolution --------------------------------------------------------
#
# Overridable so the test suite and --demo can point the board at fabricated
# data without touching the real journal. Default is always the shared contract.


def journal_path() -> Path:
    """Where the board reads fired actions from."""
    override = os.environ.get("ADJOURN_BOARD_JOURNAL", "").strip()
    return Path(override).expanduser() if override else config.executions_journal_path()


def pending_path() -> Path:
    """Where the board reads countdown items from."""
    override = os.environ.get("ADJOURN_BOARD_PENDING", "").strip()
    return Path(override).expanduser() if override else config.pending_actions_path()


def pipeline_path() -> Path:
    """Where the board reads extract/plan/watch phases from."""
    override = os.environ.get("ADJOURN_BOARD_PIPELINE", "").strip()
    return Path(override).expanduser() if override else config.pipeline_status_path()


# --- small formatters -------------------------------------------------------


def format_clock_time(iso_timestamp: str) -> str:
    """'16:04:21' in the viewer's local time. Empty string for junk input."""
    moment = parse_timestamp(iso_timestamp)
    return moment.astimezone().strftime("%H:%M:%S") if moment else ""


def parse_timestamp(iso_timestamp: str) -> datetime | None:
    """Tolerant ISO parse; returns None rather than raising on a malformed line."""
    if not iso_timestamp:
        return None
    text = iso_timestamp.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def format_duration(seconds: float | int | None) -> str:
    """'42:07' for a meeting length, '1:02:11' past an hour, '' when unknown."""
    if not seconds or seconds <= 0:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, second = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{second:02d}"
    return f"{minutes}:{second:02d}"


def format_meeting_when(iso_timestamp: str) -> str:
    """'21 Aug · 14:13' — when this meeting's last action landed, in local time."""
    moment = parse_timestamp(iso_timestamp)
    if not moment:
        return ""
    local = moment.astimezone()
    return f"{local.day} {local.strftime('%b')} · {local.strftime('%H:%M')}"


def format_elapsed_since(iso_timestamp: str, now: datetime | None = None) -> str:
    """'just now' / '38s ago' / '4m ago' — the relative stamp under each card."""
    moment = parse_timestamp(iso_timestamp)
    if not moment:
        return ""
    now = now or datetime.now(UTC)
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def humanize_kind(kind: str) -> str:
    """Fallback headline when an executor forgot to write a human_summary."""
    return KIND_VERBS.get(kind, kind.replace("_", " "))


# --- board state ------------------------------------------------------------


def describe_undo_affordance(card: dict) -> tuple[bool, str]:
    """Can this card be undone, and if not, what does the board honestly say?

    The refusals are as much a part of the pitch as the undos: an email that
    already left the machine cannot be recalled, and the board says so instead
    of offering a button that lies.
    """
    if not card["ok"]:
        return False, "nothing to undo — it never landed"
    if card["undone"]:
        return False, "undone"
    if card["kind"] == "email_send" and card["mode"] == results.MODE_LIVE:
        return False, "sent · cannot be unsent"
    if not card["has_undo_payload"]:
        return False, "no undo handle"
    return True, ""


def build_card(record: dict, index: int, undone_keys: set[str], now: datetime) -> dict:
    """One journal execution record, shaped for the template."""
    dedup_key = (record.get("dedup_key") or "").strip()
    kind = record.get("kind") or "unknown"
    fired_at = record.get("fired_at") or record.get("written_at") or ""
    card = {
        "id": dedup_key or f"row-{index}",
        "row": index,
        "dedup_key": dedup_key,
        "kind": kind,
        "label": KIND_LABELS.get(kind, kind.replace("_", " ").title()),
        "ok": bool(record.get("ok")),
        "mode": record.get("mode") or results.MODE_SIM,
        "human_summary": (record.get("human_summary") or humanize_kind(kind)).strip(),
        "quote": (record.get("quote") or "").strip(),
        "speaker": (record.get("speaker") or "").strip(),
        "url": (record.get("url") or "").strip(),
        "link_label": LINK_LABELS.get(kind, "Open"),
        "external_id": record.get("external_id") or "",
        "meeting_id": record.get("meeting_id") or "",
        "fired_at": fired_at,
        "clock": format_clock_time(fired_at),
        "elapsed": format_elapsed_since(fired_at, now),
        "undone": bool(dedup_key and dedup_key in undone_keys),
        "has_undo_payload": bool(record.get("undo_payload")),
    }
    card["can_undo"], card["undo_note"] = describe_undo_affordance(card)
    card["undo_label"] = choose_undo_label(card)
    card["undo_title"] = describe_undo_label(card)
    card["state"] = "failed" if not card["ok"] else ("undone" if card["undone"] else "ok")
    card["carries_conflict"] = record_carries_conflict(record)
    card["pinned"] = card_shows_a_transition(card)
    card["url"] = board_recap_url(card)
    card["transcript_url"] = card_transcript_url(card["meeting_id"])
    return card


def build_cancellation_card(record: dict, index: int, now: datetime) -> dict:
    """One cancelled countdown, shaped for the template as a dimmed card.

    Same shape as an execution card so the template needs no second branch; the
    "cancelled" state is what dims it and removes the undo button.
    """
    dedup_key = (record.get("dedup_key") or "").strip()
    kind = record.get("kind") or "unknown"
    cancelled_at = record.get("cancelled_at") or record.get("written_at") or ""
    preview = (record.get("human_summary") or humanize_kind(kind)).strip()
    return {
        "id": dedup_key or f"cancelled-{index}",
        "row": index,
        "dedup_key": dedup_key,
        "kind": kind,
        "label": KIND_LABELS.get(kind, kind.replace("_", " ").title()),
        "ok": True,
        "mode": results.MODE_SIM,
        "human_summary": f"{preview} — cancelled during the regret window, never sent",
        "quote": (record.get("quote") or "").strip(),
        "speaker": (record.get("speaker") or "").strip(),
        "url": "",
        "link_label": "",
        "external_id": "",
        "meeting_id": record.get("meeting_id") or "",
        "fired_at": cancelled_at,
        "clock": format_clock_time(cancelled_at),
        "elapsed": format_elapsed_since(cancelled_at, now),
        "undone": False,
        "has_undo_payload": False,
        "can_undo": False,
        "undo_note": "cancelled by a human — it never ran",
        "undo_label": "",
        "undo_title": "",
        "carries_conflict": False,
        "pinned": False,
        "state": "cancelled",
        "transcript_url": card_transcript_url(record.get("meeting_id") or ""),
    }


# Kinds whose sim mode writes a REAL local artifact — a recap page on disk, an
# .ics in state/holds. Undoing one of those genuinely removes something, so the
# button keeps its plain word. Every other kind's sim run only rendered a payload.
LOCALLY_REAL_IN_SIM_KINDS = frozenset({"recap_page", "calendar_hold"})


def choose_undo_label(card: dict) -> str:
    """The word on the undo button — three words, because there are three cases.

    A simulated Slack message was never sent, so an unqualified "Undo" offers to
    reverse something that never happened — on a board whose whole argument is
    that the words on it are honest.

    But "Undo (sim)" is equally wrong on a SIM-badged calendar hold: the .ics is
    really on disk and the button really deletes it. That card wore a SIM badge
    and a plain "Undo" and a founder reading the runbook's "a simulated card's
    button reads Undo (sim)" would have caught the board contradicting itself.
    So the third case gets its own word: the badge says the transport was
    simulated, the button says the local file is not.
    """
    if card["mode"] != results.MODE_SIM:
        return "Undo"
    if card["kind"] in LOCALLY_REAL_IN_SIM_KINDS:
        return "Undo (local)"
    return "Undo (sim)"


def describe_undo_label(card: dict) -> str:
    """The tooltip under the button's word. Says which of the three cases this is."""
    if card["mode"] != results.MODE_SIM:
        return "reverses the real thing this action did"
    if card["kind"] in LOCALLY_REAL_IN_SIM_KINDS:
        return "the transport was simulated, but the local file is real — this removes it"
    return "nothing was sent; this only clears the card"


def board_recap_url(card: dict) -> str:
    """Point a recap card at /recap/<meeting_id> instead of at a file:// path.

    The journal stores the recap's real location, which is a file:// URL because
    the page is a file the user owns. Clicking a file:// link FROM an http:// page
    is blocked without comment in stock Chrome and Safari, and that click is the
    demo's closing beat. The board serves the same bytes over its own origin, so
    the link works; the file path is still on the card as the external id.
    """
    if card["kind"] != "recap_page" or not card["meeting_id"]:
        return card["url"]
    if card["url"] and not card["url"].startswith("file:"):
        return card["url"]
    return f"/recap/{card['meeting_id']}"


def record_carries_conflict(record: dict) -> bool:
    """True when this execution is a what-changed card with a Before/After table.

    Reads the planner's own `decision-changed` label out of the payload the
    executor sent, so it is the same signal that put the banner on the issue —
    not a guess from the summary text.

    TWO SHAPES, BECAUSE THERE REALLY ARE TWO. A simulated result wraps the
    rendered body: `{"simulated": true, "payload": {..., "labels": [...]}}`
    (results.ExecutorResult.simulated). A LIVE github_update writes a FLAT
    payload whose key is `labels_added`, filled in one label at a time as each
    `gh` call succeeds (github_update_executor.py:404-418). This function used to
    read only the first shape, so on every live run carries_conflict was false,
    the star card was never pinned, and the dress rehearsal measured it 1569px
    below the fold while the runbook called it the first card on screen. Both
    shapes are read now, and both are covered by tests.
    """
    payload = record.get("undo_payload") or {}
    if not isinstance(payload, dict):
        return False
    inner = payload.get("payload")
    labels: list = []
    if isinstance(inner, dict):
        labels = list(inner.get("labels") or [])
    labels += list(payload.get("labels_added") or [])
    labels += list(payload.get("labels") or [])
    return planner.LABEL_DECISION_CHANGED in labels


def compose_destination(item: orchestrator.PendingAction) -> str:
    """Where this draft will go — shown above the editor, not inside it."""
    payload = item.payload or {}
    if item.kind == "email_send":
        recipients = payload.get("to") or []
        if isinstance(recipients, str):
            recipients = [recipients]
        named = [str(address).strip() for address in recipients if str(address).strip()]
        if named:
            return "To " + ", ".join(named)
        person = str(payload.get("person") or "").strip()
        return f"To {person}" if person else "Email"
    if item.kind == "slack_send":
        try:
            from .executors import slack_send_executor

            action = planner.Action(
                kind="slack_send",
                payload=payload,
                meeting_id=item.meeting_id or "",
            )
            channel = (
                slack_send_executor.resolve_channel(action)
                or slack_send_executor.PLACEHOLDER_CHANNEL
            )
        except Exception:  # noqa: BLE001 — a missing secret still needs a line
            channel = ""
        return f"To {channel}" if channel else "Slack"
    return KIND_LABELS.get(item.kind, item.kind.replace("_", " ").title())


def compose_draft_fields(item: orchestrator.PendingAction) -> tuple[str, str]:
    """(subject, body) for the Ready-to-send editor. Subject is empty for Slack."""
    payload = item.payload or {}
    if item.kind == "email_send":
        return (
            str(payload.get("subject") or "").strip(),
            str(payload.get("body_text") or payload.get("text") or "").strip(),
        )
    return "", str(payload.get("text") or "").strip()


def sort_pending_items(pending: list[dict]) -> None:
    """Holds first (Ready to send), then countdowns soonest-first."""
    pending.sort(
        key=lambda item: (
            0 if item.get("hold_for_send") else 1,
            item.get("created_at") or "",
            item.get("seconds_remaining") or 0,
        )
    )


def build_pending_item(item: orchestrator.PendingAction, now: datetime) -> dict:
    """One pending entry, shaped for a countdown ring or a Ready-to-send draft."""
    remaining = item.seconds_remaining(now)
    window = item.regret_window_s or config.regret_window_seconds() or 60
    subject, body = compose_draft_fields(item)
    hold = bool(item.hold_for_send)
    return {
        "id": item.dedup_key,
        "dedup_key": item.dedup_key,
        "kind": item.kind,
        "label": KIND_LABELS.get(item.kind, item.kind.replace("_", " ").title()),
        "human_preview": item.human_preview or humanize_kind(item.kind),
        "quote": (item.quote or "").strip(),
        "speaker": (item.speaker or "").strip(),
        "meeting_id": item.meeting_id or "",
        "created_at": item.created_at or "",
        "fire_at": item.fire_at,
        "fire_clock": format_clock_time(item.fire_at) if item.fire_at else "",
        "regret_window_s": window,
        "seconds_remaining": 0 if hold else int(remaining),
        "fraction_remaining": (
            0.0 if hold or not window else max(0.0, min(1.0, remaining / window))
        ),
        "status": item.status,
        "hold_for_send": hold,
        "destination": compose_destination(item),
        "draft_subject": subject,
        "draft_body": body,
        "transcript_url": card_transcript_url(item.meeting_id or ""),
    }


def read_meeting_header(meeting_id: str) -> dict:
    """Title and duration for the board header.

    Prefers meetingscribe_source (Lane A) once it is implemented; falls back to a
    read-only scan of ~/.meetingscribe/recordings for a directory ending in
    " — <id>", because the folder renames itself when the title changes. Never
    raises: an unknown meeting simply renders without a title.
    """
    # `title` stays exactly what the recording says it is — callers branch on it
    # being empty. `display_title` is the one a masthead prints: never blank,
    # never a slug. See humanize_meeting_id.
    header = {
        "meeting_id": meeting_id,
        "title": "",
        "display_title": humanize_meeting_id(meeting_id),
        "duration": "",
        "created": "",
    }
    if not meeting_id:
        return header
    # "Never raises" was the docstring's claim and not the code's behaviour: the
    # library read reaches the engine and the filesystem, and either can throw.
    # Every page on this server calls through here, so one unreadable recordings
    # folder used to be a 500 on the whole board — a missing TITLE is the correct
    # cost of that, not a missing app.
    try:
        document = _read_meeting_document(meeting_id)
    except Exception:  # noqa: BLE001
        return header
    if not document:
        return header
    header["title"] = str(document.get("title") or "")
    header["display_title"] = header["title"] or header["display_title"]
    header["duration"] = format_duration(document.get("duration"))
    header["created"] = str(document.get("created") or "")
    return header


def read_pending_meeting_title() -> str:
    """The meeting title the orchestrator stamped on state/pending.json, if any."""
    try:
        document = json.loads(pending_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(document.get("meeting_title") or "") if isinstance(document, dict) else ""


def _read_meeting_document(meeting_id: str) -> dict | None:
    try:
        from . import meetingscribe_source

        document = meetingscribe_source.read_meeting_json(meeting_id)
        if isinstance(document, dict) and document:
            return document
    except (NotImplementedError, AttributeError, ImportError, OSError, ValueError):
        pass
    from_disk = _read_meeting_document_from_disk(meeting_id)
    if from_disk:
        return from_disk
    # LAST, AND ONLY LAST: a fixture tape. A replayed meeting has a real
    # transcript and a real title but no recording, so this is what lets
    # `meeting_has_transcript` answer truthfully for `--replay` (the quote on a
    # card gets somewhere to go) and what stops `read_meeting_header` falling
    # back to printing the raw internal slug as the meeting's name on the cold
    # open. A real recording always wins: this is reached only when neither the
    # engine nor the recordings folder holds the id.
    from . import fixture_library

    return fixture_library.fixture_meeting_document(meeting_id) or from_disk


def _read_meeting_document_from_disk(meeting_id: str) -> dict | None:
    """Read-only fallback. Never writes, never touches anything but meeting.json."""
    recordings = config.RECORDINGS_DIR
    suffix = f" — {meeting_id}"
    try:
        candidates = [
            directory
            for directory in recordings.iterdir()
            if directory.is_dir() and directory.name.endswith(suffix)
        ]
    except OSError:
        return None
    for directory in candidates:
        try:
            return json.loads((directory / "meeting.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


# Kinds that REWRITE one artifact in place rather than creating a new thing each
# time. The recap page is rendered again on the reconcile pass and again after
# every countdown that fires late, so the journal honestly holds several records
# for one file — but the board shows a card per THING, not a card per write.
REWRITTEN_IN_PLACE_KINDS: frozenset[str] = frozenset({"recap_page"})

# Kinds the per-person ledger leaves out. The ledger answers "who owes what";
# the recap is a page about the whole meeting and belongs to nobody.
LEDGER_EXCLUDED_KINDS: frozenset[str] = frozenset({"recap_page"})


def collapse_rewritten_records(executions: list[dict]) -> list[dict]:
    """Keep only the newest record for each rewritten-in-place artifact.

    Position is preserved at the LAST write, so a recap that was refreshed when a
    Slack message finally went out appears where it actually happened rather than
    where the first draft of it did.
    """
    latest_index: dict[str, int] = {}
    for index, record in enumerate(executions):
        if record.get("kind") not in REWRITTEN_IN_PLACE_KINDS:
            continue
        key = record.get("dedup_key") or f"{record.get('kind')}:{record.get('meeting_id')}"
        latest_index[key] = index
    kept: list[dict] = []
    for index, record in enumerate(executions):
        if record.get("kind") not in REWRITTEN_IN_PLACE_KINDS:
            kept.append(record)
            continue
        key = record.get("dedup_key") or f"{record.get('kind')}:{record.get('meeting_id')}"
        if latest_index.get(key) == index:
            kept.append(record)
    return kept


def summarize_cards(cards: list[dict]) -> dict:
    """Running totals computed from what the board actually SHOWS.

    results.summarize_executions() counts journal lines, which is the right answer
    for the journal and the wrong one for the header: it would report a recap
    rewritten three times as three actions.
    """
    # A cancelled countdown is on the board but was never an action: counting it
    # as one (and as a "sim" one) would inflate the very number the pitch quotes.
    executed = [card for card in cards if card["state"] != "cancelled"]
    return {
        "fired": len(executed),
        "live": sum(1 for card in executed if card["mode"] == results.MODE_LIVE),
        "sim": sum(1 for card in executed if card["mode"] != results.MODE_LIVE),
        "failed": sum(1 for card in executed if not card["ok"]),
        "undone": sum(1 for card in executed if card["undone"]),
        "cancelled": len(cards) - len(executed),
        "by_kind": _count_cards_by_kind(executed),
    }


def _count_cards_by_kind(cards: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for card in cards:
        counts[card["kind"]] = counts.get(card["kind"], 0) + 1
    return counts


# Kinds that are a Before/After by definition, whatever their payload says. A
# linear_move IS a state transition (In Progress -> In Review); there is no label
# to read because the kind is the signal. Everything else earns a pin only by
# carrying the planner's decision-changed label.
ALWAYS_PINNED_KINDS: frozenset[str] = frozenset({"linear_move"})


def card_shows_a_transition(card: dict) -> bool:
    """True when the card's whole point is a Before and an After.

    Two shapes qualify, and they are the two the runbook narrates:
      * a what-changed card, carrying the planner's `decision-changed` label
      * a linear_move, which IS a state transition by definition (In Progress ->
        In Review); there is no separate label to read because the kind is the
        signal.
    Everything else reports a single fact and reads fine further down.

    A cancelled countdown never transitioned anything, so it is never pinned even
    if its kind would otherwise qualify.
    """
    if card.get("state") == "cancelled":
        return False
    return bool(card.get("carries_conflict")) or card.get("kind") in ALWAYS_PINNED_KINDS


def order_cards_for_reading(cards_in_fire_order: list[dict]) -> list[dict]:
    """Transition cards on top in fire order; everything else newest-first.

    Newest-first is right for a board being watched live — new work slides in at
    the top. It was exactly wrong for THIS board, because every action of a
    meeting fires in the same instant after extraction, so "newest first" simply
    printed the fire order backwards: the Before/After card, the beat the whole
    narration is built around, landed last, 1255px below the fold, under the
    recap page and eight others. Measured in a real browser at 1512x900.

    So the cards that show a transition are pinned above the fold and kept in the
    order they fired — which is also the order the runbook narrates them in: the
    #2 decision, the reassignment that followed it, then the SHA-5 move. The rest
    keeps the newest-first behaviour that makes a live run feel live.

    The SHA-5 card is in here because DEMO.md §4 calls it "the second-biggest
    beat" and promises it is pinned high; pinning only the conflict cards left it
    at 1890px, a full screen below the fold, and a runbook that promises
    something the screen does not do is the exact failure this board exists to
    argue against.
    """
    pinned = [card for card in cards_in_fire_order if card_shows_a_transition(card)]
    rest = [card for card in cards_in_fire_order if not card_shows_a_transition(card)]
    rest.reverse()
    return pinned + rest


def _meeting_activity_stamp(group: dict) -> str:
    """Newest ISO timestamp in the group — what orders meetings on the board."""
    stamps = [card.get("fired_at") or "" for card in group.get("cards") or []]
    stamps += [
        item.get("created_at") or item.get("fire_at") or ""
        for item in group.get("pending") or []
    ]
    return max(stamps) if stamps else ""


def meeting_group_heading(meeting_id: str) -> dict:
    """Title, time, and the door onto this meeting's own follow-through page."""
    meeting_id = (meeting_id or "").strip()
    header = read_meeting_header(meeting_id) if meeting_id else {
        "title": "",
        "display_title": "",
        "duration": "",
    }
    title = (
        (header.get("display_title") or "").strip()
        or (header.get("title") or "").strip()
        or humanize_meeting_id(meeting_id)
        or "Untitled meeting"
    )
    return {
        "meeting_id": meeting_id,
        "display_title": title,
        "duration": header.get("duration") or "",
        "href": f"/meeting/{meeting_id}" if meeting_id else "",
        "transcript_url": card_transcript_url(meeting_id) if meeting_id else "",
    }


def build_meeting_groups(cards: list[dict], pending: list[dict]) -> list[dict]:
    """Cards and drafts, bucketed by meeting, newest meeting first.

    Follow-through used to dump every meeting's work into one Executed pile, so a
    Slack draft from this morning sat next to a Linear move from last week with
    nothing saying which room they came out of. Each group is one meeting: its
    drafts (Ready to send) then its cards, in the same reading order the single
    meeting page uses.
    """
    buckets: dict[str, dict] = {}

    def bucket_for(meeting_id: str) -> dict:
        key = (meeting_id or "").strip()
        if key not in buckets:
            buckets[key] = {
                **meeting_group_heading(key),
                "cards": [],
                "pending": [],
            }
        return buckets[key]

    for item in pending or []:
        bucket_for(item.get("meeting_id") or "")["pending"].append(item)
    for card in cards or []:
        bucket_for(card.get("meeting_id") or "")["cards"].append(card)

    groups = list(buckets.values())
    for group in groups:
        fire_order = sorted(group["cards"], key=lambda card: card.get("fired_at") or "")
        group["cards"] = order_cards_for_reading(fire_order)
        sort_pending_items(group["pending"])
        stamp = _meeting_activity_stamp(group)
        group["when"] = format_meeting_when(stamp)
        group["executed_count"] = sum(
            1 for card in group["cards"] if card.get("state") != "cancelled"
        )
        group["pending_count"] = len(group["pending"])
    groups.sort(key=_meeting_activity_stamp, reverse=True)
    return groups


def executors_pipeline_view(totals: dict, pending_count: int) -> dict:
    """Countdown / live / sim / cancelled, composed from files the board already reads."""
    live = int(totals.get("live") or 0)
    sim = int(totals.get("sim") or 0)
    cancelled = int(totals.get("cancelled") or 0)
    failed = int(totals.get("failed") or 0)
    parts = []
    if pending_count:
        parts.append(f"{pending_count} to send")
    if live:
        parts.append(f"{live} live")
    if sim:
        parts.append(f"{sim} sim")
    if cancelled:
        parts.append(f"{cancelled} cancelled")
    if failed:
        parts.append(f"{failed} failed")
    if not parts:
        phase = "idle"
        detail = "nothing fired yet"
    elif pending_count:
        phase = "pending"
        detail = " · ".join(parts)
    else:
        phase = "done"
        detail = " · ".join(parts)
    return {
        "phase": phase,
        "pending": pending_count,
        "fired_live": live,
        "fired_sim": sim,
        "cancelled": cancelled,
        "failed": failed,
        "detail": detail,
    }


def _clip(text: str, limit: int = FEED_TEXT_LIMIT) -> str:
    """One rail row's worth of text. Never a paragraph, never a hard cut mid-word."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def normalize_feed_rows(raw_feed) -> list[dict]:
    """The orchestrator's own feed, shape-guarded. Junk rows are dropped, not fatal.

    The contract is scratchpad/pipeline-feed-spec.md. Everything here is defensive
    because pipeline.json is written by another process on a 1s read cadence: a
    half-written array, a row that is a string, a seq that is a float — none of
    those may take the board down mid-demo.
    """
    rows: list[dict] = []
    if not isinstance(raw_feed, list):
        return rows
    for index, item in enumerate(raw_feed):
        if not isinstance(item, dict):
            continue
        text = _clip(item.get("text") or "")
        label = _clip(str(item.get("label") or ""), 24)
        if not text and not label:
            continue
        try:
            seq = int(item.get("seq") or (index + 1))
        except (TypeError, ValueError):
            seq = index + 1
        rows.append(
            {
                "seq": seq,
                "at": str(item.get("at") or ""),
                "stage": str(item.get("stage") or "planner"),
                "tone": str(item.get("tone") or "note"),
                "label": label,
                "text": text,
                "derived": False,
            }
        )
    return rows[-FEED_MAX_ROWS:]


def derive_feed_rows(
    raw: dict, cards: list[dict], pending: list[dict]
) -> list[dict]:
    """The rail's fallback feed, composed from files the board already reads.

    This exists so the rail animates TONIGHT, whether or not the orchestrator has
    implemented the per-statement feed. It is strictly coarser — a derived row can
    say "3 decisions landed" but not quote the first words of one, because the
    statements themselves are never written anywhere the board can see. What it
    can do honestly is grow as the run advances: the watcher row appears, then the
    extraction kinds, then the planner's routes and refusals, then one row per
    action as it fires. That is a real feed, not a spinner.

    Ordering is deterministic (stage order, then count desc, then name) so a poll
    that changes nothing produces the same rows and compute_version stays quiet.
    """
    watcher = raw.get("watcher") or {}
    extraction = raw.get("extraction") or {}
    plan = raw.get("planner") or {}
    rows: list[dict] = []

    def add(stage: str, tone: str, label: str, text: str) -> None:
        rows.append(
            {
                "seq": len(rows) + 1,
                "at": "",
                "stage": stage,
                "tone": tone,
                "label": _clip(label, 24),
                "text": _clip(text),
                "derived": True,
            }
        )

    phase = str(watcher.get("phase") or orchestrator.WATCHER_WAITING)
    if phase != orchestrator.WATCHER_WAITING:
        add("watcher", "note", phase.replace("_", " "),
            str(watcher.get("detail") or "") or "the watcher saw a meeting end")

    # THE SAME PHRASE THE RAIL'S OWN LABEL USES, and for the same reason: there
    # is no zeroth batch, so the moment before the first one lands says what is
    # happening rather than printing "batch 0/5". See _batch_line.
    if int(extraction.get("batch_total") or 0):
        add("extraction", "note", "batch", _batch_line(extraction))

    for kind, count in _sorted_counts(extraction.get("kinds")):
        add("extraction", "act", kind,
            f"{count} landed" if count != 1 else "1 landed")

    silent = int(extraction.get("silent_count") or 0)
    if silent:
        add("extraction", "ignore", "silent",
            f"{silent} line{'s' if silent != 1 else ''} produced nothing")

    for kind, count in _sorted_counts(plan.get("ignored_kinds")):
        add("planner", "ignore", kind, f"{kind} → ignored")

    for kind, count in _sorted_counts(plan.get("action_kinds")):
        add("planner", "act", kind,
            f"routed → {kind}" + (f" ×{count}" if count != 1 else ""))

    for item in pending:
        label = "ready to send" if item.get("hold_for_send") else "holding"
        add("executor", "hold", item.get("kind") or "action",
            f"{label} · {item.get('human_preview') or ''}")

    for card in cards[-14:]:
        tone = "act"
        if card.get("state") in {"failed", "cancelled"} or not card.get("ok", True):
            tone = "decline"
        elif card.get("undone"):
            tone = "ignore"
        mode = card.get("mode") or ""
        add("executor", tone, card.get("kind") or "action",
            f"{card.get('human_summary') or ''}" + (f" · {mode}" if mode else ""))

    return rows[-FEED_MAX_ROWS:]


def _sorted_counts(counts) -> list[tuple[str, int]]:
    """{kind: n} → [(kind, n)] most first, name-stable. Junk in, empty out."""
    if not isinstance(counts, dict):
        return []
    clean: list[tuple[str, int]] = []
    for key, value in counts.items():
        try:
            clean.append((str(key), int(value)))
        except (TypeError, ValueError):
            continue
    clean.sort(key=lambda pair: (-pair[1], pair[0]))
    return clean


ADDITIVE_EXTRACTION_KEYS = ("batch_index", "batch_total")


def _overlay_additive_pipeline_fields(raw: dict) -> None:
    """Copy `feed` and the batch counters off pipeline.json into `raw`, in place.

    Never raises: a missing file, a half-written file, or a document that is not a
    dict all leave `raw` exactly as the dataclass produced it.
    """
    try:
        document = json.loads(pipeline_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(document, dict):
        return
    if isinstance(document.get("feed"), list):
        raw["feed"] = document["feed"]
    source = document.get("extraction")
    if isinstance(source, dict):
        target = raw.setdefault("extraction", {})
        if isinstance(target, dict):
            for key in ADDITIVE_EXTRACTION_KEYS:
                if key in source:
                    target[key] = source[key]


def load_pipeline_status(
    totals: dict | None = None,
    pending_count: int = 0,
    cards: list[dict] | None = None,
    pending: list[dict] | None = None,
) -> dict:
    """Shape pipeline.json + executor totals for the guts panel. Never raises."""
    try:
        raw = orchestrator.read_pipeline_status(path=pipeline_path()).to_dict()
    except Exception:  # noqa: BLE001 — a missing or half-written file is idle, not an error page
        raw = orchestrator.PipelineStatus().to_dict()
    # ADDITIVE FIELDS COME OFF THE RAW DOCUMENT, NOT THE DATACLASS. PipelineStatus
    # is the orchestrator's schema and it drops keys it does not declare, so
    # routing `feed` and the batch counters through it would mean the board could
    # not read them until orchestrator.py changed. Reading the JSON directly makes
    # the two halves of the contract independent: Lane B writes the field, the
    # board renders it, and neither has to land first.
    _overlay_additive_pipeline_fields(raw)
    watcher = raw.get("watcher") or {}
    extraction = raw.get("extraction") or {}
    planner = raw.get("planner") or {}
    executors = executors_pipeline_view(totals or {}, pending_count)

    # THE ORCHESTRATOR'S OWN FEED ALWAYS WINS. It is the only source that can name
    # a statement's first words; the derived one is a floor, not a competitor.
    feed = normalize_feed_rows(raw.get("feed"))
    feed_source = "orchestrator"
    if not feed:
        feed = derive_feed_rows(raw, cards or [], pending or [])
        feed_source = "derived"
    return {
        "updated_at": raw.get("updated_at") or "",
        "mode": raw.get("mode") or "idle",
        "pass_name": raw.get("pass_name") or "",
        "meeting_id": raw.get("meeting_id") or "",
        "meeting_title": raw.get("meeting_title") or "",
        "watcher": {
            "phase": watcher.get("phase") or orchestrator.WATCHER_WAITING,
            "meeting_id": watcher.get("meeting_id") or "",
            "detail": watcher.get("detail") or "waiting for a meeting to end",
            "headline": _watcher_headline(watcher.get("phase") or orchestrator.WATCHER_WAITING),
        },
        "extraction": {
            "phase": extraction.get("phase") or orchestrator.STAGE_IDLE,
            "statement_count": int(extraction.get("statement_count") or 0),
            "segment_count": int(extraction.get("segment_count") or 0),
            "silent_count": int(extraction.get("silent_count") or 0),
            # Batch progress. Absent (0/0) until the orchestrator implements the
            # feed spec; the rail renders no bar rather than a fake one.
            "batch_index": int(extraction.get("batch_index") or 0),
            "batch_total": int(extraction.get("batch_total") or 0),
            "batch_line": _batch_line(extraction),
            "batch_fraction": _batch_fraction(extraction),
            "kinds": extraction.get("kinds") or {},
            "source": extraction.get("source") or "",
            "detail": extraction.get("detail") or "idle",
            "headline": _extraction_headline(extraction),
        },
        "planner": {
            "phase": planner.get("phase") or orchestrator.STAGE_IDLE,
            "action_count": int(planner.get("action_count") or 0),
            "ignored_count": int(planner.get("ignored_count") or 0),
            "action_kinds": planner.get("action_kinds") or {},
            "ignored_kinds": planner.get("ignored_kinds") or {},
            "detail": planner.get("detail") or "idle",
            "headline": _planner_headline(planner),
        },
        "executors": {
            **executors,
            "headline": _executors_headline(executors),
        },
        "feed": feed,
        "feed_source": feed_source,
        "feed_seq": max((row["seq"] for row in feed), default=0),
        "feed_count": len(feed),
    }


def _batch_line(extraction: dict) -> str:
    """'batch 2/4' while the extractor is chewing; '' when there is no batching.

    INDEX 0 IS A PHRASE, NOT A COUNT. The writer publishes 0/N deliberately, so
    the bar exists during the longest wait rather than appearing late — that is
    right and stays. But "batch 0/5" is odd English on a label a room stares at
    for the ~8 seconds before batch 1 lands, so the zero state says what is
    actually happening. _batch_fraction still returns 0.0, so the bar renders
    empty underneath it exactly as before.
    """
    total = int(extraction.get("batch_total") or 0)
    if total <= 0:
        return ""
    index = max(0, min(total, int(extraction.get("batch_index") or 0)))
    if index == 0:
        return f"reading {total} batch{'' if total == 1 else 'es'}"
    return f"batch {index}/{total}"


def _batch_fraction(extraction: dict) -> float:
    """0.0–1.0 for the rail's progress bar. 0.0 when there is nothing to show."""
    total = int(extraction.get("batch_total") or 0)
    if total <= 0:
        return 0.0
    index = max(0, min(total, int(extraction.get("batch_index") or 0)))
    return round(index / total, 4)


def _watcher_headline(phase: str) -> str:
    if phase == orchestrator.WATCHER_MEETING_STOPPED:
        return "meeting stopped"
    if phase == orchestrator.WATCHER_TRANSCRIPT_READY:
        return "transcript ready"
    return "waiting"


def _extraction_headline(extraction: dict) -> str:
    """'12 statements · 18 lines silent' — both halves of the restraint claim.

    The headline used to read "12 statements" alone, and the panel beside it read
    "10 decided · 2 ignored". A founder hearing "eighteen of twenty-eight lines
    produced nothing" and looking at the screen saw a two, which reads as a
    system that acted on twenty-six of twenty-eight lines. The number the pitch
    is about now appears where the pitch points.
    """
    phase = extraction.get("phase") or orchestrator.STAGE_IDLE
    if phase == orchestrator.STAGE_RUNNING:
        return "running"
    count = int(extraction.get("statement_count") or 0)
    silent = int(extraction.get("silent_count") or 0)
    if phase == orchestrator.STAGE_DONE:
        headline = f"{count} statement{'s' if count != 1 else ''}"
        if extraction.get("segment_count"):
            headline += f" · {silent} line{'s' if silent != 1 else ''} silent"
        return headline
    return "idle"


def _planner_headline(planner: dict) -> str:
    phase = planner.get("phase") or orchestrator.STAGE_IDLE
    if phase == orchestrator.STAGE_RUNNING:
        return "routing"
    decided = int(planner.get("action_count") or 0)
    ignored = int(planner.get("ignored_count") or 0)
    if phase == orchestrator.STAGE_DONE:
        return f"{decided} decided · {ignored} ignored"
    return "idle"


def _executors_headline(executors: dict) -> str:
    pending = int(executors.get("pending") or 0)
    if pending:
        return f"{pending} to send"
    live = int(executors.get("fired_live") or 0)
    sim = int(executors.get("fired_sim") or 0)
    if live or sim:
        return f"{live} live · {sim} sim"
    cancelled = int(executors.get("cancelled") or 0)
    if cancelled:
        return f"{cancelled} cancelled"
    return "idle"


def load_board_state(now: datetime | None = None) -> dict:
    """Everything the board renders, in one dict. The single read of both files."""
    now = now or datetime.now(UTC)
    records = results.read_executions(path=journal_path())
    executions = collapse_rewritten_records([
        record
        for record in records
        if record.get("record_type", results.RECORD_TYPE_EXECUTION)
        == results.RECORD_TYPE_EXECUTION
    ])
    undone_keys = results.read_undone_dedup_keys(path=journal_path())

    cards = [
        build_card(record, index, undone_keys, now)
        for index, record in enumerate(executions)
    ]
    cards += [
        build_cancellation_card(record, len(cards) + offset, now)
        for offset, record in enumerate(results.read_cancellations(path=journal_path()))
    ]
    cards.sort(key=lambda card: card.get("fired_at") or "")
    cards = order_cards_for_reading(cards)

    pending = [
        build_pending_item(item, now)
        for item in orchestrator.read_pending_actions(path=pending_path())
        if item.is_waiting
    ]
    sort_pending_items(pending)

    meeting_id = _latest_meeting_id(cards, pending)
    header = read_meeting_header(meeting_id)
    if not header["title"]:
        # The orchestrator stamps the title on the pending document; use it when
        # the recording folder has not been written (or renamed) yet.
        header["title"] = read_pending_meeting_title()
        header["display_title"] = header["title"] or header["display_title"]
    totals = summarize_cards(cards)
    totals["pending"] = len(pending)
    meeting_groups = build_meeting_groups(cards, pending)
    totals["meetings"] = len(meeting_groups)

    pipeline = load_pipeline_status(totals, len(pending), cards=cards, pending=pending)
    # NEVER-BLANK OPEN. Only computed when there is nothing else to show, so a
    # board with cards on it never pays for the memory read.
    last_adjourned = load_last_adjourned(now) if not cards and not pending else None
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "meeting": header,
        "totals": totals,
        "totals_line": build_totals_line(totals, last_adjourned),
        "cards": cards,
        "pending": pending,
        "meeting_groups": meeting_groups,
        "mode_note": describe_mode_note(totals, last_adjourned),
        "pipeline": pipeline,
        "last_adjourned": last_adjourned,
    }


# --- never-blank open --------------------------------------------------------
#
# THE PROBLEM THIS SOLVES. Demo-night state is a DELETED journal — that is what
# `reset_demo` is for, and it is correct. But it meant the first thing a founder
# saw when the board came up was a single italic sentence on an empty screen, and
# the pitch ("this system has been quietly doing your follow-through") was
# contradicted by the opening frame. A system that has done work should look like
# one before anybody presses anything.
#
# Two sources, in order of how much they know:
#   1. an ARCHIVED journal (executions.<ts>.jsonl, written by reset_demo) — real
#      receipts from the last meeting that actually ran on this machine;
#   2. MEMORY — the seeded prior standup, which is what remains after a wipe.
# Neither fabricates. If both are empty the board says so and shows the pulse.

LAST_ADJOURNED_ROWS = 4


def archived_journal_paths() -> list[Path]:
    """Rotated journals beside the live one, newest first. `executions.<ts>.jsonl`."""
    live = journal_path()
    try:
        siblings = [
            path
            for path in live.parent.iterdir()
            if path.is_file()
            and path.name.startswith(live.stem + ".")
            and path.name.endswith(live.suffix)
            and path != live
        ]
    except OSError:
        return []
    siblings.sort(key=lambda path: path.name, reverse=True)
    return siblings


def _last_adjourned_from_archive(now: datetime) -> dict | None:
    """The most recent archived journal, as receipts. None when there is none."""
    for path in archived_journal_paths():
        try:
            records = [
                record
                for record in results.read_executions(path=path)
                if record.get("record_type", results.RECORD_TYPE_EXECUTION)
                == results.RECORD_TYPE_EXECUTION
            ]
        except Exception:  # noqa: BLE001 — an unreadable archive is not an error page
            continue
        if not records:
            continue
        undone = results.read_undone_dedup_keys(path=path)
        cards = [
            build_card(record, index, undone, now)
            for index, record in enumerate(collapse_rewritten_records(records))
        ]
        cards.sort(key=lambda card: card.get("fired_at") or "")
        if not cards:
            continue
        newest = cards[-1]
        rows = [
            {
                "label": card["label"],
                "kind": card["kind"],
                "summary": card["human_summary"],
                "mode": card["mode"],
                "clock": card["clock"],
            }
            for card in cards[-LAST_ADJOURNED_ROWS:]
        ]
        title = read_meeting_header(newest.get("meeting_id") or "")["title"]
        return {
            "source": "journal",
            # A FILENAME IS NOT A SENTENCE. This line sits in the masthead beside
            # the running totals and under the receipts; what a reader needs from
            # it is where the rows came from, not which timestamped file on disk
            # holds them.
            "source_note": "read from this machine's own record",
            "meeting_id": newest.get("meeting_id") or "",
            "title": title or humanize_meeting_id(newest.get("meeting_id") or "")
                     or "The last meeting",
            "when": format_elapsed_since(newest.get("fired_at") or "", now),
            "count": len(cards),
            "rows": rows,
        }
    return None


def _last_adjourned_from_memory() -> dict | None:
    """The seeded prior meeting, read out of memory. Read-only, never raises."""
    try:
        from . import memory_store

        memory = memory_store.open_memory()
    except Exception:  # noqa: BLE001 — no memory backend is a quiet miss, not a 500
        return None
    try:
        rows: list[dict] = []
        seen: set[str] = set()
        title = ""
        when = ""
        for topic in memory.known_topics(limit=12):
            for prior in memory.find_prior_commitments(topic, limit=4):
                key = prior.segment_id or f"{prior.speaker}:{prior.claim}"
                if key in seen or not prior.claim:
                    continue
                seen.add(key)
                title = title or prior.meeting_id
                when = when or prior.meeting_date
                rows.append(
                    {
                        "label": prior.speaker or "someone",
                        "kind": prior.kind or "statement",
                        "summary": _clip(prior.claim, 110),
                        "mode": "",
                        "clock": prior.meeting_date or "",
                    }
                )
        if not rows:
            return None
        return {
            "source": "memory",
            "source_note": "read from what Adjourn remembers",
            "meeting_id": title,
            "title": humanize_meeting_id(title) or "The last standup",
            "when": when,
            "count": len(rows),
            "rows": rows[:LAST_ADJOURNED_ROWS],
        }
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            memory.close()
        except Exception:  # noqa: BLE001
            pass


def load_last_adjourned(now: datetime | None = None) -> dict | None:
    """What to show when the journal is empty. Journal archive first, memory second."""
    now = now or datetime.now(UTC)
    try:
        from_archive = _last_adjourned_from_archive(now)
    except Exception:  # noqa: BLE001
        from_archive = None
    if from_archive:
        return from_archive
    return _last_adjourned_from_memory()


def _latest_meeting_id(cards: list[dict], pending: list[dict]) -> str:
    """The meeting the board is currently about — whatever fired or is firing last."""
    for item in pending:
        if item["meeting_id"]:
            return item["meeting_id"]
    for card in cards:
        if card["meeting_id"]:
            return card["meeting_id"]
    return ""


def build_totals_line(totals: dict, last_adjourned: dict | None = None) -> str:
    """'7 actions · 3 live · 4 sim' — the header's running count.

    THE COLD OPEN IS THE EXCEPTION. Before the first meeting of the session the
    honest count is zero, and "0 actions · 0 live · 0 sim" as the entire header
    is a system that reads like it has never done anything — in the same frame
    where the Last adjourned panel below is showing real receipts from the last
    meeting this machine worked. So when there is nothing in flight AND there are
    receipts to point at, the line says what is true of BOTH halves of the screen:
    nothing yet from this one, this many from the last one. It still never
    invents a number — `count` is the panel's own, read off the same source.
    """
    fired = totals.get("fired", 0)
    pending = totals.get("pending", 0)
    if not fired and not pending and last_adjourned:
        receipts = int(last_adjourned.get("count") or 0)
        if receipts:
            noun = "receipt" if receipts == 1 else "receipts"
            return f"Nothing yet this session · {receipts} {noun} from the last meeting"
    noun = "action" if fired == 1 else "actions"
    parts = [f"{fired} {noun}", f"{totals.get('live', 0)} live", f"{totals.get('sim', 0)} sim"]
    meetings = int(totals.get("meetings") or 0)
    if meetings > 1:
        parts.insert(0, f"{meetings} meetings")
    if totals.get("failed"):
        parts.append(f"{totals['failed']} failed")
    if totals.get("undone"):
        parts.append(f"{totals['undone']} undone")
    if totals.get("cancelled"):
        parts.append(f"{totals['cancelled']} cancelled")
    return " · ".join(parts)


def describe_mode_note(totals: dict, last_adjourned: dict | None = None) -> str:
    """One honest line about what mode the run is in. Never hides sim."""
    # No setting names in the masthead. The header is read from across a room by
    # someone who will never type this into a shell; "every transport simulated"
    # is the whole fact, and Connections is where the reason lives.
    if config.is_simulation_forced():
        return "simulated transports — payloads are real, the sends are not"
    if totals.get("live") and totals.get("sim"):
        return "mixed mode — badges are per action"
    if totals.get("sim") and not totals.get("live"):
        return "simulated transports — payloads are real, the sends are not"
    if totals.get("live"):
        return "live transports"
    # A COLD OPEN HAS NO TRANSPORTS TO DESCRIBE, and it must not borrow the
    # receipts panel's line to fill the gap — that sentence is already printed
    # eighty pixels below, and the same words twice in one frame reads as a
    # template that has run out of things to say. This is the mode note, so it
    # says what is true of the mode: nothing has been sent or simulated yet.
    if last_adjourned:
        return "no transports yet this session"
    return ""


def compute_version(state: dict) -> str:
    """Hash of everything that should trigger a re-render.

    Deliberately excludes seconds_remaining: countdown rings are animated
    client-side from fire_at, so a ticking clock must not churn the fragment and
    restart every card's entrance animation.
    """
    pipeline = state.get("pipeline") or {}
    signature = {
        "cards": [
            (card["id"], card["state"], card["mode"], card["human_summary"], card["url"])
            for card in state["cards"]
        ],
        "pending": [(item["id"], item["fire_at"], item["status"]) for item in state["pending"]],
        "meeting": state["meeting"],
        "totals_line": state["totals_line"],
        "pipeline": (
            pipeline.get("mode"),
            pipeline.get("pass_name"),
            (pipeline.get("watcher") or {}).get("phase"),
            (pipeline.get("watcher") or {}).get("meeting_id"),
            (pipeline.get("extraction") or {}).get("phase"),
            (pipeline.get("extraction") or {}).get("statement_count"),
            (pipeline.get("planner") or {}).get("phase"),
            (pipeline.get("planner") or {}).get("action_count"),
            (pipeline.get("planner") or {}).get("ignored_count"),
            (pipeline.get("executors") or {}).get("phase"),
            (pipeline.get("executors") or {}).get("pending"),
            (pipeline.get("executors") or {}).get("fired_live"),
            (pipeline.get("executors") or {}).get("fired_sim"),
            (pipeline.get("executors") or {}).get("cancelled"),
            # The rail is the thing that has to move within 1s of a new event, so
            # the feed's high-water mark and its length are part of the identity
            # of the page. Both are cheap integers — no row text is hashed.
            pipeline.get("feed_seq"),
            pipeline.get("feed_count"),
            (pipeline.get("extraction") or {}).get("batch_index"),
            (pipeline.get("extraction") or {}).get("batch_total"),
        ),
    }
    payload = json.dumps(signature, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:16]


# --- ledger -----------------------------------------------------------------


def load_ledger(now: datetime | None = None) -> dict:
    """Per-person commitment ledger across every meeting in the journal.

    The journal is the authority — it is the record of what was actually done.
    memory_store is consulted read-only for cross-meeting context and its
    availability is reported honestly rather than faked when Falkor is down.
    """
    now = now or datetime.now(UTC)

    def read_ledger_records(path) -> list[dict]:
        return [
            record
            for record in collapse_rewritten_records([
                record
                for record in results.read_executions(path=path)
                if record.get("record_type", results.RECORD_TYPE_EXECUTION)
                == results.RECORD_TYPE_EXECUTION
            ])
            # The recap is a page about everyone, not a promise by anyone. Left in,
            # it showed up as an "Unattributed" person owing four things.
            if record.get("kind") not in LEDGER_EXCLUDED_KINDS
        ]

    # Follow-through and the Ledger must never disagree about whether the app has
    # done anything. The cold open shows last-meeting receipts from the archived
    # journal, so when this session's journal is empty the ledger reads the same
    # archive — badged as the last meeting's books, not this session's.
    ledger_journal = journal_path()
    from_last_meeting = False
    records = read_ledger_records(ledger_journal)
    if not records:
        for archived_path in archived_journal_paths():
            try:
                archived_records = read_ledger_records(archived_path)
            except Exception:  # noqa: BLE001 — an unreadable archive is a quiet miss
                continue
            if archived_records:
                ledger_journal = archived_path
                records = archived_records
                from_last_meeting = True
                break
    undone_keys = results.read_undone_dedup_keys(path=ledger_journal)

    people: dict[str, dict] = {}
    for index, record in enumerate(records):
        card = build_card(record, index, undone_keys, now)
        # One pass through the SAME name map the executors, the recap and the
        # graph use, so a journal written before that map existed (or by a
        # backfill that went round it) does not leave a row headed "You" beside
        # a row headed "Sharique" for the same person.
        speaker = ledger_speaker_name(card["speaker"])
        person = people.setdefault(
            speaker,
            {
                "speaker": speaker,
                "initials": build_initials(speaker),
                "role_note": ledger_role_note(speaker),
                "commitments": [],
                "meetings": set(),
                "live": 0,
                "sim": 0,
                "undone": 0,
                "failed": 0,
                "by_kind": {},
            },
        )
        person["commitments"].append(card)
        if card["meeting_id"]:
            person["meetings"].add(card["meeting_id"])
        person["live" if card["mode"] == results.MODE_LIVE else "sim"] += 1
        person["undone"] += 1 if card["undone"] else 0
        person["failed"] += 0 if card["ok"] else 1
        person["by_kind"][card["kind"]] = person["by_kind"].get(card["kind"], 0) + 1

    ledger_people = []
    for person in people.values():
        person["commitments"].reverse()
        person["meeting_count"] = len(person["meetings"])
        person["meetings"] = sorted(person["meetings"])
        person["total"] = len(person["commitments"])
        person["kinds"] = [
            {"kind": kind, "label": ledger_kind_label(kind), "count": count}
            for kind, count in sorted(person["by_kind"].items(), key=lambda pair: -pair[1])
        ]
        # A bucket is not a person and should not wear a person's monogram. "GU"
        # over "Guest" is initials for a word that is not a name.
        if person["role_note"]:
            person["initials"] = "··"
        ledger_people.append(person)
    ledger_people.sort(key=lambda person: (-person["total"], person["speaker"]))

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "people": ledger_people,
        "meeting_count": len({record.get("meeting_id") for record in records if record.get("meeting_id")}),
        "commitment_count": len(records),
        "from_last_meeting": from_last_meeting,
        "memory": describe_memory_availability(),
    }


UNATTRIBUTED_SPEAKER = "Unattributed"

# ONE CHIP, TWO TALLIES. A card's chip says LINEAR for both Linear kinds on
# purpose — create and move are the same surface doing two things, and on a card
# the headline says which. In the ledger's per-person tally there is no headline,
# so the shared label rendered as "Linear 1 · Linear 1" and read like a bug.
LEDGER_KIND_LABELS: dict[str, str] = {
    "linear_create": "Linear new",
    "linear_move": "Linear move",
}


def ledger_kind_label(kind: str) -> str:
    return LEDGER_KIND_LABELS.get(kind) or KIND_LABELS.get(kind, kind)


def ledger_speaker_name(speaker: str) -> str:
    """The name a ledger row is filed under. Never a raw track label, never blank."""
    text = (speaker or "").strip()
    if not text:
        return UNATTRIBUTED_SPEAKER
    try:
        from . import extraction

        return extraction.speaker_display_name(text) or UNATTRIBUTED_SPEAKER
    except Exception:  # noqa: BLE001 — a name is never worth an error page
        return text


def ledger_role_note(speaker: str) -> str:
    """One quiet line under a ledger name, for the names that are not a person.

    THE LEDGER IS A LIST OF PEOPLE, and two of its headings are not people: the
    configured guest name covers everyone on the far end of a call that
    diarisation never named, and Unattributed covers lines that carried no
    speaker at all. Rendered bare, beside "Sharique", they read like a config
    file leaked into the product — which was the finding. Saying in one clause
    what the heading actually stands for turns both back into facts, and it is
    the honest thing besides: Adjourn genuinely does not know who these were.

    Empty for a real name — a person needs no explaining.
    """
    text = (speaker or "").strip()
    if text == UNATTRIBUTED_SPEAKER:
        return "lines that carried no speaker"
    try:
        from . import extraction

        if text and text == extraction.guest_display_name():
            return "everyone on the call Adjourn could not name by voice"
    except Exception:  # noqa: BLE001
        pass
    return ""


def build_initials(name: str) -> str:
    """'SK' from 'Sharique Khatri'; first two letters otherwise."""
    words = [word for word in (name or "").split() if word]
    if not words:
        return "··"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


def describe_memory_availability() -> dict:
    """Read-only probe of memory_store. Reports unavailability instead of faking it."""
    probe = {"backend": config.memory_backend(), "available": False, "note": "", "stats": {}}
    try:
        from . import memory_store

        memory = memory_store.open_memory()
    except NotImplementedError:
        probe["note"] = "memory backend not implemented yet — ledger derived from the journal"
        return probe
    except Exception as error:  # noqa: BLE001 — the board degrades, it does not crash
        probe["note"] = f"memory unavailable ({error}) — ledger derived from the journal"
        return probe
    try:
        probe["stats"] = memory.stats()
        probe["backend"] = getattr(memory, "backend", probe["backend"])
        probe["available"] = True
    except Exception as error:  # noqa: BLE001
        probe["note"] = f"memory opened but unreadable ({error}) — ledger derived from the journal"
    finally:
        try:
            memory.close()
        except Exception:  # noqa: BLE001
            pass
    return probe


# --- one meeting's follow-through -------------------------------------------


def load_meeting_view(meeting_id: str, now: datetime | None = None) -> dict:
    """Everything /meeting/<id> renders: that meeting's cards, filtered from the journal.

    The Meetings tab's detail page links here, so this is the seam between "what
    was said" and "what happened next". It is the same build_card() the board
    uses — one card shape, one set of badges, one undo affordance — narrowed to
    one meeting_id and left in fire order, because a page about a single meeting
    is a story and stories run forwards.
    """
    now = now or datetime.now(UTC)
    meeting_id = (meeting_id or "").strip()
    records = collapse_rewritten_records([
        record
        for record in results.read_executions(path=journal_path())
        if record.get("record_type", results.RECORD_TYPE_EXECUTION)
        == results.RECORD_TYPE_EXECUTION
        and (record.get("meeting_id") or "") == meeting_id
    ])
    undone_keys = results.read_undone_dedup_keys(path=journal_path())
    cards = [build_card(record, index, undone_keys, now) for index, record in enumerate(records)]
    cards += [
        build_cancellation_card(record, len(cards) + offset, now)
        for offset, record in enumerate(results.read_cancellations(path=journal_path()))
        if (record.get("meeting_id") or "") == meeting_id
    ]
    cards.sort(key=lambda card: card.get("fired_at") or "")

    pending = [
        build_pending_item(item, now)
        for item in orchestrator.read_pending_actions(path=pending_path())
        if item.is_waiting and (item.meeting_id or "") == meeting_id
    ]
    sort_pending_items(pending)

    totals = summarize_cards(cards)
    totals["pending"] = len(pending)
    header = read_meeting_header(meeting_id)
    recap = next((card for card in cards if card["kind"] == "recap_page"), None)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        # Same rule as Connections: the footer says a time, not an ISO string.
        "generated_clock": format_clock_time(now.isoformat()),
        "meeting": header,
        "cards": cards,
        "pending": pending,
        "totals": totals,
        "totals_line": build_totals_line(totals),
        "recap_url": recap["url"] if recap else "",
        # SAME GUARD AS A CARD'S QUOTE. This used to be an unconditional
        # f"/meetings/{meeting_id}", so the "Transcript" crosslink at the top of
        # this page pointed at a 404 for every meeting the library does not hold
        # — including the default `--replay` tape, which is the page a judge
        # reaches by clicking Follow-through from a meeting.
        "transcript_url": card_transcript_url(meeting_id),
    }


# --- connections -------------------------------------------------------------
#
# A read-only page. It answers the question a technical founder asks thirty
# seconds after the board lands — "so what of that was real?" — with booleans,
# not adjectives. Nothing here can write anywhere: every probe is a read, and
# the secret rows are PRESENCE ONLY. A value never reaches this page, and no
# route on this server can be made to print one.

# What each executor kind needs before it may run live, mirrored from the
# executor modules themselves rather than restated here. Import is lazy and
# failure is reported, not raised — a missing executor is a row that says so.
EXECUTOR_ROW_ORDER: tuple[str, ...] = (
    "github_update",
    "pull_request_stub",
    "pr_review_suggestion",
    "linear_create",
    "linear_move",
    "slack_send",
    "email_send",
    "calendar_hold",
    "recap_page",
)

# Kinds that reach GitHub through the `gh` CLI rather than through a secret.
GH_CLI_KINDS: frozenset[str] = frozenset(
    {"github_update", "pull_request_stub", "pr_review_suggestion"}
)


def executor_required_secrets(kind: str) -> tuple[tuple[str, ...], str]:
    """(required secret names, "" or the reason the module could not be read)."""
    try:
        from . import executors

        module = executors.load_executor(kind)
    except KeyError:
        return (), "not registered in executors.REGISTRY"
    except Exception as error:  # noqa: BLE001 — an unimportable executor is a row, not a 500
        return (), f"could not import ({error})"
    return tuple(getattr(module, "REQUIRED_SECRETS", ()) or ()), ""


def github_cli_present() -> bool:
    """True when the `gh` binary is on PATH. Presence only — no network, no auth probe."""
    import shutil

    return bool(shutil.which("gh"))


def describe_executor(kind: str) -> dict:
    """One executor row: would it run live right now, and in one sentence, why.

    MIRRORS secrets_store.decide_mode() rather than calling it, because
    decide_mode prints its reasoning to the orchestrator's log and this page is
    read on a timer. The two must agree; a test asserts they do for every kind.
    """
    from . import secrets_store

    required, load_error = executor_required_secrets(kind)
    missing = secrets_store.missing_secret_names(required) if required else []
    registered = not load_error
    row = {
        "kind": kind,
        "label": KIND_LABELS.get(kind, kind.replace("_", " ").title()),
        "registered": registered,
        "requires": list(required),
        "missing": list(missing),
        "uses_gh_cli": kind in GH_CLI_KINDS,
    }
    if not registered:
        row["mode"] = results.MODE_SIM
        row["why"] = load_error
        return row
    if config.is_simulation_forced():
        if kind in config.forced_live_action_kinds():
            # NO SETTING NAMES IN COPY. This tab is read from 1.5m by someone who
            # does not run the app; "ADJOURN_LIVE_KINDS" told them nothing they
            # could act on and read like a leaked config file. The FACT is the
            # same either way: this run simulates, this kind is the exception.
            if missing:
                row["mode"] = results.MODE_SIM
                row["why"] = (
                    f"carved out to run live, but no secret for {', '.join(missing)}"
                )
            else:
                row["mode"] = results.MODE_LIVE
                row["why"] = "this run simulates, and this kind is carved out to run live"
        else:
            row["mode"] = results.MODE_SIM
            row["why"] = "this run simulates every transport"
        return row
    if missing:
        row["mode"] = results.MODE_SIM
        row["why"] = f"no secret on file for {', '.join(missing)}"
        return row
    row["mode"] = results.MODE_LIVE
    if kind in GH_CLI_KINDS:
        row["why"] = (
            "gh CLI on PATH, repo allowlisted" if github_cli_present()
            else "no secret required, but `gh` is not on PATH — the call will fail loudly"
        )
    elif not required:
        row["why"] = "no secret required — this one writes locally"
    else:
        row["why"] = f"{', '.join(required)} on file"
    return row


def describe_engine_reachability() -> dict:
    """Is MeetingScribe answering on loopback? One bounded GET, never raises."""
    base_url = config.meetingscribe_base_url()
    row = {"base_url": base_url, "reachable": False, "detail": ""}
    try:
        from . import meetingscribe_source

        row["reachable"] = bool(meetingscribe_source.is_engine_reachable())
        row["detail"] = "answering on loopback" if row["reachable"] else "no answer on loopback"
    except Exception as error:  # noqa: BLE001
        row["detail"] = f"could not probe ({error})"
    return row


# The cloud probe opens a socket to a remote host with an 8-second budget, which
# is not something a page load may block on during a demo. So it runs on a daemon
# thread and the page renders whatever the last probe said; a reload picks up the
# fresh answer. "not probed yet" is a legitimate first answer and says so.
_CLOUD_PROBE_LOCK = threading.Lock()
_CLOUD_PROBE: dict = {"state": "unprobed", "message": "not probed yet — reload in a moment", "at": 0.0}
CLOUD_PROBE_TTL_SECONDS = 45.0


def _refresh_cloud_probe() -> None:
    from . import cloud_mirror

    try:
        reachable, message = cloud_mirror.probe()
        state = "reachable" if reachable else "unreachable"
    except Exception as error:  # noqa: BLE001
        state, message = "unreachable", f"probe failed ({error})"
    with _CLOUD_PROBE_LOCK:
        _CLOUD_PROBE.update({"state": state, "message": message, "at": time.monotonic()})


def describe_cloud_mirror(*, wait_seconds: float = 1.0) -> dict:
    """Where the cloud mirror points and whether it answered. Non-secret fields only."""
    try:
        from . import cloud_mirror

        target = cloud_mirror.describe_target()
    except Exception as error:  # noqa: BLE001
        return {
            "configured": False,
            "state": "unavailable",
            "message": f"cloud_mirror unavailable ({error})",
        }
    if not target.get("configured"):
        return {
            **target,
            "state": "unconfigured",
            "headline": "no cloud mirror configured",
            "message": "no cloud mirror is configured — the web surface runs on demo data",
        }
    with _CLOUD_PROBE_LOCK:
        snapshot = dict(_CLOUD_PROBE)
        stale = (time.monotonic() - snapshot["at"]) > CLOUD_PROBE_TTL_SECONDS
    if snapshot["state"] == "unprobed" or stale:
        worker = threading.Thread(target=_refresh_cloud_probe, name="cloud-probe", daemon=True)
        worker.start()
        worker.join(timeout=max(0.0, wait_seconds))
        with _CLOUD_PROBE_LOCK:
            snapshot = dict(_CLOUD_PROBE)
    return {
        **target,
        "state": snapshot["state"],
        "message": snapshot["message"],
        # THE HEADLINE IS THE ANSWER; THE MESSAGE IS THE EVIDENCE. The probe's
        # own sentence carries the full remote endpoint, which on this page was
        # rendered at 20px and then again underneath it — sixty characters of
        # cloud hostname, twice, as the largest words in the row. The row is
        # answering one question. The probe's sentence stays, on the element's
        # title, for whoever wants it.
        "headline": CLOUD_STATE_HEADLINES.get(snapshot["state"], snapshot["state"]),
    }


#: One short answer per cloud-probe state. See describe_cloud_mirror.
CLOUD_STATE_HEADLINES: dict[str, str] = {
    "reachable": "answering",
    "unreachable": "not answering",
    "unprobed": "checking",
    "unavailable": "unavailable",
}


def describe_connections(*, cloud_wait_seconds: float = 1.0) -> dict:
    """The whole Connections tab. Read-only, boolean about secrets, honest about failure."""
    from . import secrets_store

    executors_rows = [describe_executor(kind) for kind in EXECUTOR_ROW_ORDER]
    live_kinds = sorted(config.forced_live_action_kinds())
    read_at = datetime.now(UTC)
    return {
        "generated_at": read_at.isoformat(timespec="seconds"),
        # The footer says when this page was read. It used to print the ISO
        # string straight out — a UTC machine timestamp under a board whose
        # every other clock is local, so at 05:40 in the room the page said
        # 12:40. Same clock as the receipts now.
        "generated_clock": format_clock_time(read_at.isoformat()),
        "mode": {
            "simulation_forced": config.is_simulation_forced(),
            "live_kinds": live_kinds,
            "regret_window_seconds": config.regret_window_seconds(),
            "headline": (
                "simulated transports"
                + (f" · carved out: {', '.join(live_kinds)}" if live_kinds else "")
                if config.is_simulation_forced()
                else "live transports"
            ),
        },
        "executors": executors_rows,
        "engine": describe_engine_reachability(),
        "memory": describe_memory_availability(),
        "cloud": describe_cloud_mirror(wait_seconds=cloud_wait_seconds),
        "secrets": secrets_store.describe_secret_availability(),
        "targets": {
            "github_repo": config.github_repo(),
            "github_allowlist": sorted(config.ALLOWED_GITHUB_REPOS),
            "linear_team": config.linear_team_key(),
            "graph": config.graph_name(),
            "journal": display_journal_path(journal_path()),
            "recaps": display_directory_label(config.recaps_directory()),
            "board_bind": f"{BOARD_HOST}:{config.board_port()}",
        },
        "gh_cli_present": github_cli_present(),
    }


# --- undo -------------------------------------------------------------------


def perform_undo(card_id: str) -> dict:
    """Cancel a draft or countdown, or reverse a fired action.

    Order matters: a waiting entry is CANCELLED (nothing was sent, so there is
    nothing to reverse), and only a fired action goes to the executor's undo.
    """
    card_id = (card_id or "").strip()
    if not card_id:
        return {"ok": False, "action": "refused", "message": "no card id"}

    if orchestrator.cancel_pending_action(
        card_id, path=pending_path(), journal_path=journal_path()
    ):
        return {"ok": True, "action": "cancelled", "message": "cancelled before it went out"}

    record = find_execution_record(card_id)
    if record is None:
        return {"ok": False, "action": "refused", "message": "no such action on the board"}

    dedup_key = (record.get("dedup_key") or "").strip()
    if dedup_key and dedup_key in results.read_undone_dedup_keys(path=journal_path()):
        return {"ok": True, "action": "undone", "message": "already undone"}

    # The board never dispatches an undo it told the viewer was impossible. This
    # is the same rule the card rendered, applied again at the boundary, so a
    # stale page or a hand-rolled POST cannot fire a transport the UI disowned.
    card = build_card(record, 0, set(), datetime.now(UTC))
    if not card["can_undo"]:
        return {"ok": False, "action": "refused", "message": card["undo_note"]}

    undo_ok, note, orchestrator_handled = undo_execution_record(record, dedup_key)
    if undo_ok and not orchestrator_handled:
        undo_ok, note = unwind_remaining_generations(record, dedup_key, note)
    # The orchestrator rewrites the recap itself when it owned the undo, so this
    # only covers the direct path (an overridden journal, or a build without the
    # hook). Calling it twice would write a second generation of the same page
    # and a second journal row for it.
    if undo_ok and not orchestrator_handled:
        rewrite_recap_after_undo(record)
    return {
        "ok": bool(undo_ok),
        "action": "undone" if undo_ok else "refused",
        "message": note,
    }


def perform_send(card_id: str, edits: dict | None = None) -> dict:
    """Send a Ready-to-send Slack or email draft. The board's other write."""
    card_id = (card_id or "").strip()
    if not card_id:
        return {"ok": False, "action": "refused", "message": "no card id"}
    result = orchestrator.send_held_action(
        card_id, edits=edits or {}, path=pending_path()
    )
    if result is None:
        return {
            "ok": False,
            "action": "refused",
            "message": "nothing waiting to send",
        }
    if result.ok:
        return {"ok": True, "action": "sent", "message": result.human_summary}
    return {"ok": False, "action": "failed", "message": result.human_summary}


def unwind_remaining_generations(record: dict, dedup_key: str, note: str) -> tuple[bool, str]:
    """Undo the older writes of an artifact that rewrites itself in place.

    THE RECAP USED TO SURVIVE "UNDO EVERYTHING". recap_page rewrites its file
    every time an action lands, and each write backs up the previous version, so
    a meeting with one countdown action has two or three generations in the
    journal. The board collapses them into ONE card; one click undid ONE
    generation, restoring the second-newest recap — and then reported
    can_undo=false, so there was no way to remove it at all. The page left on
    disk listed six actions that had all just been undone.

    So one click unwinds every generation, newest first. The oldest write is the
    one whose undo_payload says there was no previous version, and its undo
    deletes the file — which is the correct end state for a meeting that had no
    recap before Adjourn touched it.

    Only REWRITTEN_IN_PLACE_KINDS take this path. Every other kind writes once.
    """
    if record.get("kind") not in REWRITTEN_IN_PLACE_KINDS or not dedup_key:
        return True, note
    generations = [
        candidate
        for candidate in results.read_executions(path=journal_path())
        if (candidate.get("dedup_key") or "").strip() == dedup_key
        and candidate.get("record_type", results.RECORD_TYPE_EXECUTION)
        == results.RECORD_TYPE_EXECUTION
        and candidate.get("ok")
    ]
    # The newest is the one the caller has already undone (both this module and
    # orchestrator.undo_execution resolve a dedup_key to its LAST successful
    # record, which is why the older ones have to be undone by record and not by
    # key — asking again by key would just reverse the same write repeatedly).
    older = generations[:-1]
    unwound = 1
    for candidate in reversed(older):
        if not undo_one_record_directly(candidate, dedup_key):
            return True, f"{note} — {unwound} of {len(generations)} generations rolled back"
        unwound += 1
    if unwound > 1:
        return True, f"undone — {unwound} generations of the page rolled back"
    return True, note


def undo_one_record_directly(record: dict, dedup_key: str) -> bool:
    """Undo exactly THIS journal record, bypassing dedup_key resolution."""
    from . import executors

    result = results.ExecutorResult.from_record(record)
    undo_ok = executors.undo_result(result)
    results.append_undo(
        result,
        undo_ok,
        dedup_key,
        note="earlier generation rolled back from the board",
        path=journal_path(),
    )
    return bool(undo_ok)


def rewrite_recap_after_undo(record: dict) -> None:
    """Re-render this meeting's recap so the page agrees with the board.

    Undo used to go through a different path than firing, so the recap kept
    listing an undone action as LIVE, with a permalink that now 404s. The recap
    is regenerated from the journal, which already knows the action was undone,
    so re-running the writer is all it takes — the executor renders the undone
    rows struck through.

    Never raises and never blocks the undo's answer: a recap that failed to
    rewrite is a stale page, not a failed undo.
    """
    if record.get("kind") == "recap_page":
        return  # undoing the recap itself must not immediately write it back
    meeting_id = str(record.get("meeting_id") or "")
    if not meeting_id or not is_reading_default_journal():
        return
    try:
        orchestrator.refresh_recap_after_undo(meeting_id)
    except AttributeError:
        pass  # an orchestrator build without the hook yet
    except Exception as error:  # noqa: BLE001
        print(f"[board] could not rewrite the recap for {meeting_id!r}: {error}")


def find_execution_record(card_id: str) -> dict | None:
    """Locate a journal execution by dedup_key, or by the synthetic 'row-N' id.

    Takes the LAST record with the key, not the first: a fast-fire and a
    reconcile can both write under one key, and the orchestrator's own
    undo_execution() resolves to the latest one. The two must agree or the same
    button would reverse different things depending on which path handled it.
    """
    executions = [
        record
        for record in results.read_executions(path=journal_path())
        if record.get("record_type", results.RECORD_TYPE_EXECUTION)
        == results.RECORD_TYPE_EXECUTION
    ]
    if card_id:
        matches = [
            record
            for record in executions
            if (record.get("dedup_key") or "").strip() == card_id
        ]
        successful = [record for record in matches if record.get("ok")]
        if successful:
            return successful[-1]
        if matches:
            return matches[-1]
    if card_id.startswith("row-"):
        try:
            index = int(card_id[4:])
        except ValueError:
            return None
        if 0 <= index < len(executions):
            return executions[index]
    return None


def is_reading_default_journal() -> bool:
    """True when the board and the orchestrator are looking at the same file."""
    return journal_path() == config.executions_journal_path()


def undo_execution_record(record: dict, dedup_key: str) -> tuple[bool, str, bool]:
    """Ask the orchestrator to undo; fall back to the executor directly.

    Returns (ok, note, the_orchestrator_handled_it). That third value matters:
    orchestrator.undo_execution already unwinds every generation of a rewritten
    artifact, so when it answered, the board must NOT unwind them a second time
    — the second pass would ask each executor to undo a write that is already
    reversed and report a refusal for work that succeeded.

    The orchestrator owns undo when it and the board share a journal — its answer
    is authoritative and the board does not second-guess it, or a refusal would
    turn into a second dispatch of the same transport. The direct path below is
    for the cases where the orchestrator cannot help: an overridden journal
    (--demo, tests) or an orchestrator build without undo yet.
    """
    label = KIND_LABELS.get(record.get("kind", ""), record.get("kind", "the executor"))
    if dedup_key and is_reading_default_journal():
        try:
            undone = orchestrator.undo_execution(dedup_key)
        except NotImplementedError:
            undone = None  # not landed yet; use the direct path below
        except Exception as error:  # noqa: BLE001
            return False, f"undo failed: {error}", True
        if undone is not None:
            return (
                (True, "undone", True) if undone
                else (False, f"{label} refused the undo", True)
            )

    from . import executors

    result = results.ExecutorResult.from_record(record)
    undo_ok = executors.undo_result(result)
    results.append_undo(
        result,
        undo_ok,
        dedup_key,
        note="undone from the board",
        path=journal_path(),
    )
    if undo_ok:
        return True, "undone", False
    return False, f"{label} refused the undo", False


# --- application ------------------------------------------------------------

# The header the page sends its undo token back in. A cross-origin page can POST
# a form to this server, but it cannot read the board's HTML to learn the token
# and it cannot set a custom header on a simple form post — so requiring one is
# enough to stop a stray tab from reversing a live GitHub write mid-demo.
UNDO_TOKEN_HEADER = "X-Adjourn-Undo-Token"


def mint_undo_token() -> str:
    """A fresh per-run secret. Generated once per process at app construction."""
    return secrets_module.token_urlsafe(24)


def undo_request_is_authorized(app: Flask) -> tuple[bool, str]:
    """Does this POST carry the token this process minted at start?

    Header OR JSON body field, because the board's fetch() sends a header and a
    curl-able body form is genuinely useful during a rehearsal. The comparison is
    constant-time; a missing or wrong token is a 403 with a sentence, not a
    silent refusal — the whole product argues that refusals should be legible.
    """
    expected = app.config.get("ADJOURN_UNDO_TOKEN") or ""
    if not expected:
        return True, ""  # a build without a token (tests that opt out) stays open
    offered = request.headers.get(UNDO_TOKEN_HEADER, "")
    if not offered:
        body = request.get_json(silent=True) or {}
        offered = str(body.get("token") or "")
    if offered and secrets_module.compare_digest(offered, expected):
        return True, ""
    return False, (
        "this undo did not carry the board's per-run token — reload the board and "
        "click Undo on the card itself"
    )


def create_board_application(*, undo_token: str | None = None) -> Flask:
    """Build the Flask app. Factory form so tests can drive it without a server.

    `undo_token` defaults to a fresh per-run secret; pass "" to build an app with
    the check disabled (the existing test suite drives undo directly).
    """
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["ADJOURN_UNDO_TOKEN"] = (
        mint_undo_token() if undo_token is None else undo_token
    )
    # The macro definitions sit above the doctype in board.html; without these
    # the page would be served with a pile of blank lines before <!doctype>.
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    app.jinja_env.globals.update(
        quiet_header_line=QUIET_HEADER_LINE,
        empty_state_line=EMPTY_STATE_LINE,
        privacy_line=PRIVACY_LINE,
    )

    # ONE APP, FIVE TABS. meetings_ui owns Meetings and Live and carries its own
    # templates and static files under /meetings; this is the whole mount. It is
    # guarded because a board that cannot serve its own cards is a worse failure
    # than a board missing two tabs — if the Blueprint cannot be built, the other
    # three tabs still come up and the reason is printed once, loudly.
    try:
        from .meetings_ui import build_meetings_blueprint

        app.register_blueprint(build_meetings_blueprint())
        app.config["ADJOURN_MEETINGS_MOUNTED"] = True
    except Exception as error:  # noqa: BLE001
        app.config["ADJOURN_MEETINGS_MOUNTED"] = False
        app.config["ADJOURN_MEETINGS_MOUNT_ERROR"] = str(error)
        print(f"[board] the Meetings tab could not be mounted: {error}")
    # A card's quote links to /meetings/<id>. If that Blueprint is not there the
    # link would be a 404 on the demo's closing beat, so the cards drop it rather
    # than offer a door that opens onto nothing.
    global MEETINGS_TAB_MOUNTED
    MEETINGS_TAB_MOUNTED = bool(app.config.get("ADJOURN_MEETINGS_MOUNTED"))

    def render_page(template: str, view: str, **context):
        """Every page gets the same shell context, so the tab bar cannot drift."""
        return render_template(
            template,
            view=view,
            undo_token=app.config.get("ADJOURN_UNDO_TOKEN") or "",
            meetings_mounted=bool(app.config.get("ADJOURN_MEETINGS_MOUNTED")),
            **context,
        )

    @app.get("/")
    def render_board():
        state = load_board_state()
        return render_page(
            "board.html",
            "board",
            state=state,
            version=compute_version(state),
            ledger=None,
        )

    @app.get("/ledger")
    def render_ledger():
        state = load_board_state()
        return render_page(
            "board.html",
            "ledger",
            state=state,
            version=compute_version(state),
            ledger=load_ledger(),
        )

    @app.get("/connections")
    def render_connections():
        return render_page(
            "connections.html", "connections", connections=describe_connections()
        )

    @app.get("/meeting/<meeting_id>")
    def render_meeting(meeting_id: str):
        """One meeting's follow-through. The Meetings detail page links here."""
        return render_page(
            "meeting.html", "meeting", meeting=load_meeting_view(meeting_id)
        )

    @app.get("/recap/<meeting_id>")
    def serve_recap(meeting_id: str):
        """The recap page over HTTP, so the closing beat is a same-origin click.

        Serves ONLY out of the configured recaps directory, and only a file whose
        name is exactly <meeting_id>.html built from a sanitised id — no path
        component from the URL ever reaches the filesystem unfiltered.
        """
        safe_id = "".join(
            character for character in (meeting_id or "")
            if character.isalnum() or character in {"-", "_", "."}
        ).strip(".")
        if not safe_id or safe_id != (meeting_id or ""):
            abort(404)
        path = (config.recaps_directory() / f"{safe_id}.html").resolve()
        recaps_root = config.recaps_directory().resolve()
        if recaps_root not in path.parents or not path.is_file():
            abort(404)
        return Response(path.read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/api/board")
    def serve_board_state():
        state = load_board_state()
        state["version"] = compute_version(state)
        return jsonify(state)

    @app.get("/api/fragment")
    def serve_fragment():
        """The 1s poll. Returns pre-rendered HTML from the page's own macros."""
        state = load_board_state()
        version = compute_version(state)
        if request.args.get("version") == version:
            return jsonify({"version": version, "unchanged": True})

        render_header = get_template_attribute("board.html", "board_header")
        render_followthrough = get_template_attribute("board.html", "followthrough_column")
        render_guts = get_template_attribute("board.html", "guts_panel")
        return jsonify(
            {
                "version": version,
                "unchanged": False,
                "header_html": render_header(state),
                "pending_html": "",
                "cards_html": render_followthrough(
                    state.get("meeting_groups") or [], state.get("last_adjourned")
                ),
                "guts_html": render_guts(state.get("pipeline") or {}),
                "totals_line": state["totals_line"],
                "card_count": len(state["cards"]),
                "pending_count": len(state["pending"]),
            }
        )

    @app.get("/api/pipeline")
    def serve_pipeline_status():
        """Watcher / extraction / planner / executors — the guts panel as JSON."""
        state = load_board_state()
        return jsonify(state.get("pipeline") or {})

    @app.get("/api/ledger")
    def serve_ledger_state():
        return jsonify(load_ledger())

    @app.get("/api/connections")
    def serve_connections():
        return jsonify(describe_connections())

    @app.get("/api/meeting/<meeting_id>")
    def serve_meeting_view(meeting_id: str):
        return jsonify(load_meeting_view(meeting_id))

    @app.post("/undo/<path:card_id>")
    def undo_card(card_id: str):
        authorized, refusal = undo_request_is_authorized(app)
        if not authorized:
            return jsonify({"ok": False, "action": "refused", "message": refusal}), 403
        outcome = perform_undo(card_id)
        return jsonify(outcome), (200 if outcome["ok"] else 409)

    @app.post("/undo")
    def undo_card_from_body():
        authorized, refusal = undo_request_is_authorized(app)
        if not authorized:
            return jsonify({"ok": False, "action": "refused", "message": refusal}), 403
        payload = request.get_json(silent=True) or {}
        outcome = perform_undo(str(payload.get("dedup_key") or payload.get("card_id") or ""))
        return jsonify(outcome), (200 if outcome["ok"] else 409)

    @app.post("/send/<path:card_id>")
    def send_card(card_id: str):
        authorized, refusal = undo_request_is_authorized(app)
        if not authorized:
            return jsonify({"ok": False, "action": "refused", "message": refusal}), 403
        payload = request.get_json(silent=True) or {}
        edits = {
            "text": payload.get("text"),
            "subject": payload.get("subject"),
        }
        outcome = perform_send(card_id, edits)
        return jsonify(outcome), (200 if outcome["ok"] else 409)

    @app.get("/healthz")
    def report_health():
        return jsonify(
            {
                "ok": True,
                "journal": str(journal_path()),
                "journal_exists": journal_path().exists(),
                "pending": str(pending_path()),
                "pending_exists": pending_path().exists(),
                "pipeline": str(pipeline_path()),
                "pipeline_exists": pipeline_path().exists(),
                "simulation_forced": config.is_simulation_forced(),
                "meetings_mounted": bool(app.config.get("ADJOURN_MEETINGS_MOUNTED")),
                # PRESENCE, never the value: the token is what stands between a
                # stray tab and a live undo, so it does not appear in an endpoint
                # that exists to be curled.
                "undo_token_required": bool(app.config.get("ADJOURN_UNDO_TOKEN")),
            }
        )

    return app


# --- demo data --------------------------------------------------------------


def write_demo_data(directory: Path) -> tuple[Path, Path]:
    """Fabricate a journal + pending file covering every kind. For --demo and tests.

    This is scaffolding for looking at the board without a meeting; it writes only
    into `directory` and never near the real journal.

    NOTHING FABRICATED IS BADGED LIVE AGAINST A REMOTE SERVICE. The only "live"
    rows are the two local-first transports — the recap file and the calendar
    hold — because a fabricated row claiming a live GitHub comment would put a
    real external id on the card, and clicking Undo on it would send a real gh
    request to delete a comment that never existed.
    """
    from datetime import timedelta

    directory.mkdir(parents=True, exist_ok=True)
    journal = directory / "executions.jsonl"
    pending = directory / "pending.json"
    now = datetime.now(UTC)
    meeting_id = "20260821-093000"

    def moment(seconds_ago: int) -> str:
        return (now - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")

    rows = [
        {
            "kind": "recap_page",
            "ok": True,
            "mode": "live",
            "human_summary": "Wrote the recap page for Weekly Sync — 9 statements, 7 actions",
            "quote": "Let's wrap — same time next week.",
            "speaker": "Sharique",
            # Same origin, not file://. A file:// link clicked from an http://
            # page is blocked silently by stock Chrome and Safari, and this is
            # the closing beat of the demo. It is also the one place a local
            # absolute path used to reach the published tree — percent-encoded,
            # so the path rewrite in tools/publish_tree.py walked straight past it.
            "url": "/recap/weekly-sync",
            "external_id": "weekly-sync.html",
            "dedup_key": "recap_page:20260821-093000",
            "undo_payload": {"path": "recaps/weekly-sync.html", "backup": "recaps/weekly-sync.html.bak"},
            "fired_at": moment(94),
        },
        {
            "kind": "github_update",
            "ok": True,
            "mode": "sim",
            "human_summary": "Commented on issue #14 — scope changed from 'full rewrite' to 'cache layer only'",
            "quote": "We're not rewriting the whole thing, just the cache layer for now.",
            "speaker": "Sharique",
            "url": "https://github.com/sharique2004/adjourn/issues/14#issuecomment-demo",
            "external_id": "demo-comment-14",
            "dedup_key": "github_update:14:cache-layer-scope",
            "undo_payload": {"repo": "sharique2004/adjourn", "comment_id": "demo-comment-14"},
            "fired_at": moment(88),
        },
        {
            "kind": "linear_create",
            "ok": True,
            "mode": "sim",
            "human_summary": "Created ADJ-118 'Benchmark the cache layer before the demo'",
            "quote": "Someone should benchmark it before we show it to anyone.",
            "speaker": "Priya",
            "url": "https://linear.app/adjourn/issue/ADJ-118",
            "external_id": "ADJ-118",
            "dedup_key": "linear_create:benchmark-the-cache-layer",
            "undo_payload": {"issue_id": "sim-adj-118"},
            "fired_at": moment(71),
        },
        {
            "kind": "linear_move",
            "ok": True,
            "mode": "sim",
            "human_summary": "Moved ADJ-102 to In Review — Priya said it is at 90%",
            "quote": "The importer is basically done, ninety percent, just needs review.",
            "speaker": "Priya",
            "url": "https://linear.app/adjourn/issue/ADJ-102",
            "external_id": "ADJ-102",
            "dedup_key": "linear_move:adj-102:in-review",
            "undo_payload": {"issue_id": "sim-adj-102", "previous_state_id": "sim-state-in-progress"},
            "fired_at": moment(63),
        },
        {
            "kind": "pull_request_stub",
            "ok": True,
            "mode": "sim",
            "human_summary": "Opened draft PR 'Cache layer spike' against adjourn",
            "quote": "I'll put up a draft PR tonight so people can see the shape of it.",
            "speaker": "Sharique",
            "url": "https://github.com/sharique2004/adjourn/pull/9",
            "external_id": "9",
            "dedup_key": "pull_request_stub:cache-layer-spike",
            "undo_payload": {"repo": "sharique2004/adjourn", "number": 9, "branch": "adjourn/cache-layer-spike"},
            "fired_at": moment(52),
        },
        {
            "kind": "pr_review_suggestion",
            "ok": True,
            "mode": "sim",
            "human_summary": "Suggested change on PR #9: join button green #2ecc71 → #6b7f99",
            "quote": "That green is wrong — it should be our slate blue.",
            "speaker": "Sharique",
            "url": "https://github.com/sharique2004/adjourn/pull/9#pullrequestreview-demo",
            "external_id": "demo-review-9",
            "dedup_key": "pr_review_suggestion:9:join-button",
            "undo_payload": {
                "simulated": True,
                "payload": {"repo": "sharique2004/adjourn", "pr_number": 9, "review_id": "demo"},
            },
            "fired_at": moment(58),
        },
        {
            "kind": "calendar_hold",
            "ok": True,
            "mode": "live",
            "human_summary": "Held Thursday 14:00–15:00 for the benchmark review",
            "quote": "Let's look at the numbers Thursday afternoon.",
            "speaker": "Dana",
            "url": "",
            "external_id": "adjourn-hold-thursday",
            "dedup_key": "calendar_hold:benchmark-review:thursday",
            "undo_payload": {"ics": "state/holds/adjourn-hold-thursday.ics"},
            "fired_at": moment(40),
        },
        {
            "kind": "slack_send",
            "ok": True,
            "mode": "sim",
            "human_summary": "Posted the scope change to #eng-updates",
            "quote": "I'll drop a note in eng-updates so nobody starts the rewrite.",
            "speaker": "Sharique",
            "url": "https://slack.com/archives/C0DEMO/p1755000000",
            "external_id": "1755000000.000100",
            "dedup_key": "slack_send:eng-updates:scope-change",
            "undo_payload": {"channel": "C0DEMO", "ts": "1755000000.000100"},
            "fired_at": moment(22),
        },
        {
            "kind": "email_send",
            "ok": False,
            "mode": "sim",
            "human_summary": "Could not send the follow-up to dana@example.com — no app password in the keychain",
            "quote": "I'll email Dana the summary tonight.",
            "speaker": "Sharique",
            "url": "",
            "external_id": None,
            "dedup_key": "email_send:dana-example-com:summary",
            "undo_payload": {},
            "fired_at": moment(12),
        },
    ]

    with journal.open("w", encoding="utf-8") as handle:
        for row in rows:
            record = {
                "record_type": results.RECORD_TYPE_EXECUTION,
                "dedup_key": row.pop("dedup_key"),
                "written_at": row["fired_at"],
                "meeting_id": meeting_id,
                **row,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    countdown = orchestrator.PendingAction(
        dedup_key="email_send:priya-example-com:benchmark-numbers",
        kind="email_send",
        fire_at="",
        regret_window_s=0,
        hold_for_send=True,
        human_preview="Email Priya: Benchmark numbers before Thursday",
        quote="I'll send Priya the benchmark numbers before Thursday.",
        speaker="Sharique",
        meeting_id=meeting_id,
        payload={
            "to": ["priya@example.com"],
            "person": "Priya",
            "subject": "Benchmark numbers before Thursday",
            "body_text": "Benchmark numbers before Thursday.",
            "human_preview": "Email Priya: Benchmark numbers before Thursday",
        },
    )
    second = orchestrator.PendingAction(
        dedup_key="slack_send:dana:thursday-review",
        kind="slack_send",
        fire_at="",
        regret_window_s=0,
        hold_for_send=True,
        human_preview="Slack: Thursday 14:00 works for the review",
        quote="Tell Dana Thursday at two works.",
        speaker="Priya",
        meeting_id=meeting_id,
        payload={
            "text": "Thursday 14:00 works for the review.",
            "human_preview": "Slack: Thursday 14:00 works for the review",
        },
    )
    orchestrator.write_pending_actions(
        [countdown, second],
        meeting_id=meeting_id,
        meeting_title="Weekly Sync",
        path=pending,
    )
    return journal, pending


# --- entry point ------------------------------------------------------------


def resolve_board_port(explicit: int | None = None) -> int:
    """--port -> ADJOURN_BOARD_PORT -> the lane contract's 5117."""
    if explicit:
        return explicit
    override = os.environ.get("ADJOURN_BOARD_PORT", "").strip()
    if override.isdigit():
        return int(override)
    return LANE_BOARD_PORT


def main() -> None:
    parser = argparse.ArgumentParser(description="Adjourn follow-through board")
    parser.add_argument("--port", type=int, default=None, help=f"default {LANE_BOARD_PORT}")
    # NO --host. The board's undo button reverses live writes to GitHub, Linear
    # and Slack, so the socket is loopback by construction and not by default:
    # there is no flag to widen it by muscle memory at 2am, and the bind address
    # below is the constant, not a parsed argument.
    parser.add_argument(
        "--demo",
        action="store_true",
        help="serve fabricated data covering every action kind (writes to state/demo_board/)",
    )
    arguments = parser.parse_args()

    if arguments.demo:
        journal, pending = write_demo_data(config.state_directory() / "demo_board")
        os.environ["ADJOURN_BOARD_JOURNAL"] = str(journal)
        os.environ["ADJOURN_BOARD_PENDING"] = str(pending)
        print(f"[board] demo data: {journal}")

    port = resolve_board_port(arguments.port)
    application = create_board_application()
    print(f"[board] http://{BOARD_HOST}:{port}  (config.board_port() says {config.board_port()})")
    print(f"[board] journal: {journal_path()}")
    print(f"[board] pending: {pending_path()}")
    print(f"[board] tabs: /meetings/ · /meetings/live · / · /ledger · /connections")
    # The presentation filter's detail — how many meetings are listed, and the
    # variable that switches it off — is the OPERATOR's, and it used to be
    # printed on the Meetings tab where a judge read it. It belongs here.
    from . import meetings_ui

    if meetings_ui.presentation_mode_enabled():
        print(meetings_ui.presentation_operator_note())
    print("[board] undo requires this run's token; it is in the page, not in the log")
    application.run(host=BOARD_HOST, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
