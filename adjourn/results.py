"""ExecutorResult and the execution journal — the one shared truth in Adjourn.

Every executor returns an `ExecutorResult`. Every fired action appends exactly one
JSON line to `~/.meetingscribe/executions.jsonl`. The board renders that file. The
orchestrator dedupes against that file. Undo appends another line rather than
rewriting history, so the journal stays append-only and tail-able.

Record shape on disk (one JSON object per line):
    {
      "record_type": "execution" | "undo",
      "dedup_key": "...",            # deterministic, from planner.build_dedup_key
      "written_at": "2026-08-21T...",# when the line was written
      "undo_ok": true,               # present on "undo" records only
      ...all ExecutorResult fields...
    }

This module is fully implemented. No lane should need to change it — if you need
another field, add it to the record via `extra`, not by editing the dataclass.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import config

RECORD_TYPE_EXECUTION = "execution"
RECORD_TYPE_UNDO = "undo"
# A countdown a human stopped before it went out. Its own type, because it is
# neither an execution (nothing ran) nor an undo (nothing was reversed) — and
# because "what did it decide NOT to do" deserves an artifact of its own.
RECORD_TYPE_CANCELLATION = "cancellation"

MODE_LIVE = "live"
MODE_SIM = "sim"


def utc_timestamp() -> str:
    """ISO-8601 UTC timestamp with a 'Z'-style offset. Used for every fired_at."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class ExecutorResult:
    """What one executor did (or simulated doing), in the form the board renders.

    ok             — did the action complete? False means it failed; say why in human_summary.
    kind           — the Action.kind that produced this (see planner.ACTION_KINDS).
    external_id    — id in the foreign system: comment id, issue number, Linear id, message ts.
    url            — a link a human can click, when one exists.
    human_summary  — one sentence, past tense, what a person would say happened.
    mode           — "live" or "sim". Must reflect reality; the board badge reads this.
    undo_payload   — everything undo() needs to reverse this, and nothing else.
    quote          — the verbatim sentence from the meeting that caused this action.
    speaker        — who said it.
    meeting_id     — MeetingScribe meeting id this came from.
    fired_at       — ISO timestamp of the attempt.
    """

    ok: bool
    kind: str
    external_id: str | None
    url: str | None
    human_summary: str
    mode: str
    undo_payload: dict = field(default_factory=dict)
    quote: str = ""
    speaker: str = ""
    meeting_id: str = ""
    fired_at: str = field(default_factory=utc_timestamp)

    @property
    def is_simulated(self) -> bool:
        return self.mode == MODE_SIM

    @property
    def is_undoable(self) -> bool:
        """A result is undoable when it succeeded and carries something to undo with."""
        return self.ok and bool(self.undo_payload)

    def to_record(self) -> dict:
        """Plain dict of the dataclass fields, ready to be wrapped into a journal line."""
        return asdict(self)

    @classmethod
    def from_record(cls, record: dict) -> ExecutorResult:
        """Rebuild a result from a journal line, ignoring record-level metadata."""
        fields = {
            "ok": bool(record.get("ok", False)),
            "kind": record.get("kind", ""),
            "external_id": record.get("external_id"),
            "url": record.get("url"),
            "human_summary": record.get("human_summary", ""),
            "mode": record.get("mode", MODE_SIM),
            "undo_payload": record.get("undo_payload") or {},
            "quote": record.get("quote", ""),
            "speaker": record.get("speaker", ""),
            "meeting_id": record.get("meeting_id", ""),
            "fired_at": record.get("fired_at", ""),
        }
        return cls(**fields)

    @classmethod
    def simulated(
        cls,
        kind: str,
        human_summary: str,
        *,
        rendered_payload: dict,
        quote: str = "",
        speaker: str = "",
        meeting_id: str = "",
        external_id: str | None = None,
        url: str | None = None,
    ) -> ExecutorResult:
        """The sim-mode result every executor returns from the bottom of its own module.

        `rendered_payload` is the exact body that would have gone over the wire —
        that is the honesty of sim mode: the payload is real, only the send is not.
        """
        return cls(
            ok=True,
            kind=kind,
            external_id=external_id,
            url=url,
            human_summary=human_summary,
            mode=MODE_SIM,
            undo_payload={"simulated": True, "payload": rendered_payload},
            quote=quote,
            speaker=speaker,
            meeting_id=meeting_id,
        )

    @classmethod
    def failed(
        cls,
        kind: str,
        reason: str,
        *,
        mode: str = MODE_SIM,
        quote: str = "",
        speaker: str = "",
        meeting_id: str = "",
    ) -> ExecutorResult:
        """A failed attempt. Failures are journaled too — the board shows them red.

        `mode` defaults to "sim" on purpose: a failure that never reached a
        transport did not go live, and a LIVE badge on a card that never sent
        anything is exactly the kind of lie the badge exists to prevent. An
        executor that fails AFTER its live call has already gone out must pass
        mode="live" explicitly, so the card admits something may have landed.
        """
        return cls(
            ok=False,
            kind=kind,
            external_id=None,
            url=None,
            human_summary=reason,
            mode=mode,
            undo_payload={},
            quote=quote,
            speaker=speaker,
            meeting_id=meeting_id,
        )


# --- journal writes ---------------------------------------------------------


def _append_line(path: Path, record: dict) -> dict:
    """Append one JSON line, flushed and fsynced so a tailing board sees it immediately."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return record


def append_execution(
    result: ExecutorResult,
    dedup_key: str = "",
    *,
    extra: dict | None = None,
    path: Path | None = None,
) -> dict:
    """Append one execution record to the journal. Returns the record as written.

    `dedup_key` is the planner's deterministic key for the action; pass it so the
    reconcile pass can tell a re-extraction of the same promise from a new one.
    """
    record = {
        "record_type": RECORD_TYPE_EXECUTION,
        "dedup_key": dedup_key,
        "written_at": utc_timestamp(),
        **result.to_record(),
    }
    if extra:
        record.update(extra)
    return _append_line(path or config.executions_journal_path(), record)


def append_undo(
    result: ExecutorResult,
    undo_ok: bool,
    dedup_key: str = "",
    *,
    note: str = "",
    path: Path | None = None,
) -> dict:
    """Append an undo record for a previously executed result. History is never rewritten."""
    record = {
        "record_type": RECORD_TYPE_UNDO,
        "dedup_key": dedup_key,
        "written_at": utc_timestamp(),
        "undo_ok": undo_ok,
        "undo_note": note,
        **result.to_record(),
    }
    return _append_line(path or config.executions_journal_path(), record)


def append_cancellation(
    dedup_key: str,
    kind: str,
    *,
    human_summary: str = "",
    quote: str = "",
    speaker: str = "",
    meeting_id: str = "",
    path: Path | None = None,
) -> dict:
    """Record that a human stopped a countdown before it fired.

    Before this existed, clicking Cancel simply deleted the card: no journal
    line, no ledger entry, nothing on the recap. "A model extracts, a table
    decides, and a human can stop it" is the trust argument, and the proof that
    a human stopped it did not exist anywhere.
    """
    record = {
        "record_type": RECORD_TYPE_CANCELLATION,
        "dedup_key": dedup_key,
        "written_at": utc_timestamp(),
        "kind": kind,
        "human_summary": human_summary,
        "quote": quote,
        "speaker": speaker,
        "meeting_id": meeting_id,
        "cancelled_at": utc_timestamp(),
    }
    return _append_line(path or config.executions_journal_path(), record)


def read_cancellations(path: Path | None = None) -> list[dict]:
    """Every cancellation record, in file order."""
    return [
        record
        for record in iterate_records(path)
        if record.get("record_type") == RECORD_TYPE_CANCELLATION
    ]


# --- journal reads ----------------------------------------------------------


def iterate_records(path: Path | None = None) -> Iterator[dict]:
    """Yield every journal record in file order. A missing file yields nothing.

    Malformed lines are skipped rather than raised — a half-written line must
    never take the board down mid-demo.
    """
    journal = path or config.executions_journal_path()
    if not journal.exists():
        return
    with journal.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def read_executions(
    meeting_id: str | None = None,
    path: Path | None = None,
) -> list[dict]:
    """All records, newest last, optionally filtered to one meeting. Board's read API."""
    records = list(iterate_records(path))
    if meeting_id is None:
        return records
    return [record for record in records if record.get("meeting_id") == meeting_id]


def read_execution_results(
    meeting_id: str | None = None,
    path: Path | None = None,
) -> list[ExecutorResult]:
    """Execution records only, as ExecutorResult objects (undo records excluded)."""
    return [
        ExecutorResult.from_record(record)
        for record in read_executions(meeting_id, path)
        if record.get("record_type", RECORD_TYPE_EXECUTION) == RECORD_TYPE_EXECUTION
    ]


def read_fired_dedup_keys(
    meeting_id: str | None = None,
    path: Path | None = None,
) -> set[str]:
    """Dedup keys of successful executions. The reconcile pass fires only what is NOT here."""
    return {
        record.get("dedup_key", "")
        for record in read_executions(meeting_id, path)
        if record.get("record_type", RECORD_TYPE_EXECUTION) == RECORD_TYPE_EXECUTION
        and record.get("ok")
        and record.get("dedup_key")
    }


def read_undone_dedup_keys(
    meeting_id: str | None = None,
    path: Path | None = None,
) -> set[str]:
    """Dedup keys whose latest write is a successful undo.

    A later execution of the same key (Send again after Recall) clears the
    strike-through. History stays append-only; the board reads the tail.
    """
    latest: dict[str, str] = {}
    for record in read_executions(meeting_id, path):
        key = str(record.get("dedup_key") or "")
        if not key:
            continue
        record_type = record.get("record_type", RECORD_TYPE_EXECUTION)
        if record_type == RECORD_TYPE_UNDO and record.get("undo_ok"):
            latest[key] = "undone"
        elif record_type == RECORD_TYPE_EXECUTION and record.get("ok"):
            latest[key] = "live"
    return {key for key, state in latest.items() if state == "undone"}


def read_records_since(
    byte_offset: int,
    path: Path | None = None,
) -> tuple[list[dict], int]:
    """Tail-f support for the board: records after `byte_offset`, plus the new offset.

    Returns ([], byte_offset) when nothing new. If the file shrank (someone reset
    the demo), reads from the top and returns the fresh offset.
    """
    journal = path or config.executions_journal_path()
    if not journal.exists():
        return [], 0
    size = journal.stat().st_size
    start = 0 if size < byte_offset else byte_offset
    records: list[dict] = []
    with journal.open("r", encoding="utf-8") as handle:
        handle.seek(start)
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        new_offset = handle.tell()
    return records, new_offset


def summarize_executions(
    meeting_id: str | None = None,
    path: Path | None = None,
) -> dict:
    """Running totals for the board header: fired, live, sim, failed, undone."""
    records = read_executions(meeting_id, path)
    executions = [
        record
        for record in records
        if record.get("record_type", RECORD_TYPE_EXECUTION) == RECORD_TYPE_EXECUTION
    ]
    undos = [record for record in records if record.get("record_type") == RECORD_TYPE_UNDO]
    return {
        "fired": len(executions),
        "live": sum(1 for record in executions if record.get("mode") == MODE_LIVE),
        "sim": sum(1 for record in executions if record.get("mode") == MODE_SIM),
        "failed": sum(1 for record in executions if not record.get("ok")),
        "undone": sum(1 for record in undos if record.get("undo_ok")),
        "by_kind": _count_by_kind(executions),
    }


def _count_by_kind(records: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        kind = record.get("kind", "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


if __name__ == "__main__":
    print(json.dumps(summarize_executions(), indent=2))
