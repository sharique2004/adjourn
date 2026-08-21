"""regret_window — the visible countdown between deciding and sending. (Lane D)

=============================================================================
 THE SEND IS ALREADY DECIDED.

 This is not an approval gate and must never be rendered as a question. No
 "Send this message? [Yes] [No]" — the meeting decided, the table routed, the
 action is going out. The window is a REGRET AFFORDANCE: a few seconds in
 which a human who just heard themselves promise something can say "actually,
 no" before it becomes someone else's notification.

 The difference is the whole product. An approval gate makes the human the
 bottleneck for every action, which is just a worse to-do list. A regret
 window makes the machine act by default and the human intervene by
 exception — and it costs 60 seconds only on the two action kinds that
 cannot be taken back.
=============================================================================

Who owns what:
  * planner.py decides the window length (0 for everything reversible, 60 for
    slack_send and email_send).
  * THIS module holds the countdown: `schedule()` writes an entry to
    state/pending.json and a single daemon timer thread fires it when due,
    unless `cancel()` got there first.
  * orchestrator.py owns the pending.json format and its read/write helpers;
    this module drives them rather than duplicating them, so the board's read
    contract has exactly one implementation.
  * The board reads pending.json for its countdown rings, POSTs a cancel here
    pre-fire, and POSTs an undo to the executor's undo() post-fire. Those two
    are different operations and the UI should say so: cancelling means nothing
    ever happened; undoing means something happened and was reversed.

CRASH SAFETY. pending.json is the source of truth, not the thread. The timer
holds no state the file does not have, so a restart calls
`rearm_after_restart()`: entries still in the future are re-armed, entries whose
moment passed while the process was down fire immediately. A crash inside the
regret window must not silently swallow a message someone was told would go out.

Run a live demo of the machinery (3-second window, one cancel):
    python -m adjourn.executors.regret_window
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .. import results

if TYPE_CHECKING:
    from ..orchestrator import PendingAction
    from ..planner import Action

# How long the thread will sleep at most, even when the next fire is far away.
# Keeps the loop responsive to a pending.json edited by another process.
SCHEDULER_SLEEP_CEILING_SECONDS = 1.0
SCHEDULER_SLEEP_FLOOR_SECONDS = 0.02
SCHEDULER_THREAD_NAME = "adjourn-regret-window"

FireHandler = Callable[["Action"], "results.ExecutorResult"]

_fire_lock = threading.RLock()
_wake = threading.Condition()
_scheduler: dict = {"thread": None, "running": False, "on_fire": None, "path": None}


def _pending_store():
    """orchestrator, imported lazily.

    Deliberately not a module-level import: orchestrator will import the
    executors package once Lane A wires dispatch, and a top-level import here
    would close that circle.
    """
    from .. import orchestrator

    return orchestrator


# --- scheduling -------------------------------------------------------------


def schedule(
    action: Action,
    fire_at: datetime | str | None = None,
    *,
    meeting_title: str = "",
    path: Path | None = None,
) -> PendingAction:
    """Queue an action to fire after its regret window. Returns the pending entry.

    `fire_at` overrides the action's own window — used by tests (3 seconds) and by
    `rearm_after_restart()`, which must preserve the ORIGINAL deadline rather than
    restart the countdown and give the user a second window they were not promised.

    Re-scheduling the same dedup_key replaces the earlier entry instead of adding
    a second countdown for one promise.
    """
    store = _pending_store()
    entry = store.PendingAction.from_action(action)

    if fire_at is not None:
        moment = _as_datetime(fire_at)
        entry.fire_at = moment.isoformat(timespec="seconds")
        created = _parse_iso(entry.created_at) or datetime.now(UTC)
        entry.regret_window_s = max(0, round((moment - created).total_seconds()))

    if not entry.human_preview:
        entry.human_preview = _describe_action(action)

    _upsert_pending(entry, meeting_title=meeting_title, path=path)
    _notify_scheduler()
    print(
        f"[regret_window] {entry.kind} in {entry.seconds_remaining():.0f}s "
        f"— {entry.human_preview or entry.dedup_key}"
    )
    return entry


def cancel(
    dedup_key: str,
    path: Path | None = None,
    *,
    journal_path: Path | None = None,
) -> bool:
    """Stop a scheduled send before it goes out. True when something was stopped.

    This is what the board's cancel button reaches DURING the countdown, and it is
    not an undo: nothing was sent, so there is nothing to reverse. A key that
    already fired returns False — the caller should offer undo() instead.

    The stop is recorded: `journal_path` is where the cancellation record lands
    (the configured journal when None), so the board can show what a human chose
    not to send instead of the card simply disappearing.
    """
    with _fire_lock:
        cancelled = _pending_store().cancel_pending_action(
            dedup_key, path, journal_path=journal_path
        )
    if cancelled:
        print(f"[regret_window] CANCELLED before sending — {dedup_key}")
        _notify_scheduler()
    return cancelled


def countdowns(path: Path | None = None) -> list[PendingAction]:
    """Entries still counting down, soonest first. The board's ring data."""
    waiting = [item for item in _pending_store().read_pending_actions(path) if item.is_waiting]
    return sorted(waiting, key=lambda item: item.fire_at)


def seconds_remaining(dedup_key: str, path: Path | None = None) -> float | None:
    """Seconds left on one countdown, or None when it is not waiting any more."""
    for item in countdowns(path):
        if item.dedup_key == dedup_key:
            return item.seconds_remaining()
    return None


def action_from_pending(entry: PendingAction) -> Action:
    """Rebuild the executable Action from a pending entry.

    pending.json carries everything an executor needs (kind, payload, dedup_key,
    provenance) which is what makes restart-safety possible at all. `segment_id`
    is not carried — dedup is by dedup_key, never by segment id, so nothing
    downstream depends on it.
    """
    from ..planner import Action as PlannedAction

    return PlannedAction(
        kind=entry.kind,
        payload=entry.payload or {},
        dedup_key=entry.dedup_key,
        regret_window_s=entry.regret_window_s,
        quote=entry.quote,
        speaker=entry.speaker,
        meeting_id=entry.meeting_id,
        segment_id="",
        source="regret_window",
    )


# --- firing -----------------------------------------------------------------


def execute_now(action: Action) -> results.ExecutorResult:
    """Default fire handler: dispatch to the executor and journal the result.

    Lane A's orchestrator.fire_action() does its own journalling and memory
    writes, so it should pass ITSELF as `on_fire` — otherwise every countdown
    action lands in the journal twice.
    """
    from . import execute_action

    result = execute_action(action)
    results.append_execution(result, action.dedup_key)
    return result


def fire_due_actions(
    now: datetime | None = None,
    *,
    on_fire: FireHandler | None = None,
    path: Path | None = None,
) -> list[results.ExecutorResult]:
    """Fire every countdown whose moment has arrived. Safe to call from anywhere.

    The claim (waiting -> fired) happens under a lock and before the executor
    runs, so a cancel that lands mid-flight loses the race rather than producing a
    message that is both sent and marked cancelled.
    """
    store = _pending_store()
    handler = on_fire or _scheduler.get("on_fire") or execute_now
    fired: list[results.ExecutorResult] = []

    for entry in store.due_pending_actions(now, path):
        with _fire_lock:
            claimed = store.mark_pending_action_fired(entry.dedup_key, path)
        if not claimed:
            continue  # cancelled or already fired between the read and the claim
        action = action_from_pending(entry)
        print(f"[regret_window] window elapsed — firing {entry.kind} {entry.dedup_key}")
        try:
            result = handler(action)
        except Exception as error:  # noqa: BLE001 — one bad send must not stop the rest
            result = results.ExecutorResult.failed(
                entry.kind, f"fire after regret window failed: {error}",
                quote=entry.quote, speaker=entry.speaker, meeting_id=entry.meeting_id,
            )
            results.append_execution(result, entry.dedup_key)
        fired.append(result)
    return fired


def rearm_after_restart(
    *,
    on_fire: FireHandler | None = None,
    path: Path | None = None,
) -> dict:
    """Recover the countdowns after a crash or restart. Call this at startup.

    Entries whose deadline has passed fire immediately — the user was told the
    message would go out, and a crash is not a cancellation. Entries still in the
    future keep their ORIGINAL fire_at; they are not given a fresh window.

    Returns {"overdue": n, "rearmed": [dedup_key, ...], "results": [...]}.
    """
    waiting = countdowns(path)
    overdue = [item for item in waiting if item.seconds_remaining() <= 0]
    still_waiting = [item.dedup_key for item in waiting if item.seconds_remaining() > 0]
    if waiting:
        print(
            f"[regret_window] restart — {len(overdue)} overdue, "
            f"{len(still_waiting)} still counting down"
        )
    fired = fire_due_actions(on_fire=on_fire, path=path) if overdue else []
    _notify_scheduler()
    return {"overdue": len(overdue), "rearmed": still_waiting, "results": fired}


# --- the timer thread -------------------------------------------------------


def start_scheduler(
    *,
    on_fire: FireHandler | None = None,
    path: Path | None = None,
    fire_overdue: bool = True,
) -> bool:
    """Start the daemon timer thread. Returns False if it was already running.

    One thread for all countdowns, sleeping until the next deadline rather than
    polling hard — and re-reading pending.json every wake, so the file stays the
    source of truth and an entry added by another process is still honoured.
    """
    if _scheduler["running"]:
        return False
    _scheduler["on_fire"] = on_fire
    _scheduler["path"] = path
    _scheduler["running"] = True
    if fire_overdue:
        rearm_after_restart(on_fire=on_fire, path=path)
    thread = threading.Thread(target=_scheduler_loop, name=SCHEDULER_THREAD_NAME, daemon=True)
    _scheduler["thread"] = thread
    thread.start()
    return True


def stop_scheduler(timeout: float = 2.0) -> bool:
    """Stop the timer thread. Pending entries stay on disk and survive to a restart."""
    if not _scheduler["running"]:
        return False
    _scheduler["running"] = False
    _notify_scheduler()
    thread = _scheduler.get("thread")
    if thread is not None:
        thread.join(timeout=timeout)
    _scheduler["thread"] = None
    return True


def is_scheduler_running() -> bool:
    """True while the timer thread is alive."""
    thread = _scheduler.get("thread")
    return bool(_scheduler["running"] and thread is not None and thread.is_alive())


def _scheduler_loop() -> None:
    path = _scheduler.get("path")
    on_fire = _scheduler.get("on_fire")
    while _scheduler["running"]:
        try:
            fire_due_actions(on_fire=on_fire, path=path)
            delay = _seconds_until_next_deadline(path)
        except Exception as error:  # noqa: BLE001 — the loop outlives any one failure
            print(f"[regret_window] scheduler tick failed: {error}")
            delay = SCHEDULER_SLEEP_CEILING_SECONDS
        with _wake:
            _wake.wait(timeout=delay)


def _seconds_until_next_deadline(path: Path | None) -> float:
    """How long the thread may sleep: to the next deadline, capped at one second."""
    waiting = countdowns(path)
    if not waiting:
        return SCHEDULER_SLEEP_CEILING_SECONDS
    soonest = min(item.seconds_remaining() for item in waiting)
    return max(SCHEDULER_SLEEP_FLOOR_SECONDS, min(SCHEDULER_SLEEP_CEILING_SECONDS, soonest))


def _notify_scheduler() -> None:
    """Wake the thread so a new or cancelled countdown is picked up immediately."""
    with _wake:
        _wake.notify_all()


# --- pending-file plumbing --------------------------------------------------


def _upsert_pending(
    entry: PendingAction,
    *,
    meeting_title: str = "",
    path: Path | None = None,
) -> None:
    """Replace any entry with the same dedup_key, then write atomically."""
    store = _pending_store()
    with _fire_lock:
        existing = [
            item for item in store.read_pending_actions(path) if item.dedup_key != entry.dedup_key
        ]
        existing.append(entry)
        store.write_pending_actions(
            existing,
            meeting_id=entry.meeting_id,
            meeting_title=meeting_title,
            path=path,
        )


def _as_datetime(value: datetime | str) -> datetime:
    """Accept a datetime or an ISO string; always return an aware UTC datetime."""
    moment = value if isinstance(value, datetime) else _parse_iso(value)
    if moment is None:
        moment = datetime.now(UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _parse_iso(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _describe_action(action: Action) -> str:
    """A one-line preview when the planner did not supply one."""
    payload = action.payload or {}
    for key in ("human_preview", "text", "subject", "title"):
        value = str(payload.get(key) or "").strip()
        if value:
            return f"{action.kind}: {' '.join(value.split())[:90]}"
    return action.kind


def describe_countdowns(path: Path | None = None) -> dict:
    """Board header data: what is counting down and how long is left."""
    waiting = countdowns(path)
    return {
        "scheduler_running": is_scheduler_running(),
        "waiting": len(waiting),
        "items": [
            {
                "dedup_key": item.dedup_key,
                "kind": item.kind,
                "human_preview": item.human_preview,
                "seconds_remaining": round(item.seconds_remaining(), 1),
                "regret_window_s": item.regret_window_s,
            }
            for item in waiting
        ],
    }


if __name__ == "__main__":  # a 3-second window, end to end, with one cancel
    import json
    import os
    import tempfile
    import time

    # FORCED SIM, unconditionally. A slack_bot_token IS provisioned on this Mac
    # (workspace "Test", bot "adjourn"), so without this line running the demo
    # would post a real message into a real channel. A machinery demo must never
    # be able to send; the transport is exercised by the orchestrator, not here.
    os.environ["ADJOURN_SIM"] = "1"
    print("[regret_window] demo runs with ADJOURN_SIM=1 — nothing can leave the machine\n")

    from ..planner import Action as PlannedAction

    with tempfile.TemporaryDirectory() as temporary:
        pending_path = Path(temporary) / "pending.json"
        journal_path = Path(temporary) / "executions.jsonl"

        def fire_and_journal(action):
            from . import execute_action

            result = execute_action(action)
            results.append_execution(result, action.dedup_key, path=journal_path)
            return result

        going = PlannedAction(
            kind="slack_send",
            payload={"channel": "#eng", "text": "Cache cutover is Friday.",
                     "human_preview": "Slack #eng: cache cutover is Friday"},
            dedup_key="slack_send:eng:cache-cutover-is-friday",
            regret_window_s=3,
            quote="I'll post the cutover date in eng.", speaker="Priya", meeting_id="demo",
        )
        regretted = PlannedAction(
            kind="slack_send",
            payload={"channel": "#eng", "text": "Actually never mind.",
                     "human_preview": "Slack #eng: actually never mind"},
            dedup_key="slack_send:eng:actually-never-mind",
            regret_window_s=3,
            quote="I'll say something I'll regret.", speaker="Priya", meeting_id="demo",
        )

        start_scheduler(on_fire=fire_and_journal, path=pending_path, fire_overdue=False)
        schedule(going, path=pending_path, meeting_title="Eng sync")
        schedule(regretted, path=pending_path, meeting_title="Eng sync")
        time.sleep(1.0)
        print(json.dumps(describe_countdowns(pending_path), indent=2))
        cancel(regretted.dedup_key, pending_path)
        time.sleep(3.0)
        stop_scheduler()

        print(json.dumps(results.summarize_executions(path=journal_path), indent=2))
        for record in results.read_executions(path=journal_path):
            print(f"  fired: {record['dedup_key']} — {record['human_summary']}")
