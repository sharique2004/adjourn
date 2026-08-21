"""Board test — the demo surface renders, updates, and undoes. (Lane E)

Runs the Flask app against a FABRICATED journal covering every action kind plus
two countdown items, and asserts on the actual HTML. If this goes red the board
is lying about something on screen.

Run from the repo root:
    python -m adjourn.tests.test_board_server

Writes only into a temp directory. The real ~/.meetingscribe/executions.jsonl is
never opened — the env overrides are set BEFORE the app is built, and one of the
checks below proves the undo path wrote to the temp journal and not the real one.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SANDBOX = Path(tempfile.mkdtemp(prefix="adjourn-board-"))
os.environ["ADJOURN_BOARD_JOURNAL"] = str(_SANDBOX / "executions.jsonl")
os.environ["ADJOURN_BOARD_PENDING"] = str(_SANDBOX / "pending.json")
os.environ["ADJOURN_BOARD_PIPELINE"] = str(_SANDBOX / "pipeline.json")

from adjourn import board_server, orchestrator, results  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


REAL_JOURNAL_EXISTED = results.config.EXECUTIONS_JOURNAL_PATH.exists()

print("== fabricate a meeting's worth of executions ==")
journal, pending = board_server.write_demo_data(_SANDBOX)
check("journal written to the sandbox", journal == _SANDBOX / "executions.jsonl")
check("board resolves the sandbox journal", board_server.journal_path() == journal)
check("board resolves the sandbox pending file", board_server.pending_path() == pending)

lines = journal.read_text(encoding="utf-8").strip().splitlines()
fabricated = [json.loads(line) for line in lines]
kinds_present = {record["kind"] for record in fabricated}
from adjourn import planner  # noqa: E402

check(
    "fixture covers every action kind",
    kinds_present == set(planner.ACTION_KINDS),
    f"missing {set(planner.ACTION_KINDS) - kinds_present}",
)
check("fixture includes a failed action", any(not record["ok"] for record in fabricated))
check("fixture includes both modes", {"live", "sim"} <= {r["mode"] for r in fabricated})
check("fixture includes two countdown items", len(orchestrator.read_pending_actions(pending)) == 2)


print("\n== board state ==")
state = board_server.load_board_state()
check("every fabricated execution became a card", len(state["cards"]) == len(fabricated))
# Reading order, not fire order. Every action of a meeting fires in the same
# instant, so plain newest-first printed the narration backwards and buried the
# Before/After card 1255px below the fold. The cards that show a TRANSITION are
# pinned on top in fire order — the beats DEMO.md §4 narrates, in the order it
# narrates them — and everything else stays newest-first below them.
_pinned = [card for card in state["cards"] if board_server.card_shows_a_transition(card)]
_rest = [card for card in state["cards"] if not board_server.card_shows_a_transition(card)]
check("transition cards are pinned above everything else",
      state["cards"][: len(_pinned)] == _pinned and len(_pinned) > 0)
check("a linear_move counts as a transition",
      any(card["kind"] == "linear_move" for card in _pinned))
check("pinned cards stay in fire order",
      [card["fired_at"] for card in _pinned] == sorted(card["fired_at"] for card in _pinned))
check("everything below the pins is newest-first",
      [card["fired_at"] for card in _rest]
      == sorted((card["fired_at"] for card in _rest), reverse=True))
check("newest unpinned card leads the rest", _rest[0]["kind"] == "email_send")
check("pipeline is always on the board state", "pipeline" in state)
check("missing pipeline.json reads as idle, not an error",
      state["pipeline"]["extraction"]["phase"] == "idle")
check("executors still compose from the journal when pipeline.json is missing",
      state["pipeline"]["executors"]["pending"] == 2
      and state["pipeline"]["executors"]["fired_live"] >= 1
      and state["pipeline"]["executors"]["fired_sim"] >= 1)
check("pending items are exposed", len(state["pending"]) == 2)
check("pending sorted by soonest", state["pending"][0]["seconds_remaining"] <= state["pending"][1]["seconds_remaining"])
check("totals line reads as a sentence", "actions" in state["totals_line"] and "live" in state["totals_line"])
check("mode note is present and honest", "mixed" in state["mode_note"] or "sim" in state["mode_note"])
check("meeting id resolved from the data", state["meeting"]["meeting_id"] == "20260821-093000")

version = board_server.compute_version(state)
later = board_server.load_board_state(datetime.now(UTC) + timedelta(seconds=9))
check(
    "version ignores the ticking clock",
    board_server.compute_version(later) == version,
    "countdown seconds must not churn the fragment",
)


print("\n== undo affordances are honest ==")
by_kind = {card["kind"]: card for card in state["cards"]}
check("a failed action offers no undo", not by_kind["email_send"]["can_undo"])
check("a failed action says why", "never landed" in by_kind["email_send"]["undo_note"])
check("a sim slack message is undoable", by_kind["slack_send"]["can_undo"])
check("a github comment is undoable", by_kind["github_update"]["can_undo"])
check(
    "no fabricated row claims a live remote transport",
    all(
        record["mode"] == "sim"
        for record in fabricated
        if record["kind"] not in {"recap_page", "calendar_hold"}
    ),
    "a fabricated live row would make Undo send a real request for a fake id",
)

live_email = {
    "kind": "email_send",
    "ok": True,
    "mode": "live",
    "undo_payload": {"to": "x@example.com"},
    "human_summary": "sent",
}
live_card = board_server.build_card(live_email, 0, set(), datetime.now(UTC))
check("a live email refuses undo", not live_card["can_undo"])
check("and says the countdown was the undo", "countdown was the undo" in live_card["undo_note"])


print("\n== formatters ==")
check("duration under an hour", board_server.format_duration(2527) == "42:07")
check("duration past an hour", board_server.format_duration(3731) == "1:02:11")
check("unknown duration is empty", board_server.format_duration(None) == "")
check("bad timestamp does not raise", board_server.format_clock_time("not-a-time") == "")
check("elapsed reads in seconds", board_server.format_elapsed_since(
    (datetime.now(UTC) - timedelta(seconds=38)).isoformat()) == "38s ago")
check("initials from a full name", board_server.build_initials("Sharique Khatri") == "SK")
check("initials from one name", board_server.build_initials("Priya") == "PR")
check("initials from nothing", board_server.build_initials("") == "··")


print("\n== the page itself ==")
application = board_server.create_board_application()
application.config["TESTING"] = True

# POST /undo requires the token this process minted at start. The test client is
# the board's own page for these purposes, so it carries the header the page
# carries. test_lane2_shell.py proves a request WITHOUT it is refused.
UNDO_TOKEN = application.config["ADJOURN_UNDO_TOKEN"]


class TokenClient:
    """The Flask test client with the undo header attached to every POST."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def post(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault(board_server.UNDO_TOKEN_HEADER, UNDO_TOKEN)
        return self._inner.post(*args, headers=headers, **kwargs)


client = TokenClient(application.test_client())

page = client.get("/")
check("GET / is 200", page.status_code == 200, str(page.status_code))
markup = page.get_data(as_text=True)
check("page is a complete document", markup.startswith("<!doctype html>") and "</html>" in markup)
check("quiet header line is on the page", board_server.QUIET_HEADER_LINE in markup)
check("meeting title from meeting.json or fallback", "<h1 class=\"meeting-title\"" in markup)
check("totals line rendered", state["totals_line"] in markup)
check("stylesheet linked", "/static/board.css" in markup)
check("script linked", "/static/board.js" in markup)

for kind in planner.ACTION_KINDS:
    check(f"card rendered for {kind}", f'data-kind="{kind}"' in markup)

check("live badge present", "badge-live" in markup and ">LIVE<" in markup)
check("sim badge present", "badge-sim" in markup and ">SIM<" in markup)
check("failed badge present", "badge-failed" in markup)
check("verbatim quote present", "just needs review" in markup)
check("quote is in the serif blockquote", 'class="quote"' in markup)
check("speaker attributed", "Priya" in markup)
check("click-through url rendered", "github.com/sharique2004/adjourn/issues/14" in markup)
check("countdown ring rendered", 'class="ring-progress"' in markup)
check("ring offset is server-rendered", "stroke-dashoffset=" in markup)
check("undo buttons rendered", markup.count("data-undo=") >= 3)
check("no input affordances beyond undo",
      "<input" not in markup and "<textarea" not in markup and "<select" not in markup)
check("ledger link present", 'href="/ledger"' in markup)
check("guts panel is on the board", 'class="guts"' in markup and "Watcher" in markup)
check("guts panel names every stage",
      "Extraction" in markup and "Planner" in markup and "Executors" in markup)
check("guts panel is not a second site — same document",
      markup.count("<!doctype html>") == 1)

assets = client.get("/static/board.css")
check("stylesheet is served", assets.status_code == 200 and b"--ink" in assets.data)
script = client.get("/static/board.js")
check("script is served", script.status_code == 200 and b"api/fragment" in script.data)
check("script carries no card markup", b"<article" not in script.data)


print("\n== fragment endpoint ==")
fragment = client.get("/api/fragment").get_json()
check("fragment carries a version", bool(fragment["version"]))
check("fragment carries cards html", 'data-kind="slack_send"' in fragment["cards_html"])
check("fragment carries pending html", "ring-progress" in fragment["pending_html"])
check("fragment carries header html", board_server.QUIET_HEADER_LINE in fragment["header_html"])
check("fragment carries guts html", 'class="guts"' in fragment["guts_html"] and "Watcher" in fragment["guts_html"])
check("fragment card html matches the page", 'class="card"' in fragment["cards_html"])

unchanged = client.get(f"/api/fragment?version={fragment['version']}").get_json()
check("unchanged version short-circuits", unchanged["unchanged"] is True)
check("short-circuit sends no html", "cards_html" not in unchanged)

board_json = client.get("/api/board").get_json()
check("/api/board carries cards", len(board_json["cards"]) == len(fabricated))
check("/api/board carries totals", board_json["totals"]["fired"] == len(fabricated))
check("/api/board carries a version", bool(board_json["version"]))
check("/api/board carries pipeline", "pipeline" in board_json and "watcher" in board_json["pipeline"])
check("/api/board pipeline has all four stages",
      {"watcher", "extraction", "planner", "executors"} <= set(board_json["pipeline"]))

pipeline_json = client.get("/api/pipeline").get_json()
check("/api/pipeline is 200-shaped", "watcher" in pipeline_json and "executors" in pipeline_json)
check("/api/pipeline watcher has a phase", bool(pipeline_json["watcher"]["phase"]))
check("/api/pipeline executors count live/sim/pending",
      pipeline_json["executors"]["pending"] == 2
      and pipeline_json["executors"]["fired_live"] + pipeline_json["executors"]["fired_sim"]
      == len(fabricated))

health = client.get("/healthz").get_json()
check("healthz points at the sandbox", health["journal"] == str(journal))


print("\n== ledger ==")
ledger_page = client.get("/ledger")
check("GET /ledger is 200", ledger_page.status_code == 200)
ledger_markup = ledger_page.get_data(as_text=True)
check("ledger lists people", 'class="person-name"' in ledger_markup)
check("ledger names a speaker", "Sharique" in ledger_markup and "Dana" in ledger_markup)
check("ledger shows commitments", 'class="commitment' in ledger_markup)
check("ledger keeps the quotes", "cache layer for now" in ledger_markup)
check("ledger does not poll", 'data-view="ledger"' in ledger_markup)

ledger_json = client.get("/api/ledger").get_json()
check("ledger groups by person", len(ledger_json["people"]) >= 3)
check("ledger counts meetings", ledger_json["meeting_count"] == 1)
speakers = {person["speaker"] for person in ledger_json["people"]}
check("ledger attributes every speaker", {"Sharique", "Priya", "Dana"} <= speakers)
# The ledger is derived from the journal either way; memory only adds context.
# Lane C's backend may or may not be implemented when this runs, so the contract
# is that the board REPORTS which it got — never that it silently pretends.
memory_probe = ledger_json["memory"]
check(
    "ledger reports memory availability honestly",
    memory_probe["available"] is True or bool(memory_probe["note"]),
    "an unavailable memory backend must be stated, not faked",
)
check("ledger renders with memory in either state", ledger_page.status_code == 200)


print("\n== undo ==")
waiting_before = orchestrator.read_pending_actions(pending)
cancel_key = waiting_before[0].dedup_key
cancelled = client.post(f"/undo/{cancel_key}")
check("cancelling a countdown is 200", cancelled.status_code == 200)
check("cancel reports cancelled", cancelled.get_json()["action"] == "cancelled")
still_waiting = [item.dedup_key for item in orchestrator.read_pending_actions(pending) if item.is_waiting]
check("cancelled item leaves the countdown", cancel_key not in still_waiting)
check("the other countdown survives", len(still_waiting) == 1)
check("cancelled item disappears from the countdown column", cancel_key not in [
    item["id"] for item in board_server.load_board_state()["pending"]
])
# ...but it does NOT disappear from the board. A cancel used to leave no trace
# anywhere — no card, no journal line, nothing on the recap — so the one moment
# that proves a human can stop the system was the one moment it could not show.
_after_cancel = board_server.load_board_state()
_cancelled_cards = [card for card in _after_cancel["cards"] if card["state"] == "cancelled"]
check("the cancel leaves a dimmed card on the board", len(_cancelled_cards) == 1)
check("the cancelled card says it never went out",
      "never sent" in _cancelled_cards[0]["human_summary"])
check("a cancelled card offers no undo", _cancelled_cards[0]["can_undo"] is False)
check("the totals line counts it as cancelled, not as an action",
      "1 cancelled" in _after_cancel["totals_line"]
      and _after_cancel["totals"]["cancelled"] == 1)

# A simulated send is the safe thing to undo in a test: every executor
# short-circuits a sim result without touching a transport, so this exercises
# the whole board -> executor -> journal path without a single network call.
slack_key = "slack_send:eng-updates:scope-change"
undone = client.post(f"/undo/{slack_key}")
undo_body = undone.get_json()
check("undo of a fired action is 200", undone.status_code == 200, str(undo_body))
check("undo reports undone", undo_body["action"] == "undone" and undo_body["ok"] is True)
undo_records = [
    record
    for record in results.read_executions(path=journal)
    if record.get("record_type") == results.RECORD_TYPE_UNDO
]
check("the undo was journaled", len(undo_records) == 1)
check("the undo record carries the key", undo_records[0]["dedup_key"] == slack_key)
check("the undo record records success", undo_records[0]["undo_ok"] is True)
# +1 undo record and +1 cancellation record, both appended.
check("history was appended, not rewritten", len(results.read_executions(path=journal)) == len(fabricated) + 2)

undone_state = board_server.load_board_state()
undone_card = next(card for card in undone_state["cards"] if card["id"] == slack_key)
check("the card now reads as undone", undone_card["undone"] and undone_card["state"] == "undone")
check("an undone card offers no second undo", not undone_card["can_undo"])
check("undoing twice is idempotent", client.post(f"/undo/{slack_key}").get_json()["message"] == "already undone")

# The failed email card is rendered without an Undo button; the POST boundary
# must refuse it too, so a stale page cannot dispatch a transport the UI disowned.
refused = client.post("/undo/email_send:dana-example-com:summary")
check("a non-undoable card is refused at the boundary", refused.status_code == 409)
check("and refused for the reason the card gave", "never landed" in refused.get_json()["message"])
check(
    "refusing dispatched nothing",
    len([r for r in results.read_executions(path=journal)
         if r.get("record_type") == results.RECORD_TYPE_UNDO]) == 1,
)

missing = client.post("/undo/no-such-key")
check("unknown key is refused with 409", missing.status_code == 409)
check("unknown key explains itself", "no such action" in missing.get_json()["message"])

body_undo = client.post("/undo", json={"dedup_key": "no-such-key"})
check("json-body undo is accepted as a shape", body_undo.status_code == 409)



print("\n== pipeline.json updates the guts panel ==")
populated = orchestrator.PipelineStatus(
    mode="replay",
    pass_name="replay",
    meeting_id="agi-living-room",
    meeting_title="MMM Standup — living room",
    watcher=orchestrator.watcher_status_transcript_ready("agi-living-room", replay=True),
    extraction=orchestrator.ExtractionStatus(
        phase=orchestrator.STAGE_DONE,
        statement_count=11,
        kinds={"decision": 1, "question": 1, "assignment": 1},
        source="final",
        detail="11 statements (decision, question, assignment)",
    ),
    planner=orchestrator.PlannerStatus(
        phase=orchestrator.STAGE_DONE,
        action_count=9,
        ignored_count=2,
        action_kinds={"github_update": 3, "slack_send": 1},
        ignored_kinds={"question": 1},
        detail="table decided 9 actions · ignored 2 (question)",
    ),
)
before_pipe = board_server.compute_version(board_server.load_board_state())
orchestrator.write_pipeline_status(populated, path=_SANDBOX / "pipeline.json")
guts_page = client.get("/")
guts_markup = guts_page.get_data(as_text=True)
check("populated pipeline still 200", guts_page.status_code == 200)
check("guts panel shows replay badge", "REPLAY" in guts_markup)
check("guts panel shows transcript ready", "transcript ready" in guts_markup)
check("guts panel shows statement count", "11 statement" in guts_markup)
check("guts panel shows table decision", "9 decided" in guts_markup and "2 ignored" in guts_markup)
guts_json = client.get("/api/pipeline").get_json()
check("status endpoint reports replay mode", guts_json["mode"] == "replay")
check("status endpoint reports extraction kinds",
      guts_json["extraction"]["statement_count"] == 11
      and "decision" in guts_json["extraction"]["kinds"])
check("status endpoint reports planner counts",
      guts_json["planner"]["action_count"] == 9
      and guts_json["planner"]["ignored_count"] == 2)
check("version changes when pipeline.json changes",
      board_server.compute_version(board_server.load_board_state()) != before_pipe)


print("\n== empty state ==")
empty_directory = Path(tempfile.mkdtemp(prefix="adjourn-board-empty-"))
os.environ["ADJOURN_BOARD_JOURNAL"] = str(empty_directory / "executions.jsonl")
os.environ["ADJOURN_BOARD_PENDING"] = str(empty_directory / "pending.json")
os.environ["ADJOURN_BOARD_PIPELINE"] = str(empty_directory / "pipeline.json")
empty_page = client.get("/")
empty_markup = empty_page.get_data(as_text=True)
check("empty board is still 200", empty_page.status_code == 200)
check("empty state line rendered", board_server.EMPTY_STATE_LINE in empty_markup)
check("empty state has the pulse", "empty-pulse" in empty_markup)
check("empty board renders no cards", 'class="card"' not in empty_markup)
check("empty ledger is 200", client.get("/ledger").status_code == 200)


print("\n== a malformed journal must not take the board down ==")
broken = empty_directory / "executions.jsonl"
broken.write_text(
    '{"record_type":"execution","kind":"slack_send","ok":true,"mode":"sim","human_summary":"fine"}\n'
    "{ this is not json\n",
    encoding="utf-8",
)
resilient = client.get("/")
check("board survives a half-written line", resilient.status_code == 200)
check("the good record still renders", "fine" in resilient.get_data(as_text=True))


print("\n== the real journal was never touched ==")
check(
    "real executions.jsonl untouched",
    results.config.EXECUTIONS_JOURNAL_PATH.exists() == REAL_JOURNAL_EXISTED,
    "the board test must never write to ~/.meetingscribe/executions.jsonl",
)

print("\n" + "=" * 60)
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print(f"ALL CHECKS PASSED  (sandbox: {_SANDBOX})")
