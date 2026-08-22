"""Board test — the demo surface renders, updates, and undoes. (Lane E)

Runs the Flask app against a FABRICATED journal covering every action kind plus
two send-holds, and asserts on the actual HTML. If this goes red the board
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
import time as _time
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
check("fixture includes two send-holds", len(orchestrator.read_pending_actions(pending)) == 2)
check("both holds wait for Send",
      all(item.hold_for_send for item in orchestrator.read_pending_actions(pending)))


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
check("pending items wait for Send", all(item["hold_for_send"] for item in state["pending"]))
check("totals line reads as a sentence", "actions" in state["totals_line"] and "live" in state["totals_line"])
check("mode note is present and honest", "mixed" in state["mode_note"] or "sim" in state["mode_note"])
check("meeting id resolved from the data", state["meeting"]["meeting_id"] == "20260821-093000")
check("follow-through is grouped by meeting", len(state["meeting_groups"]) == 1)
check("the meeting group wears a title, not a pile",
      bool(state["meeting_groups"][0]["display_title"]))
check("drafts sit under that meeting",
      len(state["meeting_groups"][0]["pending"]) == 2)

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
check("a github comment is recalled, not undone",
      by_kind["github_update"]["undo_label"] == "Recall (sim)",
      by_kind["github_update"]["undo_label"])
check("four workbenches are described",
      [tile["slug"] for tile in state["domains"]] == ["linear", "slack", "github", "email"])
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
check("and says it cannot be unsent", "cannot be unsent" in live_card["undo_note"])


print("\n== formatters ==")
check("duration under an hour", board_server.format_duration(2527) == "42:07")
check("duration past an hour", board_server.format_duration(3731) == "1:02:11")
check("unknown duration is empty", board_server.format_duration(None) == "")
check("bad timestamp does not raise", board_server.format_clock_time("not-a-time") == "")
check("elapsed reads in seconds", board_server.format_elapsed_since(
    (datetime.now(UTC) - timedelta(seconds=38)).isoformat()) == "38s ago")
check("a meeting stamp has a date and a clock",
      "·" in board_server.format_meeting_when("2026-08-21T21:13:00+00:00")
      and "21" in board_server.format_meeting_when("2026-08-21T21:13:00+00:00"))
check("initials from a full name", board_server.build_initials("Sharique Khatri") == "SK")
check("initials from one name", board_server.build_initials("Priya") == "PR")
check("initials from nothing", board_server.build_initials("") == "··")

print("\n== follow-through groups by meeting ==")
_older = {
    "meeting_id": "prior-standup",
    "fired_at": "2026-08-14T18:00:00+00:00",
    "kind": "github_update",
    "state": "ok",
}
_newer = {
    "meeting_id": "living-room-standup",
    "fired_at": "2026-08-21T21:00:00+00:00",
    "kind": "linear_move",
    "state": "ok",
}
_draft = {
    "meeting_id": "living-room-standup",
    "hold_for_send": True,
    "created_at": "2026-08-21T21:01:00+00:00",
    "id": "slack_send:x",
}
_groups = board_server.build_meeting_groups([_older, _newer], [_draft])
check("two meetings make two groups", len(_groups) == 2)
check("the newest meeting leads", _groups[0]["meeting_id"] == "living-room-standup")
check("the older meeting follows", _groups[1]["meeting_id"] == "prior-standup")
check("a draft stays under its own meeting", len(_groups[0]["pending"]) == 1)
check("the other meeting does not inherit that draft", _groups[1]["pending"] == [])
check("each group is named", all(group["display_title"] for group in _groups))


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
check("the page is named Follow-through", ">Follow-through</h1>" in markup)
check("home is a four-domain grid", 'class="domain-grid"' in markup)
check("the four workbenches are on the home", all(
    f'data-domain="{slug}"' in markup for slug in ("linear", "slack", "github", "email")
))
check("home does not dump every card", 'class="meeting-block"' not in markup)
check("totals line rendered", state["totals_line"] in markup)
check("stylesheet linked", "/static/board.css" in markup)
check("script linked", "/static/board.js" in markup)
check("ledger link present", 'href="/ledger"' in markup)
check("no stray select controls", "<select" not in markup)

github_page = client.get("/d/github")
check("GET /d/github is 200", github_page.status_code == 200)
github_markup = github_page.get_data(as_text=True)
check("a domain page is named for that workbench", ">GitHub</h1>" in github_markup)
check("work is grouped under a meeting header", 'class="meeting-block"' in github_markup)
check("the meeting header names the meeting", 'class="meeting-block-title"' in github_markup)
check("github cards render on the GitHub page", 'data-kind="github_update"' in github_markup)
check("sim badge present", "badge-sim" in github_markup and ">SIM<" in github_markup)
check("a local recap is still live", by_kind["recap_page"]["mode"] == "live")
check("quote is in the serif blockquote", 'class="quote"' in github_markup)
check("speaker attributed", "Sharique" in github_markup)
check("click-through url rendered", "github.com/sharique2004/adjourn/issues/14" in github_markup)
check("guts panel is on a domain page", 'class="guts"' in github_markup and "Watcher" in github_markup)
check("guts panel names every stage",
      "Extraction" in github_markup and "Planner" in github_markup and "Executors" in github_markup)
check("guts panel is not a second site — same document",
      github_markup.count("<!doctype html>") == 1)
check("Recall is the word on a GitHub card", "Recall" in github_markup)

slack_markup = client.get("/d/slack").get_data(as_text=True)
check("Ready to send is on Slack", "Ready to send" in slack_markup)
check("draft editor rendered", 'class="compose-body"' in slack_markup and 'class="send-now"' in slack_markup)

email_markup = client.get("/d/email").get_data(as_text=True)
check("failed badge present", "badge-failed" in email_markup)

linear_markup = client.get("/d/linear").get_data(as_text=True)
check("Linear cards render on the Linear page", 'data-kind="linear_create"' in linear_markup)
check("verbatim quote present", "just needs review" in linear_markup)

for kind in planner.ACTION_KINDS:
    if kind in board_server.KIND_TO_DOMAIN:
        haystack = {
            "linear": linear_markup,
            "slack": slack_markup,
            "github": github_markup,
            "email": email_markup,
        }[board_server.KIND_TO_DOMAIN[kind]]
        check(f"card rendered for {kind}", f'data-kind="{kind}"' in haystack)

check("undo buttons rendered", github_markup.count("data-undo=") >= 1)

assets = client.get("/static/board.css")
check("stylesheet is served", assets.status_code == 200 and b"--ink" in assets.data)
script = client.get("/static/board.js")
check("script is served", script.status_code == 200 and b"api/fragment" in script.data)
check("script carries no card markup", b"<article" not in script.data)


print("\n== fragment endpoint ==")
fragment = client.get("/api/fragment").get_json()
check("fragment carries a version", bool(fragment["version"]))
check("home fragment is the four-domain grid", "domain-tile" in fragment["cards_html"])
check("home fragment does not dump Slack cards", 'data-kind="slack_send"' not in fragment["cards_html"])
check("fragment carries header html", board_server.QUIET_HEADER_LINE in fragment["header_html"])
check("home fragment has no guts column", fragment["guts_html"] == "")

slack_fragment = client.get("/api/fragment?domain=slack").get_json()
check("Slack fragment carries cards html", 'data-kind="slack_send"' in slack_fragment["cards_html"])
check("Slack fragment carries pending html", "compose-body" in slack_fragment["cards_html"])
check("Slack fragment carries guts html", 'class="guts"' in slack_fragment["guts_html"] and "Watcher" in slack_fragment["guts_html"])
check("fragment card html matches the page", 'class="card"' in slack_fragment["cards_html"])

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
check("cancelling a draft is 200", cancelled.status_code == 200)
check("cancel reports cancelled", cancelled.get_json()["action"] == "cancelled")
still_waiting = [item.dedup_key for item in orchestrator.read_pending_actions(pending) if item.is_waiting]
check("cancelled item leaves the draft list", cancel_key not in still_waiting)
check("the other draft survives", len(still_waiting) == 1)
check("cancelled item disappears from Ready to send", cancel_key not in [
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

print("\n== send a draft ==")
os.environ["ADJOURN_SIM"] = "1"
from adjourn import config as _config  # noqa: E402
_original_journal = _config.executions_journal_path
_original_pending = _config.pending_actions_path
_config.executions_journal_path = board_server.journal_path
_config.pending_actions_path = board_server.pending_path
try:
    send_key = still_waiting[0]
    sent = client.post(
        f"/send/{send_key}",
        json={"text": "Thursday at two still works.", "subject": "Thursday review"},
    )
    send_body = sent.get_json()
    check("sending a draft is 200", sent.status_code == 200, str(send_body))
    check("send reports sent", send_body["ok"] is True and send_body["action"] == "sent")
    check("the draft left Ready to send",
          send_key not in [
              item.dedup_key
              for item in orchestrator.read_pending_actions(pending)
              if item.is_waiting
          ])
    check("the send was journaled",
          send_key in results.read_fired_dedup_keys(path=journal))
    refused_again = client.post(f"/send/{send_key}", json={"text": "again"})
    check("a second Send refuses",
          refused_again.status_code == 409
          and refused_again.get_json()["action"] == "refused")
finally:
    _config.executions_journal_path = _original_journal
    _config.pending_actions_path = _original_pending

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
# +1 undo, +1 cancellation, +1 send — all appended.
check("history was appended, not rewritten", len(results.read_executions(path=journal)) == len(fabricated) + 3)

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
    meeting_id="living-room-standup",
    meeting_title="MMM Standup — living room",
    watcher=orchestrator.watcher_status_transcript_ready("living-room-standup", replay=True),
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
guts_page = client.get("/d/github")
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
check("empty follow-through is still the four-domain grid", 'class="domain-grid"' in empty_markup)
check("empty tiles say nothing yet", empty_markup.count("Nothing yet") >= 4)
check("empty board renders no action cards", 'data-key=' not in empty_markup)
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
check("the good record still renders", "fine" in client.get("/d/slack").get_data(as_text=True))


# =============================================================================
print("\n== the rail: the live region, and where its rows come from ==")

# The rail is the pipeline panel moved into the right-hand column so it can be
# permanently open and animate through a run. Two sources feed its live region:
# the orchestrator's own `feed` (per statement, per decision, per fire — the
# contract is scratchpad/pipeline-feed-spec.md), and, when that is absent, a
# coarser feed the board DERIVES from the counters and the journal so the rail
# moves whether or not the orchestrator has implemented the spec yet.

rail_directory = Path(tempfile.mkdtemp(prefix="adjourn-board-rail-"))
rail_pipeline = rail_directory / "pipeline.json"
os.environ["ADJOURN_BOARD_JOURNAL"] = str(rail_directory / "executions.jsonl")
os.environ["ADJOURN_BOARD_PENDING"] = str(rail_directory / "pending.json")
os.environ["ADJOURN_BOARD_PIPELINE"] = str(rail_pipeline)

# --- derived, from nothing but counters ---
rail_pipeline.write_text(json.dumps({
    "mode": "replay",
    "watcher": {"phase": "transcript_ready", "detail": "final transcript on disk"},
    "extraction": {"phase": "done", "statement_count": 4, "segment_count": 28,
                   "silent_count": 24, "kinds": {"decision": 2, "commitment": 2}},
    "planner": {"phase": "done", "action_count": 3, "ignored_count": 2,
                "action_kinds": {"github_update": 2, "slack_send": 1},
                "ignored_kinds": {"chatter": 2}},
}), encoding="utf-8")

derived = client.get("/api/pipeline").get_json()
check("with no feed written the board derives one", derived["feed_source"] == "derived")
check("the derived feed is not empty", len(derived["feed"]) >= 5, str(len(derived["feed"])))
derived_texts = " | ".join(row["text"] for row in derived["feed"])
check("the derived feed shows what the planner DECLINED",
      "chatter → ignored" in derived_texts, derived_texts)
check("the derived feed shows what it routed",
      "routed → github_update" in derived_texts, derived_texts)
check("the derived feed shows the silent lines",
      "24 lines produced nothing" in derived_texts, derived_texts)
check("every derived row is marked derived",
      all(row["derived"] for row in derived["feed"]))
check("derived rows are ordered watcher → extraction → planner",
      [row["stage"] for row in derived["feed"]][:2] == ["watcher", "extraction"],
      str([row["stage"] for row in derived["feed"]]))

# --- the orchestrator's own feed wins ---
rail_pipeline.write_text(json.dumps({
    "mode": "replay",
    "watcher": {"phase": "transcript_ready", "detail": "final transcript on disk"},
    "extraction": {"phase": "running", "statement_count": 2, "segment_count": 28,
                   "batch_index": 3, "batch_total": 4, "kinds": {"decision": 2}},
    "planner": {"phase": "idle"},
    "feed": [
        {"seq": 1, "stage": "watcher", "tone": "note", "label": "transcript ready",
         "at": "2026-08-21T02:00:00Z", "text": "28 lines on disk"},
        {"seq": 2, "stage": "extraction", "tone": "act", "label": "decision",
         "at": "2026-08-21T02:00:01Z",
         "text": "the join button green is wrong — use the muted slate"},
        {"seq": 3, "stage": "planner", "tone": "decline", "label": "hypothetical",
         "at": "2026-08-21T02:00:02Z", "text": "declined: negated"},
        "not a dict at all",
        {"seq": "junk", "stage": "planner", "tone": "ignore", "label": "chatter",
         "text": "chatter → ignored"},
        {"seq": 9, "stage": "extraction", "tone": "act", "label": "x" * 90,
         "text": "y" * 400},
    ],
}), encoding="utf-8")

streamed = client.get("/api/pipeline").get_json()
check("a written feed beats the derived one", streamed["feed_source"] == "orchestrator")
check("junk rows are dropped, not fatal", len(streamed["feed"]) == 5,
      str(len(streamed["feed"])))
check("a statement's own words reach the rail",
      any("muted slate" in row["text"] for row in streamed["feed"]))
check("a refusal reaches the rail with its reason",
      any(row["tone"] == "decline" and "negated" in row["text"] for row in streamed["feed"]))
check("a row with a junk seq still renders, numbered by position",
      any(row["label"] == "chatter" for row in streamed["feed"]))
check("an over-long row is clipped, not truncated mid-word",
      all(len(row["text"]) <= board_server.FEED_TEXT_LIMIT for row in streamed["feed"]))
check("an over-long chip is clipped to the chip budget",
      all(len(row["label"]) <= 24 for row in streamed["feed"]))
check("feed_seq is the high-water mark", streamed["feed_seq"] == 9,
      str(streamed["feed_seq"]))
check("batch progress is reported as a line and a fraction",
      streamed["extraction"]["batch_line"] == "batch 3/4"
      and streamed["extraction"]["batch_fraction"] == 0.75,
      str(streamed["extraction"]))

rail_markup = client.get("/d/github").get_data(as_text=True)
check("the rail is beside the cards, not above them",
      'class="board-rail"' in rail_markup
      and rail_markup.index('class="board-cards"') < rail_markup.index('class="board-rail"'))
check("the rail renders its rows", rail_markup.count('class="rail-row"') == 5,
      str(rail_markup.count('class="rail-row"')))
check("each row carries the seq the animation keys on",
      'data-seq="9"' in rail_markup)
check("the batch bar renders only when there is a batch total",
      'class="rail-batch"' in rail_markup and "batch 3/4" in rail_markup)
check("the rail still names all four stages",
      all(f'data-stage="{stage}"' in rail_markup
          for stage in ("watcher", "extraction", "planner", "executors")))
check("board.js animates only rows it has not seen",
      b"is-new" in client.get("/static/board.js").data)

# THE 1s POLL HAS TO NOTICE A NEW EVENT. If the version hash ignored the feed the
# rail would freeze mid-run while the counters stayed the same.
before_version = board_server.compute_version(board_server.load_board_state())
document = json.loads(rail_pipeline.read_text(encoding="utf-8"))
document["feed"].append({"seq": 10, "stage": "executor", "tone": "act",
                         "label": "slack_send", "text": "Posted to #all-test · live"})
rail_pipeline.write_text(json.dumps(document), encoding="utf-8")
after_version = board_server.compute_version(board_server.load_board_state())
check("a new feed event changes the fragment version", before_version != after_version)

# A HALF-WRITTEN FILE IS READ ONCE A SECOND. It must be an idle rail, not a 500.
rail_pipeline.write_text('{"feed": [{"seq": 1, "stage": "extra', encoding="utf-8")
check("a half-written pipeline.json is idle, not an error page",
      client.get("/").status_code == 200)
check("...and the rail falls back to derived",
      client.get("/api/pipeline").get_json()["feed_source"] == "derived")
rail_pipeline.unlink()
check("a missing pipeline.json is still 200", client.get("/").status_code == 200)


# =============================================================================
print("\n== never-blank open: an idle board shows the last meeting's receipts ==")

# Demo-night state is a DELETED journal — reset_demo archives it as
# executions.<ts>.jsonl. The board used to open on one italic sentence and a
# pulse, which contradicts the pitch in the very first frame.

blank_directory = Path(tempfile.mkdtemp(prefix="adjourn-board-blank-"))
os.environ["ADJOURN_BOARD_JOURNAL"] = str(blank_directory / "executions.jsonl")
os.environ["ADJOURN_BOARD_PENDING"] = str(blank_directory / "pending.json")
os.environ["ADJOURN_BOARD_PIPELINE"] = str(blank_directory / "pipeline.json")

archive = blank_directory / "executions.20260814T090000Z.jsonl"
archive.write_text("\n".join(json.dumps(row) for row in [
    {"record_type": "execution", "kind": "github_update", "ok": True, "mode": "live",
     "human_summary": "Commented on issue #2 — the cache decision changed",
     "dedup_key": "prior:issue-2:decision", "meeting_id": "prior-standup",
     "fired_at": "2026-08-14T09:00:00+00:00", "speaker": "Sam"},
    {"record_type": "execution", "kind": "slack_send", "ok": True, "mode": "sim",
     "human_summary": "Posted the standup summary to #all-test",
     "dedup_key": "prior:slack", "meeting_id": "prior-standup",
     "fired_at": "2026-08-14T09:00:20+00:00", "speaker": "Sharique"},
]) + "\n", encoding="utf-8")

blank_state = board_server.load_board_state()
check("an empty journal still finds the archived one",
      blank_state["last_adjourned"] is not None)
check("the receipts come from the archive, and it says so in a sentence",
      blank_state["last_adjourned"]["source"] == "journal"
      and "machine" in blank_state["last_adjourned"]["source_note"])
# This note is printed twice on the opening frame — in the masthead beside the
# totals and under the receipts themselves. A timestamped filename in either
# place is a line nobody in the room can use and one more thing to read.
check("and it names no file while doing it",
      ".jsonl" not in blank_state["last_adjourned"]["source_note"],
      blank_state["last_adjourned"]["source_note"])
check("the receipts are the last meeting's actions",
      any("cache decision changed" in row["summary"]
          for row in blank_state["last_adjourned"]["rows"]))
check("nothing is fabricated — the count is the archive's own",
      blank_state["last_adjourned"]["count"] == 2)

blank_markup = client.get("/").get_data(as_text=True)
check("the idle board renders a Last adjourned header",
      "Last adjourned" in blank_markup and 'class="lastadj"' in blank_markup)
check("the four workbenches sit above the receipts",
      'class="domain-grid"' in blank_markup)
check("the fragment carries it too, so a poll cannot blank the page",
      "Last adjourned" in client.get("/api/fragment").get_json()["cards_html"])

# THE HEADER IS HALF THE COLD OPEN. Receipts below and "0 actions · 0 live · 0 sim"
# above is a screen that argues with itself in the first frame — the panel says
# this machine did six things last meeting, the masthead says nothing has ever
# happened. The count is still never invented: it is the panel's own.
check("a cold open's totals do not read as just '0 actions'",
      "0 actions" not in blank_state["totals_line"], blank_state["totals_line"])
check("...they point at the receipts underneath, with the panel's own number",
      "2 receipts" in blank_state["totals_line"]
      and "Nothing yet" in blank_state["totals_line"],
      blank_state["totals_line"])
# The mode note used to be a COPY of the receipts panel's source line, printed
# eighty pixels above it — the same sentence twice in the opening frame. It is
# the mode note, so it reports the mode: nothing has been sent or simulated yet.
check("...and the mode note reports the mode rather than repeating the panel",
      blank_state["mode_note"]
      and blank_state["mode_note"] != blank_state["last_adjourned"]["source_note"]
      and "transport" in blank_state["mode_note"],
      f"{blank_state['mode_note']!r}")
check("the served masthead carries that line, not a dead zero",
      blank_state["totals_line"] in blank_markup and "0 actions" not in blank_markup)
check("a board WITH actions still gets the plain running count",
      "actions" in state["totals_line"] and "live" in state["totals_line"],
      state["totals_line"])

# A board WITH cards must not pay for the memory read or show a second history.
os.environ["ADJOURN_BOARD_JOURNAL"] = str(_SANDBOX / "executions.jsonl")
os.environ["ADJOURN_BOARD_PENDING"] = str(_SANDBOX / "pending.json")
check("a board with cards does not compute last-adjourned",
      board_server.load_board_state()["last_adjourned"] is None)
check("...and does not render the header",
      "Last adjourned" not in client.get("/").get_data(as_text=True))

# An archive that is empty, unreadable, or absent must not be an error page.
os.environ["ADJOURN_BOARD_JOURNAL"] = str(blank_directory / "executions.jsonl")
archive.write_text("{ not json\n", encoding="utf-8")
check("an unreadable archive degrades quietly", client.get("/").status_code == 200)
archive.unlink()


# =============================================================================
print("\n== never-blank open, second source: memory, after a full reseed ==")

# THE REGRESSION THIS PINS. `reset_demo --reseed` is the demo-night command, and
# a reseed that also cleared the rotated journals would leave the archive branch
# with nothing to read. If the memory branch were ever to regress with it, the
# cold open would silently fall back to the exact dead frame the brief names:
# "0 actions · Adjourned. Waiting for the next meeting." So this drives the
# memory branch DIRECTLY, with no archive on disk at all.


class _FakePrior:
    """One row of what memory remembers. Same attributes memory_store yields."""

    def __init__(self, claim, speaker, kind, segment_id):
        self.segment_id = segment_id
        self.speaker = speaker
        self.topic = "cache"
        self.claim = claim
        self.kind = kind
        self.meeting_id = "prior-standup"
        self.meeting_date = "2026-08-14"


class _FakeMemory:
    backend = "falkor"
    closed = False

    def known_topics(self, limit=40):
        return ["cache", "webhooks"]

    def find_prior_commitments(self, topic, limit=10):
        if topic != "cache":
            return []
        return [
            _FakePrior("We are going with Redis for the session cache", "Sam",
                       "decision", "seg-1"),
            _FakePrior("Priya will own the webhook signature ticket", "Priya",
                       "commitment", "seg-2"),
        ]

    def close(self):
        _FakeMemory.closed = True


from adjourn import memory_store as _memory_store  # noqa: E402

# A reseeded box has NOTHING in flight either — the pending file the fixture left
# behind would otherwise keep the never-blank branch from ever being reached.
os.environ["ADJOURN_BOARD_JOURNAL"] = str(blank_directory / "executions.jsonl")
os.environ["ADJOURN_BOARD_PENDING"] = str(blank_directory / "pending.json")

_real_open_memory = _memory_store.open_memory
_memory_store.open_memory = lambda *args, **kwargs: _FakeMemory()
try:
    check("no archive is left on disk for this branch to cheat with",
          board_server.archived_journal_paths() == [])
    reseeded = board_server.load_board_state()
    check("with no journal and no archive the board still reads memory",
          reseeded["last_adjourned"] is not None)
    check("and it says memory is where the rows came from",
          reseeded["last_adjourned"]["source"] == "memory"
          and "remembers" in reseeded["last_adjourned"]["source_note"])
    check("the rows are what memory actually holds, not a placeholder",
          any("Redis" in row["summary"] for row in reseeded["last_adjourned"]["rows"]))
    check("the memory handle is closed after the read", _FakeMemory.closed)
    reseeded_markup = client.get("/").get_data(as_text=True)
    check("the reseeded cold open renders receipts, not the dead pulse alone",
          "Last adjourned" in reseeded_markup and "Redis" in reseeded_markup)
    check("...and its totals are not '0 actions' either",
          "0 actions" not in reseeded_markup, reseeded["totals_line"])
finally:
    _memory_store.open_memory = _real_open_memory

# A memory backend that will not open is a quiet miss, never a 500 — a demo box
# with the container down must still serve the board.
_memory_store.open_memory = lambda *args, **kwargs: (_ for _ in ()).throw(
    RuntimeError("falkor is not listening")
)
try:
    check("an unreachable memory backend degrades to the pulse, not an error page",
          client.get("/").status_code == 200)
    check("...and last_adjourned is simply absent",
          board_server.load_board_state()["last_adjourned"] is None)
finally:
    _memory_store.open_memory = _real_open_memory


# =============================================================================
print("\n== the rail advances: pipeline.json is the contract, batch by batch ==")

# B2's half of the feed contract. The board's reader is complete; what this pins
# is that the reader is pointed at the file the ORCHESTRATOR writes, and that a
# batch counter climbing inside one extraction pass actually moves the bar and
# re-renders the 1s fragment. A bar that only moves when the stage changes is the
# freeze the brief calls out ("the rail must move, not freeze idle").

os.environ.pop("ADJOURN_BOARD_PIPELINE", None)
check("with no override the board reads exactly the orchestrator's own path",
      board_server.pipeline_path() == board_server.config.pipeline_status_path(),
      f"{board_server.pipeline_path()} vs {board_server.config.pipeline_status_path()}")

advance_directory = Path(tempfile.mkdtemp(prefix="adjourn-board-advance-"))
advance_pipeline = advance_directory / "pipeline.json"
os.environ["ADJOURN_BOARD_JOURNAL"] = str(advance_directory / "executions.jsonl")
os.environ["ADJOURN_BOARD_PENDING"] = str(advance_directory / "pending.json")
os.environ["ADJOURN_BOARD_PIPELINE"] = str(advance_pipeline)


def _write_batch(index: int, total: int, rows: list[dict]) -> None:
    """One orchestrator write, exactly the shape the feed spec describes."""
    advance_pipeline.write_text(json.dumps({
        "updated_at": "2026-08-21T02:14:07Z",
        "mode": "replay",
        "pass_name": "replay",
        "meeting_id": "living-room-standup",
        "watcher": {"phase": "transcript_ready", "detail": "final transcript on disk"},
        "extraction": {"phase": "running", "statement_count": len(rows),
                       "segment_count": 28, "silent_count": 0,
                       "batch_index": index, "batch_total": total,
                       "kinds": {}, "source": "claude", "detail": "running"},
        "planner": {"phase": "idle"},
        "feed": rows,
    }), encoding="utf-8")


_rows: list[dict] = []
_seen_widths: list[float] = []
_seen_versions: list[str] = []
for _batch in range(1, 5):
    _rows.append({"seq": len(_rows) + 1, "at": "2026-08-21T02:14:07Z",
                  "stage": "extraction", "tone": "note", "label": "batch",
                  "text": f"batch {_batch}/4 · 7 lines"})
    _write_batch(_batch, 4, list(_rows))
    _api = client.get("/api/pipeline").get_json()
    _seen_widths.append(_api["extraction"]["batch_fraction"])
    _seen_versions.append(board_server.compute_version(board_server.load_board_state()))
    check(f"batch {_batch}/4 is reported verbatim",
          _api["extraction"]["batch_line"] == f"batch {_batch}/4"
          and _api["extraction"]["batch_index"] == _batch,
          str(_api["extraction"]))

check("the bar fraction climbs monotonically across the pass",
      _seen_widths == sorted(_seen_widths) and _seen_widths[0] < _seen_widths[-1]
      and _seen_widths[-1] == 1.0, str(_seen_widths))
check("every batch boundary changes the fragment version, so the 1s poll sees it",
      len(set(_seen_versions)) == 4, str(len(set(_seen_versions))))
check("a hand-written pipeline.json flips the rail off the derived fallback",
      client.get("/api/pipeline").get_json()["feed_source"] == "orchestrator")

_advance_markup = client.get("/d/github").get_data(as_text=True)
check("the rendered bar width matches the reported fraction",
      "width: 100.0%" in _advance_markup, "the bar must be server-rendered, not JS-only")
check("the rail names the batch on screen, not only in the API",
      "batch 4/4" in _advance_markup)
check("the always-on rail is still the one the contract names",
      "data-guts-rail" in _advance_markup and '<details class="guts"' not in _advance_markup)

print("\n== meeting titles never shout an organisation prefix ==")

check(
    "living-room-standup is a spoken title",
    board_server.humanize_meeting_id("living-room-standup") == "Living room standup",
)
check(
    "an internal agi-living-room id is the same spoken title",
    board_server.humanize_meeting_id("agi-living-room") == "Living room standup",
)
check(
    "...and the letters a-g-i do not appear in it",
    "agi" not in board_server.humanize_meeting_id("agi-living-room").lower(),
)
check(
    "an unknown agi- slug drops the prefix rather than shouting it",
    board_server.humanize_meeting_id("agi-planning-sync") == "Planning sync",
)
from adjourn.tests import dress_journal  # noqa: E402
check(
    "the dress-rehearsal masthead never says the organisation prefix",
    "agi" not in dress_journal.MEETING_TITLE.lower()
    and dress_journal.MEETING_TITLE == "Living room standup",
    dress_journal.MEETING_TITLE,
)

# The bar is a claim about work in flight. With no batching reported it must not
# render at all — a progress bar with nothing behind it is a progress bar that lies.
_write_batch(0, 0, list(_rows))
check("no batch total means no bar",
      'class="rail-batch"' not in client.get("/d/github").get_data(as_text=True))
check("...but the streamed rows still win over the derived ones",
      client.get("/api/pipeline").get_json()["feed_source"] == "orchestrator")


# =============================================================================
print("\n== a card's quote traces back to the transcript ==")

os.environ["ADJOURN_BOARD_JOURNAL"] = str(_SANDBOX / "executions.jsonl")
os.environ["ADJOURN_BOARD_PENDING"] = str(_SANDBOX / "pending.json")

_FIXTURE_MEETING = "20260821-093000"

# Whether THIS machine happens to hold a recording called 20260821-093000 is not
# what is under test, so the library answer is pinned rather than read off the
# operator's disk. Both answers are exercised below.
board_server._TRANSCRIPT_PRESENT[_FIXTURE_MEETING] = True
board_server._TRANSCRIPT_MISSES.pop(_FIXTURE_MEETING, None)

trace_state = board_server.load_board_state()
_quoted = [card for card in trace_state["cards"] if card["quote"]]
check("the fixture has cards that carry a quote", len(_quoted) > 0)
check("every quoted card knows where its words came from",
      all(card["transcript_url"] == f"/meetings/{card['meeting_id']}"
          for card in _quoted if card["meeting_id"]),
      str([card["transcript_url"] for card in _quoted]))
check("a pending countdown traces back too",
      all(item["transcript_url"] == f"/meetings/{item['meeting_id']}"
          for item in trace_state["pending"] if item["meeting_id"]))

trace_markup = client.get("/d/github").get_data(as_text=True)
check("the quote is rendered as the link, not a separate footnote",
      f'class="quote-text quote-trace" href="/meetings/{_FIXTURE_MEETING}"' in trace_markup)
check("the fragment renders the same trace, so a poll cannot drop it",
      "quote-trace" in client.get("/api/fragment?domain=github").get_json()["cards_html"])
check("board.css styles the trace so it does not read as a raw blue link",
      b".quote-trace" in client.get("/static/board.css").data)

# A MEETING WITH NOTHING BEHIND IT GETS NO LINK. This was written for the fixture
# replay, which used to have no page at all; fixture_library has since given the
# demo tape a real transcript view (checked below), so the case this now pins is
# the general one — any id neither the engine, the recordings folder nor the
# fixtures hold. A quote that links to a 404 is a dead door; it stays plain text.
board_server._TRANSCRIPT_PRESENT.pop(_FIXTURE_MEETING, None)
board_server._TRANSCRIPT_MISSES[_FIXTURE_MEETING] = _time.monotonic()
check("a meeting the library does not hold gets no link at all",
      board_server.card_transcript_url(_FIXTURE_MEETING) == "")
no_trace_markup = client.get("/d/github").get_data(as_text=True)
check("...and the quote falls back to plain text, not a 404 link",
      "quote-trace" not in no_trace_markup and "quote-text" in no_trace_markup)
check("the words themselves are still on the card",
      "&ldquo;" in no_trace_markup)

# A recording that lands mid-demo must start linking without a restart: a miss is
# remembered only briefly, a hit is remembered for good.
board_server._TRANSCRIPT_MISSES[_FIXTURE_MEETING] = (
    _time.monotonic() - board_server.TRANSCRIPT_MISS_TTL_SECONDS - 1
)
_probed: list[str] = []
_real_read_document = board_server._read_meeting_document
board_server._read_meeting_document = lambda mid: (_probed.append(mid), {"id": mid})[1]
try:
    check("an expired miss is re-probed, so a new recording starts linking",
          board_server.card_transcript_url(_FIXTURE_MEETING)
          == f"/meetings/{_FIXTURE_MEETING}" and _probed == [_FIXTURE_MEETING])
    check("...and the hit is cached, so the 1s poll does not re-stat the disk",
          board_server.card_transcript_url(_FIXTURE_MEETING)
          and len(_probed) == 1, str(_probed))
finally:
    board_server._read_meeting_document = _real_read_document

# A library that throws is "no link", never an error page.
board_server._TRANSCRIPT_PRESENT.clear()
board_server._TRANSCRIPT_MISSES.clear()
board_server._read_meeting_document = lambda mid: (_ for _ in ()).throw(OSError("gone"))
try:
    check("an unreadable library degrades to no link, not a 500",
          board_server.card_transcript_url("whatever") == "")
    check("...and the board still renders", client.get("/").status_code == 200)
finally:
    board_server._read_meeting_document = _real_read_document

# THE EMPTY-DOCUMENT TRAP. The engine reader answers `{}` — a dict, not None —
# for an id it does not hold, so an `is not None` test calls every unknown
# meeting a transcript. That is precisely how the fixture tape got a link to a
# 404, and it was invisible until the page was opened in a browser.
board_server._TRANSCRIPT_PRESENT.clear()
board_server._TRANSCRIPT_MISSES.clear()
board_server._read_meeting_document = lambda mid: {}
try:
    check("an empty document is not a transcript",
          board_server.card_transcript_url(_FIXTURE_MEETING) == "")
finally:
    board_server._read_meeting_document = _real_read_document
    board_server._TRANSCRIPT_PRESENT.clear()
    board_server._TRANSCRIPT_MISSES.clear()

check("no meeting id means no link", board_server.card_transcript_url("") == "")

# THE DEMO TAPE ITSELF. `--replay` with no target runs living-room-standup, and beat
# E — "from a card, the quote traces back to the transcript" — has to happen on
# that tape and not only on a live recording. No recording exists for it, but the
# TRANSCRIPT does: it is the .jsonl extraction reads. fixture_library hands that
# to the same reader every other consumer uses, so the link is minted the same
# way a real recording's is, and the title stops falling back to the raw slug.
board_server._TRANSCRIPT_PRESENT.clear()
board_server._TRANSCRIPT_MISSES.clear()
check("the demo's own replay tape has somewhere for a quote to go",
      board_server.card_transcript_url("living-room-standup") == "/meetings/living-room-standup")
check("...and the board can name it, rather than printing its internal slug",
      board_server.read_meeting_header("prior-standup")["title"] == "Adjourn standup — 14 Aug",
      board_server.read_meeting_header("prior-standup")["title"])
check("an id that is neither a recording nor a tape still gets no link",
      board_server.card_transcript_url("not-a-tape") == "")
board_server._TRANSCRIPT_PRESENT.clear()
board_server._TRANSCRIPT_MISSES.clear()

# THE CROSSLINK AT THE TOP OF /meeting/<id> IS THE SAME PROMISE. It is the page a
# judge lands on from the Meetings detail's Follow-through button, and it offered
# "Transcript" unconditionally — a 404 for every meeting the library does not
# hold, the fixture tape included.
board_server._TRANSCRIPT_PRESENT.clear()
board_server._TRANSCRIPT_MISSES.clear()
board_server._read_meeting_document = lambda mid: {}
try:
    _view = board_server.load_meeting_view(_FIXTURE_MEETING)
    check("a meeting with no recording offers no Transcript crosslink",
          _view["transcript_url"] == "", _view["transcript_url"])
    _meeting_markup = client.get(f"/meeting/{_FIXTURE_MEETING}").get_data(as_text=True)
    check("...and the page renders without it",
          ">Transcript<" not in _meeting_markup
          and "All meetings' actions" in _meeting_markup,
          "the crosslink bar must still be there, minus the dead door")
finally:
    board_server._read_meeting_document = _real_read_document
    board_server._TRANSCRIPT_PRESENT.clear()
    board_server._TRANSCRIPT_MISSES.clear()

board_server._TRANSCRIPT_PRESENT[_FIXTURE_MEETING] = True
check("a meeting the library DOES hold keeps its Transcript crosslink",
      board_server.load_meeting_view(_FIXTURE_MEETING)["transcript_url"]
      == f"/meetings/{_FIXTURE_MEETING}")
check("...and it is the same url the card quotes point at",
      board_server.load_meeting_view(_FIXTURE_MEETING)["transcript_url"]
      == board_server.card_transcript_url(_FIXTURE_MEETING))
board_server._TRANSCRIPT_PRESENT.clear()

_was_mounted = board_server.MEETINGS_TAB_MOUNTED
board_server.MEETINGS_TAB_MOUNTED = False
board_server._TRANSCRIPT_PRESENT[_FIXTURE_MEETING] = True
check("an unmounted Meetings tab means no link either",
      board_server.card_transcript_url(_FIXTURE_MEETING) == "")
board_server.MEETINGS_TAB_MOUNTED = _was_mounted
board_server._TRANSCRIPT_PRESENT.clear()


# =============================================================================
print("\n== the board never names the recorder as a second product ==")

# The engine is an implementation detail. A judge sits on :5117 for the whole
# demo and is never told there is another app to go find, so no page this lane
# serves may print its name — including in a comment, which View Source shows.
#
# ONE EXEMPTION, AND IT IS DELIBERATE. The journal really does live in a
# `~/.meetingscribe/` directory on this machine, and Connections is the page
# whose entire job is to say truthfully where bytes are read and written. A
# filesystem path is DATA, not copy; renaming it on screen would make the
# honest-preflight tab dishonest about the one thing it exists to disclose. So
# the rule this pins is sharper than "the string is absent": every surviving
# occurrence must sit inside a `.meetingscribe/` path, never in a sentence.

_PATH_EXEMPTION = ".meetingscribe/"

for _path in ("/", "/ledger", "/connections", "/static/board.css", "/static/board.js"):
    _served = client.get(_path).get_data(as_text=True).lower()
    _stripped = _served.replace(_PATH_EXEMPTION, "«journal-dir»/")
    check(f"{_path} never names the engine in copy",
          "meetingscribe" not in _stripped,
          f"{_stripped.count('meetingscribe')} hits outside a filesystem path")

# The sandbox journal lives in a temp dir, so the page above proves nothing about
# the real path. Assert the template itself is clean — that is the copy.
_connections_source = (
    board_server.config.PACKAGE_ROOT / "templates" / "connections.html"
).read_text(encoding="utf-8").lower()
check("connections.html carries no product name at all, in copy or comment",
      "meetingscribe" not in _connections_source,
      f"{_connections_source.count('meetingscribe')} hits")
check("the engine card still says what it is and where",
      "loopback" in client.get("/connections").get_data(as_text=True))
check("paths are shown home-relative, not with the operator's account name",
      board_server.display_path(Path.home() / ".meetingscribe" / "executions.jsonl")
      == "~/.meetingscribe/executions.jsonl")
check("a path outside home is left exactly as it is",
      board_server.display_path("/var/log/adjourn.log") == "/var/log/adjourn.log")


# =============================================================================
print("\n== the settled privacy claim is one sentence, everywhere ==")

from adjourn import meetings_ui  # noqa: E402

check("board and meetings ship the identical claim",
      board_server.PRIVACY_LINE == meetings_ui.PRIVACY_LINE)
check("the claim is not the old absolute one",
      "never leave the Mac" in board_server.PRIVACY_LINE
      and "only the receipts" in board_server.PRIVACY_LINE)
for path in ("/", "/ledger", "/connections"):
    check(f"{path} carries the claim",
          board_server.PRIVACY_LINE in client.get(path).get_data(as_text=True))
check("the rail carries it beside the thinking",
      board_server.PRIVACY_LINE in client.get("/api/fragment?domain=github").get_json()["guts_html"])
for template in ("board.html", "meetings.html", "meeting.html",
                 "meeting_detail.html", "connections.html"):
    source = (board_server.config.PACKAGE_ROOT / "templates" / template).read_text(
        encoding="utf-8"
    )
    check(f"{template} makes no absolute privacy claim",
          "never left this machine" not in source
          and "nothing leaves it" not in source)


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
