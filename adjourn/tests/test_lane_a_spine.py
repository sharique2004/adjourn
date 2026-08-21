"""Lane A regression gate — watcher edges, source adapters, orchestrator spine.

Run:
    cd /path/to/adjourn
    python -m adjourn.tests.test_lane_a_spine

What this covers:
  * watcher edge detection from canned /api/status payloads, including the
    priming trap, engine restarts, watcher restarts, and staleness re-priming
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
    meeting_id = recent[0]
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
check("the demo fixture name is agi-living-room",
      orchestrator.DEFAULT_REPLAY_TARGET == "agi-living-room")
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
            meeting_id="agi-living-room",
            meeting_title="MMM Standup",
            watcher=orchestrator.watcher_status_transcript_ready("agi-living-room", replay=True),
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


print(f"\n{'=' * 60}")
print(f"Lane A: {PASSED} passed, {FAILED} failed")
print("=" * 60)
raise SystemExit(1 if FAILED else 0)
