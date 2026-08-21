"""The Meetings tab — MeetingScribe's library, transcript and live capture, in Adjourn's skin.

WHAT THIS IS. Adjourn's board answers "what did the meeting cause?". This
blueprint answers the question that comes before it: "what was the meeting?".
Everything here is a VIEW over the MeetingScribe engine on 127.0.0.1:5005 —
this module owns no state, writes no file, and holds no cache. Reload the page
and the truth is re-derived from the engine, which is the only reason the live
view can survive a refresh mid-recording.

THE THREE VIEWS
    /meetings/            library — every recording, searchable
    /meetings/<id>        detail  — screenplay transcript, summary, stats
    /meetings/live        live    — start/stop, captions, meters, timer

MOUNTING. `build_meetings_blueprint()` returns a Blueprint with
url_prefix="/meetings" that Stage 2 registers on the board app; nothing in
board_server.py has to change for it to work. Until then
`python -m adjourn.meetings_ui` serves the same blueprint alone on 5118.

TALKING TO THE ENGINE. Every call goes through EngineClient, whose two
allowlists (`READ_ENDPOINTS`, `WRITE_ENDPOINTS`) are the whole surface this
module is permitted to touch. It sends NO Origin header: the engine is
loopback-guarded and rejects cross-origin writes, and a browser cannot reach
5005 from a page served on 5117/5118 anyway. That is precisely why the
start/stop controls are PROXIED through this server rather than fetched from
the browser — same-origin to us, header-less loopback to the engine.

DEGRADING. The engine is started by hand and may be down when the page is
opened. Reads fall back to reading ~/.meetingscribe/recordings/*/meeting.json
directly (read-only, never written), and every view renders an honest
"engine offline" state rather than an error page. Writes never fall back —
a Start button that pretends to have started a recording is the one failure
this module must not have.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import requests
from flask import (
    Blueprint,
    Flask,
    get_template_attribute,
    jsonify,
    render_template,
    request,
)

from . import config

# --- constants --------------------------------------------------------------

#: Standalone port for tonight's testing. The board owns 5117 and this must
#: never collide with it — Stage 2 mounts the blueprint and this port retires.
MEETINGS_PORT = 5118
MEETINGS_HOST = "127.0.0.1"

HTTP_TIMEOUT_SECONDS = 4
#: Start/stop can block on the audio device coming up or the WAVs closing.
WRITE_TIMEOUT_SECONDS = 20

#: The engine reads this module allows itself. Anything not here is not called.
READ_ENDPOINTS = (
    "/api/meetings",
    "/api/record/status",
    "/api/live",
    "/api/status",
)
#: The engine writes this module allows itself. Exactly two, both user-pressed.
WRITE_ENDPOINTS = (
    "/api/record/start",
    "/api/record/stop",
)

#: No Origin. See the module docstring — spelled out so nobody adds one later.
ENGINE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "adjourn-meetings/1.0",
}

#: /api/meetings is paged and clamps the limit it was asked for. We page until
#: has_more is false so the library is the WHOLE library, not the first screen —
#: capped so a pathological engine cannot spin us forever.
LIBRARY_PAGE_SIZE = 200
MAX_LIBRARY_PAGES = 25

#: Folder naming: "<Safe Title> — <YYYYMMDD-HHMMSS>". Matches meetingscribe_source.
RECORDING_ID_SEPARATOR = " — "

#: Speaker colours for the screenplay, assigned by order of first appearance so
#: the same person keeps one colour down the page. Drawn from the board's own
#: kind accents — this is the same palette the cards use, reused for people.
SPEAKER_ACCENTS = (
    "#4493f8",  # blue
    "#2eb67d",  # green
    "#e8a33d",  # amber
    "#a371f7",  # violet
    "#d6c8a6",  # bone
    "#f2585b",  # red
    "#8b93e8",  # periwinkle
)

#: Status -> the chip a library row wears. Keys are the engine's own vocabulary
#: ("recording" -> "processing" -> "done" | "error").
STATUS_LABELS = {
    "recording": "RECORDING",
    "processing": "PROCESSING",
    "done": "DONE",
    "error": "ERROR",
}

#: The quiet line under the masthead. Adjourn's voice: descriptive, not sold.
LIBRARY_LEDE = "Every meeting this machine heard, and what it made of them."
OFFLINE_LINE = "The MeetingScribe engine is not answering on {base}. Showing what is on disk."
EMPTY_LIBRARY_LINE = "No recordings yet."
EMPTY_SEARCH_LINE = "Nothing matches that."


# --- the engine client ------------------------------------------------------


class EngineClient:
    """Loopback HTTP to the MeetingScribe engine, allowlisted both ways.

    Every method returns `(payload, status)`:
      * status 200..599  — the engine answered; payload is its decoded JSON
                           (or None when the body was not JSON).
      * status 0         — the engine did not answer at all. The distinction
                           matters: 0 means "fall back to disk / say offline",
                           while 409 means "the engine said no" and must be
                           shown to the user verbatim.

    Nothing here raises for a transport error. A page that cannot reach the
    engine still has to render.
    """

    def __init__(self, base_url: str | None = None, timeout: int = HTTP_TIMEOUT_SECONDS):
        self._base_url = (base_url or config.meetingscribe_base_url()).rstrip("/")
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    def get(self, endpoint: str, params: dict | None = None) -> tuple[object, int]:
        if endpoint not in READ_ENDPOINTS:
            raise ValueError(f"{endpoint!r} is not an allowlisted engine read")
        return self._call("GET", endpoint, params=params, timeout=self._timeout)

    def post(self, endpoint: str, payload: dict | None = None) -> tuple[object, int]:
        if endpoint not in WRITE_ENDPOINTS:
            raise ValueError(f"{endpoint!r} is not an allowlisted engine write")
        return self._call("POST", endpoint, json_body=payload or {},
                          timeout=WRITE_TIMEOUT_SECONDS)

    def meeting(self, meeting_id: str) -> tuple[object, int]:
        """GET /api/meetings/<id>. Built here rather than allowlisted as a
        literal because the id is a path segment; the id is validated first."""
        if not is_meeting_id(meeting_id):
            raise ValueError(f"{meeting_id!r} is not a meeting id")
        return self._call("GET", f"/api/meetings/{meeting_id}", timeout=self._timeout)

    def _call(self, method, endpoint, params=None, json_body=None, timeout=None):
        try:
            response = requests.request(
                method,
                f"{self._base_url}{endpoint}",
                params=params or None,
                json=json_body,
                headers=ENGINE_HEADERS,
                timeout=timeout or self._timeout,
            )
        except requests.RequestException:
            return None, 0
        try:
            return response.json(), response.status_code
        except ValueError:
            return None, response.status_code


def is_meeting_id(value: str) -> bool:
    """True for the engine's own id shape, "YYYYMMDD-HHMMSS".

    The gate on every path that interpolates an id into a URL or a filesystem
    scan. Deliberately stricter than "contains no slash": the id format is
    fixed and known, so anything else is a bug or an attack and both should
    stop here rather than at the engine.
    """
    if not isinstance(value, str) or len(value) != 15 or value[8] != "-":
        return False
    return value[:8].isdigit() and value[9:].isdigit()


# --- formatting -------------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    """3612 -> "1h 0m", 2472 -> "41m", 45 -> "45s". The library row's duration.

    Compact on purpose: the row is scanned, not read, and "41m" lands from
    1.5m where "41 minutes 12 seconds" is a paragraph.
    """
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "—"
    if total <= 0:
        return "—"
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    return f"{total // 3600}h {(total % 3600) // 60}m"


def format_clock(seconds: float | None) -> str:
    """Seconds into the meeting -> "MM:SS", or "H:MM:SS" past the hour.

    The transcript gutter and the live timer both use this, so a caption at
    12:04 and the transcript line it becomes read the same.
    """
    try:
        total = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60:02d}:{total % 60:02d}"


def format_created(created: str | None) -> str:
    """"2026-08-19T15:54:01" -> "19 Aug 2026, 15:54". Falls back to the raw string."""
    parsed = parse_created(created)
    if parsed is None:
        return created or "—"
    return f"{parsed.day} {parsed:%b %Y}, {parsed:%H:%M}"


def parse_created(created: str | None) -> datetime | None:
    if not created:
        return None
    try:
        return datetime.fromisoformat(str(created))
    except (TypeError, ValueError):
        return None


def speaker_count(meta: dict) -> int:
    """How many people. The list gives an int, the detail gives a name map."""
    speakers = meta.get("speakers")
    if isinstance(speakers, int):
        return max(0, speakers)
    if isinstance(speakers, dict):
        return len(speakers)
    return 0


def status_of(meta: dict) -> str:
    """The engine's status, normalized to a key STATUS_LABELS knows."""
    status = str(meta.get("status") or "").strip().lower()
    return status if status in STATUS_LABELS else "done"


# --- view models ------------------------------------------------------------


def build_library_row(meta: dict) -> dict:
    """One /api/meetings item -> the fields the row macro draws.

    Every field is always present, because the macro must never branch on
    "was this key in the payload" — the engine documents the same discipline
    for its own rows and this is the client half of it.

    The `warnings` split is the one piece of judgement here: the engine returns
    its warning sentences verbatim and unclassified, and says outright that
    classifying them is the client's job. A capture warning (something went
    wrong with the AUDIO) earns a visible flag on the row; everything else is
    counted and left for the detail page.
    """
    warnings = [w for w in (meta.get("warnings") or []) if isinstance(w, str)]
    return {
        "id": str(meta.get("id") or ""),
        "title": (meta.get("title") or "Untitled meeting").strip(),
        "created": meta.get("created") or "",
        "created_label": format_created(meta.get("created")),
        "duration_label": format_duration(meta.get("duration")),
        "duration_seconds": float(meta.get("duration") or 0),
        "status": status_of(meta),
        "status_label": STATUS_LABELS.get(status_of(meta), "DONE"),
        "speakers": speaker_count(meta),
        "brief": (meta.get("brief") or "").strip(),
        "has_summary": bool(meta.get("has_summary")),
        "has_transcript": bool(meta.get("has_transcript")),
        "has_notes": bool(meta.get("has_notes")),
        "warnings": warnings,
        "warning_count": len(warnings),
    }


def build_library(client: EngineClient, query: str = "") -> dict:
    """The whole library view model. Engine first, disk second, honest either way.

    `source` is "engine" or "disk" and the page says which out loud — a library
    read off disk can be missing the brief and the badges, and a viewer who
    cannot tell why is being misled.
    """
    items, reachable = fetch_all_meetings(client, query)
    source = "engine"
    if not reachable:
        items = read_meetings_from_disk(query)
        source = "disk"
    rows = [build_library_row(item) for item in items]
    return {
        "rows": rows,
        "query": query,
        "total": len(rows),
        "source": source,
        "online": reachable,
        "base_url": client.base_url,
        "total_seconds": sum(row["duration_seconds"] for row in rows),
        "recording_now": any(row["status"] == "recording" for row in rows),
    }


def fetch_all_meetings(client: EngineClient, query: str = "") -> tuple[list, bool]:
    """Page /api/meetings until has_more is false. -> (items, engine_answered).

    The engine clamps the limit it was asked for and ECHOES the one it applied,
    and its docstring is explicit that a client pages until has_more is false
    or it is reading a partial library as a complete one. So: page. `q` goes to
    the engine rather than filtering the page we happen to hold, for the same
    reason — search has to reach meetings no page has been fetched for.
    """
    items: list = []
    offset = 0
    for _ in range(MAX_LIBRARY_PAGES):
        params = {"limit": LIBRARY_PAGE_SIZE, "offset": offset}
        if query:
            params["q"] = query
        payload, status = client.get("/api/meetings", params)
        if status == 0:
            return [], False          # engine down — the caller falls back to disk
        if status != 200 or not isinstance(payload, dict):
            return items, True        # engine answered badly; keep what we have
        page = payload.get("items")
        items.extend(page if isinstance(page, list) else [])
        if not payload.get("has_more"):
            break
        next_offset = payload.get("next_offset")
        if not isinstance(next_offset, int) or next_offset <= offset:
            break                     # no forward progress; stop rather than loop
        offset = next_offset
    return items, True


def read_meetings_from_disk(query: str = "") -> list[dict]:
    """The offline library: ~/.meetingscribe/recordings/*/meeting.json, READ ONLY.

    Nothing here writes, creates, or renames. The rows are thinner than the
    engine's — there is no `brief` without running the summary reader — so the
    page marks the source as disk and the row simply has less to say.

    `turns` is dropped the moment the document is parsed: some of these files
    are megabytes of transcript and the library needs none of it.
    """
    root = Path(config.RECORDINGS_DIR)
    try:
        folders = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return []

    rows = []
    for folder in folders:
        meeting_id = folder.name.rsplit(RECORDING_ID_SEPARATOR, 1)[-1].strip()
        if not is_meeting_id(meeting_id):
            continue
        meta = read_meeting_json_at(folder)
        if not meta:
            continue
        turns = meta.get("turns")
        summary = meta.get("summary")
        rows.append({
            "id": meta.get("id") or meeting_id,
            "title": meta.get("title") or folder.name,
            "created": meta.get("created") or "",
            "duration": meta.get("duration") or 0,
            "status": meta.get("status") or "done",
            "speakers": speaker_count(meta),
            "brief": (summary or {}).get("headline", "") if isinstance(summary, dict) else "",
            "has_summary": isinstance(summary, dict) and bool(summary),
            "has_transcript": bool(turns),
            "has_notes": False,
            "warnings": meta.get("warnings") or [],
        })

    if query:
        needle = query.strip().lower()
        rows = [row for row in rows if needle in str(row["title"]).lower()
                or needle in str(row["brief"]).lower()]
    # Newest first, matching the engine's ordering. The id sorts as the date.
    rows.sort(key=lambda row: str(row["id"]), reverse=True)
    return rows


def read_meeting_json_at(folder: Path) -> dict:
    """Parse one meeting.json. {} when absent, unreadable, or not an object.

    Engine writes are atomic (os.replace), so a plain read never sees a torn
    file; a JSONDecodeError still degrades to {} rather than raising, because
    a demo must not die on one bad folder.
    """
    try:
        document = json.loads((folder / "meeting.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def resolve_recording_directory(meeting_id: str) -> Path | None:
    """The folder for `meeting_id`. Re-resolved every time — it renames itself.

    The folder is "<Safe Title> — <YYYYMMDD-HHMMSS>" and MeetingScribe renames
    it when the title changes, so a cached path goes stale mid-session.
    """
    if not is_meeting_id(meeting_id):
        return None
    try:
        folders = [path for path in Path(config.RECORDINGS_DIR).iterdir() if path.is_dir()]
    except OSError:
        return None
    exact = [p for p in folders if p.name.endswith(f"{RECORDING_ID_SEPARATOR}{meeting_id}")]
    loose = [p for p in folders if p.name.endswith(meeting_id)]
    for group in (exact, loose):
        with_json = [p for p in group if (p / "meeting.json").exists()]
        chosen = with_json or group
        if chosen:
            return sorted(chosen, key=lambda path: path.name)[0]
    return None


def build_screenplay(meta: dict) -> list[dict]:
    """meeting.json turns -> screenplay blocks, speaker-mapped and colour-stable.

    A screenplay because that is what a two-track meeting transcript IS: named
    cues over dialogue, in order, with the clock in the gutter. The alternative
    — a wall of "Speaker 1:" prefixes — is what every other tool ships and it
    is unreadable at length.

    Two things happen here that the raw turns do not give you:

      * The speaker KEY ("you", "s1") is mapped through meeting.json's
        `speakers` map to the name a human recognises ("Travis"). When the map
        has no entry, "s2" becomes "Speaker 2" rather than leaking the key.
      * Each speaker gets a colour, assigned by ORDER OF FIRST APPEARANCE and
        held for the whole document. Assigning by dict order instead would
        recolour the page when the engine reclusters and reorders its map.

    Consecutive turns by one speaker are merged into a single block, so a
    person who paused for breath does not get a second name cue. The clock of
    the block is the start of its first turn.
    """
    turns = meta.get("turns")
    if not isinstance(turns, list):
        return []
    names = meta.get("speakers") if isinstance(meta.get("speakers"), dict) else {}

    order: list[str] = []
    blocks: list[dict] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        key = str(turn.get("speaker") or "unknown")
        if key not in order:
            order.append(key)
        if blocks and blocks[-1]["key"] == key:
            blocks[-1]["text"] = f"{blocks[-1]['text']} {text}"
            blocks[-1]["end"] = float(turn.get("end") or blocks[-1]["end"])
            continue
        blocks.append({
            "key": key,
            "name": speaker_name(names, key),
            "accent": SPEAKER_ACCENTS[order.index(key) % len(SPEAKER_ACCENTS)],
            "track": str(turn.get("track") or ""),
            "start": float(turn.get("start") or 0),
            "end": float(turn.get("end") or 0),
            "clock": format_clock(turn.get("start")),
            "text": text,
        })
    return blocks


def speaker_name(names: dict, key: str) -> str:
    """"s1" -> "Travis", or "Speaker 1" when the map has no name for it."""
    mapped = (names or {}).get(key)
    if isinstance(mapped, str) and mapped.strip():
        return mapped.strip()
    if key == "you":
        return "You"
    if key.startswith("s") and key[1:].isdigit():
        return f"Speaker {key[1:]}"
    return key.replace("_", " ").title() or "Unknown"


def build_summary(meta: dict) -> dict | None:
    """The summary section, or None when the meeting has none.

    Summaries are a BONUS — a meeting may never get one, and the detail page
    must be complete without it. Every list is normalized to a list of strings
    here so the template never has to ask what shape a key came back in;
    action items in particular arrive as either strings or {text, owner} dicts
    depending on which engine wrote them.
    """
    summary = meta.get("summary")
    if not isinstance(summary, dict) or not summary:
        return None
    return {
        "headline": str(summary.get("headline") or "").strip(),
        "tldr": str(summary.get("tldr") or "").strip(),
        "key_points": as_lines(summary.get("key_points")),
        "decisions": as_lines(summary.get("decisions")),
        "action_items": as_lines(summary.get("action_items")),
        "open_questions": as_lines(summary.get("open_questions")),
        "follow_ups": as_lines(summary.get("follow_ups")),
        "engine": str(summary.get("engine") or "").strip(),
    }


def as_lines(value) -> list[str]:
    """Whatever the summary put here -> a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    lines = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("item") or item.get("title") or "").strip()
            owner = str(item.get("owner") or item.get("assignee") or "").strip()
            if text and owner:
                text = f"{text} — {owner}"
        else:
            text = ""
        if text:
            lines.append(text)
    return lines


def build_speaker_stats(meta: dict) -> list[dict]:
    """Per-speaker stats, loudest first, with the share already a percentage.

    `share` arrives as a fraction (0.581) and every consumer wants a percent,
    so it is converted once here rather than in the template.
    """
    stats = meta.get("stats")
    if not isinstance(stats, dict):
        return []
    per_speaker = stats.get("per_speaker")
    if not isinstance(per_speaker, dict):
        return []
    names = meta.get("speakers") if isinstance(meta.get("speakers"), dict) else {}
    order = list(per_speaker.keys())

    rows = []
    for key, value in per_speaker.items():
        if not isinstance(value, dict):
            continue
        rows.append({
            "key": key,
            "name": speaker_name(names, key),
            "accent": SPEAKER_ACCENTS[order.index(key) % len(SPEAKER_ACCENTS)],
            "share_pct": round(float(value.get("share") or 0) * 100),
            "seconds": float(value.get("seconds") or 0),
            "duration_label": format_duration(value.get("seconds")),
            "words": int(value.get("words") or 0),
            "turns": int(value.get("turns") or 0),
            "questions": int(value.get("questions") or 0),
            "wpm": int(value.get("wpm") or 0),
            "filler_total": int(value.get("filler_total") or 0),
        })
    rows.sort(key=lambda row: row["seconds"], reverse=True)
    return rows


def build_meeting_detail(meta: dict) -> dict:
    """One full meeting.json -> everything the detail template draws."""
    tracks = meta.get("tracks") if isinstance(meta.get("tracks"), dict) else {}
    processing = meta.get("processing") if isinstance(meta.get("processing"), dict) else {}
    blocks = build_screenplay(meta)
    return {
        "id": str(meta.get("id") or ""),
        "title": (meta.get("title") or "Untitled meeting").strip(),
        "created_label": format_created(meta.get("created")),
        "created": meta.get("created") or "",
        "duration_label": format_duration(meta.get("duration")),
        "duration_clock": format_clock(meta.get("duration")),
        "status": status_of(meta),
        "status_label": STATUS_LABELS.get(status_of(meta), "DONE"),
        "mode": str(meta.get("mode") or ""),
        "speakers": speaker_count(meta),
        "screenplay": blocks,
        "turn_count": len(blocks),
        "word_count": sum(len(block["text"].split()) for block in blocks),
        "summary": build_summary(meta),
        "speaker_stats": build_speaker_stats(meta),
        "warnings": [w for w in (meta.get("warnings") or []) if isinstance(w, str)],
        "tracks": [
            {
                "name": name,
                "device": str(value.get("device") or "—"),
                "rate": int(value.get("rate") or 0),
                "codec": str(value.get("codec") or "—"),
                "duration_label": format_duration(value.get("seconds")),
            }
            for name, value in tracks.items()
            if isinstance(value, dict)
        ],
        "engine_model": str(processing.get("model") or ""),
        "engine_backend": str(processing.get("backend") or ""),
        #: The cross-link into the board. A STUB by contract: this lane does not
        #: touch board_server.py, so the route may not exist yet and the link is
        #: rendered as a plain href for Stage 2 to light up.
        "follow_through_url": f"/meeting/{meta.get('id') or ''}",
    }


def build_live_state(client: EngineClient) -> dict:
    """The live view's server-rendered starting state, re-derived from the engine.

    THIS IS THE RELOAD CONTRACT. The page holds no recording state of its own —
    press Record, reload, and the timer must still be counting. It can be,
    because the engine knows: /api/record/status carries `recording` and
    `elapsed`, so the fresh page starts from the truth rather than from zero
    and a guess. Nothing is persisted in a cookie or localStorage, because
    either one can disagree with the recorder and a UI that says "recording"
    over a stopped engine is worse than one that says nothing.
    """
    payload, status = client.get("/api/record/status")
    online = status == 200 and isinstance(payload, dict)
    snapshot = payload if online else {}
    levels = snapshot.get("levels") if isinstance(snapshot.get("levels"), dict) else {}
    disk = snapshot.get("disk") if isinstance(snapshot.get("disk"), dict) else {}
    return {
        "online": online,
        "base_url": client.base_url,
        "recording": bool(snapshot.get("recording")),
        "elapsed": float(snapshot.get("elapsed") or 0),
        "elapsed_clock": format_clock(snapshot.get("elapsed")),
        "mic_level": float(levels.get("mic") or 0),
        "system_level": float(levels.get("system") or 0),
        "disk_state": str(disk.get("state") or ""),
        "disk_message": disk.get("message") or "",
    }


# --- the blueprint ----------------------------------------------------------


def build_meetings_blueprint(client: EngineClient | None = None) -> Blueprint:
    """The Meetings tab, ready to register on any Flask app.

    `client` is injectable so tests drive every route against a fake engine
    without a socket — which is the only way to test the start/stop handlers
    at all, given the real engine's trigger bus is not ours to press.

    The blueprint carries its own template and static folders, so mounting it
    costs the host app one `register_blueprint` line and no configuration. Its
    static files are served under /meetings/assets/ rather than /static/ so
    they cannot collide with the board's own.
    """
    engine = client or EngineClient()
    blueprint = Blueprint(
        "meetings",
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/assets",
        url_prefix="/meetings",
    )

    # --- pages ---

    @blueprint.get("/")
    def library():
        query = (request.args.get("q") or "").strip()
        return render_template(
            "meetings.html",
            page="library",
            current_tab="meetings",
            library=build_library(engine, query),
            live=None,
        )

    @blueprint.get("/live")
    def live_view():
        return render_template(
            "meetings.html",
            page="live",
            current_tab="live",
            library=None,
            live=build_live_state(engine),
        )

    @blueprint.get("/<meeting_id>")
    def detail(meeting_id: str):
        if not is_meeting_id(meeting_id):
            return render_template(
                "meeting_detail.html",
                page="detail",
                current_tab="meetings",
                meeting=None,
                not_found=meeting_id,
            ), 404
        meta = load_meeting(engine, meeting_id)
        if not meta:
            return render_template(
                "meeting_detail.html",
                page="detail",
                current_tab="meetings",
                meeting=None,
                not_found=meeting_id,
            ), 404
        return render_template(
            "meeting_detail.html",
            page="detail",
            current_tab="meetings",
            meeting=build_meeting_detail(meta),
            not_found=None,
        )

    # --- fragments & proxies ---

    @blueprint.get("/api/rows")
    def rows_fragment():
        """Search results as the SAME HTML the page was served with.

        The board's rule, kept: there is one copy of the row markup and it is
        the Jinja macro. Search re-renders that macro server-side and swaps the
        HTML in, rather than keeping a second row renderer in JavaScript that
        drifts from the first one the moment either is edited.
        """
        query = (request.args.get("q") or "").strip()
        state = build_library(engine, query)
        macro = get_template_attribute("meetings.html", "meeting_rows")
        return jsonify({
            "html": macro(state),
            "total": state["total"],
            # The masthead's hours count describes the rows BELOW it, so it has
            # to narrow with the search. Sending it means the page never shows
            # "4 recordings · 12.6 hours" — a true number about a set that is
            # no longer on screen.
            "hours": round(state["total_seconds"] / 3600, 1),
            "online": state["online"],
            "source": state["source"],
            "query": query,
        })

    @blueprint.get("/api/record/status")
    def record_status():
        payload, status = engine.get("/api/record/status")
        return proxied(payload, status)

    @blueprint.get("/api/live")
    def live_stream():
        try:
            since = max(0, int(request.args.get("since", 0)))
        except (TypeError, ValueError):
            since = 0
        payload, status = engine.get("/api/live", {"since": since})
        return proxied(payload, status)

    @blueprint.post("/api/record/start")
    def record_start():
        """Proxy POST /api/record/start. The engine's refusal is passed through
        WHOLE — it ships both a human sentence (`error`) and a stable code
        (`reason`), and swallowing either is how a Record button ends up saying
        "something went wrong" over a message that named the missing device."""
        body = request.get_json(silent=True) or {}
        payload = {}
        expected = body.get("expected_speakers")
        if isinstance(expected, int) and expected > 0:
            payload["expected_speakers"] = expected
        result, status = engine.post("/api/record/start", payload)
        return proxied(result, status)

    @blueprint.post("/api/record/stop")
    def record_stop():
        result, status = engine.post("/api/record/stop", {})
        return proxied(result, status)

    return blueprint


def load_meeting(client: EngineClient, meeting_id: str) -> dict:
    """One meeting document: engine first, disk second, {} when neither has it."""
    payload, status = client.meeting(meeting_id)
    if status == 200 and isinstance(payload, dict) and payload:
        return payload
    folder = resolve_recording_directory(meeting_id)
    return read_meeting_json_at(folder) if folder is not None else {}


def proxied(payload, status: int):
    """Turn an EngineClient answer into a response for the browser.

    Status 0 — no answer at all — becomes 503 with a shape that matches the
    engine's own refusals ({error, reason}), so the JavaScript has exactly one
    error shape to handle whether the engine said no or was never there.
    """
    if status == 0:
        return jsonify({
            "error": "The MeetingScribe engine is not answering.",
            "reason": "engine_unreachable",
        }), 503
    if payload is None:
        return jsonify({"error": "The engine returned no JSON.",
                        "reason": "bad_response"}), status or 502
    return jsonify(payload), status


# --- standalone runner ------------------------------------------------------


def create_meetings_application(client: EngineClient | None = None) -> Flask:
    """The blueprint alone, on its own app. For tonight's testing and for tests.

    The app-level static folder is the package's own, at /static — the same
    path board_server serves it on — so the templates' `board.css` link
    resolves identically whether this app is serving them or the board is.
    """
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    # The macros sit above the doctype in meetings.html, exactly as they do in
    # board.html; without these the page ships a pile of blank lines first.
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    app.register_blueprint(build_meetings_blueprint(client))

    @app.get("/")
    def root():
        """Standalone convenience: / is the library. Mounted, the board owns /."""
        from flask import redirect
        return redirect("/meetings/")

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "blueprint": "meetings", "port": resolve_port()})

    return app


def resolve_port(explicit: int | None = None) -> int:
    """--port -> ADJOURN_MEETINGS_PORT -> 5118. Never the board's 5117 by default."""
    if explicit:
        return explicit
    override = os.environ.get("ADJOURN_MEETINGS_PORT", "").strip()
    if override.isdigit():
        return int(override)
    return MEETINGS_PORT


def main() -> None:
    parser = argparse.ArgumentParser(description="Adjourn meetings tab (standalone)")
    parser.add_argument("--port", type=int, default=None, help=f"default {MEETINGS_PORT}")
    parser.add_argument("--host", default=MEETINGS_HOST, help="default 127.0.0.1 (loopback only)")
    arguments = parser.parse_args()

    port = resolve_port(arguments.port)
    engine = EngineClient()
    print(f"[meetings] http://{arguments.host}:{port}/meetings/")
    print(f"[meetings] engine: {engine.base_url}")
    create_meetings_application(engine).run(
        host=arguments.host, port=port, debug=False, threaded=True
    )


if __name__ == "__main__":
    main()
