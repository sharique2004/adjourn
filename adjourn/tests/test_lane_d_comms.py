"""Lane D regression gate — slack_send, email_send, calendar_hold, regret_window.

    cd /path/to/adjourn
    python -m adjourn.tests.test_lane_d_comms

FORCED SIM. ADJOURN_SIM=1 is set before anything is imported, because a real
slack_bot_token IS provisioned in this Mac's Keychain — without the force, the
Slack tests would post to a live workspace. Nothing in this file may leave the
machine. Journals and pending files are written to temp directories only; the
one thing that touches the package's own state/ is the calendar .ics, which is
cleaned up at the end.

The live calendar attempt (which can raise a macOS permission dialog) runs ONLY
with ADJOURN_CALENDAR_LIVE_ATTEMPT=1, so the default suite stays headless-safe.
"""

from __future__ import annotations

import os

os.environ["ADJOURN_SIM"] = "1"  # before any adjourn import — mode is read at call time

import json  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

from adjourn import config, orchestrator, results  # noqa: E402
from adjourn.executors import (  # noqa: E402
    calendar_hold_executor,
    email_send_executor,
    regret_window,
    slack_send_executor,
)
from adjourn.planner import Action  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}{(' — ' + detail) if detail else ''}")


def make_action(kind: str, payload: dict, dedup_key: str, **extra) -> Action:
    return Action(
        kind=kind,
        payload=payload,
        dedup_key=dedup_key,
        regret_window_s=extra.pop("regret_window_s", 0),
        quote=extra.pop("quote", "I'll take care of that today."),
        speaker=extra.pop("speaker", "Priya"),
        meeting_id=extra.pop("meeting_id", "lane-d-test"),
    )


# =============================================================================
print("\n== mode: nothing in this suite may go live ==")
from adjourn import secrets_store  # noqa: E402

check("ADJOURN_SIM forces sim", config.is_simulation_forced() is True)
check("slack decides sim", secrets_store.decide_mode(("slack_bot_token",), label="t") == "sim")


# =============================================================================
print("\n== calendar: the deterministic deadline parser ==")
resolve = calendar_hold_executor.resolve_deadline
monday = datetime(2026, 8, 17, 9, 0)      # a Monday
friday = datetime(2026, 8, 21, 9, 0)      # a Friday
monday_evening = datetime(2026, 8, 17, 18, 30)

CASES = [
    ("Friday", monday, "2026-08-21T10:00:00"),
    ("by Friday", monday, "2026-08-21T10:00:00"),
    ("friday", friday, "2026-08-21T10:00:00"),          # said on a Friday means today
    ("next friday", monday, "2026-08-28T10:00:00"),     # always skips a week
    ("tomorrow", monday, "2026-08-18T10:00:00"),
    ("tomorrow at 3pm", monday, "2026-08-18T15:00:00"),
    ("tonight", monday, "2026-08-17T20:00:00"),
    ("the 27th", monday, "2026-08-27T10:00:00"),
    ("the 3rd", monday, "2026-09-03T10:00:00"),         # already past -> next month
    ("end of week", monday, "2026-08-21T10:00:00"),
    ("next week", monday, "2026-08-24T10:00:00"),
    ("in 2 days", monday, "2026-08-19T10:00:00"),
    ("in three weeks", monday, "2026-09-07T10:00:00"),
    ("in 4 hours", monday, "2026-08-17T13:00:00"),
    ("2026-09-01", monday, "2026-09-01T10:00:00"),
    ("aug 27", monday, "2026-08-27T10:00:00"),
    ("8/27", monday, "2026-08-27T10:00:00"),
    ("thursday morning", monday, "2026-08-20T09:00:00"),
    ("wednesday at 09:30", monday, "2026-08-19T09:30:00"),
    ("end of the month", monday, "2026-08-31T10:00:00"),
    ("eod", monday, "2026-08-17T17:00:00"),
]
for text, reference, expected in CASES:
    got = resolve(text, reference)
    check(f"{text!r} -> {expected}", got is not None and got.isoformat() == expected, str(got))

check("a phrase with no date resolves to None", resolve("sometime soon", monday) is None)
check("empty text resolves to None", resolve("", monday) is None)
check("None-ish text resolves to None", resolve("   ", monday) is None)
check(
    "a same-day deadline already past slides forward, never backward",
    resolve("eod", monday_evening) > monday_evening,
    str(resolve("eod", monday_evening)),
)
check("the parser is pure — same input, same output",
      resolve("friday", monday) == resolve("friday", monday))
window = calendar_hold_executor.resolve_deadline_window("friday", 45, monday)
check("window honours duration_minutes",
      window is not None and (window[1] - window[0]) == timedelta(minutes=45))


# =============================================================================
print("\n== calendar: event construction and the .ics ==")
hold_action = make_action(
    "calendar_hold",
    {"title": "Hold: cache cutover", "deadline_text": "friday",
     "duration_minutes": 30, "human_preview": "Calendar hold Fri: cache cutover"},
    "calendar_hold:cache-cutover",
    quote="I'll have the cache cutover done by Friday.",
)
event = calendar_hold_executor.build_calendar_event(hold_action)
check("uid is deterministic for a dedup_key",
      event["uid"] == calendar_hold_executor.build_calendar_event(hold_action)["uid"])
check("uid differs for a different action",
      event["uid"] != calendar_hold_executor.build_event_uid("calendar_hold:something-else"))
check("duration honoured",
      (datetime.fromisoformat(event["ends_at"]) - datetime.fromisoformat(event["starts_at"]))
      == timedelta(minutes=30))
check("notes carry the verbatim quote", "cache cutover done by Friday" in event["notes"])
check("notes name the speaker", "Priya" in event["notes"])

ics = calendar_hold_executor.render_ics_document(event)
check("ics is a single VEVENT",
      ics.count("BEGIN:VEVENT") == 1 and ics.count("END:VCALENDAR") == 1)
check("ics carries the uid", event["uid"] in ics)
check("ics has a 60-minute alarm", "TRIGGER:-PT60M" in ics)
check("ics escapes commas in the summary",
      "\\," in calendar_hold_executor.render_ics_document(
          {**event, "title": "Hold: a, b", "notes": ""}))
check("ics uses CRLF line endings", "\r\n" in ics)

explicit = calendar_hold_executor.build_calendar_event(make_action(
    "calendar_hold", {"starts_at": "2026-08-22T15:00:00", "title": "Explicit"},
    "calendar_hold:explicit"))
check("an explicit starts_at wins over the parser", explicit["starts_at"] == "2026-08-22T15:00:00")

try:
    calendar_hold_executor.build_calendar_event(make_action(
        "calendar_hold", {"title": "no date anywhere", "deadline_text": "soon-ish"},
        "calendar_hold:no-date"))
    check("an unresolvable deadline raises rather than guessing", False, "did not raise")
except ValueError:
    check("an unresolvable deadline raises rather than guessing", True)


print("\n== calendar: execute in sim, then undo ==")
hold_result = calendar_hold_executor.execute(hold_action)
check("sim hold succeeds", hold_result.ok is True, hold_result.human_summary)
check("sim hold is badged sim", hold_result.mode == "sim", hold_result.mode)
rendered_hold = hold_result.undo_payload["payload"]
ics_path = Path(rendered_hold["ics_path"])
check("the .ics is really written even in sim", ics_path.is_file(), str(ics_path))
check("sim card renders the exact ics", "BEGIN:VEVENT" in rendered_hold["ics"])
check("summary says it was simulated", "simulated" in hold_result.human_summary.lower())
check("undo of a sim hold removes the .ics",
      calendar_hold_executor.undo(hold_result) is True and not ics_path.exists())

bad_hold = calendar_hold_executor.execute(make_action(
    "calendar_hold", {"deadline_text": "whenever"}, "calendar_hold:whenever"))
check("an unresolvable hold fails honestly", bad_hold.ok is False)
check("a failure that never sent is badged sim", bad_hold.mode == "sim", bad_hold.mode)


print("\n== calendar: the Swift helper ==")
binary = calendar_hold_executor.ensure_calendar_binary()
check("calendar_add.swift compiles and caches", binary is not None and Path(binary).exists(),
      str(binary))
if binary:
    probe = subprocess.run([binary, "probe"], capture_output=True, text=True, timeout=30)
    check("probe exits 0 and never prompts", probe.returncode == 0, probe.stderr)
    try:
        parsed = json.loads(probe.stdout or "{}")
    except ValueError:
        parsed = {}
    check("probe reports an authorization state", "authorization" in parsed, probe.stdout)
    check("probe reports whether we may write", "canWrite" in parsed)
    bad_args = subprocess.run([binary, "add", "--title", "x"], capture_output=True, text=True)
    check("add without a date exits 4 (bad arguments)", bad_args.returncode == 4,
          str(bad_args.returncode))
    access = calendar_hold_executor.describe_calendar_access()
    check("the access probe is cached, not re-run per action",
          access is calendar_hold_executor.describe_calendar_access())

if os.environ.get("ADJOURN_CALENDAR_LIVE_ATTEMPT") == "1":
    print("\n== calendar: ONE real EventKit attempt (may prompt) ==")
    calendar_hold_executor.forget_calendar_access()
    live_action = make_action(
        "calendar_hold",
        {"title": "Adjourn test hold — safe to delete", "deadline_text": "tomorrow",
         "duration_minutes": 15},
        "calendar_hold:adjourn-live-test")
    del os.environ["ADJOURN_SIM"]
    live_result = calendar_hold_executor.execute(live_action)
    os.environ["ADJOURN_SIM"] = "1"
    print(f"  attempt -> mode={live_result.mode} ok={live_result.ok}: {live_result.human_summary}")
    check("the live attempt produced a result, granted or not", live_result.ok is True)
    check("undo cleans the attempt up", calendar_hold_executor.undo(live_result) is True)


# =============================================================================
print("\n== slack: message composition ==")
slack_action = make_action(
    "slack_send",
    {"channel": "#eng", "text": "Cache cutover lands Friday.",
     "human_preview": "Slack #eng: cache cutover lands Friday"},
    "slack_send:eng:cache-cutover-lands-friday",
    regret_window_s=60,
    quote="I'll post the cutover date in eng.",
)
body = slack_send_executor.build_message_body(slack_action)
check("a payload channel is IGNORED — the destination is configuration",
      body["channel"] != "#eng")
check("the configured channel is what gets addressed",
      body["channel"] in config.ALLOWED_SLACK_CHANNELS, body["channel"])
check("the message leads with the quote", body["text"].startswith("Priya in the meeting:"))
check("the promised text follows the quote", "Cache cutover lands Friday." in body["text"])
check("link unfurling is off", body["unfurl_links"] is False)
check("a context block carries the attribution",
      any(block["type"] == "context" for block in body["blocks"]))
check("the recap is linked from the context block",
      "recap" in json.dumps(body["blocks"]).lower())
check("build_message_body is pure",
      slack_send_executor.build_message_body(slack_action) == body)

no_channel = make_action("slack_send", {"text": "hello"}, "slack_send:hello")
check("a payload channel does NOT waive the channel secret",
      len(slack_send_executor.required_secrets_for(slack_action)) == 2)
check("without a payload channel both secrets are required",
      len(slack_send_executor.required_secrets_for(no_channel)) == 2)
check("a missing channel still renders a complete preview",
      slack_send_executor.build_message_body(no_channel)["channel"] != "")

print("\n== slack: the channel allowlist ==")
try:
    slack_send_executor.assert_channel_is_allowed("#general")
    check("#general is refused", False, "did not raise")
except PermissionError:
    check("#general is refused", True)
try:
    slack_send_executor.assert_channel_is_allowed("#all-test")
    check("#all-test is permitted", True)
except PermissionError as error:
    check("#all-test is permitted", False, str(error))
check("an undo record with an id but no channel name is refused",
      slack_send_executor.undo(results.ExecutorResult(
          ok=True, kind="slack_send", mode=results.MODE_LIVE,
          external_id="1.1", url=None, human_summary="x",
          undo_payload={"channel": "C0DEADBEEF", "ts": "1.1"},
      )) is False)

print("\n== slack: the wellx-ai guard ==")
try:
    slack_send_executor.assert_workspace_is_allowed("wellx-ai")
    check("wellx-ai is refused", False, "did not raise")
except PermissionError:
    check("wellx-ai is refused", True)
try:
    slack_send_executor.assert_workspace_is_allowed("WellX-AI  ")
    check("the guard is case- and space-insensitive", False, "did not raise")
except PermissionError:
    check("the guard is case- and space-insensitive", True)
slack_send_executor.assert_workspace_is_allowed("Test")
check("a demo workspace is permitted", True)

print("\n== slack: execute in sim, then undo ==")
slack_result = slack_send_executor.execute(slack_action)
check("sim send succeeds", slack_result.ok is True)
check("sim send is badged sim", slack_result.mode == "sim")
rendered_slack = slack_result.undo_payload["payload"]
check("sim renders the exact endpoint",
      rendered_slack["url"] == "https://slack.com/api/chat.postMessage")
check("sim renders the Bearer header shape",
      rendered_slack["headers"]["Authorization"].startswith("Bearer "))
check("sim never renders the real token",
      "xoxb-" not in json.dumps(rendered_slack))
check("sim renders the exact body", rendered_slack["body"] == body)
check("undo of a sim send is honest-true", slack_send_executor.undo(slack_result) is True)
check("undo of a live send with no ts refuses",
      slack_send_executor.undo(results.ExecutorResult(
          ok=True, kind="slack_send", external_id="x", url=None,
          human_summary="posted", mode="live")) is False)


# =============================================================================
print("\n== email: the address book ==")
os.environ["ADJOURN_ADDRESS_BOOK"] = "Div=div@example.com; Priya Nair=priya@example.com\nbroken"
book = email_send_executor.read_address_book()
check("book parses two entries", len(book) == 2, str(book))
check("lookup by first name", email_send_executor.look_up_address("Div") == "div@example.com")
check("lookup is case-insensitive", email_send_executor.look_up_address("DIV") == "div@example.com")
check("lookup by full name",
      email_send_executor.look_up_address("Priya Nair") == "priya@example.com")
check("lookup by first name of a full-name entry",
      email_send_executor.look_up_address("Priya") == "")
check("an unknown name resolves to nothing, never a guess",
      email_send_executor.look_up_address("Nobody") == "")
check("a malformed entry is skipped, not raised", "broken" not in book)

email_action = make_action(
    "email_send",
    {"person": "Div", "subject": "Cache layer timeline",
     "body_text": "Here's the timeline we agreed on.",
     "human_preview": "Email Div: cache layer timeline"},
    "email_send:div:cache-layer-timeline",
    regret_window_s=60,
    quote="I'll email Div the timeline tonight.",
)
addresses, unresolved = email_send_executor.resolve_recipients(email_action)
check("a name resolves through the book", addresses == ["div@example.com"], str(addresses))
check("nothing is left unresolved", unresolved == [])

explicit_action = make_action(
    "email_send", {"to": ["someone@example.com", "Div"]}, "email_send:explicit")
addresses, unresolved = email_send_executor.resolve_recipients(explicit_action)
check("explicit addresses and book names combine",
      addresses == ["someone@example.com", "div@example.com"], str(addresses))

unknown_action = make_action("email_send", {"person": "Mallory"}, "email_send:mallory")
addresses, unresolved = email_send_executor.resolve_recipients(unknown_action)
check("an unknown person yields no address", addresses == [])
check("an unknown person is reported by name", unresolved == ["Mallory"], str(unresolved))

print("\n== email: message construction ==")
message = email_send_executor.build_email_message(email_action, "sharique@example.com")
check("To is the resolved address", message["To"] == "div@example.com")
check("Subject comes from the payload", message["Subject"] == "Cache layer timeline")
check("a Message-ID exists before sending", bool(message["Message-ID"]))
preview = email_send_executor.render_email_preview(message)
check("the body carries the verbatim quote",
      "I'll email Div the timeline tonight." in preview["body"])
check("the body carries the written message", "timeline we agreed on" in preview["body"])
check("the preview is full RFC822", preview["rfc822"].startswith("From: sharique@example.com"))
check("the preview names the transport", "smtp.gmail.com:587" in preview["transport"])
check("an html alternative is present", "text/html" in preview["rfc822"])

derived = email_send_executor.build_email_message(
    make_action("email_send", {"to": ["a@b.com"]}, "email_send:derived",
                quote="I'll send the vendor the revised numbers."),
    "me@example.com")
check("a missing subject is derived from what was said",
      derived["Subject"].startswith("Follow-up: I'll send the vendor"), derived["Subject"])

try:
    email_send_executor.build_email_message(unknown_action, "me@example.com")
    check("an unresolvable recipient raises rather than guessing", False, "did not raise")
except ValueError as error:
    check("an unresolvable recipient raises rather than guessing", True)
    check("the error names the unresolved person", "Mallory" in str(error), str(error))

print("\n== email: recap inlining ==")
with tempfile.TemporaryDirectory() as temporary:
    recap = Path(temporary) / "recap.html"
    recap.write_text("<html><body><h1>Eng sync</h1><p>7 actions</p></body></html>")
    with_recap = make_action(
        "email_send", {"to": ["a@b.com"], "recap_path": str(recap)}, "email_send:recap")
    inlined = email_send_executor.render_email_preview(
        email_send_executor.build_email_message(with_recap, "me@example.com"))
    check("the recap is inlined in the html part", "7 actions" in inlined["rfc822"])
    check("the recap body is unwrapped, not nested",
          inlined["rfc822"].count("<html>") <= 1)
check("a missing recap is simply omitted",
      email_send_executor.read_recap_html(
          make_action("email_send", {"recap_path": "/nope/none.html"}, "k")) == "")

print("\n== email: execute in sim, then undo ==")
email_result = email_send_executor.execute(email_action)
check("sim email succeeds", email_result.ok is True)
check("sim email is badged sim", email_result.mode == "sim")
check("sim renders the whole message",
      "Subject: Cache layer timeline" in email_result.undo_payload["payload"]["rfc822"])
check("undo of a sim email is honest-true", email_send_executor.undo(email_result) is True)
check("undo of a live email is honest-false",
      email_send_executor.undo(results.ExecutorResult(
          ok=True, kind="email_send", external_id="x", url=None,
          human_summary="sent", mode="live")) is False)
check("the undo tooltip says why it refuses",
      "irreversible" in email_send_executor.describe_undo_capability(
          results.ExecutorResult(ok=True, kind="email_send", external_id="x", url=None,
                                 human_summary="sent", mode="live")).lower())
unresolved_result = email_send_executor.execute(unknown_action)
check("an unresolvable recipient fails the card rather than emailing anyone",
      unresolved_result.ok is False and unresolved_result.external_id is None)


# =============================================================================
print("\n== regret_window: schedule, cancel, fire ==")
with tempfile.TemporaryDirectory() as temporary:
    pending_path = Path(temporary) / "pending.json"
    journal_path = Path(temporary) / "executions.jsonl"
    fired_keys: list[str] = []

    def record_fire(action: Action):
        fired_keys.append(action.dedup_key)
        result = results.ExecutorResult.simulated(
            action.kind, f"fired {action.dedup_key}", rendered_payload=action.payload)
        results.append_execution(result, action.dedup_key, path=journal_path)
        return result

    going = make_action("slack_send", {"channel": "#eng", "text": "going out",
                                       "human_preview": "Slack #eng: going out"},
                        "slack_send:eng:going-out", regret_window_s=3)
    regretted = make_action("email_send", {"to": ["a@b.com"], "body_text": "regretted"},
                            "email_send:regretted", regret_window_s=3)

    entry = regret_window.schedule(going, path=pending_path, meeting_title="Eng sync")
    regret_window.schedule(regretted, path=pending_path, meeting_title="Eng sync")
    check("two countdowns are waiting", len(regret_window.countdowns(pending_path)) == 2)
    check("the countdown is ~3s", 2.0 <= entry.seconds_remaining() <= 3.0,
          str(entry.seconds_remaining()))
    check("pending.json is what the board reads",
          len(orchestrator.read_pending_actions(pending_path)) == 2)
    check("a preview reaches the card", entry.human_preview == "Slack #eng: going out")
    check("re-scheduling the same key does not add a second countdown",
          len(regret_window.countdowns(pending_path)) == 2
          and regret_window.schedule(going, path=pending_path)
          and len(regret_window.countdowns(pending_path)) == 2)
    check("nothing has fired yet",
          regret_window.fire_due_actions(on_fire=record_fire, path=pending_path) == [])

    check("cancel stops a waiting send",
          regret_window.cancel("email_send:regretted", pending_path,
                               journal_path=journal_path) is True)
    check("cancel is idempotent",
          regret_window.cancel("email_send:regretted", pending_path,
                               journal_path=journal_path) is False)
    # Stopping a send is a decision, so it leaves a record the board can show.
    check("cancelling leaves a cancellation record",
          [record.get("dedup_key")
           for record in results.read_cancellations(path=journal_path)]
          == ["email_send:regretted"])
    check("a cancelled entry leaves the countdown list",
          [item.dedup_key for item in regret_window.countdowns(pending_path)]
          == ["slack_send:eng:going-out"])

    regret_window.start_scheduler(on_fire=record_fire, path=pending_path, fire_overdue=False)
    check("the timer thread is running", regret_window.is_scheduler_running() is True)
    deadline = time.monotonic() + 8
    while not fired_keys and time.monotonic() < deadline:
        time.sleep(0.1)
    regret_window.stop_scheduler()

    check("the surviving action fired after its window", fired_keys == ["slack_send:eng:going-out"],
          str(fired_keys))
    check("the cancelled action never fired", "email_send:regretted" not in fired_keys)
    check("the fired action is journaled",
          results.read_fired_dedup_keys(path=journal_path) == {"slack_send:eng:going-out"})
    check("nothing is left counting down", regret_window.countdowns(pending_path) == [])
    check("cancelling after the fire refuses",
          regret_window.cancel("slack_send:eng:going-out", pending_path,
                               journal_path=journal_path) is False)
    check("the scheduler stopped cleanly", regret_window.is_scheduler_running() is False)

print("\n== regret_window: crash safety ==")
with tempfile.TemporaryDirectory() as temporary:
    pending_path = Path(temporary) / "pending.json"
    recovered: list[str] = []

    def note_fire(action: Action):
        recovered.append(action.dedup_key)
        return results.ExecutorResult.simulated(action.kind, "recovered", rendered_payload={})

    overdue = make_action("slack_send", {"channel": "#eng", "text": "was mid-window"},
                          "slack_send:eng:was-mid-window", regret_window_s=60)
    future = make_action("slack_send", {"channel": "#eng", "text": "still waiting"},
                         "slack_send:eng:still-waiting", regret_window_s=60)
    # The process "died" mid-window: one deadline passed while it was down.
    regret_window.schedule(overdue, datetime.now(UTC) - timedelta(seconds=5), path=pending_path)
    future_entry = regret_window.schedule(
        future, datetime.now(UTC) + timedelta(seconds=45), path=pending_path)

    report = regret_window.rearm_after_restart(on_fire=note_fire, path=pending_path)
    check("the overdue send fires on restart, not silently vanishes",
          recovered == ["slack_send:eng:was-mid-window"], str(recovered))
    check("restart reports what it recovered", report["overdue"] == 1)
    check("a future countdown is re-armed, not fired",
          report["rearmed"] == ["slack_send:eng:still-waiting"], str(report["rearmed"]))
    remaining = regret_window.seconds_remaining("slack_send:eng:still-waiting", pending_path)
    check("the re-armed countdown keeps its ORIGINAL deadline",
          remaining is not None and 40 <= remaining <= 45, str(remaining))
    check("seconds_remaining is None for a key that is not waiting",
          regret_window.seconds_remaining("slack_send:eng:was-mid-window", pending_path) is None)

    described = regret_window.describe_countdowns(pending_path)
    check("the board gets ring data", described["waiting"] == 1)
    check("the ring knows the full window",
          described["items"][0]["regret_window_s"] == future_entry.regret_window_s)

print("\n== regret_window: rebuilding an action from the file ==")
rebuilt = regret_window.action_from_pending(orchestrator.PendingAction.from_action(
    make_action("slack_send", {"channel": "#eng", "text": "x"}, "slack_send:eng:x",
                regret_window_s=60)))
check("a rebuilt action keeps its kind and key",
      rebuilt.kind == "slack_send" and rebuilt.dedup_key == "slack_send:eng:x")
check("a rebuilt action keeps its provenance",
      rebuilt.quote and rebuilt.speaker == "Priya" and rebuilt.meeting_id == "lane-d-test")
check("a rebuilt action keeps its payload", rebuilt.payload["channel"] == "#eng")


# =============================================================================
print("\n== cleanup ==")
holds = calendar_hold_executor.holds_directory()
leftovers = list(holds.glob("adjourn-*.ics")) if holds.exists() else []
for leftover in leftovers:
    leftover.unlink()
check("no .ics files left behind", not list(holds.glob("adjourn-*.ics")) if holds.exists() else True)
check("the real executions journal was never touched by this suite",
      not any(record.get("meeting_id") == "lane-d-test"
              for record in results.read_executions(path=config.executions_journal_path())))

print("\n" + "=" * 60)
if failures:
    print(f"FAILED ({len(failures)} of {checks}): {failures}")
    raise SystemExit(1)
print(f"ALL {checks} CHECKS PASSED")
