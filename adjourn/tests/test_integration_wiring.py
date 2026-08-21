"""Integration gate — the seams between lanes, and the bugs found by running them.

Run from the repo root:
    python -m adjourn.tests.test_integration_wiring

Every lane tested itself and passed. Each defect below survived that, because
each one lives in the gap BETWEEN two lanes' assumptions, and gaps have no owner.
They were all found by actually running the demo — twice, and then out loud into
a real microphone — rather than by reading the code. This file is where they stay
fixed.

SAFE to run repeatedly: ADJOURN_SIM=1 is forced before the package is imported, no
network call is made, and every file written goes to a temp directory.

  1. --replay with a target that resolves to nothing used to fall through to the
     FAST path, which reads /api/live — the engine's CURRENT caption buffer,
     regardless of the id it is handed. A typo would have fired real
     follow-through from somebody else's meeting, filed under the typo's name.
  2. Dedup keys carried the model-chosen `topic`, the one field a model is free to
     rephrase between runs. Rehearsal pass 2 placed a SECOND calendar hold for the
     same Friday because one run said "cache layer" and the next said "caching".
  3. Topics fragmented in memory for the same reason, so history lookups missed.
  4. Speech recognition drops the hyphen: saying "SHA-5" aloud produced the
     caption "SHA5". The strict identifier pattern did not match it, and the two
     spellings were two dedup keys — so the ticket would have moved twice.
  5. A ticket title was the whole claim sentence, trailing relative clause and
     all, truncated with an ellipsis on a screen founders were reading.
  6. Undo was not idempotent: removing a label a previous undo had already removed
     raised, and the board showed a red refusal for an issue that was in exactly
     the state the click asked for.
  7. Re-running a rehearsal against a repo that still held the previous run's open
     PR produced a red card, because GitHub answers "already exists" with a bare
     422.
  8. Cloud-mirror threads are daemons, so the LAST actions of a run — the two
     regret-window sends — were killed at process exit before they reached the
     cloud OR the local backlog.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

os.environ["ADJOURN_SIM"] = "1"
# EVERY path this suite can write to is redirected into one throwaway sandbox,
# BEFORE adjourn is imported. §7b fires a real recap_page executor, and an
# executor journals; pointed at the real journal it appended `recap-restart-probe`
# rows to ~/.meetingscribe/executions.jsonl — the file that decides whether the
# demo's own recap is a duplicate. A test that can silence a demo beat is worse
# than no test.
SANDBOX = Path(tempfile.mkdtemp(prefix="adjourn-wiring-"))
RECAP_DIRECTORY = SANDBOX / "recaps"
RECAP_DIRECTORY.mkdir()
os.environ["RECAP_DIR"] = str(RECAP_DIRECTORY)
os.environ["ADJOURN_STATE_DIR"] = str(SANDBOX / "state")
os.environ["EXECUTIONS_JOURNAL"] = str(SANDBOX / "executions.jsonl")
os.environ["ADJOURN_PIPELINE"] = str(SANDBOX / "pipeline.json")
# And the CLOUD graph. Firing an action mirrors it off this machine on a daemon
# thread, so a suite that fires anything writes Execution nodes into the demo's
# own cloud graph unless told not to. Blanking the host makes is_configured()
# false, and the mirror lands in the sandboxed backlog file instead of the
# network — which also exercises the offline path, for free.
os.environ["FALKORDB_CLOUD_HOST"] = ""

from adjourn import config, extraction, orchestrator, planner  # noqa: E402
from adjourn.executors import github_update_executor  # noqa: E402
from adjourn.executors import linear_create_executor, linear_move_executor  # noqa: E402
from adjourn.extraction import EntityReferences, Statement  # noqa: E402

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def statement(
    kind: str,
    topic: str,
    *,
    claim: str = "something happened",
    quote: str = "something happened",
    segment_id: str = "s01",
    **references,
) -> Statement:
    return Statement(
        segment_id=segment_id,
        speaker="Them",
        topic=topic,
        claim=claim,
        kind=kind,
        entity_refs=EntityReferences(**references),
        quote=quote,
    )


MEETING = {"meeting_id": "wiring-test", "title": "Wiring test", "date": "2026-08-21"}


# --- 1. replay refuses what it cannot resolve --------------------------------

print("\n== --replay never falls through to the live caption buffer ==")

check("a path that does not exist replays nothing",
      orchestrator.replay_meeting("/nonexistent/path.json") == [])
check("a meeting id that resolves to no recording replays nothing",
      orchestrator.replay_meeting("20991231-235959") == [])
check("an empty target replays nothing", orchestrator.replay_meeting("") == [])
# The point is not that these return [] — it is that they return [] WITHOUT
# reading /api/live. A run that fired here would have journaled something.
check("refusing to replay journals nothing",
      orchestrator.replay_meeting("definitely-not-a-meeting") == [])

# THE DOOR THE STRING TESTS ABOVE DO NOT COVER. A target that IS a directory
# takes a different branch, and load_meeting() falls back to the FOLDER NAME for
# the meeting id when meeting.json is absent — a non-empty id, so the old guard
# passed it straight through to the fast path, which reads /api/live: the
# engine's CURRENT caption buffer, whichever meeting it belongs to.
# `--replay /tmp` really did fire six actions filed under the meeting id "tmp".
_empty_folder = Path(tempfile.mkdtemp(prefix="adjourn-not-a-recording-"))
check("a real folder holding no meeting.json replays nothing",
      orchestrator.replay_meeting(str(_empty_folder)) == [])
(_empty_folder / "notes.txt").write_text("not a recording", encoding="utf-8")
check("...still nothing when the folder has other files in it",
      orchestrator.replay_meeting(str(_empty_folder)) == [])


# --- 2. dedup keys survive the extractor rephrasing a topic ------------------

print("\n== a dedup key never depends on a name the model chose ==")


def hold_key(topic: str) -> str:
    action = planner.build_calendar_hold(
        statement("deadline", topic, quote="let's review Friday", deadline_text="Friday"),
        None,
        MEETING,
    )
    return action.dedup_key if action else ""


friday_as_cache_layer = hold_key("cache layer")
friday_as_caching = hold_key("caching")
check("a calendar hold has a key at all", bool(friday_as_cache_layer), friday_as_cache_layer)
check("the same date under two topic names is ONE hold",
      friday_as_cache_layer == friday_as_caching,
      f"{friday_as_cache_layer} != {friday_as_caching}")
check("the resolved date is still in the key",
      friday_as_cache_layer.count(":") >= 2, friday_as_cache_layer)


def issue_comment_key(topic: str, kind: str = "decision") -> str:
    action = planner.build_github_update(
        statement(kind, topic, claim="drop Redis, use an LRU", issue_number=2),
        None,
        MEETING,
        None,
    )
    return action.dedup_key if action else ""


check("the same issue under two topic names is ONE comment",
      issue_comment_key("cache layer") == issue_comment_key("caching"),
      f"{issue_comment_key('cache layer')} != {issue_comment_key('caching')}")
check("a different statement KIND on the same issue is a different comment",
      issue_comment_key("caching", "decision") != issue_comment_key("caching", "assignment"))
check("a different issue is a different comment",
      issue_comment_key("caching") != (
          planner.build_github_update(
              statement("decision", "caching", claim="x", issue_number=3), None, MEETING, None
          ).dedup_key
      ))

# EVERY meeting-scoped kind carries the meeting id, with no exceptions. Two kinds
# used to be missing it — linear_create and pull_request_stub — which put them in
# a GLOBAL namespace: once any meeting had filed the webhook ticket or stubbed
# the config-loader PR, every later meeting's identical beat was silently
# dropped, no error and no card. A rehearsal without a reset cost exactly those
# two beats, and it failed in the direction that looks like the planner broke.
print("\n== every meeting-scoped dedup key carries the meeting id ==")

MEETING_SCOPED_KINDS = frozenset({
    "github_update", "calendar_hold", "linear_move", "linear_create",
    "pull_request_stub", "slack_send", "email_send", "recap_page",
})

_scope_probe = [
    statement("decision", "cache layer", claim="drop Redis, use an LRU", issue_number=2),
    statement("deadline", "cache layer", quote="ship it by the 25th",
              deadline_text="the twenty-fifth", segment_id="s02"),
    statement("ticket_request", "webhook signature verification",
              claim="we need a ticket for webhook signature verification", segment_id="s03"),
    statement("pr_intent", "config loader", segment_id="s04",
              quote="I'll open a PR for the config loader tonight",
              claim="will open a pull request for the config loader"),
    statement("progress_report", "streaming adapter", segment_id="s05",
              quote="about eighty percent done, should be in review tomorrow",
              claim="the streaming adapter is 80% done and should be in review tomorrow",
              linear_identifier="SHA-5", percent=80),
    statement("message_commitment", "meeting summary", segment_id="s06",
              quote="I'll Slack the channel the summary",
              claim="will Slack the channel the summary"),
]
for _action in planner.plan(_scope_probe, None, meeting=MEETING):
    if _action.kind not in MEETING_SCOPED_KINDS:
        continue
    check(f"{_action.kind} is scoped to the meeting",
          MEETING["meeting_id"] in _action.dedup_key, _action.dedup_key)

# The same beats in a DIFFERENT meeting must produce a disjoint key set, or the
# second meeting silently inherits the first meeting's "already done".
_other_meeting = {"meeting_id": "a-different-meeting", "title": "Other", "date": "2026-08-28"}
_first = {action.dedup_key for action in planner.plan(_scope_probe, None, meeting=MEETING)}
_second = {action.dedup_key for action in planner.plan(_scope_probe, None, meeting=_other_meeting)}
check("two meetings share no dedup keys at all", _first.isdisjoint(_second),
      f"shared: {sorted(_first & _second)}")
check("...and both meetings planned the same number of actions",
      len(_first) == len(_second) and len(_first) >= 6, f"{len(_first)} vs {len(_second)}")


# --- 3. the deterministic topic stabilizer ----------------------------------

print("\n== topics collapse onto the vocabulary memory already settled on ==")

KNOWN = ["caching", "streaming adapter", "auth migration", "config loader"]


def snapped(topic: str) -> str:
    one = statement("update", topic)
    extraction.snap_topics_to_known_vocabulary([one], KNOWN)
    return one.topic


check("'cache layer' merges into the known 'caching'", snapped("cache layer") == "caching")
check("'the auth migration' merges into 'auth migration'",
      snapped("the auth migration") == "auth migration")
check("a plural merges into its singular",
      snapped("streaming adapters") == "streaming adapter")
check("an unrelated topic is left alone", snapped("demo deck") == "demo deck")
check("a topic already in the vocabulary is left alone", snapped("caching") == "caching")
check("an empty vocabulary changes nothing", (
    lambda s: (extraction.snap_topics_to_known_vocabulary([s], []), s.topic)[1]
)(statement("update", "cache layer")) == "cache layer")
# Two topics that merely OVERLAP must not merge into each other.
check("overlap without containment does not merge",
      snapped_pair := (
          (lambda a, b: (
              extraction.snap_topics_to_known_vocabulary([a], ["cache layer"]),
              extraction.snap_topics_to_known_vocabulary([b], ["cache layer"]),
              a.topic == "cache invalidation",
          )[2])(statement("update", "cache invalidation"), statement("update", "cache layer"))
      ), str(snapped_pair))
check("stemming never shortens a token below four characters",
      extraction.stem_topic_token("ring") == "ring"
      and extraction.stem_topic_token("ping") == "ping")
check("stemming is what makes cache and caching one word",
      extraction.stem_topic_token("cache") == extraction.stem_topic_token("caching"))
check("the merge is deterministic across vocabulary orderings", (
    lambda: snapped("cache layer") == (
        lambda s: (extraction.snap_topics_to_known_vocabulary(
            [s], list(reversed(KNOWN))), s.topic)[1]
    )(statement("update", "cache layer"))
)())


# --- 4. a caption that lost the hyphen still names the ticket ----------------

print("\n== 'SHA5' and 'SHA-5' are one ticket, 'MP3' is not a ticket ==")

check("a hyphenated key resolves", linear_move_executor.find_identifier("SHA-5 is done") == "SHA-5")
check("a caption with no hyphen resolves",
      linear_move_executor.find_identifier("80% done with SHA5") == "SHA-5")
check("a caption with a space resolves",
      linear_move_executor.find_identifier("done with SHA 5 today") == "SHA-5")
check("a codec is not a ticket", linear_move_executor.find_identifier("we shipped MP3") is None)
check("a video codec is not a ticket",
      linear_move_executor.find_identifier("H264 encoding landed") is None)
check("a foreign team key still resolves when hyphenated",
      linear_move_executor.find_identifier("ENG-142 moved") == "ENG-142")
check("a foreign team key with no hyphen is NOT guessed at",
      linear_move_executor.find_identifier("ENG142 moved") is None)
check("nothing in the text means nothing", linear_move_executor.find_identifier("hello") is None)

check("the planner canonicalizes before keying",
      planner.resolve_linear_identifier(
          statement("progress_report", "streaming adapter", linear_identifier="SHA5", percent=80),
          None,
      ) == "SHA-5")
check("both spellings produce the SAME dedup key", (
    lambda a, b: a == b
)(*[
    planner.build_linear_move(
        statement("progress_report", "streaming adapter", linear_identifier=spelling, percent=80,
                  quote="should be in review tomorrow"),
        None, MEETING,
    ).dedup_key
    for spelling in ("SHA5", "SHA-5")
]))


# --- 5. a ticket title is the work, not the sentence -------------------------

print("\n== a ticket title is a name, not a paragraph ==")

title = planner._title_from_claim(
    "File a ticket for webhook signature verification on the ingestion endpoint, "
    "which has only ever been discussed verbally."
)
check("a trailing relative clause is dropped",
      title == "Webhook signature verification on the ingestion endpoint", title)
check("an 'as' clause is dropped",
      planner._title_from_claim("File a ticket for the retry logic, as it has no owner.")
      == "Retry logic")
check("a title never ends mid-word with an ellipsis when it fits",
      not title.endswith("…"), title)
check("a genuinely long title is still truncated",
      planner._title_from_claim("Add " + "very " * 40 + "long thing").endswith("…"))
check("a reason clause is still dropped",
      planner._title_from_claim("Add a cache layer, because Redis is overkill.")
      == "Add a cache layer")


# --- 6 & 7. undo and re-run are idempotent -----------------------------------

print("\n== undoing something already undone is success, not refusal ==")

check("a 404 reads as already-gone",
      github_update_executor.is_already_gone(RuntimeError("gh failed: HTTP 404: Not Found")))
check("'not found' reads as already-gone",
      github_update_executor.is_already_gone(RuntimeError("gh: label not found")))
check("a 403 does NOT read as already-gone",
      not github_update_executor.is_already_gone(RuntimeError("gh failed: HTTP 403: Forbidden")))
check("a 422 does NOT read as already-gone",
      not github_update_executor.is_already_gone(RuntimeError("Validation Failed (HTTP 422)")))
check("the PR executor shares the same rule",
      "is_already_gone" in dir(
          __import__("adjourn.executors.pull_request_stub_executor", fromlist=["x"])))


# --- 7b. a late send updates the recap from ANOTHER process ------------------

# The countdown items survive a crash: pending.json is on disk and a fresh
# --watch resumes them and sends on the original deadline. The recap did not,
# because it lived only in a module-level dict that a restarted process starts
# empty. The message went out and the recap page never grew the card for it —
# the one artifact that outlives the demo, silently short its last two beats.
print("\n== the recap can be re-rendered by a process that never planned it ==")

_recap_meeting = "recap-restart-probe"
_recap_action = planner.build_recap_page(
    [statement("decision", "cache layer", claim="drop Redis, use an LRU", issue_number=2)],
    {"meeting_id": _recap_meeting, "title": "Restart probe", "date": "2026-08-21"},
)
orchestrator.remember_recap_action(_recap_action)

# Exactly what a fresh process sees: the in-memory cache is empty.
orchestrator._recap_actions_by_meeting.clear()
_recovered = orchestrator.load_remembered_recap_action(_recap_meeting)
check("a parked recap action is readable from a cold cache", _recovered is not None)
check("the recovered action is a recap for the right meeting",
      _recovered.kind == "recap_page" and _recovered.meeting_id == _recap_meeting)
check("the recovered action still carries the statements the ledger needs",
      len(_recovered.payload.get("statements") or []) == 1)
check("the recovered action keeps its dedup key",
      _recovered.dedup_key == _recap_action.dedup_key, _recovered.dedup_key)
check("an unknown meeting recovers nothing",
      orchestrator.load_remembered_recap_action("no-such-meeting-at-all") is None)

# And the whole path: refresh with an empty cache must actually write the page.
orchestrator._recap_actions_by_meeting.clear()
orchestrator.refresh_recap_after_late_action(_recap_meeting)
_written = RECAP_DIRECTORY / f"{_recap_meeting}.html"
check("refreshing from a cold cache really wrote the recap", _written.is_file(),
      f"expected {_written}")
orchestrator.recap_action_path(_recap_meeting).unlink(missing_ok=True)
# Firing an action schedules a background mirror write; §8 counts outstanding
# threads, so hand it a clean slate rather than one of ours.
orchestrator.drain_cloud_mirrors()


# --- 8. the cloud mirror is off the hot path, and finishes ------------------

print("\n== the cloud mirror never blocks a fire, and is not killed mid-write ==")

started = threading.Event()
released = threading.Event()


def slow_mirror() -> None:
    started.set()
    released.wait(5)


fire_began = time.monotonic()
thread = orchestrator.mirror_in_background("test", slow_mirror)
elapsed = time.monotonic() - fire_began
check("scheduling a mirror returns immediately", elapsed < 0.5, f"{elapsed:.2f}s")
check("a thread was actually started", thread is not None and started.wait(2))
check("the thread is a daemon", thread.daemon is True)

drain_began = time.monotonic()
outstanding = orchestrator.drain_cloud_mirrors(timeout_seconds=0.4)
check("drain reports the outstanding write", outstanding == 1, str(outstanding))
check("drain gives up rather than hanging forever",
      time.monotonic() - drain_began < 2.0)
released.set()
thread.join(3)
check("the write was allowed to finish", not thread.is_alive())

check("draining with nothing outstanding is a no-op",
      orchestrator.drain_cloud_mirrors(timeout_seconds=0.1) == 0)

exploded = threading.Event()


def exploding_mirror() -> None:
    exploded.set()
    raise RuntimeError("the cloud is on fire")


boom = orchestrator.mirror_in_background("test-failure", exploding_mirror)
boom.join(3)
check("a mirror that raises never escapes its thread", exploded.is_set() and not boom.is_alive())
orchestrator.drain_cloud_mirrors(timeout_seconds=0.5)


# --- configuration is consistent with the workspace that exists --------------

print("\n== configuration matches the workspace this demo actually runs against ==")

check("the Linear team key is configured, not hard-coded to ENG",
      config.linear_team_key() == "SHA", config.linear_team_key())
check("the executor reads the configured key",
      linear_create_executor.default_team_key() == config.linear_team_key())
check("the sim rendering names the real team", (
    lambda payload: payload["teamId"] == f"<team-id for {config.linear_team_key()}>"
)(linear_create_executor.build_issue_input(
    planner.Action(kind="linear_create", payload={"title": "t"}, dedup_key="k"),
    f"<team-id for {linear_create_executor.default_team_key()}>",
)))
check("the board port agrees with the demo script", config.board_port() == 5117,
      str(config.board_port()))
check("the only writable repo is the demo repo",
      config.github_repo() == "sharique2004/adjourn", config.github_repo())
check("ADJOURN_LIVE_KINDS can only ever narrow ADJOURN_SIM",
      config.is_simulation_forced() and "slack_send" not in config.forced_live_action_kinds())


# --- and this suite left the real tree alone ---------------------------------

print("\n== nothing in the shipped tree was written ==")

_real_journal_rows = 0
if config.EXECUTIONS_JOURNAL_PATH.exists():
    _real_journal_rows = sum(
        1 for line in config.EXECUTIONS_JOURNAL_PATH.read_text(encoding="utf-8").splitlines()
        if _recap_meeting in line
    )
check("the real executions journal carries none of this suite's rows",
      _real_journal_rows == 0, f"{_real_journal_rows} row(s)")
check("the sandbox journal is where they went",
      Path(os.environ["EXECUTIONS_JOURNAL"]).exists())
_parked = config.STATE_DIR / "recap_actions" / f"{_recap_meeting}.json"
check("no parked recap action was left in adjourn/state", not _parked.exists(), str(_parked))
check("the recap html went to the sandbox, not adjourn/recaps",
      not (config.RECAPS_DIR / f"{_recap_meeting}.html").exists())

print(f"\n{'=' * 72}")
print(f"{passed} passed, {failed} failed")
print("=" * 72)
raise SystemExit(1 if failed else 0)
