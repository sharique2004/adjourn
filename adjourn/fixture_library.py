"""Fixture transcripts, shaped like recordings, so a replayed tape has a page.

WHY THIS EXISTS. One of the five things the room must feel is: *from a card, the
quote traces back to the transcript.* On any real recording that already worked —
the card carries a meeting id, the Meetings tab holds that recording, the quote
becomes a link. On the tape the demo actually runs (`--replay`, i.e.
`living-room-standup`) it could not: nothing was ever recorded, so `/meetings/
living-room-standup` was a 404 and the board correctly refused to mint a link to a
door that opens on nothing. The beat was simply absent on the demo path.

The words, though, exist. `adjourn/fixtures/<name>.jsonl` is a real transcript
— it is the file extraction reads, verbatim, on the same code path a live
recording takes. What was missing was not the transcript but a *reader* that
hands it to the views in the shape they already understand.

So: this module reads one fixture .jsonl and returns a document with the same
keys `meeting.json` has — id, title, created, duration, speakers, turns. Every
consumer (the library row builder, the screenplay builder, the board's meeting
header, the transcript-presence check) works on it unchanged, with no
fixture-shaped branch anywhere downstream.

HONESTY. A fixture is not a recording and the views say so: the document carries
`replay: True`, the library row wears a REPLAY badge, and the detail page states
in one line that these words were replayed rather than heard. Adjourn's whole
argument is that it does not pretend, and a synthetic tape wearing a
recording's clothes without a label would be exactly that.

SAFETY. `meetings_ui.is_meeting_id` stays strict — it guards paths that
interpolate an id into an engine URL and into a filesystem scan, and it is a
security gate rather than a formatting rule. This module has its own, separate
gate: a fixture name must match a conservative charset AND resolve to a file
that already sits directly in `adjourn/fixtures/`, so no id from a URL can walk
out of that directory.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

#: The one shape a fixture name may have. No dots, no slashes, no separators of
#: any kind — so `..`, absolute paths and `regression/foo` are all rejected
#: before a path is ever built from the value.
FIXTURE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: The line the detail page prints under a replayed tape's masthead.
REPLAY_NOTE = (
    "Replayed transcript. These are the words Adjourn was given, run through the "
    "same extraction and planning path a live recording takes — no audio was "
    "captured on this machine for this meeting."
)

#: Fixture speakers are the recorder's two track labels. They are mapped to
#: display names through extraction's own table, so the transcript page names
#: people exactly the way the cards, the Slack body and the ledger do.
DEFAULT_TRACK_FOR_SPEAKER = {"you": "mic", "them": "system"}


def fixture_transcript_path(meeting_id: str) -> Path | None:
    """The .jsonl for `meeting_id`, or None. The gate; nothing else builds paths."""
    name = str(meeting_id or "").strip()
    if not FIXTURE_NAME.match(name):
        return None
    path = config.FIXTURES_DIR / f"{name}.jsonl"
    # `parent` re-checked rather than trusted: the regex already forbids a
    # separator, and this is the belt to that brace.
    if path.parent != config.FIXTURES_DIR or not path.is_file():
        return None
    return path


def is_fixture_meeting_id(meeting_id: str) -> bool:
    """True when a fixture transcript of that exact name ships in this repo."""
    return fixture_transcript_path(meeting_id) is not None


def fixture_meeting_ids() -> list[str]:
    """Every fixture tape, by name. Sorted, and never recursive into regression/."""
    try:
        names = sorted(
            path.stem
            for path in config.FIXTURES_DIR.glob("*.jsonl")
            if path.is_file() and FIXTURE_NAME.match(path.stem)
        )
    except OSError:
        return []
    return names


def speaker_display_name(label: str) -> str:
    """"Them" -> the configured guest name. Delegated, never re-implemented here.

    Falls back to the raw label if extraction cannot be imported, because a
    transcript page must render even when the model client's dependencies are
    missing.
    """
    try:
        from . import extraction

        return extraction.speaker_display_name(label)
    except Exception:  # noqa: BLE001 — a name is not worth a 500
        return str(label or "")


def fixture_meeting_document(meeting_id: str) -> dict:
    """One fixture .jsonl -> a `meeting.json`-shaped document. {} when there is none.

    Never raises: a malformed line is skipped, an unreadable file is "no such
    meeting", and both degrade to the same 404 a missing recording gets.
    """
    path = fixture_transcript_path(meeting_id)
    if path is None:
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    meta: dict = {}
    turns: list[dict] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") == "meta":
            meta = row
        elif row.get("type") == "segment":
            speaker = str(row.get("speaker") or "")
            turns.append({
                "speaker": speaker,
                "track": str(row.get("track") or
                             DEFAULT_TRACK_FOR_SPEAKER.get(speaker.lower(), "")),
                "start": float(row.get("start") or 0),
                "end": float(row.get("end") or 0),
                "text": str(row.get("text") or ""),
            })
    if not turns:
        return {}

    # The speakers map is keyed by the raw track label, because that is the key
    # build_screenplay looks turns up under. One entry per label that spoke.
    speakers: dict[str, str] = {}
    for turn in turns:
        label = turn["speaker"]
        if label and label not in speakers:
            speakers[label] = speaker_display_name(label) or label

    return {
        "id": str(meta.get("meeting_id") or Path(path).stem),
        "title": str(meta.get("title") or Path(path).stem),
        # DATE ONLY, deliberately. The fixture records the day the meeting
        # happened and no clock; inventing "09:00" to make the field look like
        # the engine's would be a made-up fact on the honesty tab's own product.
        "created": str(meta.get("date") or ""),
        "duration": max((turn["end"] for turn in turns), default=0.0),
        "status": "done",
        "speakers": speakers,
        "turns": turns,
        "warnings": [],
        "has_summary": False,
        "has_transcript": True,
        "has_notes": False,
        # The label every view keys its honesty off. See the module docstring.
        "replay": True,
        "replay_note": REPLAY_NOTE,
    }
