"""Lane 2 — the app shell, the board's fixed defects, and undo lifecycle.

Every check here exists because an audit found the opposite behaviour on a real
run. The headings name the finding.

Run from the repo root:
    python -m adjourn.tests.test_lane2_shell

Writes only into a temp directory. The real ~/.meetingscribe/executions.jsonl is
never opened: the env overrides are set BEFORE the package is imported, and the
last section proves the file's existence is unchanged.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SANDBOX = Path(tempfile.mkdtemp(prefix="adjourn-lane2-"))
os.environ["ADJOURN_BOARD_JOURNAL"] = str(_SANDBOX / "executions.jsonl")
os.environ["ADJOURN_BOARD_PENDING"] = str(_SANDBOX / "pending.json")
os.environ["ADJOURN_BOARD_PIPELINE"] = str(_SANDBOX / "pipeline.json")

from adjourn import board_server, config, orchestrator, planner, results  # noqa: E402
from adjourn.tests import dress_journal  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


REAL_JOURNAL_EXISTED = config.EXECUTIONS_JOURNAL_PATH.exists()
JOURNAL = dress_journal.write(_SANDBOX)
application = board_server.create_board_application()
application.config["TESTING"] = True
TOKEN = application.config["ADJOURN_UNDO_TOKEN"]
client = application.test_client()


def post_undo(card_id: str, token: str | None = TOKEN):
    headers = {board_server.UNDO_TOKEN_HEADER: token} if token else {}
    return client.post(f"/undo/{card_id}", headers=headers)


# =============================================================================
print("== M2: one app, five tabs, on every page ==")

# LIVE FIRST — the order of the sitting, not the order the tabs were built in.
TAB_LABELS = ["Live", "Meetings", "Follow-through", "Ledger", "Connections"]
TAB_HREFS = ["/meetings/live", "/meetings/", "/", "/ledger", "/connections"]


def tabs_of(markup: str) -> list[tuple[str, str]]:
    """[(href, label)] of the viewbar, in order."""
    nav = re.search(r'<nav class="viewbar"[^>]*>(.*?)</nav>', markup, re.S)
    if not nav:
        return []
    return [
        (href, label.strip())
        for href, label in re.findall(r'<a class="viewlink[^"]*" href="([^"]+)">([^<]+)</a>', nav.group(1))
    ]


check("the Meetings blueprint mounted into the board app",
      application.config.get("ADJOURN_MEETINGS_MOUNTED") is True,
      str(application.config.get("ADJOURN_MEETINGS_MOUNT_ERROR", "")))

board_markup = client.get("/").get_data(as_text=True)
for path in ("/", "/ledger", "/connections", f"/meeting/{dress_journal.MEETING_ID}"):
    response = client.get(path)
    check(f"GET {path} is 200", response.status_code == 200, str(response.status_code))
    tabs = tabs_of(response.get_data(as_text=True))
    check(f"{path} carries all five tabs", [label for _, label in tabs] == TAB_LABELS, str(tabs))
    check(f"{path} tab hrefs are the five routes", [href for href, _ in tabs] == TAB_HREFS, str(tabs))

meetings_markup = client.get("/meetings/").get_data(as_text=True)
check("the Meetings page answers through the board app", "viewbar" in meetings_markup)
check(
    "the Meetings tab bar and the board tab bar are the same five links",
    [label for _, label in tabs_of(meetings_markup)] == TAB_LABELS,
    str(tabs_of(meetings_markup)),
)
check(
    "the Meetings tab bar points at the same five routes",
    [href for href, _ in tabs_of(meetings_markup)] == TAB_HREFS,
    str(tabs_of(meetings_markup)),
)
check("the current tab is marked on the board", 'class="viewlink is-current" href="/"' in board_markup)

# Five pills, sized for two, used to break "Follow-through" across two lines
# inside its own pill at a narrow width. meetings.css carried the fix for the
# pages it styles; board.css has to carry it for the four this app serves.
board_css = (config.PACKAGE_ROOT / "static" / "board.css").read_text(encoding="utf-8")
viewbar_rule = re.search(r"\.viewbar\s*\{[^}]*\}", board_css)
check("board.css wraps the tab bar rather than the words",
      viewbar_rule is not None and "flex-wrap: wrap" in viewbar_rule.group(0),
      viewbar_rule.group(0) if viewbar_rule else "no .viewbar rule")
check("and keeps each tab on one line",
      re.search(r"\.viewbar \.viewlink\s*\{[^}]*white-space:\s*nowrap", board_css) is not None)
check(
    "cards and the rail share the masthead's width — they do not hang past the tagline",
    "var(--page) + var(--rail-width)" not in board_css
    and "calc(var(--page) +" not in board_css,
)
check("the current tab is marked on connections",
      'href="/connections">Connections' in client.get("/connections").get_data(as_text=True))


# =============================================================================
print("\n== dress finding 0: the star card is pinned, and it is pinned by KIND ==")

state = board_server.load_board_state()
cards = state["cards"]
live_conflict = next(
    record for record in dress_journal.build_records()
    if record.get("undo_payload", {}).get("labels_added")
    and planner.LABEL_DECISION_CHANGED in record["undo_payload"]["labels_added"]
)
check(
    "a LIVE flat undo_payload (labels_added) is read as a conflict",
    board_server.record_carries_conflict(live_conflict) is True,
)
check(
    "a SIM nested undo_payload (payload.labels) is still read as a conflict",
    board_server.record_carries_conflict(
        {"undo_payload": {"simulated": True, "payload": {"labels": [planner.LABEL_DECISION_CHANGED]}}}
    ) is True,
)
check(
    "a github card with no decision-changed label is not a conflict",
    board_server.record_carries_conflict(
        {"undo_payload": {"operation": "comment", "labels_added": ["from-meeting"]}}
    ) is False,
)
check("an empty undo_payload is not a conflict", board_server.record_carries_conflict({}) is False)
check(
    "a malformed undo_payload does not raise",
    board_server.record_carries_conflict({"undo_payload": "not a dict"}) is False,
)

pinned = [card for card in cards if card["pinned"]]
check("three cards are pinned (2 conflicts + the linear_move)", len(pinned) == 3, str(len(pinned)))
check(
    "the pinned cards are the first three on the board",
    [card["id"] for card in cards[:3]] == [card["id"] for card in pinned],
    str([card["id"] for card in cards[:3]]),
)
check(
    "the star card is FIRST",
    "issue-2:decision" in cards[0]["id"],
    cards[0]["id"],
)
check("the linear_move is pinned by kind, not by label", any(
    card["kind"] == "linear_move" and card["pinned"] and not card["carries_conflict"]
    for card in cards
))
check(
    "pinned cards keep fire order",
    [card["fired_at"] for card in pinned] == sorted(card["fired_at"] for card in pinned),
)
check(
    "the rest are newest-first",
    [card["fired_at"] for card in cards[3:] if card["state"] != "cancelled"]
    == sorted((card["fired_at"] for card in cards[3:] if card["state"] != "cancelled"), reverse=True),
)
check("data-pinned reaches the markup", 'data-pinned="1"' in board_markup)
check(
    "exactly three cards carry data-pinned in the HTML",
    board_markup.count('data-pinned="1"') == 3,
    str(board_markup.count('data-pinned="1"')),
)

# ORDER LUCK: shuffling the journal must not change which cards are pinned.
shuffled = list(reversed(dress_journal.build_records()))
shuffled_cards = [
    board_server.build_card(record, index, set(), datetime.now(UTC))
    for index, record in enumerate(shuffled)
    if record.get("record_type") == "execution"
]
check(
    "pinning survives the journal being read in any order",
    {card["id"] for card in shuffled_cards if card["pinned"]} == {card["id"] for card in pinned},
)


# =============================================================================
print("\n== dress finding 2: the pipeline panel does not eat the first screen ==")

# THIS FINDING WAS SOLVED A SECOND TIME, BETTER. The original fix collapsed the
# panel into a <details> so it stopped eating the first screen — which worked and
# cost the demo the thing the panel was for: nobody could watch the system think
# without clicking. The panel is now a RAIL in the right-hand column, so it takes
# ZERO vertical space above the cards (the fold measurement improved: first card
# top 289px -> 177px at 1512x900) and can be permanently open.
#
# The finding's real contract is what these assertions now say: the pipeline must
# not sit above the card feed, and all four stages must be on screen without a
# click.
check("the pipeline panel is not a collapsed <details> any more",
      "<details" not in board_markup)
check("the pipeline lives in the rail column, not above the cards",
      'class="board-rail"' in board_markup
      and board_markup.index('class="board-cards"') < board_markup.index('class="board-rail"'))
check("all four stage headlines are visible without a click",
      board_markup.count('class="rail-stage"') == 4)
check("every stage is still named", all(
    f'data-stage="{stage}"' in board_markup
    for stage in ("watcher", "extraction", "planner", "executors")
))
check("the rail carries a live region", 'class="rail-live"' in board_markup)


# =============================================================================
print("\n== dress finding 7: the undo button is labelled per its actual behaviour ==")

by_kind = {card["kind"]: card for card in cards if card["state"] != "cancelled"}
check(
    "a live card's button reads Undo",
    by_kind["github_update"]["undo_label"] == "Undo",
    by_kind["github_update"]["undo_label"],
)
check(
    "a sim card whose send never happened reads Undo (sim)",
    by_kind["linear_create"]["undo_label"] == "Undo (sim)",
    by_kind["linear_create"]["undo_label"],
)
check(
    "a SIM-badged calendar hold reads Undo (local) — the .ics really is on disk",
    by_kind["calendar_hold"]["undo_label"] == "Undo (local)",
    by_kind["calendar_hold"]["undo_label"],
)
check(
    "every undo button carries a tooltip explaining which case it is",
    all(card["undo_title"] for card in cards if card["can_undo"]),
)
check("the tooltip reaches the markup", 'title="the transport was simulated' in board_markup)


# =============================================================================
print("\n== cancelled cards render as a cancelled thing, not as an action ==")

cancelled = [card for card in cards if card["state"] == "cancelled"]
check("the cancelled countdown is on the board", len(cancelled) == 1, str(len(cancelled)))
check("it is never pinned", cancelled[0]["pinned"] is False)
check("it wears no mode badge, it wears CANCELLED", "CANCELLED" in board_markup)
check("it offers no undo button", cancelled[0]["can_undo"] is False)
check("it says why there is no button", "cancelled by a human" in cancelled[0]["undo_note"])
check('it renders with data-state="cancelled"', 'data-state="cancelled"' in board_markup)
check(
    "it is NOT counted as an action in the header",
    state["totals"]["fired"] == len(cards) - 1,
    f"fired={state['totals']['fired']} cards={len(cards)}",
)
check("it is NOT counted as a sim send", state["totals"]["cancelled"] == 1)
check("the header names it", "1 cancelled" in state["totals_line"], state["totals_line"])


# =============================================================================
print("\n== dress finding 5: the counts on screen are the counts in the narration ==")

statements = [
    type("S", (), {"segment_id": f"lr-s{n}", "kind": "decision"})() for n in (1, 3, 7)
] + [type("S", (), {"segment_id": "lr-s7.2", "kind": "update"})()]
status = orchestrator.extraction_status_from_statements(
    statements, source="final", segment_count=28
)
check("the silent count is segments minus the LINES that spoke", status.silent_count == 25,
      str(status.silent_count))
check("a split claim silences one line, not minus one",
      orchestrator.count_silent_segments(statements, 28) == 25)
check("the panel says how many segments there were", "28 segments" in status.detail, status.detail)
check("the panel says how many produced nothing", "25 produced nothing" in status.detail, status.detail)
check("the statement count is still there", "4 statements" in status.detail, status.detail)
check(
    "no segment count means no invented number",
    "produced nothing" not in orchestrator.extraction_status_from_statements(
        statements, source="final"
    ).detail,
)


# =============================================================================
print("\n== the recap link is same-origin, and the route refuses to walk out of recaps/ ==")

recap_card = by_kind["recap_page"]
check(
    "a file:// recap URL is rewritten to /recap/<meeting_id>",
    recap_card["url"] == f"/recap/{dress_journal.MEETING_ID}",
    recap_card["url"],
)
check("the board links it", f'href="/recap/{dress_journal.MEETING_ID}"' in board_markup)
check("a missing recap is a 404, not a traceback",
      client.get("/recap/no-such-meeting").status_code == 404)
for hostile in ("..%2f..%2fetc%2fpasswd", "..", "a/b"):
    response = client.get(f"/recap/{hostile}")
    check(f"/recap/{hostile} is refused", response.status_code == 404, str(response.status_code))

recaps_dir = config.recaps_directory()
recaps_dir.mkdir(parents=True, exist_ok=True)
sample = recaps_dir / f"{dress_journal.MEETING_ID}.html"
sample_existed = sample.exists()
if not sample_existed:
    sample.write_text("<!doctype html><p>recap under test</p>", encoding="utf-8")
served = client.get(f"/recap/{dress_journal.MEETING_ID}")
check("an existing recap is served over http", served.status_code == 200, str(served.status_code))
check("it is served as html", served.mimetype == "text/html")
if not sample_existed:
    sample.unlink()


# =============================================================================
print("\n== /undo is loopback-bound and needs this run's token ==")

check("the bind address is the constant 127.0.0.1", board_server.BOARD_HOST == "127.0.0.1")

import inspect  # noqa: E402

main_source = inspect.getsource(board_server.main)
check(
    "there is no --host flag to widen the bind by muscle memory",
    '"--host"' not in main_source and "'--host'" not in main_source,
)
check(
    "app.run() binds the constant, not a parsed argument",
    "host=BOARD_HOST" in main_source,
)
check("a token was minted", len(TOKEN) >= 24, str(len(TOKEN)))
check("the token is printed into the page", f'data-undo-token="{TOKEN}"' in board_markup)
check("the token is NOT in /healthz", TOKEN not in client.get("/healthz").get_data(as_text=True))
check("/healthz reports that a token is required",
      client.get("/healthz").get_json()["undo_token_required"] is True)

target = "linear_create:living-room-standup:benchmark-lru"
no_token = client.post(f"/undo/{target}")
check("an undo with no token is 403", no_token.status_code == 403, str(no_token.status_code))
check("the refusal says what to do", "reload the board" in no_token.get_json()["message"])
bad_token = post_undo(target, token="not-the-token")
check("an undo with the wrong token is 403", bad_token.status_code == 403)
body_no_token = client.post("/undo", json={"dedup_key": target})
check("the body form is guarded too", body_no_token.status_code == 403)
body_with_token = client.post("/undo", json={"dedup_key": "no-such-key", "token": TOKEN})
check("the body form accepts the token in the body", body_with_token.status_code == 409)
check(
    "a second app mints a DIFFERENT token",
    board_server.create_board_application().config["ADJOURN_UNDO_TOKEN"] != TOKEN,
)


# =============================================================================
print("\n== /meeting/<id>: one meeting's follow-through, filtered from the journal ==")

view = board_server.load_meeting_view(dress_journal.MEETING_ID)
check("every card belongs to this meeting",
      all(card["meeting_id"] == dress_journal.MEETING_ID for card in view["cards"]))
check("the recap's generations are collapsed to one card",
      sum(1 for card in view["cards"] if card["kind"] == "recap_page") == 1)
check("the cancelled countdown is included", any(card["state"] == "cancelled" for card in view["cards"]))
check("cards run forwards in fire order",
      [card["fired_at"] for card in view["cards"]]
      == sorted(card["fired_at"] for card in view["cards"]))
# THE TRANSCRIPT CROSSLINK IS CONDITIONAL, AND THAT IS THE FIX, NOT A REGRESSION.
# It used to be an unconditional f"/meetings/{meeting_id}", which meant every
# meeting the library does NOT hold — this dress-rehearsal journal, and the
# default `--replay` tape — offered a "Transcript" link straight to a 404, on
# the page a judge reaches by clicking Follow-through from a meeting. It now
# asks the library first. Both answers are pinned here.
board_server._TRANSCRIPT_PRESENT.clear()
board_server._TRANSCRIPT_MISSES.clear()
_real_read_document = board_server._read_meeting_document
board_server._read_meeting_document = lambda mid: {}
try:
    check("a fabricated meeting offers no transcript link, rather than a 404",
          board_server.load_meeting_view(dress_journal.MEETING_ID)["transcript_url"] == "")
finally:
    board_server._read_meeting_document = _real_read_document
    board_server._TRANSCRIPT_PRESENT.clear()
    board_server._TRANSCRIPT_MISSES.clear()

board_server._TRANSCRIPT_PRESENT[dress_journal.MEETING_ID] = True
check("a meeting the library holds links back to the transcript",
      board_server.load_meeting_view(dress_journal.MEETING_ID)["transcript_url"]
      == f"/meetings/{dress_journal.MEETING_ID}")
board_server._TRANSCRIPT_PRESENT.clear()
check("it links to the recap", view["recap_url"] == f"/recap/{dress_journal.MEETING_ID}")
check("an unknown meeting renders an honest empty page",
      board_server.load_meeting_view("no-such-meeting")["cards"] == [])
empty_page = client.get("/meeting/no-such-meeting")
check("an unknown meeting is 200, not 404", empty_page.status_code == 200)
check("and it says restraint is a result",
      "That is a result, not a gap" in empty_page.get_data(as_text=True))
detail = client.get(f"/meeting/{dress_journal.MEETING_ID}").get_data(as_text=True)
check("the meeting page renders the same card macro", 'class="card"' in detail)
check("the meeting page carries the undo token", f'data-undo-token="{TOKEN}"' in detail)


# =============================================================================
print("\n== /connections: honest, read-only, and boolean about secrets ==")

connections = board_server.describe_connections(cloud_wait_seconds=0.0)
kinds = [row["kind"] for row in connections["executors"]]
check("every registered executor kind has a row",
      set(planner.ACTION_KINDS).issubset(set(kinds)),
      str(set(planner.ACTION_KINDS) - set(kinds)))
check("every row says live or sim", all(row["mode"] in ("live", "sim") for row in connections["executors"]))
check("every row says WHY", all(row["why"] for row in connections["executors"]))
check("the engine row is present", "reachable" in connections["engine"])
check("the memory row names its backend", bool(connections["memory"]["backend"]))
check("the cloud row reports a state",
      connections["cloud"]["state"] in {"reachable", "unreachable", "unconfigured", "unprobed", "unavailable"},
      connections["cloud"]["state"])
check("secrets are booleans, never values",
      all(isinstance(value, bool) for value in connections["secrets"].values()))

connections_markup = client.get("/connections").get_data(as_text=True)
from adjourn import secrets_store  # noqa: E402

leaked = [
    name for name in secrets_store.describe_secret_availability()
    if (secrets_store.get_secret(name) or "") and (secrets_store.get_secret(name) or "") in connections_markup
]
check("no secret VALUE appears anywhere on the page", not leaked, str(leaked))
check("the page reports presence", "PRESENT" in connections_markup or "ABSENT" in connections_markup)
check("the repo allowlist is on the page", sorted(config.ALLOWED_GITHUB_REPOS)[0] in connections_markup)
check("the loopback bind is stated", "127.0.0.1" in connections_markup)

# The connections page must agree with the function every executor actually calls.
for row in connections["executors"]:
    if not row["registered"]:
        continue
    expected = secrets_store.decide_mode(row["requires"], label=row["kind"])
    check(f"{row['kind']}: the page agrees with decide_mode", row["mode"] == expected,
          f"page={row['mode']} decide_mode={expected}")


# =============================================================================
print("\n== lifecycle: one undo unwinds every generation of a rewritten page ==")

generations = [
    record for record in results.read_executions(path=JOURNAL)
    if record.get("kind") == "recap_page" and record.get("record_type") == "execution"
]
check("the fixture really has two recap generations", len(generations) == 2, str(len(generations)))

undone_paths: list[str] = []


class _FakeRecapExecutor:
    """Stands in for the recap executor so the test never writes a real page."""

    @staticmethod
    def undo(result) -> bool:
        undone_paths.append((result.undo_payload or {}).get("path", ""))
        return True


import adjourn.executors as _executors  # noqa: E402

_real_load = _executors.load_executor


def _fake_load(kind: str):
    if kind == "recap_page":
        return _FakeRecapExecutor
    return _real_load(kind)


_executors.load_executor = _fake_load
try:
    outcome = post_undo(f"recap_page:{dress_journal.MEETING_ID}").get_json()
finally:
    _executors.load_executor = _real_load

check("the undo succeeded", outcome["ok"] is True, str(outcome))
check("BOTH generations were rolled back", len(undone_paths) == 2, str(undone_paths))
check("the message says so", "generations" in outcome["message"], outcome["message"])
check(
    "the journal records an undo for each generation",
    sum(
        1 for record in results.read_executions(path=JOURNAL)
        if record.get("record_type") == "undo"
        and record.get("dedup_key") == f"recap_page:{dress_journal.MEETING_ID}"
    ) == 2,
)
after = board_server.load_board_state()
recap_after = next(card for card in after["cards"] if card["kind"] == "recap_page")
check("the card now reads undone", recap_after["undone"] is True)
check("and offers no button", recap_after["can_undo"] is False)

# The orchestrator unwinds generations too. When IT answered, the board must not
# unwind them a second time — that second pass would ask each executor to reverse
# a write already reversed and report a refusal for work that succeeded.
second_pass: list[str] = []


def _counting_undo(record, dedup_key):
    second_pass.append(dedup_key)
    return True, "undone", True  # third value: the orchestrator handled it


_real_undo_record = board_server.undo_execution_record
board_server.undo_execution_record = _counting_undo
try:
    replayed = board_server.perform_undo("github_update:living-room-standup:issue-1:update")
finally:
    board_server.undo_execution_record = _real_undo_record
check("an orchestrator-handled undo is dispatched exactly once", len(second_pass) == 1,
      str(second_pass))
check("and reports success", replayed["ok"] is True, str(replayed))


# =============================================================================
print("\n== lifecycle: an undone action is struck through in the recap file ==")

from adjourn.executors import recap_page_executor  # noqa: E402

page = recap_page_executor.render_recap_html({
    "meeting_title": dress_journal.MEETING_TITLE,
    "meeting_id": dress_journal.MEETING_ID,
    "statements": [],
    "segment_count": 28,
    "executed": [
        {
            "kind": "github_update", "mode": "live", "ok": True, "undone": True,
            "dedup_key": "k1", "human_summary": "Comment on #1",
            "url": "https://github.com/x/y/issues/1#issuecomment-5365732010",
            "quote": "q", "speaker": "You",
        },
        {
            "kind": "linear_move", "mode": "sim", "ok": True, "undone": False,
            "dedup_key": "k2", "human_summary": "Moved SHA-5",
            "url": "https://linear.app/sha/issue/SHA-5", "quote": "q2", "speaker": "You",
        },
    ],
})
check("the undone row wears an UNDONE badge", "badge undone" in page)
check("the undone row is struck through", ".card.undone .summary" in page and "undone" in page)
check(
    "the undone row's permalink is NOT a live link",
    'href="https://github.com/x/y/issues/1#issuecomment-5365732010"' not in page,
)
check("the dead permalink is still shown as text, marked removed",
      "issuecomment-5365732010" in page and "(removed)" in page)
check("the surviving row keeps its link", 'href="https://linear.app/sha/issue/SHA-5"' in page)
check("the header counts the undone one", "<b>1</b> undone" in page)

empty_page_html = recap_page_executor.render_recap_html({
    "meeting_title": "Saturday hang", "meeting_id": "fp-social",
    "statements": [], "segment_count": 41, "executed": [],
})
check("a meeting that produced nothing still says what happened",
      "41 lines were spoken" in empty_page_html)
check("and frames it as the system working",
      "That is the system working" in empty_page_html)


# =============================================================================
print("\n== lifecycle: attach_recap_context marks undone rows for the writer ==")

action = planner.Action(
    kind="recap_page",
    payload={"meeting_id": dress_journal.MEETING_ID, "statements": []},
    dedup_key=f"recap_page:{dress_journal.MEETING_ID}",
    meeting_id=dress_journal.MEETING_ID,
)
_previous_journal = os.environ.get("EXECUTIONS_JOURNAL")
os.environ["EXECUTIONS_JOURNAL"] = str(JOURNAL)
try:
    orchestrator.attach_recap_context(action, segment_count=28)
finally:
    if _previous_journal is None:
        os.environ.pop("EXECUTIONS_JOURNAL", None)
    else:
        os.environ["EXECUTIONS_JOURNAL"] = _previous_journal

executed = action.payload["executed"]
check("every row carries an undone flag", all("undone" in row for row in executed))
check("the recap does not report on itself",
      all(row["kind"] != "recap_page" for row in executed))
check("the segment count reaches the payload", action.payload["segment_count"] == 28)


# =============================================================================
print("\n== lifecycle, end to end: an undo really does rewrite the page on disk ==")
#
# No model, no network, no live executor. A recap is written for real into a
# sandbox directory, a fired action is undone, and the FILE is re-read.

live_sandbox = _SANDBOX / "endtoend"
live_sandbox.mkdir(parents=True, exist_ok=True)
end_journal = live_sandbox / "executions.jsonl"
os.environ["EXECUTIONS_JOURNAL"] = str(end_journal)
os.environ["RECAP_DIR"] = str(live_sandbox / "recaps")
os.environ["ADJOURN_SIM"] = "1"
try:
    meeting_id = "undo-rewrites-the-recap"
    fired_action = planner.Action(
        kind="github_update",
        payload={
            "repo": "example/repo", "issue_number": 2,
            "human_preview": "Comment on #2: Redis -> in-process LRU",
        },
        dedup_key=f"github_update:{meeting_id}:issue-2",
        quote="We are not using Redis for the ingestion cache.",
        speaker="You",
        meeting_id=meeting_id,
    )
    orchestrator.fire_action(fired_action, meeting_title="Undo test")

    recap_action = planner.build_recap_page([], {"meeting_id": meeting_id, "title": "Undo test"})
    orchestrator.fire_actions([recap_action], meeting_title="Undo test", segment_count=17)

    recap_file = (live_sandbox / "recaps") / f"{meeting_id}.html"
    check("the recap was written", recap_file.is_file(), str(recap_file))
    before_html = recap_file.read_text(encoding="utf-8")
    check("it lists the fired action", "Comment on #2" in before_html)
    check("it does NOT yet say undone", "badge undone" not in before_html)
    check("it counts the lines spoken", "<b>17</b> lines spoken" in before_html)

    undone_ok = orchestrator.undo_execution(fired_action.dedup_key, journal_path=end_journal)
    check("the sim action undid cleanly", undone_ok is True)
    rewritten = orchestrator.refresh_recap_after_undo(meeting_id)
    check("the recap was rewritten after the undo", rewritten is True)

    after_html = recap_file.read_text(encoding="utf-8")
    check("the undone row now wears UNDONE", "badge undone" in after_html)
    check("the header counts it as undone", "<b>1</b> undone" in after_html)
    check("the page still names the action", "Comment on #2" in after_html)
    check("the file on disk really changed", after_html != before_html)

    # And undoing the recap itself leaves it gone, rather than being resurrected
    # by the next undo of some other card.
    orchestrator.undo_execution(recap_action.dedup_key, journal_path=end_journal)
    check("undoing the recap removes the page", not recap_file.exists())
    check(
        "a later undo does not resurrect it",
        orchestrator.refresh_recap_after_undo(meeting_id) is False,
    )
    check("and the file stays gone", not recap_file.exists())
finally:
    for name in ("EXECUTIONS_JOURNAL", "RECAP_DIR", "ADJOURN_SIM"):
        os.environ.pop(name, None)
    orchestrator._recap_actions_by_meeting.clear()
    # remember_recap_action() parks its payload under adjourn/state/recap_actions
    # unconditionally — there is no env override for that path — so this test has
    # to take its own file back out. The final-state contract says that directory
    # is empty on demo night, and a test is not allowed to be the exception.
    parked = orchestrator.recap_action_path("undo-rewrites-the-recap")
    if parked.exists():
        parked.unlink()
    check("the test left nothing parked in state/recap_actions", not parked.exists())


# =============================================================================
print("\n== restraint finding 5: a meeting with zero statements still gets a recap ==")

quiet = orchestrator.plan_actions_safely([], None, meeting={"meeting_id": "quiet", "title": "Quiet"})
check("a zero-statement meeting still plans one action", len(quiet) == 1, str(len(quiet)))
check("and that action is the recap", quiet and quiet[0].kind == "recap_page")
check("a meeting with no id at all still plans nothing",
      orchestrator.plan_actions_safely([], None, meeting={}) == [])

# End to end through the REPLAY path, with the extractor stubbed to find nothing.
# No model call, no network, no live executor — but every other function is the
# real one, which is the only way to know the board really shows something.
def _count_demo_graph_nodes() -> int:
    """Nodes in the demo's real memory graph, or -1 when it is not reachable.

    READ ONLY, and deliberately tolerant: on a machine with no FalkorDB the
    answer is -1 both before and after, so the comparison still holds and the
    suite still runs. It exists so that a test which starts writing to the demo
    graph says so, instead of being discovered by counting nodes by hand at the
    end of the night.
    """
    try:
        from .. import memory_store
        memory = memory_store.open_memory()
    except Exception:  # noqa: BLE001
        return -1
    try:
        counts = memory.stats()
        return int(counts.get("nodes", -1)) if isinstance(counts, dict) else -1
    except Exception:  # noqa: BLE001
        return -1
    finally:
        try:
            memory.close()
        except Exception:  # noqa: BLE001
            pass


silent_sandbox = _SANDBOX / "silent"
silent_sandbox.mkdir(parents=True, exist_ok=True)
silent_journal = silent_sandbox / "executions.jsonl"
os.environ["EXECUTIONS_JOURNAL"] = str(silent_journal)
os.environ["RECAP_DIR"] = str(silent_sandbox / "recaps")
os.environ["ADJOURN_SIM"] = "1"
os.environ["ADJOURN_BOARD_JOURNAL"] = str(silent_journal)
# report_pipeline() writes through config.pipeline_status_path(), which reads
# ADJOURN_PIPELINE — ADJOURN_BOARD_PIPELINE only redirects the board's READ. A
# test that leaves adjourn/state/pipeline.json behind has broken the final-state
# contract on its way to proving something else.
_previous_pipeline = os.environ.get("ADJOURN_PIPELINE")
_state_pipeline = config.STATE_DIR / "pipeline.json"
_state_pipeline_existed = _state_pipeline.exists()
os.environ["ADJOURN_PIPELINE"] = str(silent_sandbox / "pipeline.json")
_real_extract = orchestrator.extract_statements_safely
_real_judge = orchestrator.judge_conflicts_safely
_real_mirror = orchestrator.mirror_meeting_in_background
_real_open_memory = orchestrator.open_memory_safely
orchestrator.extract_statements_safely = lambda *a, **k: []
orchestrator.judge_conflicts_safely = lambda *a, **k: {}
orchestrator.mirror_meeting_in_background = lambda *a, **k: None
# AND memory, which is not optional. _replay_transcript_file opens the REAL
# FalkorDB graph and ingests the meeting there — extraction being stubbed to
# nothing does not stop it, because the Meeting node and the recap Action are
# ingested regardless. Left unstubbed this suite added a Meeting titled
# "MMM Standup — living room" and an Action node to the demo's own graph on
# every run, which the final-state contract says must hold the prior standup
# and nothing else. This test is about the recap, not about memory.
orchestrator.open_memory_safely = lambda *a, **k: None
_graph_before = _count_demo_graph_nodes()
try:
    fired = orchestrator._replay_transcript_file(config.FIXTURES_DIR / "living-room-standup.jsonl")
    check("a meeting that produced no statements still fires exactly one action",
          len(fired) == 1, str([r.kind for r in fired]))
    check("and that action is the recap", fired and fired[0].kind == "recap_page")

    silent_state = board_server.load_board_state()
    check("the board shows a card for it", len(silent_state["cards"]) == 1,
          str(len(silent_state["cards"])))
    check("the header counts it", "1 action" in silent_state["totals_line"],
          silent_state["totals_line"])
    silent_recap = (silent_sandbox / "recaps") / "living-room-standup.html"
    check("a recap page was written", silent_recap.is_file())
    written = silent_recap.read_text(encoding="utf-8")
    check("the page says how many lines were spoken", "lines spoken" in written)
    check("and says nothing followed", "Nothing followed from this meeting" in written)
finally:
    orchestrator.extract_statements_safely = _real_extract
    orchestrator.judge_conflicts_safely = _real_judge
    orchestrator.mirror_meeting_in_background = _real_mirror
    orchestrator.open_memory_safely = _real_open_memory
    check("the demo's own memory graph was not written to",
          _count_demo_graph_nodes() == _graph_before,
          f"{_graph_before} -> {_count_demo_graph_nodes()} nodes")
    for name in ("EXECUTIONS_JOURNAL", "RECAP_DIR", "ADJOURN_SIM"):
        os.environ.pop(name, None)
    if _previous_pipeline is None:
        os.environ.pop("ADJOURN_PIPELINE", None)
    else:
        os.environ["ADJOURN_PIPELINE"] = _previous_pipeline
    os.environ["ADJOURN_BOARD_JOURNAL"] = str(JOURNAL)
    orchestrator._recap_actions_by_meeting.clear()
    parked_silent = orchestrator.recap_action_path("living-room-standup")
    if parked_silent.exists():
        parked_silent.unlink()
    check("this test also left state/recap_actions empty", not parked_silent.exists())
    check("and did not create adjourn/state/pipeline.json",
          _state_pipeline.exists() == _state_pipeline_existed,
          "the replay wrote the real pipeline file instead of the sandbox one")
    check("the replay's pipeline status went to the sandbox",
          (silent_sandbox / "pipeline.json").is_file())


# =============================================================================
print("\n== reset_demo: archives the journal, and refuses to orphan a live write ==")

from adjourn import reset_demo  # noqa: E402

reset_sandbox = _SANDBOX / "reset"
reset_sandbox.mkdir(parents=True, exist_ok=True)
guard_journal = dress_journal.write(reset_sandbox)

stranded = reset_demo.unreversed_live_actions(path=guard_journal)
kinds_stranded = sorted({record["kind"] for record in stranded})
check("live un-undone actions are found", bool(stranded))
check("a live github comment blocks a reset", "github_update" in kinds_stranded, str(kinds_stranded))
check("a live draft PR blocks a reset", "pull_request_stub" in kinds_stranded, str(kinds_stranded))
check("a SIM action never blocks a reset", "linear_create" not in kinds_stranded, str(kinds_stranded))
check(
    "a live recap does NOT block — reset deletes that file itself",
    "recap_page" not in kinds_stranded,
    str(kinds_stranded),
)
check(
    "three writes of one artifact are one thing left behind",
    len({record.get("dedup_key") for record in stranded}) == len(stranded),
)
line = reset_demo.describe_orphaned_action(stranded[0])
check("each orphan is named with its undo handle", "undo with: POST /undo/" in line, line)
check("and with its URL where it has one", "https://" in line, line)

# The refusal must leave the journal exactly where it was.
before_bytes = guard_journal.read_bytes()
os.environ["EXECUTIONS_JOURNAL"] = str(guard_journal)
try:
    exit_code = reset_demo.main(["--check"])
    check("--check reports the block", exit_code == 1, str(exit_code))
    check("--check changed nothing", guard_journal.read_bytes() == before_bytes)

    # Now undo everything live, and the guard clears.
    cleared = reset_sandbox / "cleared.jsonl"
    records = dress_journal.build_records()
    with cleared.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        for record in records:
            if record.get("mode") == "live" and record.get("record_type") == "execution":
                handle.write(json.dumps({
                    "record_type": "undo", "undo_ok": True,
                    "dedup_key": record["dedup_key"], "kind": record["kind"],
                    "written_at": "2026-08-21T00:00:00+00:00",
                }) + "\n")
    check("undoing every live card clears the guard",
          reset_demo.unreversed_live_actions(path=cleared) == [])

    # Archive, never delete.
    archive_target = reset_sandbox / "archive-me.jsonl"
    archive_target.write_text(guard_journal.read_text(encoding="utf-8"), encoding="utf-8")
    check("archive_journal reports success", reset_demo.archive_journal(path=archive_target) is True)
    check("the original name is gone", not archive_target.exists())
    siblings = list(reset_sandbox.glob("archive-me.*.jsonl"))
    check("a timestamped sibling holds the undo payloads", len(siblings) == 1, str(siblings))
    check("the archive is byte-identical", siblings[0].read_bytes() == before_bytes)
    check("the timestamp is in the name",
          re.search(r"\.\d{8}-\d{6}\.jsonl$", siblings[0].name) is not None, siblings[0].name)
finally:
    os.environ.pop("EXECUTIONS_JOURNAL", None)

check("the PR-review beat is in the rehearsal meetings reset forgets",
      "pr-review-beat" in reset_demo.DEFAULT_REHEARSAL_MEETINGS,
      str(reset_demo.DEFAULT_REHEARSAL_MEETINGS))
check("Issue is deliberately NOT swept — the seed holds edgeless Issue nodes",
      "Issue" not in reset_demo.ORPHAN_SWEEP_LABELS,
      str(reset_demo.ORPHAN_SWEEP_LABELS))
check("Topic, Person and Ticket are swept",
      set(reset_demo.ORPHAN_SWEEP_LABELS) == {"Topic", "Person", "Ticket"})


# =============================================================================
print("\n== seed_memory's pre-flight checks the NUMBER, derived from the roadmap ==")

from adjourn import seed_memory  # noqa: E402

# The expectation is no longer hand-written. It comes from seed_demo_world's
# ROADMAP — the same list that decides which issue GitHub actually creates — so
# a reordering of the roadmap can no longer leave the pre-flight calling a
# healthy seed "unexpected", which is exactly what it did for two days after
# "streaming adapter" gained a mirror issue at #3.
EXPECTED = seed_memory.expected_issue_numbers()
check("the roadmap answers for every probe topic",
      set(EXPECTED) == set(seed_memory.SEED_TOPIC_PROBES), str(EXPECTED))
check("auth migration is roadmap entry 1 — the number prior-standup says out loud",
      EXPECTED["auth migration"] == 1, str(EXPECTED))
check("cache layer is roadmap entry 2 — likewise",
      EXPECTED["cache layer"] == 2, str(EXPECTED))
check("streaming adapter has a mirror issue too, and the table knows it",
      EXPECTED["streaming adapter"] == 3, str(EXPECTED))


class _FakeMemory:
    """Resolves exactly what a correctly seeded memory would."""

    def find_issue_for_topic(self, topic: str):
        return EXPECTED.get(topic)


printed: list[str] = []
_real_print = print


def capture(memory) -> bool:
    import builtins

    printed.clear()
    try:
        builtins.print = lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args))
        return seed_memory.report_topic_resolution(memory, "falkor")
    finally:
        builtins.print = _real_print


healthy = capture(_FakeMemory())
check("a healthy seed reports healthy", healthy is True, str(printed))
check("every probe printed a line", len(printed) == len(seed_memory.SEED_TOPIC_PROBES),
      str(printed))
check("nothing is labelled unexpected",
      not any("unexpected" in line for line in printed), str(printed))


class _MisnumberedMemory:
    """The failure that costs a demo: every card fires, onto the wrong issue."""

    def find_issue_for_topic(self, topic: str):
        return {"cache layer": 3, "auth migration": 1, "streaming adapter": 2}.get(topic)


misnumbered = capture(_MisnumberedMemory())
check("a seed whose numbers are swapped is reported BROKEN", misnumbered is False, str(printed))
check("...and it says which number was expected",
      any("expected issue #2" in line for line in printed), str(printed))


class _BrokenMemory:
    def find_issue_for_topic(self, topic: str):
        return None


printed.clear()
try:
    import builtins

    builtins.print = lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args))
    broken = seed_memory.report_topic_resolution(_BrokenMemory(), "falkor")
finally:
    builtins.print = _real_print
check("a genuinely broken seed is reported broken", broken is False)
check("and it names the fix",
      any("re-run --reseed" in line for line in printed), str(printed))


# =============================================================================
print("\n== the real journal was never touched ==")

check(
    "real executions.jsonl untouched",
    config.EXECUTIONS_JOURNAL_PATH.exists() == REAL_JOURNAL_EXISTED,
    "this test must never write to ~/.meetingscribe/executions.jsonl",
)
check("board still reads the sandbox journal", board_server.journal_path() == JOURNAL)

print("\n" + "=" * 60)
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print(f"ALL CHECKS PASSED  (sandbox: {_SANDBOX})")
