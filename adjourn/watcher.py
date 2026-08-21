"""Watcher — notices the moment a meeting ends. IMPLEMENTED (Lane A).

Polls the MeetingScribe engine and emits three events:

  MEETING_STOPPED  — a NEW key appeared in /api/status `jobs`. This is the stop
                     edge AND the meeting id in one observation, which is why
                     /api/status is the trigger bus and /api/record/status (which
                     carries no id) is only corroboration.

  TRANSCRIPT_READY — jobs[<id>].state flipped to "done". The full transcript now
                     exists on disk and the reconcile pass can run.

  SUMMARY_READY    — summary_jobs[<id>].state flipped to "done". LOG ONLY. The
                     auto-summary is a bonus that may never arrive; nothing in
                     Adjourn is allowed to wait on it.

POLL RATES. /api/record/status is polled at 2 Hz because its `recording` boolean
flips the instant the user hits stop; /api/status at 1 Hz because that is where
the id shows up. When the fast poll sees the recording edge it forces an
immediate status poll rather than waiting out the slow tick, which is worth
roughly half a second on stage.

The loop owns no business logic: it detects edges and calls back. It must never
raise out — a dropped connection is a `continue`, not a crash, because the demo
runs while the engine is being started and stopped by hand.

STATE PERSISTENCE. Everything the loop remembers is mirrored to
adjourn/state/watcher.json, so restarting the watcher mid-demo does not re-fire
actions for meetings it already handled. Persisted state that is older than
WATCH_STATE_STALENESS_SECONDS is treated as untrustworthy and re-primed instead:
after a long gap we would otherwise fire the whole back catalogue of meetings
that happened while nothing was running.

Run standalone to watch the edges scroll by:
    python -m adjourn.watcher
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import config, meetingscribe_source, results

DEFAULT_POLL_INTERVAL_SECONDS = 1.0
FAST_POLL_INTERVAL_SECONDS = 0.5

# Persisted state older than this is re-primed rather than trusted. Ten minutes
# is long enough to survive a restart between demo takes and short enough that a
# machine left off overnight does not wake up and fire yesterday's meetings.
WATCH_STATE_STALENESS_SECONDS = 600.0

EVENT_MEETING_STOPPED = "meeting_stopped"
EVENT_TRANSCRIPT_READY = "transcript_ready"
EVENT_SUMMARY_READY = "summary_ready"

JOB_STATE_PROCESSING = "processing"
JOB_STATE_DONE = "done"
JOB_STATE_ERROR = "error"


@dataclass
class MeetingEvent:
    """One observed edge from the engine.

    event       — EVENT_MEETING_STOPPED, EVENT_TRANSCRIPT_READY, EVENT_SUMMARY_READY.
    meeting_id  — the jobs key, which is the MeetingScribe meeting id.
    detected_at — ISO timestamp of the observation, not of the underlying event.
    elapsed     — recording length in seconds, when /api/record/status gave one.
    """

    event: str
    meeting_id: str
    detected_at: str = field(default_factory=results.utc_timestamp)
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "meeting_id": self.meeting_id,
            "detected_at": self.detected_at,
            "elapsed": self.elapsed,
        }


@dataclass
class WatchState:
    """What the loop remembers between polls. Kept explicit so it is testable.

    known_job_ids       — every jobs key seen so far. A key NOT in here is new.
    stopped_meeting_ids — meetings we already announced as stopped.
    ready_meeting_ids   — meetings we already announced as transcript-ready.
    summarized_ids      — meetings whose summary job we already logged.
    was_recording       — last seen recorder flag, for the corroborating edge.
    is_primed           — False until we have absorbed one status payload without
                          firing. Guards the back-catalogue stampede.
    saved_at            — ISO timestamp of the last persist, for staleness checks.
    """

    known_job_ids: set[str] = field(default_factory=set)
    stopped_meeting_ids: set[str] = field(default_factory=set)
    ready_meeting_ids: set[str] = field(default_factory=set)
    summarized_ids: set[str] = field(default_factory=set)
    was_recording: bool = False
    is_primed: bool = False
    saved_at: str = ""

    def to_dict(self) -> dict:
        return {
            "known_job_ids": sorted(self.known_job_ids),
            "stopped_meeting_ids": sorted(self.stopped_meeting_ids),
            "ready_meeting_ids": sorted(self.ready_meeting_ids),
            "summarized_ids": sorted(self.summarized_ids),
            "was_recording": self.was_recording,
            "is_primed": self.is_primed,
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WatchState:
        return cls(
            known_job_ids=set(data.get("known_job_ids") or []),
            stopped_meeting_ids=set(data.get("stopped_meeting_ids") or []),
            ready_meeting_ids=set(data.get("ready_meeting_ids") or []),
            summarized_ids=set(data.get("summarized_ids") or []),
            was_recording=bool(data.get("was_recording", False)),
            is_primed=bool(data.get("is_primed", False)),
            saved_at=data.get("saved_at", ""),
        )

    def age_seconds(self, now: datetime | None = None) -> float:
        """How long ago this state was persisted. Infinite when never persisted."""
        if not self.saved_at:
            return float("inf")
        try:
            stamp = datetime.fromisoformat(self.saved_at)
        except ValueError:
            return float("inf")
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return max(0.0, ((now or datetime.now(UTC)) - stamp).total_seconds())


# --- persistence ------------------------------------------------------------


def watch_state_path() -> Path:
    """adjourn/state/watcher.json — the watcher's memory across restarts."""
    return config.state_directory() / "watcher.json"


def save_watch_state(state: WatchState, path: Path | None = None) -> None:
    """Persist atomically (temp + os.replace), same discipline as pending.json."""
    target = path or watch_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    state.saved_at = results.utc_timestamp()
    temporary_path = target.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    os.replace(temporary_path, target)


def load_watch_state(path: Path | None = None) -> WatchState:
    """Read persisted state. Missing, malformed, or STALE state comes back unprimed.

    Unprimed is the safe default: the next detect_events() call absorbs whatever
    the engine currently reports without firing anything.
    """
    target = path or watch_state_path()
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return WatchState()
    state = WatchState.from_dict(document if isinstance(document, dict) else {})
    if state.is_primed and not state.known_job_ids:
        # A state that claims to be primed but knows no jobs carries no
        # information, and is indistinguishable from one primed against a dead
        # engine. Re-priming costs nothing (it re-absorbs whatever is there) and
        # removes the one way this file could cause a back-catalogue stampede.
        print("[watcher] persisted state is primed but empty — re-priming")
        state.is_primed = False
    if state.age_seconds() > WATCH_STATE_STALENESS_SECONDS:
        print(
            f"[watcher] persisted state is {state.age_seconds():.0f}s old "
            f"(> {WATCH_STATE_STALENESS_SECONDS:.0f}s) — re-priming instead of "
            "firing the back catalogue"
        )
        state.is_primed = False
    return state


# --- edge detection ---------------------------------------------------------


def prime_watch_state(state: WatchState | None = None) -> WatchState:
    """Snapshot the jobs that already exist, so old meetings do not fire on startup.

    Critical for the demo: without priming, the first poll would treat every
    historical job id as a brand-new meeting and fire the whole back catalogue.

    If the engine is unreachable right now, the state stays UNPRIMED and priming
    happens on the first successful poll instead — an engine that is still
    booting must not become an excuse to fire everything.
    """
    state = state or WatchState()
    payload = meetingscribe_source.fetch_engine_status()
    if not payload:
        print("[watcher] engine unreachable at startup — will prime on first successful poll")
        return state
    absorb_status_payload(payload, state)
    state.is_primed = True
    print(
        f"[watcher] primed with {len(state.known_job_ids)} existing job(s); "
        "only new meetings will fire"
    )
    return state


def absorb_status_payload(status_payload: dict, state: WatchState) -> None:
    """Record everything in a payload as already-seen, WITHOUT emitting events.

    This is priming's whole implementation, and it is deliberately separate from
    detect_events() so the two can never drift apart.
    """
    jobs = (status_payload or {}).get("jobs") or {}
    summary_jobs = (status_payload or {}).get("summary_jobs") or {}
    for meeting_id, job in jobs.items():
        state.known_job_ids.add(meeting_id)
        state.stopped_meeting_ids.add(meeting_id)
        if isinstance(job, dict) and job.get("state") == JOB_STATE_DONE:
            state.ready_meeting_ids.add(meeting_id)
    for meeting_id, job in summary_jobs.items():
        if isinstance(job, dict) and job.get("state") == JOB_STATE_DONE:
            state.summarized_ids.add(meeting_id)
    recorder = (status_payload or {}).get("recorder") or {}
    state.was_recording = bool(recorder.get("recording"))


def detect_events(status_payload: dict, state: WatchState) -> list[MeetingEvent]:
    """Pure edge detection: one status payload + prior state -> new events.

    Mutates `state` to record what has now been seen. Pure with respect to the
    outside world (no I/O), which is what makes the watcher testable from a
    handful of canned payloads.

    An empty payload (engine down) yields no events and changes no state — a
    connection failure is not evidence that a meeting ended.

    A job that appears already "done" — the watcher was restarted after a meeting
    finished — emits STOPPED then READY in that order, so the orchestrator still
    runs both phases and the board shows the fast cards before the reconciled ones.
    """
    if not status_payload:
        return []

    if not state.is_primed:
        absorb_status_payload(status_payload, state)
        state.is_primed = True
        return []

    events: list[MeetingEvent] = []
    jobs = status_payload.get("jobs") or {}
    for meeting_id, job in jobs.items():
        job = job if isinstance(job, dict) else {}
        if meeting_id not in state.known_job_ids:
            state.known_job_ids.add(meeting_id)
            if meeting_id not in state.stopped_meeting_ids:
                state.stopped_meeting_ids.add(meeting_id)
                events.append(MeetingEvent(EVENT_MEETING_STOPPED, meeting_id))
        if job.get("state") == JOB_STATE_DONE and meeting_id not in state.ready_meeting_ids:
            state.ready_meeting_ids.add(meeting_id)
            events.append(MeetingEvent(EVENT_TRANSCRIPT_READY, meeting_id))

    summary_jobs = status_payload.get("summary_jobs") or {}
    for meeting_id, job in summary_jobs.items():
        job = job if isinstance(job, dict) else {}
        if job.get("state") == JOB_STATE_DONE and meeting_id not in state.summarized_ids:
            state.summarized_ids.add(meeting_id)
            events.append(MeetingEvent(EVENT_SUMMARY_READY, meeting_id))

    recorder = status_payload.get("recorder") or {}
    state.was_recording = bool(recorder.get("recording"))
    return events


def detect_recording_stop_edge(record_payload: dict, state: WatchState) -> bool:
    """True on the recording True -> False transition. Corroboration, not a trigger.

    Carries no meeting id, so it cannot start the fast fire on its own. What it
    buys is latency: seeing this edge makes the loop poll /api/status immediately
    instead of waiting out the slow tick.
    """
    if not record_payload:
        return False
    now_recording = bool(record_payload.get("recording"))
    stopped = state.was_recording and not now_recording
    state.was_recording = now_recording
    return stopped


# --- the loop ---------------------------------------------------------------


def watch_for_meeting_events(
    on_event: Callable[[MeetingEvent], None],
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    fast_poll_interval_seconds: float = FAST_POLL_INTERVAL_SECONDS,
    state: WatchState | None = None,
    stop_after_seconds: float | None = None,
    on_tick: Callable[[], None] | None = None,
    state_path: Path | None = None,
    persist: bool = True,
    # Default OFF so nothing that merely exercises the loop — a test, a bounded
    # smoke run — can start an application on somebody's Mac. run_orchestrator
    # passes True; that is the "--watch startup" the runbook means.
    launch_engine: bool = False,
) -> None:
    """Poll the engine forever, calling `on_event` for every detected edge.

    Never raises out of the loop: transport errors, malformed payloads, and a
    callback that throws are all logged and stepped over. `stop_after_seconds`
    exists so tests and the smoke script can run a bounded loop.

    `on_tick` fires once per fast tick regardless of events — the orchestrator
    hangs its regret-window countdown on it, so the two never need separate threads.

    `launch_engine` starts MeetingScribe when it is not already up. Priming reads
    the engine, so this has to happen BEFORE prime_watch_state or the watcher
    primes against a dead port and spends its first thirty seconds in backoff
    while the engine it just started comes up behind it.
    """
    if launch_engine:
        meetingscribe_source.ensure_engine_running()

    if state is None:
        state = load_watch_state(state_path) if persist else WatchState()
    if not state.is_primed:
        state = prime_watch_state(state)
        if persist and state.is_primed:
            save_watch_state(state, state_path)

    started_at = time.monotonic()
    next_status_poll_at = 0.0
    consecutive_failures = 0

    while True:
        if stop_after_seconds is not None and time.monotonic() - started_at >= stop_after_seconds:
            return

        now = time.monotonic()
        force_status_poll = False

        # Fast poll: the recorder boolean, twice a second.
        try:
            record_payload = meetingscribe_source.fetch_record_status()
            if detect_recording_stop_edge(record_payload, state):
                print("[watcher] recording stopped — pulling status now for the id")
                force_status_poll = True
        except Exception as error:  # noqa: BLE001 — the loop outlives every error
            print(f"[watcher] record-status poll failed: {error}")

        # Slow poll: the trigger bus.
        if force_status_poll or now >= next_status_poll_at:
            next_status_poll_at = now + poll_interval_seconds
            try:
                status_payload = meetingscribe_source.fetch_engine_status()
            except Exception as error:  # noqa: BLE001
                print(f"[watcher] status poll failed: {error}")
                status_payload = {}

            if not status_payload:
                consecutive_failures += 1
                backoff = _backoff_seconds(consecutive_failures)
                if consecutive_failures in (1, 5, 20) or consecutive_failures % 60 == 0:
                    print(
                        f"[watcher] engine unreachable at {config.meetingscribe_base_url()} "
                        f"({consecutive_failures} in a row) — retrying in {backoff:.1f}s"
                    )
                next_status_poll_at = now + backoff
            else:
                if consecutive_failures:
                    print("[watcher] engine reachable again")
                consecutive_failures = 0
                events = detect_events(status_payload, state)
                if events and persist:
                    save_watch_state(state, state_path)
                for event in events:
                    try:
                        on_event(event)
                    except Exception as error:  # noqa: BLE001 — one bad handler must not end the demo
                        print(f"[watcher] handler for {event.event} {event.meeting_id} raised: {error}")

        if on_tick is not None:
            try:
                on_tick()
            except Exception as error:  # noqa: BLE001
                print(f"[watcher] tick handler raised: {error}")

        time.sleep(fast_poll_interval_seconds)


def _backoff_seconds(consecutive_failures: int) -> float:
    """1, 2, 4, 8 ... capped at 15s. Long enough to be quiet, short enough to notice a restart."""
    return min(15.0, float(2 ** min(consecutive_failures - 1, 4)))


if __name__ == "__main__":
    config.ensure_state_directories()

    def _print_event(event: MeetingEvent) -> None:
        print(f"[watcher] {event.event} {event.meeting_id}")

    print(f"[watcher] watching {config.meetingscribe_base_url()} — Ctrl-C to stop")
    try:
        watch_for_meeting_events(_print_event)
    except KeyboardInterrupt:
        print("\n[watcher] stopped")
