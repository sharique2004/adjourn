"""Reading MeetingScribe — the transcript source. IMPLEMENTED (Lane A).

MeetingScribe is Sharique's shipped app. This module is READ-ONLY against it:
GETs to the loopback engine and reads of files under ~/.meetingscribe/recordings.
Never POST to a mutating endpoint. Never call /api/shutdown. Never write into
~/MeetingScribe. The endpoint allowlist below is the enforcement point — if a
call is not built from one of those three constants, it does not belong here.

Two transcripts, two speeds:

  FAST PATH  — GET /api/live?since=<seq>
      Accumulated live captions. Available ~1-2s after stop and, importantly, the
      snapshot SURVIVES the stop, so it is still readable after recording ends.
      Two speaker labels only ("You" / "Them") — no real diarization. May come
      back {"enabled": false}: always check before using it.

      Verified on the live engine 2026-08-20: the buffer is BOUNDED. A finished
      meeting reported seq=456 but returned only 400 turns (first seq 57) with
      dropped=0, i.e. the oldest turns roll out of the ring without being counted
      as dropped. The fast path is therefore allowed to be incomplete — that is
      exactly what the reconcile pass exists to repair.

  RECONCILE  — ~/.meetingscribe/recordings/<Safe Title> — <id>/meeting.json
      The full transcript once jobs[<id>].state == "done". Better text, real
      speaker keys. Written atomically via os.replace, so a read either sees the
      old file or the new one — never a partial. The FOLDER CAN RENAME ITSELF when
      the title changes, so resolve by scanning for the " — <id>" suffix, never by
      remembering a path. (Verified: "Salient Interview — 20260819-155401" carries
      a real U+2014 EM DASH with a space on each side.)

ECHO. The two tracks overlap. On a real 400-turn snapshot, 20% of turns were a
near-verbatim cross-track repeat: system audio leaking into the microphone, the
exact problem MeetingScribe's own transcript-level echo cancellation solves. Left
alone it produces duplicate statements attributed to the wrong speaker. So
`collapse_echoed_turns()` drops the MIC copy of anything the SYSTEM track already
said inside a short window — deterministic string work, no model, matching the
approach the app's author took himself.

Normalized segment shape this module returns — a superset that satisfies both the
skeleton contract (segment_id/speaker/track/start/end/text) and drift's
pipeline.read_transcript shape (segment_id/ts/speaker/text), so extraction can be
ported from drift with no adaptation:

    {"segment_id": "live-57", "ts": "00:07:50", "speaker": "Them",
     "text": "...", "track": "system", "start": 470.52, "end": 476.28,
     "source": "live"}

Live segment ids are "live-<seq>", final-transcript ids "m<meeting_id>-t<index>",
so a live statement and a final statement can never be confused. Dedup still
happens on planner dedup_keys, never on segment ids.

Run standalone to see what it can read right now:
    python -m adjourn.meetingscribe_source
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

import requests

from . import config

# Segment id prefixes. "live-" and "m" rather than bare "L"/"T" so an id is
# readable in a log line and carries the meeting it came from.
LIVE_SEGMENT_PREFIX = "live-"
FINAL_SEGMENT_PREFIX = "m"

SOURCE_LIVE = "live"
SOURCE_FINAL = "final"

# Read-only endpoints. This tuple is the allowlist — if a call is not built from
# one of these, it does not belong in this module.
STATUS_ENDPOINT = "/api/status"
RECORD_STATUS_ENDPOINT = "/api/record/status"
LIVE_ENDPOINT = "/api/live"

READ_ONLY_ENDPOINTS = (STATUS_ENDPOINT, RECORD_STATUS_ENDPOINT, LIVE_ENDPOINT)

HTTP_TIMEOUT_SECONDS = 3

# The engine is loopback-guarded and expects no Origin header. requests sends none
# by default; this dict is spelled out so nobody "helpfully" adds one later.
READ_ONLY_HEADERS = {"Accept": "application/json", "User-Agent": "adjourn/1.0 (read-only)"}

# Folder naming: "<Safe Title> — <YYYYMMDD-HHMMSS>", em dash, spaces either side.
RECORDING_ID_SEPARATOR = " — "

TRACK_MICROPHONE = "mic"
TRACK_SYSTEM = "system"

# Echo suppression. A mic turn is an echo of a system turn when the normalized
# text matches closely AND the two started within this many seconds of each other.
# The tracks carry slightly different start_offsets, so the window is generous.
ECHO_SIMILARITY_THRESHOLD = 0.90
ECHO_TIME_WINDOW_SECONDS = 6.0
ECHO_LOOKBACK_TURNS = 6

_WORD_PATTERN = re.compile(r"[a-z0-9]+")


# --- HTTP reads -------------------------------------------------------------


def _get_json(endpoint: str, parameters: dict | None = None) -> dict:
    """GET one allowlisted read-only endpoint. Returns {} on any transport error.

    The engine being down, restarting, or mid-write must never raise into the
    watcher loop — the demo runs while MeetingScribe is being started by hand.
    """
    if endpoint not in READ_ONLY_ENDPOINTS:
        raise ValueError(f"{endpoint!r} is not a read-only MeetingScribe endpoint")
    url = f"{config.meetingscribe_base_url()}{endpoint}"
    try:
        response = requests.get(
            url,
            params=parameters or {},
            headers=READ_ONLY_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def fetch_engine_status() -> dict:
    """GET /api/status — the trigger bus.

    Shape: {"recorder": {"recording": bool},
            "jobs": {"<id>": {"state": "processing"|"done"|...}},
            "summary_jobs": {...}}

    A NEW key in `jobs` with state "processing" appears the instant stop returns,
    which is how a meeting's END and its ID arrive together. jobs[id].state ==
    "done" means the full transcript exists on disk.

    Returns {} on any transport error.
    """
    return _get_json(STATUS_ENDPOINT)


def fetch_record_status() -> dict:
    """GET /api/record/status — {"recording": bool, "elapsed": float, ...}.

    Carries no meeting id. Polled faster than /api/status because the boolean
    flips the instant recording stops; the id still comes from /api/status.
    """
    return _get_json(RECORD_STATUS_ENDPOINT)


def fetch_live_turns(since_sequence: int = 0) -> dict:
    """GET /api/live?since=<seq> — the accumulated live caption snapshot.

    Shape: {"enabled": bool,
            "turns": [{"seq", "track": "mic"|"system", "who": "You"|"Them",
                       "start", "end", "text"}],
            "partials": {}, "seq": N, "dropped": N}

    ALWAYS check `enabled` first — when live captions are off this returns
    {"enabled": false} and the fast path must degrade to fixtures rather than
    crash. Returns {"enabled": False, "turns": []} on transport error.
    """
    payload = _get_json(LIVE_ENDPOINT, {"since": int(since_sequence)})
    if not payload:
        return {"enabled": False, "turns": [], "seq": 0, "dropped": 0}
    payload.setdefault("turns", [])
    payload.setdefault("enabled", False)
    return payload


def is_engine_reachable() -> bool:
    """True when the engine answered a status GET. Used for the board's header dot."""
    return bool(fetch_record_status())


# --- normalization helpers --------------------------------------------------


def format_timestamp(seconds: float) -> str:
    """Seconds since meeting start -> "HH:MM:SS", drift's `ts` field."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        total = 0
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def normalize_spoken_text(text: str) -> str:
    """Lowercase word tokens joined by single spaces. For echo comparison only."""
    return " ".join(_WORD_PATTERN.findall((text or "").lower()))


def build_segment(
    segment_id: str,
    speaker: str,
    text: str,
    *,
    track: str,
    start: float,
    end: float,
    source: str,
) -> dict:
    """One normalized segment, in the superset shape both contracts expect."""
    return {
        "segment_id": segment_id,
        "ts": format_timestamp(start),
        "speaker": speaker,
        "text": (text or "").strip(),
        "track": track,
        "start": float(start or 0.0),
        "end": float(end or 0.0),
        "source": source,
    }


def collapse_echoed_turns(turns: list[dict]) -> list[dict]:
    """Drop microphone turns that merely echo what the system track already said.

    Deterministic and model-free: normalize both texts to word tokens, compare
    with difflib, and require the two turns to have started within
    ECHO_TIME_WINDOW_SECONDS of each other. Only the MIC copy is ever dropped,
    because speaker-into-microphone is the leak direction that actually happens —
    the system tap hears only the far end.

    The comparison window is SYMMETRIC — earlier and later system turns both
    count. It has to be: the two tracks carry different start_offsets, and on the
    real engine the microphone's echoed copy routinely timestamps ~1s EARLIER
    than the system audio it is echoing, so a look-backwards-only pass catches
    almost nothing.

    Order is preserved; the function is a pure filter over its input list.
    """
    system_turns = [
        (float(turn.get("start") or 0.0), normalize_spoken_text(turn.get("text", "")))
        for turn in turns
        if turn.get("track") == TRACK_SYSTEM and (turn.get("text") or "").strip()
    ]

    def echoes_system_audio(start: float, text: str) -> bool:
        nearby = [
            other_text
            for other_start, other_text in system_turns
            if abs(other_start - start) <= ECHO_TIME_WINDOW_SECONDS
        ]
        for other_text in nearby[:ECHO_LOOKBACK_TURNS]:
            if SequenceMatcher(None, other_text, text).ratio() >= ECHO_SIMILARITY_THRESHOLD:
                return True
        return False

    kept: list[dict] = []
    for turn in turns:
        text = normalize_spoken_text(turn.get("text", ""))
        if turn.get("track") == TRACK_MICROPHONE and text:
            if echoes_system_audio(float(turn.get("start") or 0.0), text):
                continue
        kept.append(turn)
    return kept


# --- the fast path: live captions -------------------------------------------


def load_live_snapshot(
    meeting_id: str = "",
    *,
    since_sequence: int = 0,
    suppress_echo: bool = True,
    payload: dict | None = None,
) -> tuple[dict, list[dict]]:
    """The live caption snapshot as (meta, segments). drift read_transcript shape.

    `payload` lets tests inject a canned /api/live document and exercise exactly
    the same normalization the network path uses — the transport swap rule applied
    to reading, not just to writing.

    Returns ({...}, []) rather than raising when live captions are disabled, the
    engine is down, or the buffer is empty. The caller decides what to do with
    nothing; this module never decides for it.
    """
    document = payload if payload is not None else fetch_live_turns(since_sequence)
    enabled = bool(document.get("enabled"))
    raw_turns = document.get("turns") or []
    turns = [turn for turn in raw_turns if isinstance(turn, dict) and (turn.get("text") or "").strip()]
    turns.sort(key=lambda turn: (float(turn.get("start") or 0.0), int(turn.get("seq") or 0)))
    if suppress_echo:
        kept = collapse_echoed_turns(turns)
    else:
        kept = turns

    segments = [
        build_segment(
            f"{LIVE_SEGMENT_PREFIX}{turn.get('seq', index)}",
            turn.get("who") or "Unknown",
            turn.get("text", ""),
            track=turn.get("track", ""),
            start=turn.get("start") or 0.0,
            end=turn.get("end") or 0.0,
            source=SOURCE_LIVE,
        )
        for index, turn in enumerate(kept)
    ]

    meta = {
        "meeting_id": meeting_id,
        "title": read_meeting_title(meeting_id) if meeting_id else "Live meeting",
        "date": _today_iso_date(),
        "source": SOURCE_LIVE,
        "enabled": enabled,
        "segment_count": len(segments),
        "echoes_removed": len(turns) - len(kept),
        "sequence": int(document.get("seq") or 0),
        "dropped": int(document.get("dropped") or 0),
        "speakers": sorted({segment["speaker"] for segment in segments}),
    }
    return meta, segments


def read_live_segments(meeting_id: str = "") -> list[dict]:
    """The live snapshot, normalized to segments for extraction. Fast path."""
    _meta, segments = load_live_snapshot(meeting_id)
    return segments


# --- the reconcile path: meeting.json ---------------------------------------

# The file whose presence makes a folder a recording rather than just a folder.
MEETING_JSON_NAME = "meeting.json"


def is_transcript_ready(status_payload: dict, meeting_id: str) -> bool:
    """True when jobs[meeting_id].state == "done" in a /api/status payload.

    Pure over an already-fetched payload so the watcher can call it without a
    second HTTP round trip.
    """
    jobs = (status_payload or {}).get("jobs") or {}
    job = jobs.get(meeting_id) or {}
    return isinstance(job, dict) and job.get("state") == "done"


def resolve_recording_directory(meeting_id: str) -> Path | None:
    """Find the recording folder for `meeting_id` by scanning for the " — <id>" suffix.

    The folder is named "<Safe Title> — <YYYYMMDD-HHMMSS>" and RENAMES ITSELF when
    the meeting title changes, so never cache the path — re-resolve every time.
    Prefers a folder that actually contains meeting.json; falls back to a plain
    id-suffix match in case a future title contains a different dash character.
    Returns None when nothing matches.
    """
    if not meeting_id:
        return None
    root = config.RECORDINGS_DIR
    try:
        candidates = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return None

    exact = [path for path in candidates if path.name.endswith(f"{RECORDING_ID_SEPARATOR}{meeting_id}")]
    loose = [path for path in candidates if path.name.endswith(meeting_id)]
    for group in (exact, loose):
        with_json = [path for path in group if (path / "meeting.json").exists()]
        chosen = with_json or group
        if chosen:
            # Deterministic when several folders somehow share a suffix.
            return sorted(chosen, key=lambda path: path.name)[0]
    return None


def read_meeting_json(meeting_id: str) -> dict:
    """Parse meeting.json for `meeting_id`. {} when absent or unreadable.

    Keys: id, title, created, mode, status, tracks, duration, speakers, turns,
    stats, languages, processing, summary.
      status   — "recording" -> "processing" -> "done" | "error"
      turns    — [{speaker: "you"|"s1"|"s2", track, start, end, text}]
      speakers — {"you": "You", "s1": "Travis"}; real names sometimes resolved
      summary  — auto-generated, may exist later or never. BONUS ONLY. Never
                 depend on it, never block on it.

    Writes are atomic (os.replace), so a plain read is safe; a JSONDecodeError
    still returns {} rather than raising, because a demo must degrade.
    """
    directory = resolve_recording_directory(meeting_id)
    if directory is None:
        return {}
    return read_meeting_json_at(directory)


def read_meeting_json_at(directory: Path) -> dict:
    """Parse meeting.json inside an already-resolved recording directory."""
    try:
        document = json.loads((Path(directory) / "meeting.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def load_meeting(recording_directory_or_id: str | Path) -> tuple[dict, list[dict]]:
    """The full transcript as (meta, segments). Accepts a meeting id or a folder path.

    Speaker keys ("you", "s1") are mapped through meeting.speakers to display
    names; an unmapped key falls back to a readable "Speaker s3" rather than
    leaking a database key onto a card.

    Returns ({...}, []) when the meeting cannot be read at all.
    """
    target = Path(recording_directory_or_id)
    if target.is_dir():
        directory: Path | None = target
        document = read_meeting_json_at(target)
        meeting_id = document.get("id") or _meeting_id_from_directory_name(target.name)
    else:
        meeting_id = str(recording_directory_or_id)
        directory = resolve_recording_directory(meeting_id)
        document = read_meeting_json_at(directory) if directory else {}
        meeting_id = document.get("id") or meeting_id

    speakers = document.get("speakers") or {}
    segments: list[dict] = []
    for index, turn in enumerate(document.get("turns") or []):
        if not isinstance(turn, dict):
            continue
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        speaker_key = turn.get("speaker") or "unknown"
        segments.append(
            build_segment(
                f"{FINAL_SEGMENT_PREFIX}{meeting_id}-t{index}",
                speakers.get(speaker_key) or f"Speaker {speaker_key}",
                text,
                track=turn.get("track", ""),
                start=turn.get("start") or 0.0,
                end=turn.get("end") or 0.0,
                source=SOURCE_FINAL,
            )
        )

    meta = {
        "meeting_id": meeting_id,
        "title": document.get("title") or _title_from_directory_name(directory),
        "date": _date_from_created(document.get("created")),
        "source": SOURCE_FINAL,
        "status": document.get("status", ""),
        "duration": document.get("duration", 0.0),
        "segment_count": len(segments),
        "speakers": sorted(set(speakers.values())) if speakers else [],
        "directory": str(directory) if directory else "",
        "has_summary": bool(document.get("summary")),
    }
    return meta, segments


def read_final_segments(meeting_id: str) -> list[dict]:
    """The full transcript, normalized to segments. Reconcile path."""
    _meta, segments = load_meeting(meeting_id)
    return segments


def read_meeting_title(meeting_id: str) -> str:
    """Display title for the board header. meeting.json title -> folder name -> id."""
    if not meeting_id:
        return ""
    document = read_meeting_json(meeting_id)
    title = (document.get("title") or "").strip()
    if title:
        return title
    directory = resolve_recording_directory(meeting_id)
    folder_title = _title_from_directory_name(directory)
    return folder_title or meeting_id


def list_recent_meeting_ids(limit: int = 10) -> list[str]:
    """Most recent meeting ids on disk, newest first. For --replay and smoke tests."""
    try:
        directories = [path for path in config.RECORDINGS_DIR.iterdir() if path.is_dir()]
    except OSError:
        return []
    # Sort by the id, not the folder name: the name LEADS with the title, so a
    # name sort returns whatever meeting happens to start with 'z'. The id is a
    # YYYYMMDD-HHMMSS stamp, which sorts chronologically as a plain string.
    identifiers = [
        _meeting_id_from_directory_name(path.name)
        for path in directories
        if (path / "meeting.json").exists()
    ]
    return sorted({identifier for identifier in identifiers if identifier}, reverse=True)[:limit]


# --- small private helpers --------------------------------------------------


def _meeting_id_from_directory_name(name: str) -> str:
    if RECORDING_ID_SEPARATOR in name:
        return name.rsplit(RECORDING_ID_SEPARATOR, 1)[-1].strip()
    return name.strip()


def _title_from_directory_name(directory: Path | None) -> str:
    if directory is None:
        return ""
    name = directory.name
    if RECORDING_ID_SEPARATOR in name:
        return name.rsplit(RECORDING_ID_SEPARATOR, 1)[0].strip()
    return name.strip()


def _date_from_created(created: str | None) -> str:
    """'2026-08-09T14:38:10' -> '2026-08-09'. Empty string when absent."""
    if not created:
        return ""
    return str(created).split("T", 1)[0]


def _today_iso_date() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).date().isoformat()


def describe_source() -> dict:
    """Where this module will read from, for the board header and startup logs."""
    return {
        "engine": config.meetingscribe_base_url(),
        "endpoints": list(READ_ONLY_ENDPOINTS),
        "recordings_dir": str(config.RECORDINGS_DIR),
        "access": "read-only",
        "reachable": is_engine_reachable(),
    }


if __name__ == "__main__":
    print(json.dumps(describe_source(), indent=2))
    status = fetch_engine_status()
    print("jobs:", json.dumps(status.get("jobs", {}), indent=2))
    live_meta, live_segments = load_live_snapshot()
    print("live:", json.dumps(live_meta, indent=2))
    for segment in live_segments[:3]:
        print("  ", segment["segment_id"], segment["ts"], segment["speaker"], segment["text"][:70])
    recent = list_recent_meeting_ids(3)
    print("recent meetings:", recent)
    if recent:
        final_meta, final_segments = load_meeting(recent[0])
        print("final:", json.dumps(final_meta, indent=2))
        for segment in final_segments[:3]:
            print("  ", segment["segment_id"], segment["ts"], segment["speaker"], segment["text"][:70])
