"""Orchestrator — the two-phase run loop, and the owner of state/pending.json.

    watcher edge -> read transcript -> extract (model) -> plan (table) -> execute
                 -> journal -> board

TWO PHASES, deliberately:

  FAST FIRE (on the stop edge, ~1-2s after the meeting ends)
      Read the /api/live snapshot, extract with source="live", plan, and fire.
      This is what the founders see: the meeting ends and the board starts
      filling in before anyone has switched windows.

  RECONCILE (when jobs[<id>].state == "done")
      Re-read the real transcript from meeting.json, re-extract with
      source="final", re-plan, then fire ONLY the actions whose dedup_key is not
      already in the executions journal or in memory. Existing recap quotes get
      upgraded to the better final-transcript wording in place.

Dedup is by planner.build_dedup_key() — never by segment_id, because the live
pass and the final pass number their segments differently.

REGRET WINDOW: actions with regret_window_s > 0 are not sent immediately. They
are written to state/pending.json with a `fire_at` timestamp, the board draws a
countdown ring, and a human can cancel. This module owns that file; the board
only reads it (and POSTs cancellations back here).

Run:
    python -m adjourn.orchestrator --watch
    python -m adjourn.orchestrator --replay
        # canned fixture through the real extract → plan → execute path;
        # MeetingScribe is not required. Pass a fixture name, .jsonl, or
        # meeting id to override; with no target this is agi-living-room.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config, executors, extraction, meetingscribe_source, planner, results, watcher
from .planner import Action

PENDING_STATUS_WAITING = "waiting"
PENDING_STATUS_CANCELLED = "cancelled"
PENDING_STATUS_FIRED = "fired"

# The recap is a PAGE, not an event. Re-running it on the reconcile pass is the
# whole point: it rewrites in place with the better final-transcript quotes.
# Every other kind fires at most once per dedup_key.
ALWAYS_REFIRE_KINDS: frozenset[str] = frozenset({"recap_page"})

# Fire order within one plan. The recap goes last so it can cite everything the
# other executors just did; everything else keeps planner order.
LAST_ACTION_KINDS: tuple[str, ...] = ("recap_page",)

DEFAULT_REPLAY_TARGET = "agi-living-room"

WATCHER_WAITING = "waiting"
WATCHER_MEETING_STOPPED = "meeting_stopped"
WATCHER_TRANSCRIPT_READY = "transcript_ready"

STAGE_IDLE = "idle"
STAGE_RUNNING = "running"
STAGE_DONE = "done"


# --- pipeline status (IMPLEMENTED — the board's guts-panel read contract) ----
#
# The board is read-mostly: it already watches pending.json and the journal.
# Extraction and planner phases exist only while a run is in flight, so they
# have to be written somewhere the 1s poll can see. This file is that somewhere.
# Executors are NOT duplicated here — pending.json + the journal are the truth
# for countdown / live / sim / cancelled, and the board composes those itself.


@dataclass
class WatcherStatus:
    """One line the guts panel can show for the watcher.

    phase — waiting | meeting_stopped | transcript_ready. Replay uses
            transcript_ready (the canned transcript is already on disk).
    """

    phase: str = WATCHER_WAITING
    meeting_id: str = ""
    detail: str = "waiting for a meeting to end"

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "meeting_id": self.meeting_id,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> WatcherStatus:
        data = data if isinstance(data, dict) else {}
        return cls(
            phase=str(data.get("phase") or WATCHER_WAITING),
            meeting_id=str(data.get("meeting_id") or ""),
            detail=str(data.get("detail") or "waiting for a meeting to end"),
        )


@dataclass
class ExtractionStatus:
    """Idle, running, or done with a statement count and kinds.

    segment_count and silent_count exist because the runbook's best line —
    "eighteen of twenty-eight lines produced nothing at all" — was a number on
    no screen anywhere. The board read "10 decided · 2 ignored", which a founder
    looking at the screen reads as the system having acted on 26 of 28 lines.
    The restraint is the product; the count of what produced nothing belongs on
    the same panel as the count of what did.
    """

    phase: str = STAGE_IDLE
    statement_count: int = 0
    segment_count: int = 0
    silent_count: int = 0
    kinds: dict = field(default_factory=dict)
    source: str = ""
    detail: str = "idle"
    # batch_index/batch_total are what make the rail MOVE during the ~25s the
    # model is working. They are written from inside the extraction loop, one
    # report per batch boundary — not at the end, where they would only ever
    # render the final value and the bar would jump from empty to full. The
    # board renders "batch 2/4" and a progress bar whenever batch_total > 0, and
    # renders no bar at all when it is 0, so an un-batched pass loses nothing.
    batch_index: int = 0  # 1-based, the batch in flight (or the last one done)
    batch_total: int = 0  # batches this pass will run; 0 == not batching

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "statement_count": self.statement_count,
            "segment_count": self.segment_count,
            "silent_count": self.silent_count,
            "kinds": dict(self.kinds),
            "source": self.source,
            "detail": self.detail,
            "batch_index": self.batch_index,
            "batch_total": self.batch_total,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> ExtractionStatus:
        data = data if isinstance(data, dict) else {}
        kinds = data.get("kinds") or {}
        return cls(
            phase=str(data.get("phase") or STAGE_IDLE),
            statement_count=int(data.get("statement_count") or 0),
            segment_count=int(data.get("segment_count") or 0),
            silent_count=int(data.get("silent_count") or 0),
            kinds=dict(kinds) if isinstance(kinds, dict) else {},
            source=str(data.get("source") or ""),
            detail=str(data.get("detail") or STAGE_IDLE),
            batch_index=int(data.get("batch_index") or 0),
            batch_total=int(data.get("batch_total") or 0),
        )


@dataclass
class PlannerStatus:
    """What the table decided, and what it declined.

    action_count is the non-recap actions the table emitted — recap_page is
    forced by the orchestrator so it does not count as a table decision.
    ignored_count is statements that produced no such action (inert kinds,
    failed guards).
    """

    phase: str = STAGE_IDLE
    action_count: int = 0
    ignored_count: int = 0
    action_kinds: dict = field(default_factory=dict)
    ignored_kinds: dict = field(default_factory=dict)
    detail: str = "idle"

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "action_count": self.action_count,
            "ignored_count": self.ignored_count,
            "action_kinds": dict(self.action_kinds),
            "ignored_kinds": dict(self.ignored_kinds),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> PlannerStatus:
        data = data if isinstance(data, dict) else {}
        action_kinds = data.get("action_kinds") or {}
        ignored_kinds = data.get("ignored_kinds") or {}
        return cls(
            phase=str(data.get("phase") or STAGE_IDLE),
            action_count=int(data.get("action_count") or 0),
            ignored_count=int(data.get("ignored_count") or 0),
            action_kinds=dict(action_kinds) if isinstance(action_kinds, dict) else {},
            ignored_kinds=dict(ignored_kinds) if isinstance(ignored_kinds, dict) else {},
            detail=str(data.get("detail") or STAGE_IDLE),
        )


@dataclass
class PipelineStatus:
    """adjourn/state/pipeline.json — live phases for the board guts panel.

    The board reads this. The orchestrator writes it. Nothing else.
    """

    updated_at: str = ""
    mode: str = "idle"  # idle | watch | replay
    pass_name: str = ""  # fast | final | replay
    meeting_id: str = ""
    meeting_title: str = ""
    watcher: WatcherStatus = field(default_factory=WatcherStatus)
    extraction: ExtractionStatus = field(default_factory=ExtractionStatus)
    planner: PlannerStatus = field(default_factory=PlannerStatus)
    # The narrated rail: one row per statement, per decision, per fire — which is
    # what makes the panel read as thinking rather than as a progress bar. Newest
    # LAST (the board scrolls to the bottom); at most FEED_LIMIT rows so a 1s poll
    # stays free; `seq` restarts at 1 each run and the board treats a lower seq as
    # a new run and clears. See FEED_LIMIT / report_pipeline_event below.
    feed: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "updated_at": self.updated_at,
            "mode": self.mode,
            "pass_name": self.pass_name,
            "meeting_id": self.meeting_id,
            "meeting_title": self.meeting_title,
            "watcher": self.watcher.to_dict(),
            "extraction": self.extraction.to_dict(),
            "planner": self.planner.to_dict(),
            "feed": [dict(row) for row in self.feed if isinstance(row, dict)],
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> PipelineStatus:
        data = data if isinstance(data, dict) else {}
        raw_feed = data.get("feed")
        feed = [row for row in raw_feed if isinstance(row, dict)] if isinstance(raw_feed, list) else []
        return cls(
            updated_at=str(data.get("updated_at") or ""),
            mode=str(data.get("mode") or "idle"),
            pass_name=str(data.get("pass_name") or ""),
            meeting_id=str(data.get("meeting_id") or ""),
            meeting_title=str(data.get("meeting_title") or ""),
            watcher=WatcherStatus.from_dict(data.get("watcher")),
            extraction=ExtractionStatus.from_dict(data.get("extraction")),
            planner=PlannerStatus.from_dict(data.get("planner")),
            feed=feed,
        )


def pipeline_status_path() -> Path:
    """Where the guts panel reads live phases from. Overridable for tests."""
    return config.pipeline_status_path()


def read_pipeline_status(path: Path | None = None) -> PipelineStatus:
    """Current pipeline.json, or an idle document. Never raises."""
    target = path or pipeline_status_path()
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PipelineStatus()
    if not isinstance(document, dict):
        return PipelineStatus()
    return PipelineStatus.from_dict(document)


def write_pipeline_status(status: PipelineStatus, path: Path | None = None) -> None:
    """Replace pipeline.json atomically (write temp + os.replace)."""
    target = path or pipeline_status_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    status.updated_at = results.utc_timestamp()
    temporary = target.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(status.to_dict(), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except OSError as error:
        print(f"[orchestrator] could not write pipeline status: {error}")
        try:
            temporary.unlink()
        except OSError:
            pass


def report_pipeline(
    *,
    mode: str | None = None,
    pass_name: str | None = None,
    meeting_id: str | None = None,
    meeting_title: str | None = None,
    watcher: WatcherStatus | dict | None = None,
    extraction: ExtractionStatus | dict | None = None,
    planner: PlannerStatus | dict | None = None,
    feed_reset: bool = False,
    path: Path | None = None,
) -> PipelineStatus:
    """Merge a stage update into pipeline.json. Never raises.

    Called at each real transition (watch start, meeting stopped, extraction
    running/done, planner done, replay start). Not on every watcher tick — a
    ticking clock must not churn the board fragment.

    `feed_reset=True` empties the narrated feed so `seq` restarts at 1. Call it
    exactly where a RUN begins — replay start, and the watcher's transcript_ready
    -> extraction handoff — because without it the rail opens the next meeting
    still showing the previous meeting's thinking.
    """
    status = read_pipeline_status(path)
    if feed_reset:
        status.feed = []
    if mode is not None:
        status.mode = mode
    if pass_name is not None:
        status.pass_name = pass_name
    if meeting_id is not None:
        status.meeting_id = meeting_id
    if meeting_title is not None:
        status.meeting_title = meeting_title
    if watcher is not None:
        status.watcher = (
            watcher if isinstance(watcher, WatcherStatus) else WatcherStatus.from_dict(watcher)
        )
    if extraction is not None:
        status.extraction = (
            extraction
            if isinstance(extraction, ExtractionStatus)
            else ExtractionStatus.from_dict(extraction)
        )
    if planner is not None:
        status.planner = (
            planner if isinstance(planner, PlannerStatus) else PlannerStatus.from_dict(planner)
        )
    try:
        write_pipeline_status(status, path)
    except Exception as error:  # noqa: BLE001 — a status file must never kill a run
        print(f"[orchestrator] pipeline status update failed: {error}")
    return status


# --- the narrated feed ------------------------------------------------------

FEED_LIMIT = 80  # rows kept in pipeline.json; the file is read once a second
FEED_TEXT_LIMIT = 140  # characters; this file is read by a page on a projector
FEED_LABEL_LIMIT = 24  # characters; the chip is one short word
FEED_STAGES: frozenset[str] = frozenset({"watcher", "extraction", "planner", "executor"})
FEED_TONES: frozenset[str] = frozenset({"act", "hold", "ignore", "decline", "note"})


def _trim_feed_text(value: str, limit: int) -> str:
    """One line, at most `limit` chars, ellipsis when it had to be cut. Pure."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def report_pipeline_event(
    *,
    stage: str,
    tone: str = "note",
    label: str = "",
    text: str = "",
    path: Path | None = None,
) -> None:
    """Append one line to pipeline.json's feed. Never raises.

    The writer owns the shape entirely — the board only truncates for display —
    so trimming happens HERE: `text` is capped at 140 characters and the list at
    80 rows, because the alternative is a status file that grows without bound
    and a 1s poll that stops being free.

    `seq` is monotonic within a run and is what the board de-duplicates and
    animates on. It is derived from the highest seq already in the file rather
    than from a counter in memory, so a fresh process joining mid-run (the
    watcher and a replay do not share one) cannot rewind the rail.
    """
    try:
        stage_name = str(stage or "").strip().lower()
        if stage_name not in FEED_STAGES:
            return
        tone_name = str(tone or "note").strip().lower()
        if tone_name not in FEED_TONES:
            tone_name = "note"
        status = read_pipeline_status(path)
        highest = 0
        for row in status.feed:
            try:
                highest = max(highest, int(row.get("seq") or 0))
            except (TypeError, ValueError):
                continue
        status.feed = [*status.feed, {
            "seq": highest + 1,
            "at": results.utc_timestamp(),
            "stage": stage_name,
            "tone": tone_name,
            "label": _trim_feed_text(label, FEED_LABEL_LIMIT).lower(),
            "text": _trim_feed_text(text, FEED_TEXT_LIMIT),
        }][-FEED_LIMIT:]
        write_pipeline_status(status, path)
    except Exception as error:  # noqa: BLE001 — a status file must never kill a run
        print(f"[orchestrator] pipeline feed event failed: {error}")


def feed_belongs_to_a_new_run(meeting_id: str, path: Path | None = None) -> bool:
    """True when the rail is still showing a DIFFERENT meeting's thinking.

    The reset is keyed on the meeting rather than on the pass because one
    meeting is narrated twice — the fast pass at the stop edge and the final
    reconcile when the transcript lands. Resetting per pass would blank the rail
    the room just watched fill; resetting per meeting is what the rule
    ("the rail must not open the next meeting on the last one's rows") actually
    means.
    """
    try:
        current = read_pipeline_status(path).meeting_id
    except Exception:  # noqa: BLE001
        return True
    return bool(meeting_id) and current != meeting_id


def count_kinds(items) -> dict[str, int]:
    """{kind: n} from statements or actions. Empty items -> empty dict."""
    counts: dict[str, int] = {}
    for item in items or []:
        kind = str(getattr(item, "kind", "") or "")
        if not kind:
            continue
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def format_kind_counts(counts: dict[str, int]) -> str:
    """'decision ×3, question ×1' — stable-enough for a 1s poll to read."""
    if not counts:
        return ""
    return ", ".join(
        f"{kind} ×{count}" if count != 1 else kind
        for kind, count in counts.items()
    )


def extraction_status_running(source: str) -> ExtractionStatus:
    label = {
        "live": "extracting live captions…",
        "final": "extracting final transcript…",
        "fixture": "extracting fixture transcript…",
    }.get(source, "extracting…")
    return ExtractionStatus(phase=STAGE_RUNNING, source=source, detail=label)


def count_silent_segments(statements: list, segment_count: int) -> int:
    """Lines of the transcript that produced no statement at all.

    A statement's segment_id may be suffixed ("agi-s24.2") when the extractor
    split one sentence into two claims, so the LINE is the part before the dot —
    two claims off one sentence silence one line, not minus one.
    """
    if not segment_count:
        return 0
    spoke = {
        str(getattr(statement, "segment_id", "") or "").split(".", 1)[0]
        for statement in statements or []
    }
    spoke.discard("")
    return max(0, int(segment_count) - len(spoke))


def extraction_status_from_statements(
    statements: list, *, source: str, segment_count: int = 0
) -> ExtractionStatus:
    kinds = count_kinds(statements)
    n = len(statements or [])
    silent = count_silent_segments(statements, segment_count)
    kind_line = format_kind_counts(kinds)
    parts: list[str] = []
    if segment_count:
        parts.append(f"{segment_count} segment{'s' if segment_count != 1 else ''}")
    parts.append(
        f"{n} statement{'s' if n != 1 else ''}" + (f" ({kind_line})" if kind_line else "")
    )
    if segment_count:
        parts.append(f"{silent} produced nothing")
    return ExtractionStatus(
        phase=STAGE_DONE,
        statement_count=n,
        segment_count=int(segment_count or 0),
        silent_count=silent,
        kinds=kinds,
        source=source,
        detail=" · ".join(parts),
    )


def narrate_plan(statements: list, actions: list[Action]) -> None:
    """Emit one rail row per routed statement, per ignored line, per refusal.

    The restraint rows are the point. A rail that only shows what fired reads as
    a progress bar; a rail that shows "chatter → ignored" and "declined: negated"
    next to "ticket_request → linear_create" reads as judgement, which is the
    thing the room is actually being asked to believe. Never raises.
    """
    try:
        acted_bases: dict[str, list[str]] = {}
        for action in actions or []:
            if action.kind == "recap_page":
                continue
            base = str(action.segment_id or "").split(".", 1)[0]
            if base:
                acted_bases.setdefault(base, []).append(action.kind)
        for statement in statements or []:
            base = str(getattr(statement, "segment_id", "") or "").split(".", 1)[0]
            kind = str(getattr(statement, "kind", "") or "statement")
            fired = acted_bases.get(base) or []
            if fired:
                report_pipeline_event(
                    stage="planner", tone="act", label=kind,
                    text=f"{kind} → {', '.join(dict.fromkeys(fired))}",
                )
                continue
            reason = str(getattr(statement, "restraint_reason", "") or "")
            if getattr(statement, "fires_no_work", False):
                report_pipeline_event(
                    stage="planner", tone="decline", label=kind,
                    text=f"declined: {reason}" if reason else f"{kind} → declined",
                )
                continue
            report_pipeline_event(
                stage="planner", tone="ignore", label=kind, text=f"{kind} → ignored",
            )
    except Exception as error:  # noqa: BLE001 — narration is never worth a run
        print(f"[orchestrator] could not narrate the plan: {error}")


def planner_status_from_plan(statements: list, actions: list[Action]) -> PlannerStatus:
    """Table decided N actions, ignored the rest. Recap does not count as a decision."""
    action_kinds: dict[str, int] = {}
    acted_bases: set[str] = set()
    for action in actions or []:
        if action.kind == "recap_page":
            continue
        action_kinds[action.kind] = action_kinds.get(action.kind, 0) + 1
        base = str(action.segment_id or "").split(".", 1)[0]
        if base:
            acted_bases.add(base)
    ignored_kinds: dict[str, int] = {}
    ignored = 0
    for statement in statements or []:
        base = str(getattr(statement, "segment_id", "") or "").split(".", 1)[0]
        kind = str(getattr(statement, "kind", "") or "unknown")
        if base and base in acted_bases:
            continue
        ignored += 1
        ignored_kinds[kind] = ignored_kinds.get(kind, 0) + 1
    decided = sum(action_kinds.values())
    decided_line = format_kind_counts(action_kinds)
    ignored_line = format_kind_counts(ignored_kinds)
    parts = [
        f"table decided {decided} action{'s' if decided != 1 else ''}"
        + (f" ({decided_line})" if decided_line else "")
    ]
    if ignored:
        parts.append(
            f"ignored {ignored}" + (f" ({ignored_line})" if ignored_line else "")
        )
    else:
        parts.append("ignored none")
    return PlannerStatus(
        phase=STAGE_DONE,
        action_count=decided,
        ignored_count=ignored,
        action_kinds=action_kinds,
        ignored_kinds=ignored_kinds,
        detail=" · ".join(parts),
    )


def watcher_status_waiting() -> WatcherStatus:
    return WatcherStatus(
        phase=WATCHER_WAITING, meeting_id="", detail="waiting for a meeting to end"
    )


def watcher_status_stopped(meeting_id: str) -> WatcherStatus:
    return WatcherStatus(
        phase=WATCHER_MEETING_STOPPED,
        meeting_id=meeting_id,
        detail=f"meeting stopped ({meeting_id})" if meeting_id else "meeting stopped",
    )


def watcher_status_transcript_ready(meeting_id: str, *, replay: bool = False) -> WatcherStatus:
    if replay:
        detail = (
            f"replay · transcript ready ({meeting_id})"
            if meeting_id
            else "replay · transcript ready"
        )
    else:
        detail = f"transcript ready ({meeting_id})" if meeting_id else "transcript ready"
    return WatcherStatus(
        phase=WATCHER_TRANSCRIPT_READY,
        meeting_id=meeting_id,
        detail=detail,
    )



# --- the pending-actions file (IMPLEMENTED — the board's read contract) ------


@dataclass
class PendingAction:
    """An action inside its regret window, waiting out a visible countdown.

    dedup_key       — same key the journal will carry, so the board can match the
                      countdown card to the fired card.
    fire_at         — ISO timestamp when this goes out unless cancelled.
    human_preview   — one line of what is about to be sent, shown on the card.
    status          — "waiting" | "cancelled" | "fired".
    """

    dedup_key: str
    kind: str
    fire_at: str
    created_at: str = field(default_factory=results.utc_timestamp)
    regret_window_s: int = 0
    human_preview: str = ""
    quote: str = ""
    speaker: str = ""
    meeting_id: str = ""
    payload: dict = field(default_factory=dict)
    status: str = PENDING_STATUS_WAITING

    @property
    def is_waiting(self) -> bool:
        return self.status == PENDING_STATUS_WAITING

    def seconds_remaining(self, now: datetime | None = None) -> float:
        """Seconds until fire_at, floored at 0. Drives the board's countdown ring."""
        now = now or datetime.now(UTC)
        try:
            deadline = datetime.fromisoformat(self.fire_at)
        except ValueError:
            return 0.0
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return max(0.0, (deadline - now).total_seconds())

    def to_dict(self) -> dict:
        return {
            "dedup_key": self.dedup_key,
            "kind": self.kind,
            "fire_at": self.fire_at,
            "created_at": self.created_at,
            "regret_window_s": self.regret_window_s,
            "human_preview": self.human_preview,
            "quote": self.quote,
            "speaker": self.speaker,
            "meeting_id": self.meeting_id,
            "payload": self.payload,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PendingAction:
        return cls(
            dedup_key=data.get("dedup_key", ""),
            kind=data.get("kind", ""),
            fire_at=data.get("fire_at", ""),
            created_at=data.get("created_at", ""),
            regret_window_s=int(data.get("regret_window_s", 0)),
            human_preview=data.get("human_preview", ""),
            quote=data.get("quote", ""),
            speaker=data.get("speaker", ""),
            meeting_id=data.get("meeting_id", ""),
            payload=data.get("payload") or {},
            status=data.get("status", PENDING_STATUS_WAITING),
        )

    @classmethod
    def from_action(cls, action: Action, now: datetime | None = None) -> PendingAction:
        """Build a countdown entry from a planned action."""
        now = now or datetime.now(UTC)
        fire_at = now + timedelta(seconds=action.regret_window_s)
        return cls(
            dedup_key=action.dedup_key,
            kind=action.kind,
            fire_at=fire_at.isoformat(timespec="seconds"),
            regret_window_s=action.regret_window_s,
            human_preview=str(action.payload.get("human_preview", "")),
            quote=action.quote,
            speaker=action.speaker,
            meeting_id=action.meeting_id,
            payload=action.payload,
        )


def read_pending_header(path: Path | None = None) -> dict:
    """The meeting_id/meeting_title header of state/pending.json, or empty strings.

    Read back before every rewrite so a cancel or a fire does not blank the
    meeting name out of the board's countdown column — the items were always
    preserved, but the header is what the board titles that column with.
    """
    pending_path = path or config.pending_actions_path()
    try:
        document = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"meeting_id": "", "meeting_title": ""}
    if not isinstance(document, dict):
        return {"meeting_id": "", "meeting_title": ""}
    return {
        "meeting_id": str(document.get("meeting_id", "") or ""),
        "meeting_title": str(document.get("meeting_title", "") or ""),
    }


def read_pending_actions(path: Path | None = None) -> list[PendingAction]:
    """Everything currently in state/pending.json. THE BOARD'S READ API.

    A missing or malformed file reads as empty — the board must render an empty
    countdown column rather than an error page.
    """
    pending_path = path or config.pending_actions_path()
    try:
        document = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = document.get("items") if isinstance(document, dict) else document
    if not isinstance(items, list):
        return []
    return [PendingAction.from_dict(item) for item in items if isinstance(item, dict)]


def write_pending_actions(
    items: list[PendingAction],
    *,
    meeting_id: str = "",
    meeting_title: str = "",
    path: Path | None = None,
) -> None:
    """Replace state/pending.json atomically (write temp + os.replace).

    Atomic because the board polls this file continuously: a reader must see the
    old document or the new one, never a half-written one. Same discipline
    MeetingScribe uses for meeting.json.
    """
    pending_path = path or config.pending_actions_path()
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "meeting_id": meeting_id,
        "meeting_title": meeting_title,
        "updated_at": results.utc_timestamp(),
        "items": [item.to_dict() for item in items],
    }
    temporary_path = pending_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    os.replace(temporary_path, pending_path)


def add_pending_action(
    action: Action,
    *,
    meeting_title: str = "",
    path: Path | None = None,
) -> PendingAction:
    """Queue one action for its countdown, replacing any earlier entry with the same key."""
    entry = PendingAction.from_action(action)
    existing = [item for item in read_pending_actions(path) if item.dedup_key != entry.dedup_key]
    existing.append(entry)
    write_pending_actions(
        existing,
        meeting_id=action.meeting_id,
        meeting_title=meeting_title,
        path=path,
    )
    return entry


def cancel_pending_action(
    dedup_key: str,
    path: Path | None = None,
    *,
    journal_path: Path | None = None,
) -> bool:
    """Mark a waiting action cancelled. Returns True if something was actually cancelled.

    This is what the board's cancel button reaches. Cancelling is not undo — the
    message never went out, so there is nothing to reverse.

    It DOES leave a record. A cancelled countdown used to disappear without a
    trace anywhere: the pending filter drops it, no journal line was written, and
    nothing reached the recap. Ask "what did it decide not to do?" and there was
    no answer on screen. Now the journal carries a cancellation record and the
    board renders it as a dimmed card.
    """
    items = read_pending_actions(path)
    cancelled: list[PendingAction] = []
    for item in items:
        if item.dedup_key == dedup_key and item.is_waiting:
            item.status = PENDING_STATUS_CANCELLED
            cancelled.append(item)
    if not cancelled:
        return False
    header = read_pending_header(path)
    write_pending_actions(items, path=path, **header)
    for item in cancelled:
        try:
            results.append_cancellation(
                item.dedup_key,
                item.kind,
                human_summary=item.human_preview or "",
                quote=item.quote or "",
                speaker=item.speaker or "",
                meeting_id=item.meeting_id or "",
                path=journal_path,
            )
        except OSError as error:  # the cancel itself already succeeded
            print(f"[orchestrator] cancelled {item.dedup_key} but could not journal it: {error}")
    return True


def mark_pending_action_fired(dedup_key: str, path: Path | None = None) -> bool:
    """Flip a waiting entry to "fired" once its executor has run."""
    items = read_pending_actions(path)
    changed = False
    for item in items:
        if item.dedup_key == dedup_key and item.is_waiting:
            item.status = PENDING_STATUS_FIRED
            changed = True
    if changed:
        header = read_pending_header(path)
        write_pending_actions(items, path=path, **header)
    return changed


def due_pending_actions(
    now: datetime | None = None,
    path: Path | None = None,
) -> list[PendingAction]:
    """Waiting entries whose countdown has expired — the ones to fire on this tick."""
    now = now or datetime.now(UTC)
    return [
        item
        for item in read_pending_actions(path)
        if item.is_waiting and item.seconds_remaining(now) <= 0
    ]


def clear_pending_actions(path: Path | None = None) -> None:
    """Empty the pending file. Used by the demo reset."""
    write_pending_actions([], path=path)


# --- lane-boundary adapters -------------------------------------------------
#
# Extraction (Lane B), the planner (Lane C), and memory (Lane C) land in
# parallel with this file. Until they do, their functions raise
# NotImplementedError. The spine treats that exactly like any other failure: log
# it once, keep going, run the rest of the pipeline. That is not defensive
# padding — it is what lets this module be run and tested tonight, and it is the
# same degradation the demo needs if extraction times out on stage.


def open_memory_safely():
    """Memory if it opens, None if it does not. Never raises.

    A planner that gets None simply loses cross-meeting recall; it still routes.
    """
    try:
        from . import memory_store

        memory = memory_store.open_memory()
    except NotImplementedError:
        print("[orchestrator] memory not implemented yet — running without recall")
        return None
    except Exception as error:  # noqa: BLE001
        print(f"[orchestrator] memory unavailable ({error}) — running without recall")
        return None
    return memory


def read_memory_vocabulary(memory) -> tuple[list[str], dict[str, str]]:
    """(known_topics, reference_topics) from memory, or empty ones. Never raises.

    Both feed extraction, and they do different jobs: known_topics NUDGES the
    model to reuse last week's name for a work item, while reference_topics is
    applied afterwards as a deterministic override wherever a statement carries a
    handle like MMM-7. Without this wiring the second mechanism never ran at all
    in the real pipeline, and topic names drifted between passes — which makes
    dedup keys drift, which double-fires.
    """
    if memory is None:
        return [], {}
    topics: list[str] = []
    references: dict[str, str] = {}
    try:
        topics = list(memory.known_topics())
    except Exception as error:  # noqa: BLE001
        print(f"[orchestrator] memory could not list topics: {error}")
    try:
        references = dict(memory.topic_index_by_reference())
    except Exception as error:  # noqa: BLE001
        print(f"[orchestrator] memory could not build the reference index: {error}")
    if topics or references:
        print(
            f"[orchestrator] memory: {len(topics)} known topic(s), "
            f"{len(references)} handle(s) indexed"
        )
    return topics, references


def extract_statements_safely(
    segments: list[dict],
    meeting_title: str,
    *,
    source: str,
    meeting_id: str = "",
    memory=None,
) -> list:
    """Statements from segments, degrading extraction -> fixtures -> empty list."""
    known_topics, reference_topics = read_memory_vocabulary(memory)
    if segments:
        try:
            statements = extraction.extract_statements(
                segments,
                meeting_title,
                source=source,
                known_topics=known_topics,
                reference_topics=reference_topics,
                meeting_id=meeting_id,
            )
            if statements:
                return statements
            print(f"[orchestrator] extraction returned nothing for {source} pass")
        except NotImplementedError:
            print("[orchestrator] extraction not implemented yet — trying fixtures")
        except Exception as error:  # noqa: BLE001
            print(f"[orchestrator] extraction failed ({error}) — trying fixtures")
    else:
        print(f"[orchestrator] no {source} segments to extract from — trying fixtures")

    try:
        return extraction.load_fixture_statements(meeting_id)
    except NotImplementedError:
        print("[orchestrator] fixtures not implemented yet — no statements this pass")
    except Exception as error:  # noqa: BLE001
        print(f"[orchestrator] fixtures unavailable ({error}) — no statements this pass")
    return []


# The conflict judge is a MODEL call, so it lives here on the extraction side of
# the line rather than inside planner.py — the planner receives its verdict as
# data. Only these kinds are ever judged: a question or a Slack promise cannot
# overturn a prior decision, and asking about one is a wasted 15 seconds.
JUDGED_KINDS: frozenset[str] = frozenset({"decision"})
# A hard budget, because each verdict is a serialized model call and the fast
# fire is racing a human's attention. Two is enough for any real standup; the
# statements are judged in transcript order, so the budget spends itself on the
# beats that came first.
MAX_CONFLICT_JUDGEMENTS = 2


def judge_conflicts_safely(statements: list, memory, meeting: dict) -> dict[str, dict]:
    """{segment_id: verdict} for the statements that could plausibly overturn history.

    Reads history from memory EXCLUDING this meeting — memory is written before
    planning, so without that filter the "before" in a what-changed table would
    be the very sentence that caused the comment.

    Never raises, and an empty result is fine: with no verdict the planner still
    routes every statement, it just writes a quieter comment.
    """
    if memory is None or not statements:
        return {}
    if not config.read_flag("ADJOURN_CONFLICT_JUDGE", True):
        print("[orchestrator] conflict judging disabled (ADJOURN_CONFLICT_JUDGE=0)")
        return {}

    meeting_id = str(meeting.get("meeting_id", ""))
    verdicts: dict[str, dict] = {}
    judged = 0
    for statement in statements:
        if judged >= MAX_CONFLICT_JUDGEMENTS:
            break
        if getattr(statement, "kind", "") not in JUDGED_KINDS:
            continue
        topic = (getattr(statement, "topic", "") or "").strip()
        if not topic:
            continue
        try:
            history = [
                row
                for row in memory.history(topic)
                if row.get("meeting") != meeting_id
            ]
        except Exception as error:  # noqa: BLE001
            print(f"[orchestrator] memory could not read history for {topic!r}: {error}")
            continue
        if not history:
            continue
        new_statement = {
            "date": str(meeting.get("date", "")),
            "meeting": str(meeting.get("title", "") or meeting_id),
            "who": getattr(statement, "speaker", ""),
            "kind": getattr(statement, "kind", ""),
            "text": getattr(statement, "claim", ""),
        }
        try:
            verdict = extraction.judge_conflict(history, new_statement, topic)
        except Exception as error:  # noqa: BLE001
            print(f"[orchestrator] conflict judge failed on {topic!r}: {error}")
            continue
        judged += 1
        if verdict.conflict:
            verdicts[str(getattr(statement, "segment_id", "")).split(".", 1)[0]] = verdict.to_dict()
            print(
                f"[orchestrator] conflict on {topic!r} [{verdict.engine}]: {verdict.what_changed}"
            )
    return verdicts


def plan_actions_safely(
    statements: list,
    memory=None,
    *,
    meeting: dict | None = None,
    conflict_verdicts: dict | None = None,
) -> list[Action]:
    """Actions from statements. Never raises; an empty plan is a valid answer.

    A meeting with ZERO statements still goes through the table. It used to
    return here, and the result was that a three-minute conversation the system
    correctly judged as pure chat produced no recap, no card, and a board
    indistinguishable from a crashed pipeline — the one screen you cannot tell
    apart from a bug while standing in front of an audience. The planner already
    forces a recap for any meeting it is handed (planner.py:995-1003); it only
    needed to be handed one. The restraint thesis is the product, so restraint
    has to leave evidence.
    """
    if not statements and not (meeting or {}).get("meeting_id"):
        return []
    try:
        return list(
            planner.plan(
                statements,
                memory,
                meeting=meeting or {},
                conflict_verdicts=conflict_verdicts or {},
            )
        )
    except NotImplementedError:
        print("[orchestrator] planner not implemented yet — nothing to fire")
    except Exception as error:  # noqa: BLE001
        print(f"[orchestrator] planner failed ({error}) — nothing to fire")
    return []


def record_statements_safely(memory, statements: list, meeting_id: str, meta: dict) -> None:
    """Ingest the meeting and its statements into memory. Best effort, never raises."""
    if memory is None:
        return
    try:
        memory.record_meeting(meeting_id, meta.get("title", ""), meta.get("date", ""))
        for statement in statements:
            memory.record_statement(statement, meeting_id)
    except NotImplementedError:
        pass
    except Exception as error:  # noqa: BLE001
        print(f"[orchestrator] memory ingest failed: {error}")


def record_action_safely(memory, action: Action, result: results.ExecutorResult) -> None:
    """Link a fired action back to the statement that caused it. Best effort."""
    if memory is None:
        return
    try:
        memory.record_action(action, result)
    except NotImplementedError:
        pass
    except Exception as error:  # noqa: BLE001
        print(f"[orchestrator] memory action write failed: {error}")


def has_memory_fired(memory, dedup_key: str) -> bool:
    """Cross-run dedup from memory. False whenever memory cannot answer."""
    if memory is None:
        return False
    try:
        return bool(memory.has_fired_dedup_key(dedup_key))
    except Exception:  # noqa: BLE001 — NotImplementedError included; no answer means "no"
        return False


# --- dedup and ordering -----------------------------------------------------


def order_actions_for_firing(actions: list[Action]) -> list[Action]:
    """Planner order, except the recap page goes last so it can cite the rest."""
    head = [action for action in actions if action.kind not in LAST_ACTION_KINDS]
    tail = [action for action in actions if action.kind in LAST_ACTION_KINDS]
    return head + tail


def already_handled_keys(meeting_id: str) -> set[str]:
    """Every dedup_key that has already fired, is counting down, or a human stopped.

    The journal is the authority within a run and across restarts; pending.json
    covers the window where an action has been planned but not yet sent.

    CANCELLED COUNTS AS HANDLED. A cancellation is a decision, and the reconcile
    pass re-plans the same beats from the final transcript — so without this, a
    Slack message someone deliberately stopped during its regret window comes
    straight back a minute later and sends itself. "A human can stop it" has to
    mean stopped, not deferred.
    """
    fired = results.read_fired_dedup_keys(meeting_id)
    waiting = {
        item.dedup_key
        for item in read_pending_actions()
        if item.status in (PENDING_STATUS_WAITING, PENDING_STATUS_FIRED)
    }
    cancelled = {
        record.get("dedup_key", "")
        for record in results.read_cancellations()
        if record.get("dedup_key")
        and (not meeting_id or record.get("meeting_id") == meeting_id)
    }
    return fired | waiting | cancelled


def select_new_actions(
    actions: list[Action],
    handled: set[str],
    memory=None,
) -> list[Action]:
    """Drop actions already fired or already queued, keeping the always-refire kinds.

    Also collapses duplicates WITHIN this plan, so one plan cannot fire the same
    key twice even if the planner emitted it twice.
    """
    selected: list[Action] = []
    seen_this_plan: set[str] = set()
    for action in actions:
        if action.kind in ALWAYS_REFIRE_KINDS:
            selected.append(action)
            continue
        key = action.dedup_key
        if not key:
            print(f"[orchestrator] action {action.kind} has no dedup_key — firing once, unguarded")
            selected.append(action)
            continue
        if key in seen_this_plan or key in handled or has_memory_fired(memory, key):
            continue
        seen_this_plan.add(key)
        selected.append(action)
    return selected


# --- firing -----------------------------------------------------------------


# --- cloud mirror: strictly off the hot path --------------------------------
#
# The mirror is a NICE-TO-HAVE that lives on the far side of the internet, and
# the demo's whole claim is that follow-through lands in seconds. So it never
# runs inline. Every mirror call goes onto a daemon thread that nothing waits
# for, and cloud_mirror itself already swallows its own failures into a local
# backlog file — so a dead network, a slow round trip, or a rotated password can
# cost an action AT MOST one thread, never one millisecond of latency and never a
# failed fire. Daemon threads also mean a mid-mirror Ctrl-C still exits now.

MIRROR_THREAD_NAME = "adjourn-cloud-mirror"
MIRROR_DRAIN_SECONDS = 8.0
# Ceiling on the scaled budget below: a wedged connection must not hold the exit.
MIRROR_DRAIN_CEILING_SECONDS = 30.0

# Every mirror thread still in flight. Daemon threads are killed outright when the
# interpreter exits, and a cloud round trip takes ~2.6s from this machine — so the
# LAST actions of a run (the two regret-window sends, which fire seconds before
# the process ends) were reaching the cloud exactly never, and not reaching the
# local backlog either, because the thread died before its own except clause ran.
# The fix is not to make the mirror blocking; it is to give the threads a bounded
# moment to finish once there is no longer any action waiting on them.
_mirror_threads: list[threading.Thread] = []
_mirror_threads_lock = threading.Lock()


def mirror_in_background(what: str, call) -> threading.Thread | None:
    """Run one cloud_mirror call on a throwaway daemon thread. Returns immediately.

    `call` is a zero-argument callable so the import of cloud_mirror happens on
    the worker thread too: a missing redis package or an unparseable .env must not
    be able to raise on the firing path.
    """
    def run() -> None:
        try:
            call()
        except Exception as error:  # noqa: BLE001 — a mirror is never worth a stack trace on stage
            print(f"[mirror] {what} failed ({type(error).__name__}) — kept in the local backlog")

    try:
        thread = threading.Thread(target=run, name=MIRROR_THREAD_NAME, daemon=True)
        thread.start()
    except RuntimeError as error:  # interpreter shutting down; nothing to do
        print(f"[mirror] {what} not started: {error}")
        return None
    with _mirror_threads_lock:
        _mirror_threads[:] = [item for item in _mirror_threads if item.is_alive()]
        _mirror_threads.append(thread)
    return thread


def drain_cloud_mirrors(timeout_seconds: float | None = None) -> int:
    """Wait, briefly and at the very end, for outstanding mirror writes to land.

    Called once when a run is otherwise finished, so it never delays an action —
    by the time this runs there is nothing left to fire. Returns how many threads
    were still going. A thread that outlasts the budget is simply left as a daemon
    and dies with the process; its payload is lost to the cloud, which is the
    correct trade for a mirror and is why nothing downstream reads its result.
    """
    with _mirror_threads_lock:
        outstanding = [item for item in _mirror_threads if item.is_alive()]
    if not outstanding:
        return 0
    # The DEFAULT budget scales with the queue. A cloud round trip is ~2.6s from
    # here, so one shared 8s deadline could never drain ten writes — every run
    # printed a promise it could not keep and left the tail of the run in the
    # backlog. Nothing is left to fire by the time this runs, so waiting longer
    # costs only the wait; the ceiling keeps a wedged connection from holding the
    # process. An explicit timeout is honoured exactly as given.
    if timeout_seconds is None:
        budget = min(
            MIRROR_DRAIN_CEILING_SECONDS,
            max(MIRROR_DRAIN_SECONDS, 3.0 * len(outstanding)),
        )
    else:
        budget = timeout_seconds
    print(f"[mirror] waiting up to {budget:.0f}s for {len(outstanding)} write(s) to land")
    deadline = time.monotonic() + budget
    for thread in outstanding:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(remaining)
    still_running = sum(1 for item in outstanding if item.is_alive())
    if still_running:
        # A number, not an adjective: "abandoned" alone left you guessing whether
        # the graph you are about to put on screen is complete.
        try:
            from . import cloud_mirror  # imported here, like every other mirror call

            depth = cloud_mirror.backlog_depth()
        except Exception:  # noqa: BLE001 — reporting must not crash the exit path
            depth = -1
        depth_note = f"backlog now {depth}" if depth >= 0 else "backlog depth unreadable"
        print(
            f"[mirror] {still_running} write(s) did not finish in time — "
            f"abandoned, not retried; {depth_note}"
        )
    return len(outstanding)


# --- the idle flush ---------------------------------------------------------
#
# A rehearsal held while the cloud is unreachable queues its writes into
# state/mirror_backlog.jsonl, and nothing drains that file until the NEXT
# successful mirror — which, between demos, may be hours away or never. So the
# public board sits empty while a perfectly good backlog waits on disk, and then
# fires the lot into the graph at the worst possible moment: the middle of the
# next run.
#
# The watcher ticks twice a second doing nothing. That is the right place to
# drain it: no meeting is in flight, no action is waiting, and the work goes on a
# daemon thread anyway. Throttled, silent when there is nothing to do, and it
# never runs while an actual mirror write is outstanding.
MIRROR_IDLE_FLUSH_INTERVAL_SECONDS = 30.0
_last_idle_flush_at = 0.0


def flush_cloud_backlog_when_idle(now: float | None = None) -> bool:
    """Opportunistically drain the mirror backlog on an idle tick. Never raises.

    Returns True when a flush was actually started. Cheap on the common path: a
    monotonic comparison, then one stat of the backlog file.
    """
    global _last_idle_flush_at

    moment = time.monotonic() if now is None else now
    if moment - _last_idle_flush_at < MIRROR_IDLE_FLUSH_INTERVAL_SECONDS:
        return False
    _last_idle_flush_at = moment

    with _mirror_threads_lock:
        if any(item.is_alive() for item in _mirror_threads):
            return False  # a real mirror write is in flight; it flushes on its own

    try:
        from . import cloud_mirror

        if not cloud_mirror.is_configured():
            return False
        depth = cloud_mirror.backlog_depth()
    except Exception:  # noqa: BLE001 — an idle nicety must never break the loop
        return False
    if depth <= 0:
        return False

    print(f"[mirror] idle — draining {depth} queued write(s) to the cloud")

    def drain() -> None:
        from . import cloud_mirror

        sent = cloud_mirror.flush_backlog()
        if sent:
            print(f"[mirror] idle flush sent {sent} write(s); backlog now {cloud_mirror.backlog_depth()}")

    mirror_in_background("idle backlog flush", drain)
    return True


def mirror_execution_in_background(result: results.ExecutorResult, dedup_key: str = "") -> None:
    """Mirror one fired action to FalkorDB Cloud without making anyone wait for it."""
    record = dict(result.to_record())
    if dedup_key:
        record.setdefault("dedup_key", dedup_key)

    def send() -> None:
        from . import cloud_mirror

        cloud_mirror.mirror_execution(record)

    mirror_in_background(f"execution {result.kind}", send)


def mirror_meeting_in_background(meta: dict, statements: list) -> None:
    """Mirror a reconciled meeting and its statements. Called once per reconcile."""
    rows = [
        statement.to_dict() if hasattr(statement, "to_dict") else dict(statement)
        for statement in (statements or [])
    ]
    snapshot = dict(meta or {})

    def send() -> None:
        from . import cloud_mirror

        cloud_mirror.mirror_meeting(snapshot, rows)

    mirror_in_background(f"meeting {snapshot.get('meeting_id', '?')}", send)


def fire_action(
    action: Action,
    *,
    meeting_title: str = "",
    memory=None,
    ignore_regret_window: bool = False,
) -> results.ExecutorResult | None:
    """Dispatch one action to its executor, journal the result, update memory.

    Returns None when the action was QUEUED for its regret window rather than
    fired. Nothing is journaled until something actually happens — the board
    learns about countdowns from pending.json, and the journal stays a record of
    events, not of intentions.

    Never raises: executors.execute_action() turns any explosion into a failed
    result, and a failed result is journaled too so the board can show it red.
    """
    # The orchestrator is the only component that knows the RESOLVED meeting
    # title — the planner sees statements, not meeting.json — and every executor
    # renders payload["meeting_title"] into its comment, ticket, or recap. Fill it
    # in here rather than in eight executors, and never clobber a planner-supplied
    # one.
    if meeting_title and not action.payload.get("meeting_title"):
        action.payload["meeting_title"] = meeting_title

    if action.regret_window_s > 0 and not ignore_regret_window:
        entry = add_pending_action(action, meeting_title=meeting_title)
        print(
            f"[orchestrator] queued {action.kind} for {action.regret_window_s}s "
            f"(fires {entry.fire_at} unless cancelled) — {action.dedup_key}"
        )
        report_pipeline_event(
            stage="executor", tone="hold", label=action.kind,
            text=f"holding {action.regret_window_s}s · "
                 f"{action.payload.get('human_preview') or action.kind}",
        )
        return None

    result = executors.execute_action(action)
    results.append_execution(
        result,
        action.dedup_key,
        extra={"source": action.source, "segment_id": action.segment_id},
    )
    mirror_execution_in_background(result, action.dedup_key)
    mark_pending_action_fired(action.dedup_key)
    record_action_safely(memory, action, result)
    status = "ok" if result.ok else "FAILED"
    print(f"[orchestrator] {action.kind} [{result.mode}] {status}: {result.human_summary}")
    report_pipeline_event(
        stage="executor",
        tone="act" if result.ok else "decline",
        label=action.kind,
        text=f"{result.human_summary} · {result.mode}",
    )
    return result


def build_segment_time_index(segments: list[dict]) -> dict[str, float]:
    """{segment_id: start seconds} — what the recap turns into [mm:ss] on each card."""
    index: dict[str, float] = {}
    for segment in segments or []:
        segment_id = str(segment.get("segment_id", ""))
        if not segment_id:
            continue
        try:
            index[segment_id] = float(segment.get("start") or 0.0)
        except (TypeError, ValueError):
            continue
    return index


def attach_recap_context(
    action: Action,
    *,
    segment_times: dict[str, float] | None = None,
    segment_count: int | None = None,
) -> None:
    """Fill in what only the orchestrator knows before the recap page is rendered.

    The planner cannot supply `executed` — it plans, it does not watch. Reading it
    back out of the journal (rather than out of this run's return values) is what
    makes the reconcile recap show the fast pass's cards too, instead of a page
    that forgets everything that happened ninety seconds ago.

    UNDONE ACTIONS ARE MARKED, NOT DROPPED. The journal knows which dedup keys
    were reversed; each record carries that through as `undone` so the recap can
    strike the row through and stop linking a permalink that now 404s. Dropping
    the row instead would be a quieter kind of dishonesty: the action really did
    happen, and then a human really did take it back, and the page that outlives
    the demo should say both.
    """
    meeting_id = str(action.payload.get("meeting_id") or action.meeting_id or "")
    undone_keys = results.read_undone_dedup_keys()
    executed = []
    for record in results.read_executions(meeting_id or None):
        if record.get("record_type", results.RECORD_TYPE_EXECUTION) != results.RECORD_TYPE_EXECUTION:
            continue
        if record.get("kind") == "recap_page":  # the recap does not report on itself
            continue
        row = dict(record)
        row["undone"] = bool((record.get("dedup_key") or "").strip() in undone_keys)
        executed.append(row)
    action.payload["executed"] = executed
    if segment_times:
        action.payload["segment_times"] = dict(segment_times)
    if segment_count is not None:
        action.payload["segment_count"] = int(segment_count)
    elif segment_times and not action.payload.get("segment_count"):
        action.payload["segment_count"] = len(segment_times)


def fire_actions(
    actions: list[Action],
    *,
    meeting_title: str = "",
    memory=None,
    segment_times: dict[str, float] | None = None,
    segment_count: int | None = None,
) -> list[results.ExecutorResult]:
    """Fire a whole plan in order. Queued countdown actions are not in the return."""
    fired: list[results.ExecutorResult] = []
    for action in order_actions_for_firing(actions):
        if action.kind == "recap_page":
            attach_recap_context(
                action, segment_times=segment_times, segment_count=segment_count
            )
            _recap_actions_by_meeting[str(action.meeting_id or "")] = action
            remember_recap_action(action)
        result = fire_action(action, meeting_title=meeting_title, memory=memory)
        if result is not None:
            fired.append(result)
    return fired


# The last recap action planned for each meeting, so a message that goes out
# after its 60-second countdown can still land on the recap page. This is a
# read-through cache in front of state/recap_actions/, not the storage itself —
# see remember_recap_action() for why the disk copy has to exist.
_recap_actions_by_meeting: dict[str, Action] = {}


def recap_action_path(meeting_id: str) -> Path:
    """Where the planned recap action for one meeting is parked between processes."""
    directory = config.state_directory() / "recap_actions"
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", str(meeting_id or "unknown-meeting"))
    return directory / f"{safe_name}.json"


def remember_recap_action(action: Action) -> None:
    """Park the planned recap action on disk so ANOTHER process can re-render it.

    The countdown items survive a crash — pending.json is on disk and a fresh
    `--watch` resumes them and sends the Slack message on the original deadline.
    The recap did NOT survive, because it lived only in _recap_actions_by_meeting
    and a restarted process starts with that dict empty. So the message went out
    and the recap page never grew the card for it: the one artifact that outlives
    the demo was silently short the last two beats. Same story, no crash needed,
    whenever the countdowns are picked up by a second process (`--replay
    --no-wait-for-countdowns` in one shell, `--watch` in another).

    The journal cannot stand in for this file: its recap record carries the
    rendered path, not the `statements` list the "Who owes what" ledger is built
    from. Writing the whole payload is the only way to rebuild the same page.
    """
    path = recap_action_path(action.meeting_id)
    document = {
        "kind": action.kind,
        "payload": {key: value for key, value in action.payload.items() if key != "executed"},
        "dedup_key": action.dedup_key,
        "regret_window_s": action.regret_window_s,
        "quote": action.quote,
        "speaker": action.speaker,
        "meeting_id": action.meeting_id,
    }
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)  # atomic: a reader never sees half a payload
    except OSError as error:
        print(f"[orchestrator] could not park the recap action for {action.meeting_id!r}: {error}")


def load_remembered_recap_action(meeting_id: str) -> Action | None:
    """The recap action parked by whichever process planned it, or None."""
    path = recap_action_path(meeting_id)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"[orchestrator] parked recap action for {meeting_id!r} is unreadable: {error}")
        return None
    return Action(
        kind=str(document.get("kind") or "recap_page"),
        payload=dict(document.get("payload") or {}),
        dedup_key=str(document.get("dedup_key") or ""),
        regret_window_s=int(document.get("regret_window_s") or 0),
        quote=str(document.get("quote") or ""),
        speaker=str(document.get("speaker") or ""),
        meeting_id=str(document.get("meeting_id") or meeting_id),
    )


def refresh_recap_after_late_action(meeting_id: str, memory=None) -> None:
    """Re-render the recap for `meeting_id` after a countdown action finally fired."""
    key = str(meeting_id or "")
    action = _recap_actions_by_meeting.get(key) or load_remembered_recap_action(key)
    if action is None:
        print(f"[orchestrator] no recap to refresh for {meeting_id!r} — it will update on reconcile")
        return
    _recap_actions_by_meeting[key] = action
    attach_recap_context(action)
    fire_action(action, memory=memory, ignore_regret_window=True)


def refresh_recap_after_undo(meeting_id: str, memory=None) -> bool:
    """Re-render the recap after a human undid one of its actions.

    UNDO USED TO GO THROUGH A DIFFERENT DOOR THAN FIRING. The recap is rewritten
    whenever an action lands — end of the executor pass, and again after each
    countdown — but undo never touched it. So after undoing the draft PR and the
    #1 comment, the board honestly showed both cards struck through while the
    recap page on disk still listed them under "What Adjourn did", badged LIVE,
    linking a comment permalink that now 404s. The demo pairs the undo beat and
    the recap beat; the page you close on cannot contradict the board you just
    showed.

    Returns True when the page was rewritten. Never raises — a stale recap is a
    cosmetic failure and must not turn a successful undo into a refusal.
    """
    key = str(meeting_id or "")
    action = _recap_actions_by_meeting.get(key) or load_remembered_recap_action(key)
    if action is None:
        print(f"[orchestrator] no parked recap for {meeting_id!r} — nothing to rewrite after the undo")
        return False
    # If the recap itself has been taken back, leave it gone. Rewriting it here
    # would resurrect a page the human deleted, one undo click later.
    if action.dedup_key and action.dedup_key in results.read_undone_dedup_keys():
        print(f"[orchestrator] recap for {meeting_id!r} was itself undone — leaving it removed")
        return False
    _recap_actions_by_meeting[key] = action
    try:
        attach_recap_context(action)
        # ignore_regret_window because the recap has none, and because an undo is
        # a human decision that has already happened: there is nothing to wait for.
        fire_action(action, memory=memory, ignore_regret_window=True)
    except Exception as error:  # noqa: BLE001
        print(f"[orchestrator] could not rewrite the recap for {meeting_id!r}: {error}")
        return False
    print(f"[orchestrator] recap for {meeting_id!r} rewritten — undone rows struck through")
    return True


def tick_pending_actions(memory=None) -> list[results.ExecutorResult]:
    """Fire every countdown that has expired. Called once per loop iteration.

    Rebuilds an Action from the pending entry rather than holding one in memory,
    so a restart mid-countdown still sends the message when its time comes.

    CLAIM BEFORE FIRING. executors/regret_window.py runs its own daemon timer over
    the same pending.json, so two things can notice the same expired entry in the
    same second. mark_pending_action_fired() only returns True for an entry that
    was still waiting, which makes it an atomic-enough claim: whoever flips the
    status sends the message, and the other one steps over it. Without this, a
    Slack message someone was promised once goes out twice.
    """
    fired: list[results.ExecutorResult] = []
    for entry in due_pending_actions():
        if not mark_pending_action_fired(entry.dedup_key):
            continue
        action = Action(
            kind=entry.kind,
            payload=entry.payload,
            dedup_key=entry.dedup_key,
            regret_window_s=entry.regret_window_s,
            quote=entry.quote,
            speaker=entry.speaker,
            meeting_id=entry.meeting_id,
        )
        print(f"[orchestrator] regret window elapsed — sending {entry.kind} {entry.dedup_key}")
        result = fire_action(action, memory=memory, ignore_regret_window=True)
        if result is not None:
            fired.append(result)
            refresh_recap_after_late_action(entry.meeting_id, memory)
    return fired


# --- the two phases ---------------------------------------------------------


def handle_meeting_stopped(meeting_id: str, memory=None) -> list[results.ExecutorResult]:
    """FAST FIRE. Live snapshot -> statements(source="live") -> plan -> execute.

    This is the moment the founders are watching: the meeting ends and the board
    starts filling in before anyone has switched windows. It runs off the live
    caption snapshot, which survives the stop and is readable within a second or
    two — at the cost of rougher text and only two speaker labels. Whatever it
    gets wrong, the reconcile pass repairs.
    """
    owns_memory = memory is None
    memory = memory or open_memory_safely()
    started_at = time.monotonic()
    try:
        meta, segments = meetingscribe_source.load_live_snapshot(meeting_id)
        title = meta.get("title") or meetingscribe_source.read_meeting_title(meeting_id)
        print(
            f"[orchestrator] FAST FIRE {meeting_id} '{title}' — "
            f"{len(segments)} live segment(s), {meta.get('echoes_removed', 0)} echo(es) removed"
        )
        if not meta.get("enabled"):
            print("[orchestrator] live captions are disabled — the fast path will rely on fixtures")

        report_pipeline(
            mode="watch",
            pass_name="fast",
            meeting_id=meeting_id,
            meeting_title=title,
            watcher=watcher_status_stopped(meeting_id),
            extraction=extraction_status_running(extraction.SOURCE_LIVE),
            planner=PlannerStatus(phase=STAGE_IDLE, detail="idle"),
            feed_reset=feed_belongs_to_a_new_run(meeting_id),
        )
        report_pipeline_event(
            stage="watcher", tone="note", label="stopped",
            text=f"{title} ended · {len(segments)} live lines to read",
        )
        statements = extract_statements_safely(
            segments, title, source=extraction.SOURCE_LIVE,
            meeting_id=meeting_id, memory=memory,
        )
        report_pipeline(extraction=extraction_status_from_statements(
            statements, source=extraction.SOURCE_LIVE, segment_count=len(segments)
        ))
        record_statements_safely(memory, statements, meeting_id, meta)

        meeting = {
            "meeting_id": meeting_id,
            "title": title,
            "date": str(meta.get("date", "") or ""),
        }
        report_pipeline(planner=PlannerStatus(phase=STAGE_RUNNING, detail="routing through the table…"))
        verdicts = judge_conflicts_safely(statements, memory, meeting)
        actions = plan_actions_safely(
            statements, memory, meeting=meeting, conflict_verdicts=verdicts
        )
        narrate_plan(statements, actions)
        report_pipeline(planner=planner_status_from_plan(statements, actions))
        new_actions = select_new_actions(actions, already_handled_keys(meeting_id), memory)
        print(
            f"[orchestrator] {len(statements)} statement(s) -> {len(actions)} action(s), "
            f"{len(new_actions)} new  [{time.monotonic() - started_at:.1f}s so far]"
        )
        fired = fire_actions(
            new_actions, meeting_title=title, memory=memory,
            segment_times=build_segment_time_index(segments),
            segment_count=len(segments),
        )
        print(f"[orchestrator] FAST FIRE complete in {time.monotonic() - started_at:.1f}s")
        return fired
    finally:
        if owns_memory:
            close_memory_safely(memory)


def reconcile_from_final_transcript(meeting_id: str, memory=None) -> list[results.ExecutorResult]:
    """RECONCILE. Full transcript -> re-extract -> fire only what is NEW.

    Dedup runs against the executions journal, memory, and anything still waiting
    in pending.json, so a promise caught by the fast pass is never sent twice.
    The recap page is the deliberate exception: it re-runs and rewrites itself
    with the better final-transcript quotes.
    """
    owns_memory = memory is None
    memory = memory or open_memory_safely()
    started_at = time.monotonic()
    try:
        meta, segments = meetingscribe_source.load_meeting(meeting_id)
        title = meta.get("title") or meeting_id
        print(
            f"[orchestrator] RECONCILE {meeting_id} '{title}' — "
            f"{len(segments)} final segment(s), speakers {meta.get('speakers')}"
        )
        if not segments:
            print("[orchestrator] final transcript is empty — nothing to reconcile")
            return []

        report_pipeline(
            mode="watch" if read_pipeline_status().mode != "replay" else "replay",
            pass_name="final",
            meeting_id=meeting_id,
            meeting_title=title,
            watcher=watcher_status_transcript_ready(meeting_id),
            extraction=extraction_status_running(extraction.SOURCE_FINAL),
            planner=PlannerStatus(phase=STAGE_IDLE, detail="idle"),
            feed_reset=feed_belongs_to_a_new_run(meeting_id),
        )
        report_pipeline_event(
            stage="watcher", tone="note", label="transcript",
            text=f"final transcript ready · {len(segments)} lines",
        )
        statements = extract_statements_safely(
            segments, title, source=extraction.SOURCE_FINAL,
            meeting_id=meeting_id, memory=memory,
        )
        report_pipeline(extraction=extraction_status_from_statements(
            statements, source=extraction.SOURCE_FINAL, segment_count=len(segments)
        ))
        record_statements_safely(memory, statements, meeting_id, meta)

        meeting = {
            "meeting_id": meeting_id,
            "title": title,
            "date": str(meta.get("date", "") or ""),
        }
        report_pipeline(planner=PlannerStatus(phase=STAGE_RUNNING, detail="routing through the table…"))
        verdicts = judge_conflicts_safely(statements, memory, meeting)
        actions = plan_actions_safely(
            statements, memory, meeting=meeting, conflict_verdicts=verdicts
        )
        narrate_plan(statements, actions)
        report_pipeline(planner=planner_status_from_plan(statements, actions))
        new_actions = select_new_actions(actions, already_handled_keys(meeting_id), memory)
        print(
            f"[orchestrator] reconcile: {len(statements)} statement(s) -> "
            f"{len(actions)} action(s), {len(new_actions)} new after dedup "
            f"[{time.monotonic() - started_at:.1f}s so far]"
        )
        upgrade_recap_quotes(meeting_id, actions)
        fired = fire_actions(
            new_actions, meeting_title=title, memory=memory,
            segment_times=build_segment_time_index(segments),
            segment_count=len(segments),
        )
        # The final transcript is the version worth keeping, so the meeting is
        # mirrored HERE and not on the fast pass — one write of the good text
        # rather than two writes where the second corrects the first.
        mirror_meeting_in_background(meeting, statements)
        print(f"[orchestrator] RECONCILE complete in {time.monotonic() - started_at:.1f}s")
        return fired
    finally:
        if owns_memory:
            close_memory_safely(memory)


def upgrade_recap_quotes(meeting_id: str, actions: list[Action]) -> bool:
    """Swap the live-caption wording on already-written recap cards for the final one.

    Runs BEFORE the reconcile pass fires anything, so the page a viewer has open
    improves even for the cards that are deduped away and never fire again — which
    is most of them, and is the entire point of the reconcile pass.
    """
    better_quotes = {
        action.dedup_key: action.quote
        for action in actions
        if action.dedup_key and action.quote
    }
    if not better_quotes:
        return False
    try:
        from .executors.recap_page_executor import upgrade_quotes_in_recap

        changed = upgrade_quotes_in_recap(meeting_id, better_quotes)
    except Exception as error:  # noqa: BLE001 — a cosmetic upgrade never blocks a pass
        print(f"[orchestrator] could not upgrade recap quotes: {error}")
        return False
    if changed:
        print(f"[orchestrator] recap quotes upgraded to the final transcript wording")
    return changed


def close_memory_safely(memory) -> None:
    if memory is None:
        return
    try:
        memory.close()
    except Exception:  # noqa: BLE001
        pass


# --- undo -------------------------------------------------------------------


def undo_execution(
    dedup_key: str,
    *,
    pending_path: Path | None = None,
    journal_path: Path | None = None,
) -> bool:
    """Reverse a fired action by dedup_key. Reached from the board's undo button.

    Finds the most recent successful execution with that key, rebuilds the
    ExecutorResult, calls the executor's undo(), and appends an undo record.
    History is never rewritten — the board strikes the card through instead.

    A key that is still counting down is CANCELLED rather than undone: nothing
    was sent, so there is nothing to reverse. The board renders those as two
    different words because they mean two different things.

    The path arguments exist for board_server, which can be pointed at a fixture
    journal via ADJOURN_BOARD_JOURNAL / ADJOURN_BOARD_PENDING; both default to
    the configured files, which is what the real demo uses.
    """
    if cancel_pending_action(dedup_key, pending_path):
        print(f"[orchestrator] cancelled {dedup_key} before it was sent")
        return True

    generations = [
        candidate
        for candidate in results.read_executions(path=journal_path)
        if candidate.get("dedup_key") == dedup_key
        and candidate.get("record_type", results.RECORD_TYPE_EXECUTION)
        == results.RECORD_TYPE_EXECUTION
        and candidate.get("ok")
    ]
    if not generations:
        print(f"[orchestrator] nothing to undo for {dedup_key}")
        return False

    # EVERY GENERATION, NEWEST FIRST. A recap_page rewrites itself in place —
    # once per fired action, again after each countdown — so one dedup_key can
    # own three journal rows and three .bak generations. Undoing only the newest
    # restored the SECOND-newest recap and then reported can_undo=false, which
    # left a page on disk listing six actions that had all just been undone,
    # with no way to remove it from the board at all. The board collapses those
    # rows into one card, so one click has to unwind all of them; the oldest
    # write is the one whose undo_payload says there was no previous version,
    # and its undo deletes the file.
    ordered = list(reversed(generations)) if record_rewrites_itself(generations[-1]) else [
        generations[-1]
    ]
    undo_ok = True
    for index, candidate in enumerate(ordered):
        result = results.ExecutorResult.from_record(candidate)
        this_undo = executors.undo_result(result)
        results.append_undo(
            result,
            this_undo,
            dedup_key,
            note=(
                "undo requested from the board" if index == 0
                else "earlier generation rolled back"
            ) if this_undo else "executor refused the undo",
            path=journal_path,
        )
        if index == 0:
            undo_ok = this_undo
        if not this_undo:
            break
    if len(ordered) > 1:
        print(
            f"[orchestrator] undo {dedup_key}: {'ok' if undo_ok else 'refused'} "
            f"({len(ordered)} generations)"
        )
    else:
        print(f"[orchestrator] undo {dedup_key}: {'ok' if undo_ok else 'refused'}")

    # THE RECAP IS REWRITTEN HERE, not at the caller. It used to happen only in
    # board_server's undo route, which meant `-m adjourn.orchestrator --undo <key>`
    # — the escape hatch DEMO.md hands you when the board is not up — reversed the
    # action and left the recap page still listing it as LIVE with a permalink
    # that now 404s. One door, one behaviour.
    #
    # Skipped for an overridden journal: that is a fixture or a test, and the
    # recap belongs to whatever meeting the real journal knows about.
    if undo_ok and journal_path is None:
        undone_kind = ordered[0].get("kind")
        meeting_id = str(ordered[0].get("meeting_id") or "")
        if undone_kind not in ALWAYS_REFIRE_KINDS and meeting_id:
            refresh_recap_after_undo(meeting_id)
    return undo_ok


def record_rewrites_itself(record: dict) -> bool:
    """True for a kind whose every write replaces the previous one in place."""
    return record.get("kind") in ALWAYS_REFIRE_KINDS


# --- the run loop -----------------------------------------------------------


def run_orchestrator(*, stop_after_seconds: float | None = None) -> None:
    """Start the watcher and drive both phases until interrupted.

    Also ticks the pending file on every watcher tick so expired countdowns fire
    — one loop, one thread, no timer threads to leak. Never raises out: one
    broken action must not stop the rest of the meeting.
    """
    config.ensure_state_directories()
    memory = open_memory_safely()
    # Before anything reads the engine: start it if it is not up. Bounded,
    # best-effort, and it writes state/engine_launch.json so the board can show
    # what happened rather than leaving a dead dot with no explanation.
    launch = meetingscribe_source.ensure_engine_running()
    if launch.get("status") not in (
        meetingscribe_source.LAUNCH_ALREADY_RUNNING,
        meetingscribe_source.LAUNCH_STARTED,
    ):
        print(f"[orchestrator] engine not available: {launch.get('detail', '')}")
    source = meetingscribe_source.describe_source()
    print(f"[orchestrator] engine {source['engine']} reachable={source['reachable']}")
    print(f"[orchestrator] journal {config.executions_journal_path()}")
    print(f"[orchestrator] pending {config.pending_actions_path()}")
    if config.is_simulation_forced():
        print("[orchestrator] ADJOURN_SIM=1 — every executor is in SIMULATION mode")

    report_pipeline(
        mode="watch",
        pass_name="",
        meeting_id="",
        meeting_title="",
        watcher=watcher_status_waiting(),
        extraction=ExtractionStatus(),
        planner=PlannerStatus(),
    )

    def handle(event: watcher.MeetingEvent) -> None:
        if event.event == watcher.EVENT_MEETING_STOPPED:
            report_pipeline(
                mode="watch",
                pass_name="fast",
                meeting_id=event.meeting_id,
                watcher=watcher_status_stopped(event.meeting_id),
            )
            handle_meeting_stopped(event.meeting_id, memory)
        elif event.event == watcher.EVENT_TRANSCRIPT_READY:
            report_pipeline(
                mode="watch",
                pass_name="final",
                meeting_id=event.meeting_id,
                watcher=watcher_status_transcript_ready(event.meeting_id),
            )
            reconcile_from_final_transcript(event.meeting_id, memory)
        elif event.event == watcher.EVENT_SUMMARY_READY:
            print(f"[orchestrator] summary ready for {event.meeting_id} (bonus, nothing waits on it)")

    def tick() -> None:
        tick_pending_actions(memory)
        # Nothing is in flight on a tick, so this is the cheapest moment in the
        # program to hand the cloud whatever a previous offline rehearsal queued.
        flush_cloud_backlog_when_idle()

    try:
        watcher.watch_for_meeting_events(
            handle,
            on_tick=tick,
            stop_after_seconds=stop_after_seconds,
            # Already launched above, before describe_source() reported on it.
            # The watcher's own flag is for anyone driving the loop directly.
            launch_engine=False,
        )
    except KeyboardInterrupt:
        print("\n[orchestrator] stopped")
    finally:
        close_memory_safely(memory)


# --- replay (testing without a live meeting) --------------------------------


def replay_meeting(target: str, *, phase: str = "both") -> list[results.ExecutorResult]:
    """Run the pipeline over a finished meeting, a folder, or a canned live snapshot.

    Accepts:
      * a meeting id                 -> reconcile pass over meeting.json
      * a recording directory path   -> reconcile pass over that folder
      * a /api/live JSON dump        -> fast pass over the canned snapshot
      * a statements fixture (.json) -> plan and fire directly, no extraction

    This is how the spine gets exercised without waiting for someone to hold a
    meeting, and how the demo gets rehearsed at 2am.
    """
    # An empty target is not "replay whatever". Path("") is Path("."), the CURRENT
    # DIRECTORY, which is_dir() answers True for — so a blank argument used to be
    # read as "replay this folder", fail to find a meeting.json in it, keep the
    # empty meeting id, and then fast-fire off /api/live. Reject it here, before
    # any of that can start.
    if not str(target or "").strip():
        print("[orchestrator] --replay needs a fixture, a meeting id, or a folder")
        return []

    path = Path(target).expanduser()
    if path.is_file() and path.suffix == ".json":
        return _replay_json_file(path)
    if path.is_file() and path.suffix == ".jsonl":
        return _replay_transcript_file(path)
    # A bare fixture name ("agi-living-room") resolves to its transcript before it
    # is treated as a meeting id, so a rehearsal and the demo run the same code.
    fixture_transcript = config.FIXTURES_DIR / f"{Path(target).name}.jsonl"
    if fixture_transcript.is_file():
        return _replay_transcript_file(fixture_transcript)

    meeting_id = target
    if path.is_dir():
        # The folder must PROVE it is a recording. load_meeting() falls back to
        # the folder name for the meeting id when meeting.json is missing, so a
        # non-empty id is not evidence of anything: `--replay /tmp` produced the
        # meeting id "tmp" and then fast-fired off /api/live — the engine's
        # CURRENT caption buffer — filing another meeting's words under a typo.
        # Requiring the file closes the last of the three replay doors.
        if not (path / meetingscribe_source.MEETING_JSON_NAME).is_file():
            print(
                f"[orchestrator] {path} holds no {meetingscribe_source.MEETING_JSON_NAME} — "
                f"refusing to replay (the live buffer belongs to another meeting)"
            )
            return []
        meta, _segments = meetingscribe_source.load_meeting(path)
        meeting_id = str(meta.get("meeting_id") or "").strip()
        if not meeting_id:
            print(f"[orchestrator] {path} holds no readable meeting.json — refusing to replay")
            return []
    elif meetingscribe_source.resolve_recording_directory(meeting_id) is None:
        # NOTHING resolved: not a file, not a fixture, not a folder, not a
        # recording id. Falling through to the fast path here would be actively
        # dangerous — handle_meeting_stopped() reads /api/live, which returns the
        # engine's CURRENT caption buffer no matter which id it is handed. A typo
        # in --replay would then fire real follow-through from whatever meeting
        # happens to be sitting in the ring, filed under the typo's name. Refuse.
        print(
            f"[orchestrator] {target!r} is not a fixture, a recording folder, or a known "
            f"meeting id — refusing to replay (the live buffer belongs to another meeting)"
        )
        return []

    memory = open_memory_safely()
    try:
        fired: list[results.ExecutorResult] = []
        if phase in ("fast", "both"):
            fired += handle_meeting_stopped(meeting_id, memory)
        if phase in ("final", "both"):
            fired += reconcile_from_final_transcript(meeting_id, memory)
        return fired
    finally:
        close_memory_safely(memory)


def _replay_transcript_file(path: Path) -> list[results.ExecutorResult]:
    """Replay a fixture .jsonl transcript through the WHOLE pipeline.

    Extraction, conflict judging, planning, firing — the same functions a live
    meeting runs, with the transcript coming off disk instead of off the engine.
    There is no fixtures-only branch below this line, which is what makes a
    rehearsal evidence about the demo rather than a separate program.
    """
    segments, meta = extraction.load_fixture_transcript(str(path))
    if not segments:
        print(f"[orchestrator] {path.name} has no segments — nothing to replay")
        return []
    meeting_id = str(meta.get("meeting_id") or path.stem)
    title = str(meta.get("title") or path.stem)
    meeting = {
        "meeting_id": meeting_id,
        "title": title,
        "date": str(meta.get("date", "") or ""),
    }
    source = str(meta.get("source") or extraction.SOURCE_FINAL)
    print(
        f"[orchestrator] REPLAY transcript {path.name} — {len(segments)} segment(s), "
        f"meeting {meeting_id!r} '{title}'"
    )

    report_pipeline(
        mode="replay",
        pass_name="replay",
        meeting_id=meeting_id,
        meeting_title=title,
        watcher=watcher_status_transcript_ready(meeting_id, replay=True),
        extraction=extraction_status_running(source if source else "fixture"),
        planner=PlannerStatus(phase=STAGE_IDLE, detail="idle"),
        # A replay is unconditionally a new run: it is the command you type to
        # start the story over, so the rail starts empty every time.
        feed_reset=True,
    )
    report_pipeline_event(
        stage="watcher", tone="note", label="replay",
        text=f"{title} · {len(segments)} lines to read",
    )

    memory = open_memory_safely()
    started_at = time.monotonic()
    try:
        statements = extract_statements_safely(
            segments, title, source=source, meeting_id=meeting_id, memory=memory
        )
        report_pipeline(extraction=extraction_status_from_statements(
            statements, source=source, segment_count=len(segments)
        ))
        record_statements_safely(memory, statements, meeting_id, meeting)
        report_pipeline(planner=PlannerStatus(phase=STAGE_RUNNING, detail="routing through the table…"))
        verdicts = judge_conflicts_safely(statements, memory, meeting)
        actions = plan_actions_safely(
            statements, memory, meeting=meeting, conflict_verdicts=verdicts
        )
        narrate_plan(statements, actions)
        report_pipeline(planner=planner_status_from_plan(statements, actions))
        new_actions = select_new_actions(actions, already_handled_keys(meeting_id), memory)
        print(
            f"[orchestrator] {len(statements)} statement(s) -> {len(actions)} action(s), "
            f"{len(new_actions)} new after dedup [{time.monotonic() - started_at:.1f}s]"
        )
        upgrade_recap_quotes(meeting_id, actions)
        fired = fire_actions(
            new_actions, meeting_title=title, memory=memory,
            segment_times=build_segment_time_index(segments),
            segment_count=len(segments),
        )
        mirror_meeting_in_background(meeting, statements)
        print(f"[orchestrator] REPLAY complete in {time.monotonic() - started_at:.1f}s")
        return fired
    finally:
        close_memory_safely(memory)


def wait_out_regret_windows(
    *,
    memory=None,
    poll_interval_seconds: float = 1.0,
    maximum_wait_seconds: float | None = None,
) -> list[results.ExecutorResult]:
    """Tick the countdown until nothing is waiting. What a rehearsal needs and a
    live run gets for free from the watch loop.

    Without this, `--replay` would exit with a Slack message still sitting in
    pending.json and the rehearsal would never exercise the send at all — the one
    path that most needs rehearsing.
    """
    waiting = [item for item in read_pending_actions() if item.is_waiting]
    if not waiting:
        return []
    longest = max(item.seconds_remaining() for item in waiting)
    budget = maximum_wait_seconds if maximum_wait_seconds is not None else longest + 5
    print(
        f"[orchestrator] {len(waiting)} action(s) in their regret window — "
        f"waiting up to {budget:.0f}s (cancel from the board to stop one)"
    )
    fired: list[results.ExecutorResult] = []
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        fired += tick_pending_actions(memory)
        if not [item for item in read_pending_actions() if item.is_waiting]:
            break
        time.sleep(poll_interval_seconds)
    return fired


def _replay_json_file(path: Path) -> list[results.ExecutorResult]:
    """A live-snapshot dump replays through the fast path; a statements fixture skips extraction."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"[orchestrator] cannot replay {path}: {error}")
        return []

    memory = open_memory_safely()
    try:
        if isinstance(document, dict) and "turns" in document:
            meeting_id = str(document.get("meeting_id") or path.stem)
            meta, segments = meetingscribe_source.load_live_snapshot(
                meeting_id, payload=document
            )
            title = meta.get("title") or path.stem
            print(f"[orchestrator] REPLAY live snapshot {path.name} — {len(segments)} segment(s)")
            report_pipeline(
                mode="replay",
                pass_name="replay",
                meeting_id=meeting_id,
                meeting_title=title,
                watcher=watcher_status_transcript_ready(meeting_id, replay=True),
                extraction=extraction_status_running(extraction.SOURCE_LIVE),
                planner=PlannerStatus(phase=STAGE_IDLE, detail="idle"),
            )
            statements = extract_statements_safely(
                segments, title, source=extraction.SOURCE_LIVE, meeting_id=meeting_id
            )
            report_pipeline(extraction=extraction_status_from_statements(
                statements, source=extraction.SOURCE_LIVE, segment_count=len(segments)
            ))
        else:
            raw = document.get("statements") if isinstance(document, dict) else document
            statements = [extraction.Statement.from_dict(item) for item in (raw or [])]
            meeting_id = str(path.stem)
            title = path.stem
            print(f"[orchestrator] REPLAY statements fixture {path.name} — {len(statements)} statement(s)")
            report_pipeline(
                mode="replay",
                pass_name="replay",
                meeting_id=meeting_id,
                meeting_title=title,
                watcher=watcher_status_transcript_ready(meeting_id, replay=True),
                extraction=extraction_status_from_statements(statements, source="fixture"),
                planner=PlannerStatus(phase=STAGE_RUNNING, detail="routing through the table…"),
            )

        # The meeting dict has to reach the PLANNER, not just the recorder.
        # Without it the planner builds dedup keys with an empty meeting id
        # ("github_update:issue-2:decision"), which lands in a parallel
        # namespace that dedups against nothing the normal path ever fired —
        # and the recap page comes out titled "unknown-meeting (0 actions)".
        meeting = {"meeting_id": meeting_id, "title": title, "date": ""}
        record_statements_safely(memory, statements, meeting_id, meeting)
        report_pipeline(planner=PlannerStatus(phase=STAGE_RUNNING, detail="routing through the table…"))
        actions = plan_actions_safely(statements, memory, meeting=meeting)
        narrate_plan(statements, actions)
        report_pipeline(planner=planner_status_from_plan(statements, actions))
        new_actions = select_new_actions(actions, already_handled_keys(meeting_id), memory)
        upgrade_recap_quotes(meeting_id, actions)
        return fire_actions(new_actions, meeting_title=title, memory=memory)
    finally:
        close_memory_safely(memory)


# --- CLI --------------------------------------------------------------------


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adjourn.orchestrator",
        description="The Adjourn spine: watch for a meeting to end, then execute the follow-through.",
    )
    parser.add_argument("--watch", action="store_true", help="poll the engine and run both phases")
    parser.add_argument(
        "--replay",
        nargs="?",
        const=DEFAULT_REPLAY_TARGET,
        metavar="MEETING_ID|PATH",
        help=(
            "run the pipeline over a finished meeting, a recording folder, a fixture "
            "transcript (.jsonl or a bare fixture name), or a statements fixture (.json). "
            f"With no target, replays the demo fixture ({DEFAULT_REPLAY_TARGET}). "
            "MeetingScribe is not required for a fixture replay."
        ),
    )
    parser.add_argument(
        "--wait-for-countdowns",
        dest="wait_for_countdowns",
        action="store_true",
        default=True,
        help="after --replay, keep ticking until every regret window has elapsed (default)",
    )
    parser.add_argument(
        "--no-wait-for-countdowns",
        dest="wait_for_countdowns",
        action="store_false",
        help="exit as soon as the plan is fired, leaving countdowns in pending.json",
    )
    parser.add_argument(
        "--phase",
        choices=("fast", "final", "both"),
        default="both",
        help="which pass --replay should run (default: both)",
    )
    parser.add_argument(
        "--sim", action="store_true",
        help="simulate every executor EXCEPT the kinds ADJOURN_LIVE_KINDS keeps live",
    )
    parser.add_argument(
        "--sim-all", dest="sim_all", action="store_true",
        help="simulate everything, ignoring ADJOURN_LIVE_KINDS (the panic button)",
    )
    parser.add_argument("--undo", metavar="DEDUP_KEY", help="undo (or cancel) one fired action")
    parser.add_argument("--status", action="store_true", help="print what the spine can see, then exit")
    parser.add_argument(
        "--stop-after",
        type=float,
        metavar="SECONDS",
        help="with --watch, exit after this long (smoke tests)",
    )
    return parser


def print_status() -> None:
    """One screen of everything the spine can currently see. No side effects."""
    payload = {
        "config": config.describe_configuration(),
        "source": meetingscribe_source.describe_source(),
        "engine_jobs": meetingscribe_source.fetch_engine_status().get("jobs", {}),
        "recent_meetings": meetingscribe_source.list_recent_meeting_ids(5),
        "executions": results.summarize_executions(),
        "pending": [item.to_dict() for item in read_pending_actions()],
        "pipeline": read_pipeline_status().to_dict(),
    }
    print(json.dumps(payload, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    if arguments.sim_all:
        # The panic button. ADJOURN_LIVE_KINDS is what carves live kinds back out
        # of ADJOURN_SIM, so the only flag that can honestly promise "nothing
        # leaves this machine" is one that clears it — in the environment AND in
        # the .env config, since decide_mode() reads both.
        os.environ["ADJOURN_SIM"] = "1"
        os.environ["ADJOURN_LIVE_KINDS"] = ""
        print("[orchestrator] --sim-all: ADJOURN_SIM=1, ADJOURN_LIVE_KINDS cleared — "
              "nothing will leave this machine")
    elif arguments.sim:
        # Set before anything reads config: config.is_simulation_forced() and
        # secrets_store.decide_mode() both consult the live environment.
        os.environ["ADJOURN_SIM"] = "1"
        # Tell the truth about what --sim actually does. decide_mode() narrows
        # ADJOURN_SIM by ADJOURN_LIVE_KINDS no matter HOW ADJOURN_SIM got set, so
        # a --sim run with github_update in that list still writes to a public
        # repo. A safety switch that lies is worse than no safety switch.
        live_kinds = config.forced_live_action_kinds()
        if live_kinds:
            print(
                f"[orchestrator] --sim: ADJOURN_SIM=1 — sim EXCEPT {', '.join(sorted(live_kinds))}, "
                f"which ADJOURN_LIVE_KINDS keeps LIVE (use --sim-all to force everything to sim)"
            )
        else:
            print("[orchestrator] --sim: ADJOURN_SIM=1, nothing will leave this machine")

    config.ensure_state_directories()

    if arguments.status:
        print_status()
        return 0
    if arguments.undo:
        return 0 if undo_execution(arguments.undo) else 1
    if arguments.replay:
        fired = replay_meeting(arguments.replay, phase=arguments.phase)
        if arguments.wait_for_countdowns:
            fired += wait_out_regret_windows()
        print(f"[orchestrator] replay fired {len(fired)} action(s)")
        drain_cloud_mirrors()
        return 0
    if arguments.watch:
        run_orchestrator(stop_after_seconds=arguments.stop_after)
        drain_cloud_mirrors()
        return 0

    build_argument_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
