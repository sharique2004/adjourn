"""calendar_hold — block time for a deadline someone committed to. (Lane D)

Payload shape:
    {
      "title": "Hold: cache layer cutover",
      "deadline_text": "Friday",          # exactly as spoken
      "starts_at": "2026-08-22T15:00:00", # resolved local ISO (optional)
      "duration_minutes": 60,
      "notes": "...",                     # includes the verbatim quote
      "calendar_name": "Home",
      "alarm_minutes_before": 60,
      "human_preview": "Calendar hold Fri 3pm: cache layer cutover"
    }

Live transport: local-first, no cloud API. Every run writes an .ics file to
adjourn/state/holds/ (that file is real in both modes — it is the fallback
deliverable), and then hands the event to EventKit through the compiled Swift
helper in adjourn/bin/calendar_add.swift, so the hold lands in the user's own
Calendar.app with no OAuth and no network.

TWO GATES, not one:
  1. secrets_store.decide_mode(()) — no secrets are needed here, so this returns
     "live" unless ADJOURN_SIM=1.
  2. TCC. macOS may simply refuse calendar access to a headless process. We probe
     ONCE per run with `calendar_add probe` (which never prompts) and, if access
     is not granted, drop to sim with a note. We do NOT loop on TCC and we do NOT
     re-prompt per action — a permission dialog per deadline would be hostile,
     and a demo that hangs on a system dialog is worse than one that says "the
     .ics is on disk, double-click it".

DEADLINE RESOLUTION IS DETERMINISTIC AND LIVES HERE — but it is plain date
arithmetic, never a model, so the planner is free to import `resolve_deadline()`
to decide whether an action can be built at all. If a phrase cannot be resolved
deterministically the planner declines, and if one reaches us anyway the card
fails honestly rather than guessing a date.

Undo: remove the event by its eventIdentifier through the same helper, and delete
the .ics. Holds are created ATTENDEE-LESS on purpose — an attendee is an
invitation, an invitation is an irreversible message to a human, and undo would
then be a lie.

Secrets: none — this never leaves the machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .. import config, results, secrets_store

if TYPE_CHECKING:
    from ..planner import Action

ACTION_KIND = "calendar_hold"
REQUIRED_SECRETS: tuple[str, ...] = ()

DEFAULT_DURATION_MINUTES = 60
DEFAULT_CALENDAR_NAME = "Home"
DEFAULT_ALARM_MINUTES_BEFORE = 60

# When a phrase carries a date but no clock time.
DEFAULT_HOUR = 10
DEFAULT_MINUTE = 0

# How far ahead to push a hold whose stated time has already passed today.
PAST_DEADLINE_GRACE_MINUTES = 30

SWIFT_SOURCE_PATH = config.PACKAGE_ROOT / "bin" / "calendar_add.swift"
SWIFT_BINARY_PATH = config.PACKAGE_ROOT / "bin" / "calendar_add"
SWIFT_BUILD_TIMEOUT_SECONDS = 180
HELPER_PROBE_TIMEOUT_SECONDS = 20
# Generous: the very first `add` may be sitting on the macOS permission prompt.
HELPER_ADD_TIMEOUT_SECONDS = 150

EXIT_ACCESS_DENIED = 3


class CalendarAccessDenied(RuntimeError):
    """macOS refused calendar access. Not a bug — a mode. Caught, never raised out."""


def holds_directory() -> Path:
    """Where .ics files are written. Under state/, so the demo reset can clear it."""
    return config.state_directory() / "holds"


# --- the deterministic deadline parser --------------------------------------
# No model touches a date. Ever. These are tables and arithmetic, which is the
# whole reason the planner is allowed to call into this module.

WEEKDAY_NUMBERS: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

MONTH_NUMBERS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# Phrases that name a time of day without naming a clock.
TIME_OF_DAY_HOURS: dict[str, tuple[int, int]] = {
    "first thing": (9, 0),
    "morning": (9, 0),
    "midday": (12, 0),
    "noon": (12, 0),
    "lunch": (12, 30),
    "afternoon": (14, 0),
    "end of day": (17, 0),
    "eod": (17, 0),
    "cob": (17, 0),
    "close of business": (17, 0),
    "evening": (18, 0),
    "tonight": (20, 0),
    "night": (20, 0),
}

NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a": 1, "an": 1, "couple": 2, "few": 3,
}

# Spoken dates are spelled out, not typed. A transcript says "push it to the
# twenty-fifth", never "the 25th", so a parser that only understands digits
# declines every date anyone actually says out loud. Built as a table rather
# than written out by hand so the tens and the units cannot disagree.
_ORDINAL_UNITS: tuple[str, ...] = (
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth",
    "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
)


def _build_ordinal_day_words() -> dict[str, int]:
    """{"first": 1, ..., "twenty-fifth": 25, ..., "thirty-first": 31}. Pure."""
    words: dict[str, int] = {name: index for index, name in enumerate(_ORDINAL_UNITS, 1)}
    words["twentieth"] = 20
    words["thirtieth"] = 30
    for tens_name, tens_value in (("twenty", 20), ("thirty", 30)):
        for unit_index, unit_name in enumerate(_ORDINAL_UNITS[:9], 1):
            day = tens_value + unit_index
            if day <= 31:
                words[f"{tens_name}-{unit_name}"] = day
    return words


ORDINAL_DAY_WORDS: dict[str, int] = _build_ordinal_day_words()

# Longest first, so "twenty-fifth" wins over "fifth".
_ORDINAL_WORD_DAY = re.compile(
    r"\b(?:the\s+)?("
    + "|".join(sorted(ORDINAL_DAY_WORDS, key=len, reverse=True))
    + r")\b"
)

_CLOCK_WITH_MERIDIEM = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b")
_CLOCK_24_HOUR = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_ORDINAL_DAY = re.compile(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b")
_RELATIVE_COUNT = re.compile(
    r"\bin\s+(\d+|" + "|".join(NUMBER_WORDS) + r")\s+(hour|day|week|month)s?\b"
)
_MONTH_DAY = re.compile(
    r"\b(" + "|".join(MONTH_NUMBERS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
)


_SPOKEN_TENS_AND_UNIT = re.compile(
    r"\b(twenty|thirty)[\s-]+(" + "|".join(_ORDINAL_UNITS[:9]) + r")\b"
)


def normalize_deadline_text(text: str) -> str:
    """Lowercase, collapse whitespace, drop leading 'by/before/due'. Pure.

    Also hyphenates a spoken "twenty fifth" into "twenty-fifth", because ASR
    writes the two halves of a compound ordinal as separate words about half the
    time and the table only carries one spelling of each day.
    """
    cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
    cleaned = re.sub(r"^(by|before|due|due by|no later than|nlt)\s+", "", cleaned)
    return _SPOKEN_TENS_AND_UNIT.sub(lambda match: f"{match.group(1)}-{match.group(2)}", cleaned)


def parse_time_of_day(text: str) -> tuple[int, int] | None:
    """(hour, minute) from an explicit clock or a time-of-day word. None if absent."""
    meridiem = _CLOCK_WITH_MERIDIEM.search(text)
    if meridiem:
        hour = int(meridiem.group(1)) % 12
        minute = int(meridiem.group(2) or 0)
        if meridiem.group(3).replace(".", "").startswith("p"):
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    twenty_four = _CLOCK_24_HOUR.search(text)
    if twenty_four:
        hour, minute = int(twenty_four.group(1)), int(twenty_four.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    for phrase, clock in TIME_OF_DAY_HOURS.items():
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            return clock
    return None


def _weekday_offset(text: str, reference: datetime) -> int | None:
    """Days from `reference` to the named weekday, or None when none is named.

    TWO RULES, and they are different on purpose.

    BARE "friday" is the next Friday there is, and resolves to TODAY when today
    is Friday — people say "get it in by Friday" on a Friday and mean today.

    "NEXT friday" means the one in NEXT WEEK, counted from next Monday. It does
    NOT mean "the upcoming one, plus seven". That was the old rule and it was
    right only when the named day still lay ahead in the current week; said on
    Thursday 20 Aug 2026, "next Wednesday" landed on 2 Sep — a full week late,
    with the wrong date baked into the .ics, the dedup key, the board card and
    the recap. From Thu 20 Aug the answer is Wed 26 Aug; from Fri 21 Aug,
    "next Monday" is 24 Aug, "next Wednesday" is 26 Aug and "next Friday" is
    28 Aug. Anchoring to next week's Monday gives all four, and the bare-weekday
    branch above is untouched.

        >>> _weekday_offset("next wednesday", datetime(2026, 8, 20))   # a Thursday
        6
        >>> _weekday_offset("wednesday", datetime(2026, 8, 20))
        6
        >>> _weekday_offset("next monday", datetime(2026, 8, 21))      # a Friday
        3
    """
    for name, number in WEEKDAY_NUMBERS.items():
        if not re.search(rf"\b{name}\b", text):
            continue
        if re.search(rf"\bnext\s+{name}\b", text):
            days_to_next_monday = 7 - reference.weekday()
            return days_to_next_monday + number
        return (number - reference.weekday()) % 7
    return None


def _day_of_month_date(day: int, reference: datetime, month: int | None = None) -> datetime | None:
    """The next occurrence of a calendar day, rolling into the next month if needed."""
    if not 1 <= day <= 31:
        return None
    year, target_month = reference.year, month or reference.month
    for _ in range(14):  # at most a year of rolling; bounded on purpose
        try:
            candidate = reference.replace(
                year=year, month=target_month, day=day,
                hour=0, minute=0, second=0, microsecond=0,
            )
        except ValueError:  # e.g. the 31st of a 30-day month
            candidate = None
        if candidate is not None and candidate.date() >= reference.date():
            return candidate
        if month is not None and candidate is not None:
            return candidate  # an explicit month was named; trust it even if past
        target_month += 1
        if target_month > 12:
            target_month, year = 1, year + 1
    return None


def resolve_deadline(deadline_text: str, reference: datetime | None = None) -> datetime | None:
    """"Friday" -> a real local datetime. DETERMINISTIC — a table and arithmetic.

    Returns None when nothing in the phrase names a date, which is the planner's
    signal to decline the action rather than to invent a day.

        >>> monday = datetime(2026, 8, 17, 9, 0)          # a Monday
        >>> resolve_deadline("friday", monday).isoformat()
        '2026-08-21T10:00:00'
        >>> resolve_deadline("tomorrow at 3pm", monday).isoformat()
        '2026-08-18T15:00:00'
        >>> resolve_deadline("sometime soon", monday) is None
        True
    """
    reference = reference or datetime.now()
    text = normalize_deadline_text(deadline_text)
    if not text:
        return None

    clock = parse_time_of_day(text)
    midnight = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    resolved: datetime | None = None
    same_day_phrase = False

    iso = _ISO_DATE.search(text)
    month_day = _MONTH_DAY.search(text)
    slash = _SLASH_DATE.search(text)
    relative = _RELATIVE_COUNT.search(text)
    weekday_offset = _weekday_offset(text, reference)

    if iso:
        try:
            resolved = midnight.replace(
                year=int(iso.group(1)), month=int(iso.group(2)), day=int(iso.group(3))
            )
        except ValueError:
            resolved = None
    elif month_day:
        resolved = _day_of_month_date(
            int(month_day.group(2)), midnight, MONTH_NUMBERS[month_day.group(1)]
        )
    elif slash:
        try:
            year_text = slash.group(3)
            year = reference.year if not year_text else int(year_text)
            if year < 100:
                year += 2000
            resolved = midnight.replace(
                year=year, month=int(slash.group(1)), day=int(slash.group(2))
            )
        except ValueError:
            resolved = None
    elif relative:
        raw = relative.group(1)
        count = int(raw) if raw.isdigit() else NUMBER_WORDS[raw]
        unit = relative.group(2)
        if unit == "hour":
            return reference + timedelta(hours=count)
        if unit == "day":
            resolved = midnight + timedelta(days=count)
        elif unit == "week":
            resolved = midnight + timedelta(weeks=count)
        else:
            resolved = midnight + timedelta(days=30 * count)
    elif re.search(r"\bday after tomorrow\b", text):
        resolved = midnight + timedelta(days=2)
    elif re.search(r"\btomorrow\b", text):
        resolved = midnight + timedelta(days=1)
    elif re.search(r"\b(today|tonight|this (morning|afternoon|evening)|eod|cob|end of day)\b", text):
        resolved = midnight
        same_day_phrase = True
    elif re.search(r"\b(end of (the )?week|eow)\b", text):
        resolved = midnight + timedelta(days=(4 - reference.weekday()) % 7)
    elif re.search(r"\bnext week\b", text):
        resolved = midnight + timedelta(days=(0 - reference.weekday()) % 7 or 7)
    elif re.search(r"\bend of (the )?month\b", text):
        first_of_next = (midnight.replace(day=1) + timedelta(days=32)).replace(day=1)
        resolved = first_of_next - timedelta(days=1)
    elif weekday_offset is not None:
        resolved = midnight + timedelta(days=weekday_offset)
        same_day_phrase = weekday_offset == 0
    else:
        ordinal = _ORDINAL_DAY.search(text)
        if ordinal:
            resolved = _day_of_month_date(int(ordinal.group(1)), midnight)
        else:
            # "push it to the twenty-fifth" — the way a date is actually spoken.
            # "first thing" is a time of day, not the first of the month, and is
            # excluded here rather than being allowed to invent a date.
            spoken = _ORDINAL_WORD_DAY.search(re.sub(r"\bfirst thing\b", "", text))
            if spoken:
                resolved = _day_of_month_date(ORDINAL_DAY_WORDS[spoken.group(1)], midnight)

    if resolved is None:
        return None

    hour, minute = clock or (DEFAULT_HOUR, DEFAULT_MINUTE)
    if clock is None and re.search(r"\b(tonight|night)\b", text):
        hour, minute = TIME_OF_DAY_HOURS["tonight"]
    resolved = resolved.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # A hold whose moment already passed is useless on a board. For same-day
    # phrases only, slide it forward by a fixed grace period — still deterministic.
    if same_day_phrase and resolved <= reference:
        resolved = (reference + timedelta(minutes=PAST_DEADLINE_GRACE_MINUTES)).replace(
            second=0, microsecond=0
        )
    return resolved


def resolve_deadline_window(
    deadline_text: str,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    reference: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    """(start, end) for a spoken deadline, or None when it cannot be resolved."""
    start = resolve_deadline(deadline_text, reference)
    if start is None:
        return None
    return start, start + timedelta(minutes=max(1, duration_minutes))


# --- pure event construction ------------------------------------------------


def build_calendar_event(action: Action) -> dict:
    """{"uid", "title", "starts_at", "ends_at", "notes", ...}. Pure. Same in live and sim.

    The uid is derived from the dedup_key, so a reconcile pass that re-plans the
    same promise replaces the same hold instead of stacking a second one.

    Raises ValueError when neither `starts_at` nor a resolvable `deadline_text`
    is present — the card then fails honestly instead of inventing a date.
    """
    payload = action.payload or {}
    duration = int(payload.get("duration_minutes") or DEFAULT_DURATION_MINUTES)

    starts_at_text = str(payload.get("starts_at") or "").strip()
    starts_at: datetime | None = None
    if starts_at_text:
        try:
            starts_at = datetime.fromisoformat(starts_at_text)
        except ValueError:
            starts_at = None

    deadline_text = str(payload.get("deadline_text") or "").strip()
    if starts_at is None and deadline_text:
        starts_at = resolve_deadline(deadline_text)
    if starts_at is None:
        raise ValueError(
            f"no resolvable date in starts_at={starts_at_text!r} / deadline_text={deadline_text!r}"
        )
    if starts_at.tzinfo is not None:
        starts_at = starts_at.astimezone().replace(tzinfo=None)

    title = str(payload.get("title") or "").strip() or _fallback_title(action)
    notes = str(payload.get("notes") or "").strip() or _build_notes(action)

    return {
        "uid": build_event_uid(action.dedup_key or title),
        "title": title,
        "starts_at": starts_at.isoformat(timespec="seconds"),
        "ends_at": (starts_at + timedelta(minutes=max(1, duration))).isoformat(timespec="seconds"),
        "duration_minutes": max(1, duration),
        "notes": notes,
        "deadline_text": deadline_text,
        "calendar_name": str(payload.get("calendar_name") or "").strip(),
        "alarm_minutes_before": int(
            payload.get("alarm_minutes_before") or DEFAULT_ALARM_MINUTES_BEFORE
        ),
    }


def build_event_uid(seed: str) -> str:
    """Stable per dedup_key, so re-planning replaces the hold instead of duplicating it."""
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
    return f"adjourn-{digest}@adjourn.local"


def _fallback_title(action: Action) -> str:
    """A title from the quote when the planner did not supply one."""
    quote = (action.quote or "").strip().rstrip(".")
    if not quote:
        return "Hold: follow-up from the meeting"
    words = quote.split()
    short = " ".join(words[:9]) + ("…" if len(words) > 9 else "")
    return f"Hold: {short}"


def _build_notes(action: Action) -> str:
    """Notes always carry the verbatim sentence and who said it — that is the receipt."""
    lines = []
    if action.quote:
        speaker = action.speaker or "someone in the meeting"
        lines.append(f'"{action.quote}" — {speaker}')
    if action.meeting_id:
        lines.append(f"Meeting: {action.meeting_id}")
    lines.append("Held automatically by Adjourn when the meeting ended.")
    return "\n".join(lines)


def render_ics_document(event: dict) -> str:
    """A minimal single-VEVENT iCalendar document. Pure string building.

    Written in BOTH modes: the .ics is the local-first fallback, so a denied TCC
    permission still leaves a double-clickable artifact rather than nothing.
    """
    def stamp(value: str) -> str:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            moment = datetime.now()
        return moment.strftime("%Y%m%dT%H%M%S")

    alarm_minutes = int(event.get("alarm_minutes_before") or DEFAULT_ALARM_MINUTES_BEFORE)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Adjourn//Meeting holds//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{event['uid']}",
        f"DTSTAMP:{datetime.now().strftime('%Y%m%dT%H%M%S')}",
        f"DTSTART:{stamp(event['starts_at'])}",
        f"DTEND:{stamp(event['ends_at'])}",
        f"SUMMARY:{_escape_ics_text(event['title'])}",
        f"DESCRIPTION:{_escape_ics_text(event.get('notes', ''))}",
        "TRANSP:OPAQUE",
    ]
    if alarm_minutes > 0:
        lines += [
            "BEGIN:VALARM",
            f"TRIGGER:-PT{alarm_minutes}M",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape_ics_text(event['title'])}",
            "END:VALARM",
        ]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def _escape_ics_text(text: str) -> str:
    """RFC 5545 text escaping: backslash, semicolon, comma, newline."""
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def write_ics_file(event: dict, ics_text: str) -> Path:
    """Write the .ics atomically (temp + os.replace), same discipline as pending.json."""
    directory = holds_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{event['uid'].split('@')[0]}.ics"
    temporary = path.with_suffix(".ics.tmp")
    temporary.write_text(ics_text, encoding="utf-8")
    os.replace(temporary, path)
    return path


# --- the Swift helper (compile-and-cache, ported from MeetingScribe) ---------
# ported from ~/MeetingScribe/swift_helpers.py:ensure_binary — same idea, minus
# the packaged-app prebuilt path, which Adjourn does not have.


def ensure_calendar_binary() -> str | None:
    """Path to the compiled helper, building it on demand. None when unavailable.

    Rebuilds only when calendar_add.swift is newer than the cached binary.
    """
    if sys.platform != "darwin":
        return None
    if not SWIFT_SOURCE_PATH.exists():
        return None
    if SWIFT_BINARY_PATH.exists() and (
        SWIFT_BINARY_PATH.stat().st_mtime >= SWIFT_SOURCE_PATH.stat().st_mtime
    ):
        return str(SWIFT_BINARY_PATH)
    compiler = shutil.which("swiftc") or "/usr/bin/swiftc"
    if not Path(compiler).exists():
        print("[calendar_hold] swiftc not found — calendar writes will stay simulated")
        return None
    try:
        SWIFT_BINARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [compiler, "-O", str(SWIFT_SOURCE_PATH), "-o", str(SWIFT_BINARY_PATH)],
            check=True, capture_output=True, text=True, timeout=SWIFT_BUILD_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as error:
        detail = getattr(error, "stderr", "") or error
        print(f"[calendar_hold] could not build calendar_add: {detail}")
        return None
    return str(SWIFT_BINARY_PATH)


_access_probe: dict | None = None


def describe_calendar_access(force_refresh: bool = False) -> dict:
    """{"available", "authorization", "canWrite", "defaultCalendar", ...}. Probed ONCE.

    `probe` never shows a permission dialog, so this is safe to call on every
    action. The result is cached for the process: TCC does not change mid-demo,
    and re-probing per action would be pure latency.
    """
    global _access_probe
    if _access_probe is not None and not force_refresh:
        return _access_probe
    unavailable = {
        "available": False, "authorization": "unavailable",
        "canWrite": False, "canRead": False, "defaultCalendar": None,
    }
    binary = ensure_calendar_binary()
    if binary is None:
        _access_probe = unavailable
        return _access_probe
    try:
        completed = subprocess.run(
            [binary, "probe"], capture_output=True, text=True,
            timeout=HELPER_PROBE_TIMEOUT_SECONDS,
        )
        probe = json.loads(completed.stdout or "{}")
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        print(f"[calendar_hold] calendar probe failed: {error}")
        _access_probe = unavailable
        return _access_probe
    probe["available"] = True
    _access_probe = probe
    return probe


def forget_calendar_access() -> None:
    """Drop the cached probe — for tests, and for a permission granted mid-demo."""
    global _access_probe
    _access_probe = None


# --- execute / undo ---------------------------------------------------------


def execute(action: Action) -> results.ExecutorResult:
    """Write the .ics and add the event to the Mac's own calendar."""
    mode = secrets_store.decide_mode(REQUIRED_SECRETS, label=ACTION_KIND)

    try:
        event = build_calendar_event(action)
    except ValueError as error:
        return results.ExecutorResult.failed(
            ACTION_KIND, f"could not resolve a date deterministically: {error}",
            quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
        )

    ics_text = render_ics_document(event)
    ics_path = write_ics_file(event, ics_text)
    note = ""

    # SECOND GATE: TCC. Probed once, never looped on.
    if mode == results.MODE_LIVE:
        access = describe_calendar_access()
        if not access.get("canWrite"):
            note = (
                f"Calendar.app access is {access.get('authorization', 'unavailable')} — "
                "the .ics is on disk; double-click it to add the hold"
            )
            print(f"[{ACTION_KIND}] SIM mode — {note}")
            mode = results.MODE_SIM

    # --- TRANSPORT SWAP ---
    if mode == results.MODE_LIVE:
        try:
            added = _add_to_calendar_live(event, ics_path, event.get("calendar_name") or "")
        except CalendarAccessDenied as error:
            forget_calendar_access()
            return _render_simulated(
                action, event, ics_text, ics_path=ics_path, note=str(error)
            )
        except Exception as error:  # noqa: BLE001 — the helper may have committed already
            return results.ExecutorResult.failed(
                ACTION_KIND, f"calendar helper failed: {error}",
                mode=results.MODE_LIVE,
                quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
            )
        return results.ExecutorResult(
            ok=True,
            kind=ACTION_KIND,
            external_id=added.get("eventIdentifier") or event["uid"],
            url=None,
            human_summary=describe_hold(event, added.get("calendar") or ""),
            mode=results.MODE_LIVE,
            undo_payload={
                "event_identifier": added.get("eventIdentifier", ""),
                "uid": event["uid"],
                "ics_path": str(ics_path),
                "calendar": added.get("calendar", ""),
            },
            quote=action.quote,
            speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    return _render_simulated(action, event, ics_text, ics_path=ics_path, note=note)


def undo(result: results.ExecutorResult) -> bool:
    """Delete the event by eventIdentifier and remove the .ics. Idempotent."""
    fields = _undo_fields(result)
    ics_path = fields.get("ics_path")
    identifier = (fields.get("event_identifier") or "").strip()

    removed = True
    if identifier and result.mode == results.MODE_LIVE:
        binary = ensure_calendar_binary()
        if binary is None:
            removed = False
        else:
            try:
                completed = subprocess.run(
                    [binary, "remove", "--id", identifier],
                    capture_output=True, text=True, timeout=HELPER_PROBE_TIMEOUT_SECONDS,
                )
                removed = completed.returncode == 0
                if not removed:
                    print(f"[{ACTION_KIND}] undo failed: {(completed.stderr or '').strip()}")
            except (subprocess.SubprocessError, OSError) as error:
                print(f"[{ACTION_KIND}] undo failed: {error}")
                removed = False

    if ics_path:
        try:
            Path(ics_path).unlink(missing_ok=True)
        except OSError:
            pass
    return removed


def _undo_fields(result: results.ExecutorResult) -> dict:
    """Undo details, whether the result came from a live send or from sim.

    `ExecutorResult.simulated()` nests the rendered payload one level down, so a
    naive undo_payload read would silently find nothing for sim cards.
    """
    payload = result.undo_payload or {}
    if payload.get("simulated") and isinstance(payload.get("payload"), dict):
        return payload["payload"]
    return payload


def describe_hold(event: dict, calendar_name: str) -> str:
    """One past-tense sentence for the board card."""
    try:
        when = datetime.fromisoformat(event["starts_at"]).strftime("%a %-d %b, %-I:%M %p")
    except (ValueError, KeyError):
        when = event.get("starts_at", "")
    where = f" in {calendar_name}" if calendar_name else ""
    return f"Held {when}{where} — {event['title']}"


# --- TRANSPORT SWAP (bottom of the module) ----------------------------------


def _add_to_calendar_live(event: dict, ics_path: Path, calendar_name: str) -> dict:
    """The only side effect that reaches outside this package.

    Shells out to the compiled EventKit helper. Returns its parsed JSON:
    {"eventIdentifier", "calendar", "title", "start", "end", ...}.

    Raises CalendarAccessDenied (exit 3) so the caller can degrade to the .ics
    rather than showing a red card for something the user simply has not
    permitted yet.
    """
    binary = ensure_calendar_binary()
    if binary is None:
        raise CalendarAccessDenied("calendar helper unavailable (no swiftc, or not macOS)")

    starts_at = datetime.fromisoformat(event["starts_at"])
    ends_at = datetime.fromisoformat(event["ends_at"])
    argv = [
        binary, "add",
        "--title", event["title"],
        "--start", f"{starts_at.timestamp():.0f}",
        "--end", f"{ends_at.timestamp():.0f}",
        "--notes", event.get("notes", ""),
        "--alarm-minutes", str(event.get("alarm_minutes_before", DEFAULT_ALARM_MINUTES_BEFORE)),
    ]
    if calendar_name:
        argv += ["--calendar", calendar_name]

    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=HELPER_ADD_TIMEOUT_SECONDS
    )
    if completed.returncode == EXIT_ACCESS_DENIED:
        raise CalendarAccessDenied(
            (completed.stderr or "calendar access not granted").strip().splitlines()[-1]
        )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "calendar helper failed").strip())
    return json.loads(completed.stdout or "{}")


def _render_simulated(
    action: Action,
    event: dict,
    ics_text: str,
    *,
    ics_path: Path | None = None,
    note: str = "",
) -> results.ExecutorResult:
    """Sim mode: the exact hold that would have been added — and the .ics really is written.

    Honest framing for the badge: the file on disk is real, the Calendar.app entry
    is not. The card says so in one sentence.
    """
    summary = describe_hold(event, "")
    if note:
        summary = f"{summary} (simulated — {note})"
    else:
        summary = f"{summary} (simulated)"
    return results.ExecutorResult.simulated(
        ACTION_KIND,
        summary,
        rendered_payload={
            "event": event,
            "ics_path": str(ics_path) if ics_path else "",
            "ics": ics_text,
            "note": note,
            "uid": event["uid"],
            "event_identifier": "",
        },
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )


if __name__ == "__main__":  # quick manual probe: which mode would this run in?
    print(json.dumps({
        "platform": platform.platform(),
        "binary": ensure_calendar_binary(),
        "access": describe_calendar_access(),
        "holds_directory": str(holds_directory()),
    }, indent=2))
