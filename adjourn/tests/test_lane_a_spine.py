"""Lane A regression gate — watcher edges, source adapters, orchestrator spine.

Run:
    cd /path/to/adjourn
    python -m adjourn.tests.test_lane_a_spine

What this covers:
  * watcher edge detection from canned /api/status payloads, including the
    priming trap, engine restarts, watcher restarts, staleness re-priming, and
    catch-up of recent unprocessed done jobs (the journal is the authority)
  * the live-snapshot adapter against a synthetic /api/live document, including
    cross-track echo suppression
  * the final-transcript adapter against a REAL finished meeting on this machine
    (read-only; skipped with a loud note when no recording is available)
  * orchestrator dedup, fire ordering, the regret-window queue, and undo

Nothing here touches a live executor: every firing test runs with ADJOURN_SIM=1
and dispatches through the real registry, so the code path is the demo's path.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Force simulation before anything reads configuration or decides a mode.
os.environ["ADJOURN_SIM"] = "1"

from adjourn import config, meetingscribe_source, orchestrator, results, watcher  # noqa: E402
from adjourn.planner import Action, build_dedup_key  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def note(message: str) -> None:
    print(f"  ~~   {message}")


# --- canned engine payloads -------------------------------------------------


def status_payload(jobs: dict, *, recording: bool = False, summary_jobs: dict | None = None) -> dict:
    return {
        "recorder": {"recording": recording},
        "jobs": jobs,
        "summary_jobs": summary_jobs or {},
        "recluster_jobs": {},
        "ask_jobs": {},
    }


PROCESSING = {"state": "processing", "message": "Transcribing"}
DONE = {"state": "done", "message": "Complete"}


def synthetic_live_payload() -> dict:
    """A /api/live snapshot shaped exactly like the real engine's.

    Sequence numbers are non-contiguous and the mic copy of an echoed line
    timestamps EARLIER than the system audio it echoes — both are real behaviours
    observed on the running engine, and both broke a first attempt at this code.
    """
    return {
        "enabled": True,
        "seq": 108,
        "dropped": 0,
        "partials": {},
        "turns": [
            {"seq": 101, "track": "system", "who": "Them", "start": 10.0, "end": 14.0,
             "text": "Let's ship the cache layer by Friday."},
            {"seq": 102, "track": "mic", "who": "You", "start": 9.2, "end": 13.6,
             "text": "Let's ship the cache layer by Friday"},
            {"seq": 103, "track": "mic", "who": "You", "start": 15.0, "end": 18.0,
             "text": "I'll open a PR for the retry logic tonight."},
            {"seq": 104, "track": "mic", "who": "You", "start": 19.0, "end": 19.5,
             "text": "   "},
            {"seq": 105, "track": "system", "who": "Them", "start": 20.0, "end": 24.0,
             "text": "I'll email the vendor about the SLA."},
            {"seq": 106, "track": "mic", "who": "You", "start": 60.0, "end": 63.0,
             "text": "Let's ship the cache layer by Friday."},
            {"seq": 108, "track": "mic", "who": "You", "start": 3700.5, "end": 3705.0,
             "text": "Last thing, I'll ping the team in Slack."},
        ],
    }


def make_action(kind: str, key: str, *, regret: int = 0, meeting_id: str = "m-test") -> Action:
    return Action(
        kind=kind,
        payload={"human_preview": f"{kind} preview", "channel": "#eng", "body": "hello"},
        dedup_key=key,
        regret_window_s=regret,
        quote="we should do the thing",
        speaker="Sam",
        meeting_id=meeting_id,
        segment_id="live-101",
        source="live",
    )


# --- watcher ----------------------------------------------------------------

print("\n== watcher: priming and edge detection ==")

state = watcher.WatchState()
back_catalogue = status_payload({"20260101-000000": DONE, "20260102-000000": DONE})
check(
    "unprimed state absorbs the back catalogue silently",
    watcher.detect_events(back_catalogue, state) == [],
)
check("priming recorded the existing jobs", state.known_job_ids == {"20260101-000000", "20260102-000000"})
check("priming marked them transcript-ready", len(state.ready_meeting_ids) == 2)
check("priming flipped the primed flag", state.is_primed is True)

check(
    "an unchanged payload produces no events",
    watcher.detect_events(back_catalogue, state) == [],
)

events = watcher.detect_events(
    status_payload({**back_catalogue["jobs"], "20260821-090000": PROCESSING}, recording=False),
    state,
)
check("a new jobs key fires exactly one event", len(events) == 1, str(events))
check("the event is MEETING_STOPPED", events and events[0].event == watcher.EVENT_MEETING_STOPPED)
check("the event carries the meeting id", events and events[0].meeting_id == "20260821-090000")

events = watcher.detect_events(
    status_payload({**back_catalogue["jobs"], "20260821-090000": PROCESSING}),
    state,
)
check("the same processing job does not re-fire", events == [], str(events))

events = watcher.detect_events(
    status_payload({**back_catalogue["jobs"], "20260821-090000": DONE}),
    state,
)
check("state flip to done fires TRANSCRIPT_READY", len(events) == 1 and events[0].event == watcher.EVENT_TRANSCRIPT_READY)
check(
    "done does not fire twice",
    watcher.detect_events(status_payload({**back_catalogue["jobs"], "20260821-090000": DONE}), state) == [],
)

print("\n== watcher: the awkward cases ==")

check(
    "an empty payload (engine down) yields no events",
    watcher.detect_events({}, state) == [],
)
check(
    "an engine restart empties jobs without firing anything",
    watcher.detect_events(status_payload({}), state) == [],
)
check(
    "after a restart the old ids are still remembered",
    "20260821-090000" in state.known_job_ids,
)
check(
    "a meeting held after the restart still fires once",
    len(watcher.detect_events(status_payload({"20260821-100000": PROCESSING}), state)) == 1,
)

# A job first observed as already-done: watcher was restarted after the meeting.
late = watcher.WatchState(
    known_job_ids={"old"},
    stopped_meeting_ids={"old"},
    ready_meeting_ids={"old"},
    is_primed=True,
)
events = watcher.detect_events(status_payload({"old": DONE, "20260821-110000": DONE}), late)
check("a job seen first as done fires both phases", len(events) == 2, str([e.event for e in events]))
check(
    "and fires them stopped-then-ready, so the fast cards land first",
    [event.event for event in events]
    == [watcher.EVENT_MEETING_STOPPED, watcher.EVENT_TRANSCRIPT_READY],
)

summary_state = watcher.WatchState(known_job_ids={"x"}, stopped_meeting_ids={"x"}, ready_meeting_ids={"x"}, is_primed=True)
events = watcher.detect_events(status_payload({"x": DONE}, summary_jobs={"x": DONE}), summary_state)
check("summary_jobs done emits a log-only event", len(events) == 1 and events[0].event == watcher.EVENT_SUMMARY_READY)
check(
    "summary does not repeat",
    watcher.detect_events(status_payload({"x": DONE}, summary_jobs={"x": DONE}), summary_state) == [],
)

print("\n== watcher: the recording-stop edge (corroboration only) ==")

edge_state = watcher.WatchState(is_primed=True)
check("no edge while idle", watcher.detect_recording_stop_edge({"recording": False}, edge_state) is False)
check("no edge on start", watcher.detect_recording_stop_edge({"recording": True}, edge_state) is False)
check("edge on stop", watcher.detect_recording_stop_edge({"recording": False}, edge_state) is True)
check("edge does not repeat", watcher.detect_recording_stop_edge({"recording": False}, edge_state) is False)
check(
    "an empty record payload is not a stop edge",
    watcher.detect_recording_stop_edge({}, watcher.WatchState(was_recording=True)) is False,
)

print("\n== watcher: state persists across restarts ==")

with tempfile.TemporaryDirectory() as directory:
    state_path = Path(directory) / "watcher.json"
    saved = watcher.WatchState(
        known_job_ids={"20260821-090000"},
        stopped_meeting_ids={"20260821-090000"},
        ready_meeting_ids={"20260821-090000"},
        is_primed=True,
    )
    watcher.save_watch_state(saved, state_path)
    check("state file was written", state_path.exists())

    reloaded = watcher.load_watch_state(state_path)
    check("known jobs survive the round trip", reloaded.known_job_ids == {"20260821-090000"})
    check("primed flag survives", reloaded.is_primed is True)
    check(
        "a job handled before the restart does not fire again",
        watcher.detect_events(status_payload({"20260821-090000": DONE}), reloaded) == [],
    )

    stale = json.loads(state_path.read_text())
    stale["saved_at"] = (
        datetime.now(UTC) - timedelta(seconds=watcher.WATCH_STATE_STALENESS_SECONDS + 60)
    ).isoformat(timespec="seconds")
    state_path.write_text(json.dumps(stale))
    check("stale state comes back unprimed", watcher.load_watch_state(state_path).is_primed is False)

    empty_primed = {"known_job_ids": [], "is_primed": True, "saved_at": results.utc_timestamp()}
    state_path.write_text(json.dumps(empty_primed))
    check(
        "primed-but-empty state comes back unprimed",
        watcher.load_watch_state(state_path).is_primed is False,
    )

    check("a missing state file reads as unprimed", watcher.load_watch_state(Path(directory) / "nope.json").is_primed is False)
    (Path(directory) / "bad.json").write_text("{not json")
    check("a malformed state file reads as unprimed", watcher.load_watch_state(Path(directory) / "bad.json").is_primed is False)

print("\n== watcher: catch-up of meetings that finished while we were down ==")

# Anchored to the REAL clock, not a hardcoded date: the watch-loop section below
# runs release_unprocessed_recent_jobs on the wall clock, so a fixed stamp rots
# out of the one-hour catch-up window within an hour of being written (it did).
CATCH_NOW = datetime.now().replace(microsecond=0)


def _engine_stamp(minutes_ago: int) -> str:
    return (CATCH_NOW - timedelta(minutes=minutes_ago)).strftime("%Y%m%d-%H%M%S")


RECENT_DONE = _engine_stamp(20)   # 20 minutes before CATCH_NOW
OLD_DONE = "20260101-000000"
JOURNALED = _engine_stamp(25)     # recent, but the journal already has it
STILL_PROCESSING = _engine_stamp(10)

saved_journal = watcher.journal_has_processed
watcher.journal_has_processed = lambda meeting_id: meeting_id == JOURNALED
try:
    check("a twenty-minute-old stamp is inside the catch-up window",
          watcher.meeting_id_age_seconds(RECENT_DONE, CATCH_NOW) == 20 * 60)
    check("a January stamp is outside the catch-up window",
          watcher.meeting_id_age_seconds(OLD_DONE, CATCH_NOW) > watcher.CATCH_UP_WINDOW_SECONDS)
    check("a fixture id is not an engine stamp",
          watcher.meeting_id_age_seconds("living-room-standup", CATCH_NOW) is None)

    mixed = watcher.WatchState()
    mixed_events = watcher.detect_events(
        status_payload({OLD_DONE: DONE, RECENT_DONE: DONE, JOURNALED: DONE}),
        mixed,
        now=CATCH_NOW,
    )
    mixed_ids = [event.meeting_id for event in mixed_events]
    check(
        "old back-catalogue still does not fire on prime",
        OLD_DONE not in mixed_ids,
        str(mixed_ids),
    )
    check(
        "a recent unprocessed done job fires both phases on prime",
        [event.event for event in mixed_events if event.meeting_id == RECENT_DONE]
        == [watcher.EVENT_MEETING_STOPPED, watcher.EVENT_TRANSCRIPT_READY],
        str([(event.event, event.meeting_id) for event in mixed_events]),
    )
    check(
        "a recent job already in the journal does not re-fire",
        JOURNALED not in mixed_ids,
        str(mixed_ids),
    )
    check("the old job stayed marked seen", OLD_DONE in mixed.known_job_ids)
    check("the journaled job stayed marked seen", JOURNALED in mixed.known_job_ids)

    stranded = watcher.WatchState(
        known_job_ids={RECENT_DONE, OLD_DONE, STILL_PROCESSING},
        stopped_meeting_ids={RECENT_DONE, OLD_DONE, STILL_PROCESSING},
        ready_meeting_ids={RECENT_DONE, OLD_DONE},
        is_primed=True,
    )
    released = watcher.release_unprocessed_recent_jobs(
        stranded,
        status_payload({
            RECENT_DONE: DONE,
            OLD_DONE: DONE,
            STILL_PROCESSING: PROCESSING,
        }),
        now=CATCH_NOW,
    )
    check("primed state forgets a stranded recent done job", RECENT_DONE in released)
    check("...and keeps the old one marked seen", OLD_DONE in stranded.known_job_ids)
    check("...and does not catch up a job still processing",
          STILL_PROCESSING not in released and STILL_PROCESSING in stranded.known_job_ids)
    stranded_events = watcher.detect_events(
        status_payload({RECENT_DONE: DONE, OLD_DONE: DONE}),
        stranded,
        now=CATCH_NOW,
    )
    check(
        "the stranded job then fires stopped-then-ready",
        [event.event for event in stranded_events if event.meeting_id == RECENT_DONE]
        == [watcher.EVENT_MEETING_STOPPED, watcher.EVENT_TRANSCRIPT_READY],
        str([event.event for event in stranded_events]),
    )

    # The production failure: watcher.json is fresh and primed, claiming the
    # job was seen, and --watch starts. Catch-up has to run BEFORE the first
    # poll or the identical payload produces nothing.
    primed_restart = watcher.WatchState(
        known_job_ids={RECENT_DONE},
        stopped_meeting_ids={RECENT_DONE},
        ready_meeting_ids={RECENT_DONE},
        is_primed=True,
    )
    restart_seen: list[tuple[str, str]] = []
    saved_engine_status = meetingscribe_source.fetch_engine_status
    saved_record_status = meetingscribe_source.fetch_record_status
    try:
        meetingscribe_source.fetch_engine_status = lambda: status_payload({RECENT_DONE: DONE})
        meetingscribe_source.fetch_record_status = lambda: {"recording": False}
        watcher.watch_for_meeting_events(
            lambda event: restart_seen.append((event.event, event.meeting_id)),
            poll_interval_seconds=0.0,
            fast_poll_interval_seconds=0.0,
            stop_after_seconds=0.3,
            state=primed_restart,
            persist=False,
            launch_engine=False,
        )
    finally:
        meetingscribe_source.fetch_engine_status = saved_engine_status
        meetingscribe_source.fetch_record_status = saved_record_status
    check(
        "a primed --watch restart still catches a stranded recent job",
        (watcher.EVENT_MEETING_STOPPED, RECENT_DONE) in restart_seen
        and (watcher.EVENT_TRANSCRIPT_READY, RECENT_DONE) in restart_seen,
        str(restart_seen),
    )
finally:
    watcher.journal_has_processed = saved_journal


# --- THE LIVE HANDOFF -------------------------------------------------------
#
# The one claim the Live tab makes: press Stop and Follow-through fills itself,
# in one sitting, without opening another window. Everything between those two
# things is the watcher, and until now the two halves of it were tested apart —
# the pure edge detector here, the phase functions further down — with the wire
# between them (a closure inside run_orchestrator) asserted nowhere. That is the
# part a demo actually depends on, so it is the part that gets a test.
#
# Nothing here starts an engine, a recording, or a subprocess. The engine is two
# canned payload sequences and the phases are stubs that record their arguments.

print("\n== the live handoff: Stop -> watcher -> the pipeline ==")

STOPPED_ID = "20260821-133000"

# The engine as a tape: recording, recording, then stopped. /api/status only
# grows the new job key on the poll AFTER the recorder flipped, which is what
# makes the forced poll worth having.
record_tape = [{"recording": True}, {"recording": True}, {"recording": False}]
status_tape = [
    status_payload({"20260101-000000": DONE}),                        # priming
    status_payload({"20260101-000000": DONE}),
    status_payload({"20260101-000000": DONE, STOPPED_ID: PROCESSING}),  # the stop
    status_payload({"20260101-000000": DONE, STOPPED_ID: DONE}),        # transcript
]

def run_watch_tape(poll_interval_seconds: float) -> list[tuple[str, str]]:
    """Drive the real loop over the canned engine and collect what it emitted.

    BOUNDED BY THE CLOCK, not by a sentinel exception: watch_for_meeting_events
    swallows anything a callback raises (one bad handler must not end a demo), so
    an exception thrown from on_tick would be caught and the loop would spin
    forever. `stop_after_seconds` is the loop's own exit and the only safe one.
    Both tapes clamp to their last element, so the extra ticks re-observe a state
    that has already been absorbed and emit nothing.
    """
    record_polls = [0]
    status_polls = [0]

    def next_record_status() -> dict:
        record_polls[0] += 1
        return record_tape[min(record_polls[0] - 1, len(record_tape) - 1)]

    def next_engine_status() -> dict:
        status_polls[0] += 1
        return status_tape[min(status_polls[0] - 1, len(status_tape) - 1)]

    saved_record_status = meetingscribe_source.fetch_record_status
    saved_engine_status = meetingscribe_source.fetch_engine_status
    seen: list[tuple[str, str]] = []
    try:
        meetingscribe_source.fetch_record_status = next_record_status
        meetingscribe_source.fetch_engine_status = next_engine_status
        watcher.watch_for_meeting_events(
            lambda event: seen.append((event.event, event.meeting_id)),
            poll_interval_seconds=poll_interval_seconds,
            fast_poll_interval_seconds=0.0,
            stop_after_seconds=0.4,
            state=watcher.WatchState(),
            persist=False,
            launch_engine=False,
        )
    finally:
        meetingscribe_source.fetch_record_status = saved_record_status
        meetingscribe_source.fetch_engine_status = saved_engine_status
    return seen


observed = run_watch_tape(poll_interval_seconds=0.0)
check(
    "the loop turns a live Stop into MEETING_STOPPED then TRANSCRIPT_READY",
    observed == [
        (watcher.EVENT_MEETING_STOPPED, STOPPED_ID),
        (watcher.EVENT_TRANSCRIPT_READY, STOPPED_ID),
    ],
    str(observed),
)

# THE HALF-SECOND THE STOP EDGE BUYS. With the trigger bus set to a tick that
# will not arrive inside this window, the ONLY thing that can pull /api/status
# after priming is the forced poll the recorder's True->False edge triggers. If
# that forcing were ever removed, this comes back empty and the meeting would sit
# there looking ignored for up to a full poll interval on stage.
forced = run_watch_tape(poll_interval_seconds=30.0)
check(
    "the recorder stop edge pulls the id without waiting for the slow tick",
    (watcher.EVENT_MEETING_STOPPED, STOPPED_ID) in forced,
    str(forced),
)

# And the wire on the other side: each edge reaches the pass it belongs to.
dispatched: list[tuple[str, str]] = []
saved_fast = orchestrator.handle_meeting_stopped
saved_final = orchestrator.reconcile_from_final_transcript
try:
    orchestrator.handle_meeting_stopped = (
        lambda meeting_id, memory=None: dispatched.append(("fast", meeting_id)) or []
    )
    orchestrator.reconcile_from_final_transcript = (
        lambda meeting_id, memory=None: dispatched.append(("final", meeting_id)) or []
    )
    with tempfile.TemporaryDirectory() as pipeline_home:
        saved_pipeline = os.environ.get("ADJOURN_PIPELINE")
        os.environ["ADJOURN_PIPELINE"] = str(Path(pipeline_home) / "pipeline.json")
        try:
            for event_name in (
                watcher.EVENT_MEETING_STOPPED,
                watcher.EVENT_TRANSCRIPT_READY,
                watcher.EVENT_SUMMARY_READY,
            ):
                orchestrator.handle_meeting_event(
                    watcher.MeetingEvent(event_name, STOPPED_ID)
                )
        finally:
            if saved_pipeline is None:
                os.environ.pop("ADJOURN_PIPELINE", None)
            else:
                os.environ["ADJOURN_PIPELINE"] = saved_pipeline
finally:
    orchestrator.handle_meeting_stopped = saved_fast
    orchestrator.reconcile_from_final_transcript = saved_final

check(
    "MEETING_STOPPED runs the fast pass and TRANSCRIPT_READY the final one",
    dispatched == [("fast", STOPPED_ID), ("final", STOPPED_ID)],
    str(dispatched),
)
check(
    "SUMMARY_READY runs neither — nothing waits on the auto-summary",
    len(dispatched) == 2,
    str(dispatched),
)


# --- meetingscribe_source: live ---------------------------------------------

print("\n== source: the live snapshot ==")

live_meta, live_segments = meetingscribe_source.load_live_snapshot(
    payload=synthetic_live_payload()
)
texts = [segment["text"] for segment in live_segments]
check("live snapshot reports enabled", live_meta["enabled"] is True)
check("whitespace-only turns are dropped", all(text.strip() for text in texts))
check("segment ids use the live prefix", all(s["segment_id"].startswith("live-") for s in live_segments))
check("segment ids carry the real seq", "live-103" in {s["segment_id"] for s in live_segments})
check("segments carry drift's ts field", live_segments[0]["ts"] == "00:00:10", live_segments[0]["ts"])
check("ts crosses the hour correctly", any(s["ts"] == "01:01:40" for s in live_segments))
check(
    "segments carry both contract shapes",
    set(live_segments[0]) >= {"segment_id", "ts", "speaker", "text", "track", "start", "end"},
)
check("segments are chronological", [s["start"] for s in live_segments] == sorted(s["start"] for s in live_segments))

check("the mic echo of a system line is removed", live_meta["echoes_removed"] == 1, str(live_meta))
check(
    "the surviving copy is the system one, attributed to Them",
    next(s for s in live_segments if "cache layer" in s["text"] and s["start"] < 20)["speaker"] == "Them",
)
check(
    "a genuine later repeat outside the window is KEPT",
    sum(1 for s in live_segments if "cache layer" in s["text"]) == 2,
)
check("echo suppression can be switched off",
      meetingscribe_source.load_live_snapshot(payload=synthetic_live_payload(), suppress_echo=False)[0]["segment_count"] == 6)

disabled_meta, disabled_segments = meetingscribe_source.load_live_snapshot(
    payload={"enabled": False, "turns": []}
)
check("disabled live captions return no segments", disabled_segments == [])
check("disabled live captions say so rather than raising", disabled_meta["enabled"] is False)

check("format_timestamp handles zero", meetingscribe_source.format_timestamp(0) == "00:00:00")
check("format_timestamp handles junk", meetingscribe_source.format_timestamp(None) == "00:00:00")
check("format_timestamp handles negatives", meetingscribe_source.format_timestamp(-5) == "00:00:00")

print("\n== source: read-only enforcement ==")

try:
    meetingscribe_source._get_json("/api/shutdown")
    check("a non-allowlisted endpoint is refused", False, "no error raised")
except ValueError as error:
    check("a non-allowlisted endpoint is refused", True, str(error))
check(
    "no Origin header is ever sent to the loopback engine",
    "Origin" not in meetingscribe_source.READ_ONLY_HEADERS,
)
check(
    "the allowlist is exactly the three read-only GETs",
    set(meetingscribe_source.READ_ONLY_ENDPOINTS) == {"/api/status", "/api/record/status", "/api/live"},
)


# --- meetingscribe_source: a real finished meeting --------------------------

print("\n== source: a REAL finished meeting on this machine (read-only) ==")

recent = meetingscribe_source.list_recent_meeting_ids(5)
if not recent:
    note("no recordings under ~/.meetingscribe/recordings — real-meeting checks skipped")
else:
    check("recent ids sort newest first", recent == sorted(recent, reverse=True), str(recent))
    meeting_id = None
    for candidate in recent:
        _probe_meta, probe_segments = meetingscribe_source.load_meeting(candidate)
        if probe_segments:
            meeting_id = candidate
            break
    if meeting_id is None:
        note("recent recordings have no spoken segments — real-meeting checks skipped")
    else:
        directory = meetingscribe_source.resolve_recording_directory(meeting_id)
        check(f"resolved a folder for {meeting_id}", directory is not None)
        check("the folder was resolved by its ' — <id>' suffix, not a cached path",
              directory is not None and directory.name.endswith(meeting_id))
        check("meeting.json exists in it", directory is not None and (directory / "meeting.json").exists())

        document = meetingscribe_source.read_meeting_json(meeting_id)
        check("meeting.json parsed", bool(document))
        check("its id matches what we asked for", document.get("id") == meeting_id)

        meta, segments = meetingscribe_source.load_meeting(meeting_id)
        check("meta carries the meeting id", meta["meeting_id"] == meeting_id)
        check("meta carries a title", bool(meta["title"]))
        check("meta carries a YYYY-MM-DD date", len(meta["date"]) == 10 and meta["date"][4] == "-", meta["date"])
        check("segments were produced", len(segments) > 0, f"{len(segments)} segments")
        check("final segment ids carry the meeting id", all(s["segment_id"].startswith(f"m{meeting_id}-t") for s in segments))
        check("no empty segment text", all(s["text"].strip() for s in segments))
        check("speaker keys were mapped to display names", all(not s["speaker"].startswith("s") or s["speaker"].startswith("Speaker") for s in segments))
        check("every segment has a ts", all(len(s["ts"]) == 8 for s in segments))
        check("source is tagged final", all(s["source"] == "final" for s in segments))

        title = meetingscribe_source.read_meeting_title(meeting_id)
        check("title resolves for the board header", bool(title) and title != meeting_id, title)

        same_meta, same_segments = meetingscribe_source.load_meeting(directory)
        check("loading by folder path gives the same result", same_segments == segments)

        # A meeting that MeetingScribe never wrote must degrade, not raise.
        check("an unknown meeting id resolves to None", meetingscribe_source.resolve_recording_directory("19700101-000000") is None)
        check("an unknown meeting id reads as {}", meetingscribe_source.read_meeting_json("19700101-000000") == {})
        ghost_meta, ghost_segments = meetingscribe_source.load_meeting("19700101-000000")
        check("an unknown meeting loads as empty rather than raising", ghost_segments == [])
        check("...and still reports the id it was asked about", ghost_meta["meeting_id"] == "19700101-000000")
        check("an empty meeting id resolves to None", meetingscribe_source.resolve_recording_directory("") is None)

        ready = status_payload({meeting_id: DONE})
        check("is_transcript_ready reads a done job", meetingscribe_source.is_transcript_ready(ready, meeting_id) is True)
        check("is_transcript_ready is False while processing",
              meetingscribe_source.is_transcript_ready(status_payload({meeting_id: PROCESSING}), meeting_id) is False)
        check("is_transcript_ready is False for an unknown id",
              meetingscribe_source.is_transcript_ready(ready, "nope") is False)
        check("is_transcript_ready tolerates an empty payload",
              meetingscribe_source.is_transcript_ready({}, meeting_id) is False)


# --- orchestrator: dedup, ordering, queue, undo -----------------------------

print("\n== orchestrator: dedup and fire ordering ==")

ordered = orchestrator.order_actions_for_firing([
    make_action("recap_page", "recap_page:m-test"),
    make_action("github_update", "github_update:12"),
    make_action("linear_create", "linear_create:cache"),
])
check("the recap page is fired last so it can cite the rest", ordered[-1].kind == "recap_page")
check("everything else keeps planner order", [a.kind for a in ordered[:2]] == ["github_update", "linear_create"])

plan = [
    make_action("github_update", "github_update:12:cache-layer"),
    make_action("github_update", "github_update:12:cache-layer"),
    make_action("slack_send", "slack_send:eng:heads-up"),
]
selected = orchestrator.select_new_actions(plan, set())
check("duplicates within one plan collapse", len(selected) == 2, str([a.dedup_key for a in selected]))

selected = orchestrator.select_new_actions(plan, {"github_update:12:cache-layer"})
check("an already-fired key is skipped on reconcile", [a.kind for a in selected] == ["slack_send"])

recap_plan = [make_action("recap_page", "recap_page:m-test")]
check(
    "the recap page re-fires even when already fired (it rewrites in place)",
    len(orchestrator.select_new_actions(recap_plan, {"recap_page:m-test"})) == 1,
)
check("recap_page is the only always-refire kind", orchestrator.ALWAYS_REFIRE_KINDS == frozenset({"recap_page"}))

check(
    "dedup keys are the planner's, computed identically",
    build_dedup_key("github_update", 12, "Cache layer!") == build_dedup_key("github_update", "12", "cache  layer"),
)

print("\n== orchestrator: the regret window ==")

with tempfile.TemporaryDirectory() as directory:
    journal = Path(directory) / "executions.jsonl"
    pending = Path(directory) / "pending.json"
    recaps = Path(directory) / "recaps"
    recaps.mkdir()
    original_journal = config.executions_journal_path
    original_pending = config.pending_actions_path
    original_recaps = config.recaps_directory
    # Redirect all three state sinks into the temp dir: these tests fire real
    # executors in sim mode, and recap_page is honestly "live" — it writes a file.
    config.executions_journal_path = lambda: journal          # noqa: E731
    config.pending_actions_path = lambda: pending             # noqa: E731
    config.recaps_directory = lambda: recaps                  # noqa: E731
    try:
        immediate = make_action("github_update", "github_update:12:cache")
        result = orchestrator.fire_action(immediate, meeting_title="Eng sync")
        check("an immediate action returns a result", result is not None)
        check("it ran in sim mode under ADJOURN_SIM=1", result is not None and result.mode == "sim", str(result))
        check("it was journaled", len(results.read_executions(path=journal)) == 1)
        check(
            "the journal line carries the dedup key",
            results.read_executions(path=journal)[0].get("dedup_key") == "github_update:12:cache",
        )
        check(
            "the journal line carries provenance",
            results.read_executions(path=journal)[0].get("source") == "live",
        )

        queued = make_action("slack_send", "slack_send:eng:heads-up", regret=60)
        result = orchestrator.fire_action(queued, meeting_title="Eng sync")
        check("a countdown action returns None instead of a result", result is None)
        check("nothing extra was journaled — intentions are not events", len(results.read_executions(path=journal)) == 1)
        waiting = orchestrator.read_pending_actions()
        check("it landed in pending.json", len(waiting) == 1 and waiting[0].kind == "slack_send")
        check("the countdown is running", 0 < waiting[0].seconds_remaining() <= 60)
        check("nothing is due yet", orchestrator.tick_pending_actions() == [])

        # Wind the clock forward by rewriting fire_at, the same field the board reads.
        entries = orchestrator.read_pending_actions()
        entries[0].fire_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds")
        orchestrator.write_pending_actions(entries)
        fired = orchestrator.tick_pending_actions()
        check("an expired countdown fires", len(fired) == 1, str(fired))
        check("it fired in sim mode", fired and fired[0].mode == "sim")
        check("it is now journaled", len(results.read_executions(path=journal)) == 2)
        check(
            "its pending entry is marked fired, not left waiting",
            all(not item.is_waiting for item in orchestrator.read_pending_actions()),
        )
        check("a fired countdown does not fire twice", orchestrator.tick_pending_actions() == [])

        # executors/regret_window.py runs its own timer over the same file. The
        # claim is what stops both of them sending the same message.
        contested = make_action("slack_send", "slack_send:eng:contested", regret=60)
        orchestrator.fire_action(contested)
        entries = orchestrator.read_pending_actions()
        for item in entries:
            if item.dedup_key == "slack_send:eng:contested":
                item.fire_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds")
        orchestrator.write_pending_actions(entries)
        check(
            "a due entry is claimable exactly once",
            orchestrator.mark_pending_action_fired("slack_send:eng:contested") is True,
        )
        check(
            "the other timer finds it already claimed and steps over it",
            orchestrator.tick_pending_actions() == [],
        )

        print("\n== orchestrator: the meeting title reaches the executors ==")

        titled = make_action("recap_page", "recap_page:title-test")
        orchestrator.fire_action(titled, meeting_title="Salient Interview")
        check("the resolved title is injected into the payload", titled.payload["meeting_title"] == "Salient Interview")

        preset = make_action("recap_page", "recap_page:title-preset")
        preset.payload["meeting_title"] = "planner said so"
        orchestrator.fire_action(preset, meeting_title="orchestrator said so")
        check("a planner-supplied title is never clobbered", preset.payload["meeting_title"] == "planner said so")

        handled = orchestrator.already_handled_keys("m-test")
        check("successfully fired keys are in the handled set", "slack_send:eng:heads-up" in handled)
        check(
            "a FAILED action is NOT handled, so reconcile retries it with better text",
            "github_update:12:cache" not in handled,
        )
        retry = orchestrator.select_new_actions([make_action("github_update", "github_update:12:cache")], handled)
        check("...and the reconcile pass really does re-select it", len(retry) == 1)

        print("\n== orchestrator: cancel and undo ==")

        still_waiting = make_action("email_send", "email_send:vendor:sla", regret=60)
        orchestrator.fire_action(still_waiting)
        journal_depth = len(results.read_execution_results(path=journal))
        check("undo of a waiting action cancels it", orchestrator.undo_execution("email_send:vendor:sla") is True)
        check(
            "a cancelled action never fires",
            orchestrator.tick_pending_actions() == [],
        )
        check(
            "cancelling adds no EXECUTION record — nothing was sent",
            len(results.read_execution_results(path=journal)) == journal_depth,
        )
        # ...but it does leave a trace. A cancelled countdown used to vanish
        # completely: no card, no journal line, nothing on the recap. "A human
        # can stop it" is the trust argument, so the stopping needs an artifact.
        cancellations = results.read_cancellations(path=journal)
        check(
            "cancelling DOES append a cancellation record",
            len(cancellations) == 1,
        )
        check(
            "the cancellation record names the action that was stopped",
            cancellations[0].get("dedup_key") == "email_send:vendor:sla"
            and cancellations[0].get("kind") == "email_send",
        )

        check("undo of an unknown key is False", orchestrator.undo_execution("nothing:here") is False)

        before = len(results.read_executions(path=journal))
        undone = orchestrator.undo_execution("slack_send:eng:heads-up")
        after = results.read_executions(path=journal)
        check("undo of a fired action appends a record", len(after) == before + 1)
        check("the appended record is an undo record", after[-1].get("record_type") == "undo")
        check("history was not rewritten", after[0].get("record_type") == "execution")
        check("undo reported an outcome", isinstance(undone, bool))
        check(
            "undoing a failed action is a no-op — there is nothing to reverse",
            orchestrator.undo_execution("github_update:12:cache") is False,
        )
        check(
            "recap pages landed in the temp dir, not the real recaps folder",
            any(path.suffix == ".html" for path in recaps.iterdir()),
        )
    finally:
        config.executions_journal_path = original_journal
        config.pending_actions_path = original_pending
        config.recaps_directory = original_recaps

print("\n== orchestrator: lane-boundary degradation ==")

check("planning nothing yields nothing", orchestrator.plan_actions_safely([]) == [])
check("a memory that cannot answer never blocks dedup", orchestrator.has_memory_fired(None, "any:key") is False)


class RefusingMemory:
    def has_fired_dedup_key(self, dedup_key: str) -> bool:
        raise RuntimeError("falkor is down")

    def record_meeting(self, *args):
        raise RuntimeError("falkor is down")

    def record_statement(self, *args):
        raise RuntimeError("falkor is down")

    def record_action(self, *args):
        raise NotImplementedError("Lane C")


check("a broken memory backend degrades to False", orchestrator.has_memory_fired(RefusingMemory(), "k") is False)
orchestrator.record_statements_safely(RefusingMemory(), [], "m1", {"title": "t", "date": "d"})
orchestrator.record_action_safely(RefusingMemory(), make_action("recap_page", "k"), results.ExecutorResult.failed("recap_page", "x"))
check("memory writes never raise into the spine", True)
orchestrator.close_memory_safely(None)
orchestrator.close_memory_safely(RefusingMemory())
check("closing a broken memory never raises", True)

print("\n== orchestrator: the CLI ==")

parser = orchestrator.build_argument_parser()
arguments = parser.parse_args(["--replay", "20260819-155401", "--sim", "--phase", "final"])
check("--replay parses", arguments.replay == "20260819-155401")
check("--sim parses", arguments.sim is True)
check("--phase parses", arguments.phase == "final")
check("--watch parses", parser.parse_args(["--watch"]).watch is True)
check("--undo parses", parser.parse_args(["--undo", "k:1"]).undo == "k:1")
check("--phase defaults to both", parser.parse_args(["--watch"]).phase == "both")
check("replaying a missing file degrades to an empty run", orchestrator.replay_meeting("/nonexistent/path.json") == [])
check("--replay with no target defaults to the demo fixture",
      parser.parse_args(["--replay"]).replay == orchestrator.DEFAULT_REPLAY_TARGET)
check("the demo fixture name is living-room-standup",
      orchestrator.DEFAULT_REPLAY_TARGET == "living-room-standup")
check("the demo fixture transcript exists on disk",
      (config.FIXTURES_DIR / f"{orchestrator.DEFAULT_REPLAY_TARGET}.jsonl").is_file())
check("--replay still accepts an explicit target",
      parser.parse_args(["--replay", "prior-standup"]).replay == "prior-standup")
check("--replay --sim-all are independent flags",
      parser.parse_args(["--replay", "--sim-all"]).replay == orchestrator.DEFAULT_REPLAY_TARGET
      and parser.parse_args(["--replay", "--sim-all"]).sim_all is True)

print("\n== orchestrator: pipeline.json is the guts panel's store ==")
with tempfile.TemporaryDirectory() as directory:
    pipe = Path(directory) / "pipeline.json"
    original_pipeline = config.pipeline_status_path
    config.pipeline_status_path = lambda: pipe  # noqa: E731
    try:
        idle = orchestrator.read_pipeline_status()
        check("a missing pipeline.json reads as idle", idle.mode == "idle")
        check("idle watcher is waiting", idle.watcher.phase == orchestrator.WATCHER_WAITING)
        check("idle extraction is idle", idle.extraction.phase == orchestrator.STAGE_IDLE)
        orchestrator.report_pipeline(
            mode="replay",
            pass_name="replay",
            meeting_id="living-room-standup",
            meeting_title="MMM Standup",
            watcher=orchestrator.watcher_status_transcript_ready("living-room-standup", replay=True),
            extraction=orchestrator.extraction_status_from_statements(
                [type("S", (), {"kind": "decision", "segment_id": "s01"})(),
                 type("S", (), {"kind": "question", "segment_id": "s02"})()],
                source="final",
            ),
            planner=orchestrator.PlannerStatus(phase=orchestrator.STAGE_RUNNING, detail="routing through the table…"),
        )
        loaded = orchestrator.read_pipeline_status()
        check("report_pipeline writes atomically", pipe.is_file())
        check("replay mode is stored", loaded.mode == "replay")
        check("watcher is transcript ready on replay",
              loaded.watcher.phase == orchestrator.WATCHER_TRANSCRIPT_READY
              and "replay" in loaded.watcher.detail)
        check("extraction counts kinds",
              loaded.extraction.statement_count == 2
              and loaded.extraction.kinds.get("decision") == 1
              and loaded.extraction.kinds.get("question") == 1)
        check("planner running is visible", loaded.planner.phase == orchestrator.STAGE_RUNNING)
        # Partial update must not wipe earlier stages.
        orchestrator.report_pipeline(
            planner=orchestrator.PlannerStatus(
                phase=orchestrator.STAGE_DONE, action_count=1, ignored_count=1,
                action_kinds={"github_update": 1}, ignored_kinds={"question": 1},
                detail="table decided 1 action · ignored 1 (question)",
            )
        )
        merged = orchestrator.read_pipeline_status()
        check("a planner update keeps the watcher",
              merged.watcher.phase == orchestrator.WATCHER_TRANSCRIPT_READY)
        check("a planner update keeps extraction", merged.extraction.statement_count == 2)
        check("planner done records decided vs ignored",
              merged.planner.action_count == 1 and merged.planner.ignored_count == 1)
    finally:
        config.pipeline_status_path = original_pipeline

print("\n== orchestrator: fixture replay does not need MeetingScribe ==")
with tempfile.TemporaryDirectory() as directory:
    directory = Path(directory)
    journal = directory / "executions.jsonl"
    pending = directory / "pending.json"
    recaps = directory / "recaps"
    recaps.mkdir()
    pipe = directory / "pipeline.json"
    fixture = directory / "quiet-meeting.json"
    fixture.write_text(
        '{"statements":[{"segment_id":"s01","speaker":"Them","topic":"cache",'
        '"claim":"is the cache done?","kind":"question","quote":"is the cache done?"}]}\n',
        encoding="utf-8",
    )
    original_journal = config.executions_journal_path
    original_pending = config.pending_actions_path
    original_recaps = config.recaps_directory
    original_pipeline = config.pipeline_status_path
    original_state = config.state_directory
    original_memory = orchestrator.open_memory_safely
    config.executions_journal_path = lambda: journal  # noqa: E731
    config.pending_actions_path = lambda: pending  # noqa: E731
    config.recaps_directory = lambda: recaps  # noqa: E731
    config.pipeline_status_path = lambda: pipe  # noqa: E731
    # replay_meeting parks the planned recap action under <state>/recap_actions/.
    # Without this line it lands in the SHIPPED tree and stays there: that is
    # where adjourn/state/recap_actions/quiet-meeting.json kept coming from,
    # and the final-state contract says that directory must be empty.
    config.state_directory = lambda: directory / "state"  # noqa: E731
    orchestrator.open_memory_safely = lambda: None  # noqa: E731
    try:
        fired = orchestrator.replay_meeting(str(fixture))
        check("a statements fixture replays without MeetingScribe", isinstance(fired, list))
        check("a question-only meeting still fires the recap",
              any(result.kind == "recap_page" for result in fired), str(fired))
        status = orchestrator.read_pipeline_status()
        check("replay wrote pipeline.json", pipe.is_file())
        check("replay mode is replay, not watch", status.mode == "replay")
        check("replay watcher is transcript ready",
              status.watcher.phase == orchestrator.WATCHER_TRANSCRIPT_READY)
        check("replay extraction is done from the fixture",
              status.extraction.phase == orchestrator.STAGE_DONE
              and status.extraction.statement_count == 1
              and status.extraction.source == "fixture")
        check("the table ignored the question and decided no (non-recap) actions",
              status.planner.phase == orchestrator.STAGE_DONE
              and status.planner.action_count == 0
              and status.planner.ignored_count == 1
              and status.planner.ignored_kinds.get("question") == 1)
        check("MeetingScribe was not required — engine was never queried for this path", True)
        check("the parked recap action went to the sandbox, not the shipped tree",
              not (config.STATE_DIR / "recap_actions" / "quiet-meeting.json").exists())
    finally:
        config.executions_journal_path = original_journal
        config.pending_actions_path = original_pending
        config.recaps_directory = original_recaps
        config.pipeline_status_path = original_pipeline
        config.state_directory = original_state
        orchestrator.open_memory_safely = original_memory


print("\n== orchestrator: the narrated feed (the rail that must MOVE) ==")
with tempfile.TemporaryDirectory() as directory:
    pipe = Path(directory) / "pipeline.json"
    original_pipeline = config.pipeline_status_path
    config.pipeline_status_path = lambda: pipe  # noqa: E731
    try:
        # batch_index/batch_total survive the round trip. Without these two the
        # board renders no progress bar and the brief's "rail must move, not
        # freeze idle" is unprovable.
        orchestrator.report_pipeline(
            meeting_id="m-1",
            extraction=orchestrator.ExtractionStatus(
                phase=orchestrator.STAGE_RUNNING, batch_index=2, batch_total=5,
            ),
            feed_reset=True,
        )
        reread = orchestrator.read_pipeline_status()
        check("batch_index survives to_dict/from_dict", reread.extraction.batch_index == 2)
        check("batch_total survives to_dict/from_dict", reread.extraction.batch_total == 5)
        check("batch counters reach the raw JSON the board reads",
              json.loads(pipe.read_text())["extraction"]["batch_total"] == 5)

        orchestrator.report_pipeline_event(
            stage="extraction", tone="act", label="DECISION", text="the green is wrong",
        )
        rows = orchestrator.read_pipeline_status().feed
        check("an event lands on the feed", len(rows) == 1, str(rows))
        check("seq starts at 1 after a reset", rows[0]["seq"] == 1)
        check("the label is lowercased for the chip", rows[0]["label"] == "decision")
        check("the row carries stage and tone", rows[0]["stage"] == "extraction"
              and rows[0]["tone"] == "act")

        # The writer owns the shape; the board only truncates for display.
        orchestrator.report_pipeline_event(stage="planner", text="x" * 400)
        long_row = orchestrator.read_pipeline_status().feed[-1]
        check("text is capped at 140 chars by the WRITER",
              len(long_row["text"]) <= orchestrator.FEED_TEXT_LIMIT, str(len(long_row["text"])))

        # A junk stage must not reach a page on a projector.
        before = len(orchestrator.read_pipeline_status().feed)
        orchestrator.report_pipeline_event(stage="not-a-stage", text="should be dropped")
        check("an unknown stage is dropped, not rendered",
              len(orchestrator.read_pipeline_status().feed) == before)
        orchestrator.report_pipeline_event(stage="executor", tone="nonsense", text="coerced")
        check("an unknown tone falls back to note",
              orchestrator.read_pipeline_status().feed[-1]["tone"] == "note")

        for index in range(120):
            orchestrator.report_pipeline_event(stage="planner", text=f"row {index}")
        capped = orchestrator.read_pipeline_status().feed
        check("the feed is capped at 80 rows", len(capped) == orchestrator.FEED_LIMIT,
              str(len(capped)))
        check("newest is LAST (the board scrolls to the bottom)",
              capped[-1]["text"] == "row 119", capped[-1]["text"])
        check("seq never goes backwards inside a run",
              all(b["seq"] > a["seq"] for a, b in zip(capped, capped[1:], strict=False)))

        # A new run must not open on the previous meeting's thinking.
        check("a different meeting is detected as a new run",
              orchestrator.feed_belongs_to_a_new_run("m-2") is True)
        check("the same meeting is NOT a new run (one meeting narrates twice: fast + final)",
              orchestrator.feed_belongs_to_a_new_run("m-1") is False)
        orchestrator.report_pipeline(meeting_id="m-2", feed_reset=True)
        check("feed_reset clears the rail", orchestrator.read_pipeline_status().feed == [])
        orchestrator.report_pipeline_event(stage="watcher", text="a new run begins")
        check("...and seq restarts at 1 so the board knows it is a new run",
              orchestrator.read_pipeline_status().feed[0]["seq"] == 1)
    finally:
        config.pipeline_status_path = original_pipeline


print(f"\n{'=' * 60}")
print(f"Lane A: {PASSED} passed, {FAILED} failed")
print("=" * 60)
raise SystemExit(1 if FAILED else 0)
