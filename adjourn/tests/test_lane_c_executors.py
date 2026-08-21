"""Lane C regression gate — the work executors (GitHub, Linear, draft PR, recap).

Run from the repo root:
    python -m adjourn.tests.test_lane_c_executors

Everything here is SAFE to run repeatedly: ADJOURN_SIM=1 is forced before any
executor is imported, so no HTTP call, no `gh` invocation and no Linear write can
happen from this file. The recap executor genuinely writes a file (that is its
whole transport) so it is pointed at a temp directory.

What is checked:
  * every executor renders a real payload in sim mode and badges it SIM
  * undo of a simulated result is honest-true (nothing left the machine)
  * the pure decision functions are deterministic and table-driven
  * the repo allowlist guards actually refuse
  * the recap page writes, backs up, undoes, and upgrades quotes in place
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Forced BEFORE the package is imported: dotenv does not override a set variable,
# so this holds even though adjourn/.env ships ADJOURN_SIM=0.
os.environ["ADJOURN_SIM"] = "1"
RECAP_DIRECTORY = Path(tempfile.mkdtemp(prefix="adjourn-recaps-"))
os.environ["RECAP_DIR"] = str(RECAP_DIRECTORY)

from adjourn import results  # noqa: E402
from adjourn.executors import (  # noqa: E402
    github_update_executor,
    linear_create_executor,
    linear_move_executor,
    pull_request_stub_executor,
    recap_page_executor,
)
from adjourn.executors import execute_action, undo_result  # noqa: E402
from adjourn.planner import Action  # noqa: E402

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


def make_action(kind: str, payload: dict, **overrides) -> Action:
    return Action(
        kind=kind,
        payload=payload,
        dedup_key=overrides.get("dedup_key", f"{kind}:test"),
        quote=overrides.get(
            "quote", "Let's move the cache layer to Redis before the demo on Friday."
        ),
        speaker=overrides.get("speaker", "Priya"),
        meeting_id=overrides.get("meeting_id", "20260821-140312"),
        segment_id=overrides.get("segment_id", "L12"),
        source=overrides.get("source", "live"),
    )


print("== github_update: sim mode renders the exact comment ==")
github_action = make_action(
    "github_update",
    {
        "issue_number": 4,
        "before": "in-process LRU cache",
        "after": "Redis, shared across the workers",
        "what_changed": "cache moved out of process",
        "meeting_title": "Eng sync",
        "meeting_date": "2026-08-21",
        "timestamp": "12:03",
        "labels": ["from-meeting", "decision-changed"],
        "plan_markdown": "**Current plan:** Redis cache, shared across workers.",
    },
)
github_result = github_update_executor.execute(github_action)
check("returns ok", github_result.ok, github_result.human_summary)
check("badged sim", github_result.mode == "sim", github_result.mode)
comment = github_result.undo_payload["payload"]["comment"]
check("comment carries the GFM alert", "> [!IMPORTANT]" in comment)
check("comment carries the before/after table", "| **Before** |" in comment and "| **After**  |" in comment)
check("comment carries the collapsible excerpt", "<details>" in comment and "<summary>Meeting excerpt</summary>" in comment)
check("comment quotes the speaker verbatim", "**Priya:** Let's move the cache layer" in comment)
check("comment carries the timestamp", "[12:03]" in comment)
check("plan block is rendered too", "<!-- adjourn:plan -->" in github_result.undo_payload["payload"]["plan_block"])
check("labels are in the rendered payload", github_result.undo_payload["payload"]["labels"] == ["from-meeting", "decision-changed"])
check("undo of a sim comment is honest-true", github_update_executor.undo(github_result) is True)

quiet = github_update_executor.build_comment_markdown(
    make_action("github_update", {"issue_number": 4, "meeting_title": "Eng sync"})
)
check("no before/after => quieter [!NOTE] banner", "> [!NOTE]" in quiet and "| **Before** |" not in quiet)

print("\n== github_update: plan block rewriting is pure and idempotent ==")
body = "Some issue text.\n\nMore text."
once = github_update_executor.rewrite_plan_block(body, "plan one")
twice = github_update_executor.rewrite_plan_block(once, "plan two")
check("block is appended when absent", once.endswith("<!-- /adjourn:plan -->"))
check("original body is preserved", once.startswith("Some issue text."))
check("second write replaces rather than appends", twice.count("<!-- adjourn:plan -->") == 1)
check("second write carries the new plan", "plan two" in twice and "plan one" not in twice)
check("surrounding text survives", "More text." in twice)
check(
    "rewriting the same plan twice is a fixed point",
    github_update_executor.rewrite_plan_block(twice, "plan two") == twice,
)

print("\n== github_update: new-issue path ==")
create_result = github_update_executor.execute(
    make_action(
        "github_update",
        {"create_issue": True, "title": "Add Redis cache layer", "meeting_title": "Eng sync"},
    )
)
check("create_issue renders in sim", create_result.ok and create_result.mode == "sim")
check("operation is tagged", create_result.undo_payload["payload"]["operation"] == "create_issue")
check("issue body carries the quote", "**Priya:**" in create_result.undo_payload["payload"]["body"])

print("\n== repo allowlist guards refuse ==")
for label, guard in (
    ("github_update", github_update_executor.assert_repo_is_allowed),
    ("pull_request_stub", pull_request_stub_executor.assert_repo_is_allowed),
):
    try:
        guard("someone-else/private-repo")
        check(f"{label} guard refuses a foreign repo", False, "did not raise")
    except PermissionError:
        check(f"{label} guard refuses a foreign repo", True)
    try:
        guard("sharique2004/adjourn")
        check(f"{label} guard allows adjourn", True)
    except PermissionError as error:
        check(f"{label} guard allows adjourn", False, str(error))

print("\n== pull_request_stub: sim mode and deterministic branches ==")
pull_action = make_action(
    "pull_request_stub",
    {"topic": "Cache layer!", "title": "WIP: cache layer", "issue_number": 4, "meeting_title": "Eng sync"},
    quote="I'll open a PR for the cache layer tonight.",
    speaker="Dev",
)
pull_result = pull_request_stub_executor.execute(pull_action)
check("returns ok", pull_result.ok, pull_result.human_summary)
check("badged sim", pull_result.mode == "sim")
preview = pull_result.undo_payload["payload"]
check("draft is always true", preview["draft"] is True)
check("branch is namespaced", preview["branch"] == "adjourn/cache-layer", preview["branch"])
check(
    "branch name is deterministic across punctuation",
    pull_request_stub_executor.build_branch_name("Cache layer!")
    == pull_request_stub_executor.build_branch_name("  cache   LAYER "),
)
check("placeholder path is per-branch", preview["placeholder_path"] == ".adjourn/cache-layer.md")
check("PR body quotes the speaker", "**Dev:** I'll open a PR" in preview["body"])
check("PR body links the issue", "Relates to #4" in preview["body"])
check("PR body has the pre-flight checklist", "- [ ] Mark ready for review" in preview["body"])
check("undo of a sim PR is honest-true", pull_request_stub_executor.undo(pull_result) is True)
try:
    pull_request_stub_executor.placeholder_path_for("adjourn/.github/workflows/ci")
    check("workflow paths are refused", False, "did not raise")
except PermissionError:
    check("workflow paths are refused", True)

print("\n== linear_create: sim mode renders the exact GraphQL ==")
linear_action = make_action(
    "linear_create",
    {"title": "Add Redis cache layer", "team_key": "ENG", "meeting_title": "Eng sync"},
)
linear_result = linear_create_executor.execute(linear_action)
check("returns ok", linear_result.ok, linear_result.human_summary)
check("badged sim", linear_result.mode == "sim")
rendered = linear_result.undo_payload["payload"]
check("endpoint is rendered", rendered["url"] == "https://api.linear.app/graphql")
check("header rule is spelled out (no Bearer)", "no Bearer prefix" in rendered["authorization_header"])
check("mutation is issueCreate", "issueCreate" in rendered["mutation"])
issue_input = rendered["variables"]["input"]
check("input carries the title", issue_input["title"] == "Add Redis cache layer")
check("input names the team", "ENG" in issue_input["teamId"])
check("description carries the verbatim quote", "**Priya:** Let's move the cache layer" in issue_input["description"])
check("undo of a sim ticket is honest-true", linear_create_executor.undo(linear_result) is True)

print("\n== linear_move: the percent table is deterministic, not a model ==")
table = [
    (5, "", None),
    (24, "", None),
    (25, "", "In Progress"),
    (70, "", "In Progress"),
    (89, "", "In Progress"),
    (90, "", "In Review"),
    (100, "", "In Review"),
    (None, "", None),
    (10, "the PR is up already", "In Review"),
    (None, "it's in review now", "In Review"),
]
for percent, spoken, expected in table:
    got = linear_move_executor.choose_target_state(percent, spoken)
    check(f"choose_target_state({percent!r}, {spoken!r}) -> {expected!r}", got == expected, repr(got))

for raw, expected_percent in ((70, 70), ("70", 70), ("70%", 70), (70.0, 70), ("most of it", None), (None, None)):
    check(f"coerce_percent({raw!r}) -> {expected_percent!r}",
          linear_move_executor.coerce_percent(raw) == expected_percent)

check("identifier regex finds ENG-142", linear_move_executor.find_identifier("bump ENG-142 please") == "ENG-142")
check("identifier regex is case-insensitive on input", linear_move_executor.find_identifier("bump eng-142") == "ENG-142")
check("identifier regex ignores prose", linear_move_executor.find_identifier("no ticket here") is None)
check("state synonyms match", linear_move_executor.matches_state_name("Started", "In Progress"))
check("unrelated states do not match", not linear_move_executor.matches_state_name("Done", "In Progress"))

move_result = linear_move_executor.execute(
    make_action(
        "linear_move",
        {"linear_identifier": "ENG-142", "percent": 70, "meeting_title": "Eng sync"},
        quote="ENG-142 is about seventy percent done.",
    )
)
check("move returns ok", move_result.ok, move_result.human_summary)
check("move badged sim", move_result.mode == "sim")
check("move renders the mutation", "issueUpdate" in move_result.undo_payload["payload"]["mutation"])
check("move renders the quote comment", "**Priya:**" in move_result.undo_payload["payload"]["comment"])
check("undo of a sim move is honest-true", linear_move_executor.undo(move_result) is True)

no_move = linear_move_executor.execute(
    make_action("linear_move", {"linear_identifier": "ENG-9", "percent": 5}, quote="barely started ENG-9")
)
check("a 5% report moves nothing but still reports", no_move.ok and not no_move.undo_payload)
check("no-op summary says so", "left ENG-9 where it is" in no_move.human_summary, no_move.human_summary)

print("\n== recap_page: writes, backs up, undoes, upgrades ==")
recap_payload = {
    "meeting_id": "20260821-140312",
    "meeting_title": "Eng sync",
    "generated_at": "2026-08-21T14:05:00",
    "segment_times": {"L12": 723.4},
    "statements": [
        {
            "segment_id": "L12",
            "speaker": "Priya",
            "topic": "cache layer",
            "claim": "Move the cache layer to Redis",
            "kind": "decision",
            "quote": "Let's move the cache layer to Redis before the demo on Friday.",
            "entity_refs": {"issue_number": 4},
        },
        {
            "segment_id": "L18",
            "speaker": "Dev",
            "topic": "cache layer",
            "claim": "Dev will open the PR tonight",
            "kind": "pr_intent",
            "quote": "I'll open a PR for the cache layer tonight.",
            "entity_refs": {"deadline_text": "tonight"},
        },
        {
            "segment_id": "L24",
            "speaker": "Priya",
            "topic": "vendor",
            "claim": "Priya will email the vendor",
            "kind": "email_commitment",
            "quote": "I'll email the vendor tonight.",
            "entity_refs": {},
        },
    ],
    "executed": [
        {
            "kind": "github_update",
            "ok": True,
            "mode": "live",
            "human_summary": "commented on sharique2004/adjourn#4",
            "url": "https://github.com/sharique2004/adjourn/issues/4",
            "quote": "Let's move the cache layer to Redis before the demo on Friday.",
            "speaker": "Priya",
            "dedup_key": "github_update:4:cache-layer",
        },
        {
            "kind": "email_send",
            "ok": False,
            "mode": "sim",
            "human_summary": "no gmail app password — nothing sent",
            "quote": "I'll email the vendor tonight.",
            "speaker": "Priya",
            "dedup_key": "email_send:vendor",
        },
    ],
}
recap_result = recap_page_executor.execute(
    make_action("recap_page", recap_payload, meeting_id="20260821-140312")
)
recap_path = recap_page_executor.recap_path_for("20260821-140312")
check("recap returns ok", recap_result.ok, recap_result.human_summary)
check("recap is honestly live even under ADJOURN_SIM=1", recap_result.mode == "live")
check("recap file exists", recap_path.exists(), str(recap_path))
document = recap_path.read_text(encoding="utf-8")
check("dark near-black background", "--ink: #0a0b0d" in document)
check("mono type stack", "JetBrains Mono" in document)
check("no network assets", "http://" not in document.replace("http://www.w3.org", ""))
check("meeting header present", "<h1>Eng sync</h1>" in document)
check("live badge rendered", 'class="badge live">LIVE' in document)
check("sim badge rendered", 'class="badge sim">SIM' in document)
check("failed card marked", 'class="card failed"' in document)
check("mm:ss stamp from segment_times", ">12:03<" in document)
check("verbatim quote present", "Let&#x27;s move the cache layer to Redis" in document or "Let's move the cache layer to Redis" in document)
check("ledger lists Dev", "<h3>Dev" in document)
check("ledger lists Priya", "<h3>Priya" in document)
check("ledger shows the deadline text", "tonight" in document)
check("ledger excludes non-commitment kinds", "Move the cache layer to Redis" not in document)

check(
    "mm:ss formatting",
    recap_page_executor.format_minutes_seconds(723.4) == "12:03"
    and recap_page_executor.format_minutes_seconds(None) == "",
)

upgraded = recap_page_executor.upgrade_quotes_in_recap(
    "20260821-140312",
    {"github_update:4:cache-layer": "Let us move the cache layer over to Redis before Friday's demo."},
)
document = recap_path.read_text(encoding="utf-8")
check("quote upgrade rewrote the file", upgraded)
check("upgraded wording is in place", "Let us move the cache layer over to Redis" in document)
check("upgrade did not duplicate the card", document.count('data-dedup-key="github_update:4:cache-layer"') == 2)
check(
    "upgrading an unknown key changes nothing",
    recap_page_executor.upgrade_quotes_in_recap("20260821-140312", {"nope": "x"}) is False,
)

undo_recap = recap_page_executor.undo(recap_result)
check("recap undo reports success", undo_recap)
check("recap undo deleted the first-write file", not recap_path.exists())

print("\n== dispatch: the registry reaches all four executors ==")
for kind, payload in (
    ("github_update", {"issue_number": 4}),
    ("linear_create", {"title": "t"}),
    ("linear_move", {"linear_identifier": "ENG-1", "percent": 50}),
    ("pull_request_stub", {"topic": "t", "title": "WIP: t"}),
    ("recap_page", {"meeting_id": "dispatch-test", "meeting_title": "t"}),
):
    dispatched = execute_action(make_action(kind, payload))
    check(f"execute_action({kind}) succeeds", dispatched.ok, dispatched.human_summary)
    check(f"undo_result({kind}) succeeds", undo_result(dispatched) is True)
    check(
        f"{kind} never claims live without a transport",
        dispatched.mode == "sim" or kind == "recap_page",
        dispatched.mode,
    )

print("\n== failure records never wear a LIVE badge by mistake ==")
missing_issue = github_update_executor.execute(make_action("github_update", {}))
check("no issue_number fails cleanly", not missing_issue.ok)
check("and is badged sim", missing_issue.mode == results.MODE_SIM, missing_issue.mode)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
