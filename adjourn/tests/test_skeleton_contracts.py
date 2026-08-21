"""Skeleton contract test — every module imports, contracts hold, stubs are honest.

This is the regression gate for the SHARED CONTRACTS. If you are implementing a
lane and this goes red, you changed a surface another lane is coding against —
fix the change, or tell the other lanes.

It asserts nothing about lane internals, only about the contracts:
  * every module imports clean
  * ACTION_KINDS, the executor REGISTRY, and ROUTING_TABLE agree with each other
  * dedup keys are deterministic and meaning-based
  * planner.py imports no model or network library
  * the journal and pending.json round-trip
  * every contract function is implemented (this gate inverted once they landed)
  * dispatch degrades to a failed result instead of crashing
  * the GitHub repo and Slack workspace guards actually refuse

Run from the repo root:
    python -m adjourn.tests.test_skeleton_contracts

Writes only to temp directories — it never touches the real executions journal.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# BEFORE any adjourn import. This gate dispatches a real slack_send through the
# real registry, and a real Slack bot token IS provisioned on this machine — so
# without this line, running the contract test posts a message to a workspace.
# Also redirect the journal and the recap directory, so a contract test never
# writes into the real ~/.meetingscribe/executions.jsonl or adjourn/recaps/.
os.environ["ADJOURN_SIM"] = "1"
_SANDBOX = tempfile.mkdtemp(prefix="adjourn-contracts-")
os.environ.setdefault("EXECUTIONS_JOURNAL", str(Path(_SANDBOX) / "executions.jsonl"))
os.environ.setdefault("RECAP_DIR", str(Path(_SANDBOX) / "recaps"))

# Importable whether run as -m from the repo root or as a plain script path.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


print("== import every module ==")
MODULES = [
    "adjourn",
    "adjourn.config",
    "adjourn.secrets_store",
    "adjourn.results",
    "adjourn.planner",
    "adjourn.extraction",
    "adjourn.orchestrator",
    "adjourn.watcher",
    "adjourn.meetingscribe_source",
    "adjourn.memory_store",
    "adjourn.executors",
    "adjourn.executors.github_update_executor",
    "adjourn.executors.linear_create_executor",
    "adjourn.executors.linear_move_executor",
    "adjourn.executors.pull_request_stub_executor",
    "adjourn.executors.slack_send_executor",
    "adjourn.executors.email_send_executor",
    "adjourn.executors.calendar_hold_executor",
    "adjourn.executors.recap_page_executor",
]
for name in MODULES:
    try:
        importlib.import_module(name)
        print(f"  ok   {name}")
    except Exception as error:
        print(f"  FAIL {name}: {type(error).__name__}: {error}")
        failures.append(name)

from adjourn import config, executors, extraction, orchestrator, planner, results, secrets_store

print("\n== config ==")
snapshot = config.describe_configuration()
check("port discovery returns an int", isinstance(config.discover_meetingscribe_port(), int),
      snapshot["meetingscribe_base_url"])
check("graph name is 'adjourn' not 'drift'", config.graph_name() == "adjourn", config.graph_name())
# This file forces ADJOURN_SIM=1 for its own safety, so the live environment
# cannot answer the question. Read what adjourn/.env ships instead.
_env_text = (config.PACKAGE_ROOT / ".env").read_text()
check("adjourn/.env ships ADJOURN_SIM=0", "\nADJOURN_SIM=0" in _env_text)
check("sim is forced inside this test", config.is_simulation_forced() is True)
check("memory backend is falkor", config.memory_backend() == "falkor")
check("github repo allowlisted", config.github_repo() in config.ALLOWED_GITHUB_REPOS)
check("regret window is 60", config.regret_window_seconds() == 60)
print(f"       engine -> {snapshot['meetingscribe_base_url']}")

print("\n== secrets_store ==")
availability = secrets_store.describe_secret_availability()
check("describe returns booleans only",
      all(isinstance(value, bool) for value in availability.values()), str(availability))
check("missing secret returns None", secrets_store.get_secret("definitely_not_provisioned") is None)
check("decide_mode falls back to sim",
      secrets_store.decide_mode(["definitely_not_provisioned"], label="smoke") == "sim")
print(f"       provisioned: {availability}")

print("\n== planner: dedup keys (deterministic core) ==")
check("normalize collapses punctuation and case",
      planner.normalize_key_text("I'll ping the team in Slack -- tonight!")
      == "i-ll-ping-the-team-in-slack-tonight",
      planner.normalize_key_text("I'll ping the team in Slack -- tonight!"))
key_one = planner.build_dedup_key("github_update", 4, "Cache Layer")
key_two = planner.build_dedup_key("github_update", 4, "cache   layer")
check("same meaning -> same key", key_one == key_two, f"{key_one} vs {key_two}")
check("key shape is kind-prefixed", key_one == "github_update:4:cache-layer", key_one)
check("different kind -> different key",
      planner.build_dedup_key("linear_create", "cache layer") != key_one)
check("empty parts dropped", planner.build_dedup_key("recap_page", "", "m1") == "recap_page:m1")
check("slack regret window is 60", planner.choose_regret_window("slack_send") == 60)
check("email regret window is 60", planner.choose_regret_window("email_send") == 60)
check("github fires immediately", planner.choose_regret_window("github_update") == 0)

print("\n== planner: no model may be imported here ==")
source = (config.PACKAGE_ROOT / "planner.py").read_text()
banned = [token for token in ("genai", "anthropic", "openai", "requests") if f"import {token}" in source]
check("planner imports no model/network library", not banned, str(banned))

print("\n== contract surfaces line up ==")
check("ACTION_KINDS matches executor REGISTRY",
      set(planner.ACTION_KINDS) == set(executors.REGISTRY),
      f"{set(planner.ACTION_KINDS) ^ set(executors.REGISTRY)}")
check("ROUTING_TABLE covers every statement kind",
      set(planner.ROUTING_TABLE) == set(extraction.STATEMENT_KINDS),
      f"{set(planner.ROUTING_TABLE) ^ set(extraction.STATEMENT_KINDS)}")
check("9 action kinds", len(planner.ACTION_KINDS) == 9, str(len(planner.ACTION_KINDS)))
check("10 statement kinds", len(extraction.STATEMENT_KINDS) == 10)

print("\n== every executor module exposes execute/undo ==")
for kind in executors.registered_kinds():
    module = executors.load_executor(kind)
    has_both = callable(getattr(module, "execute", None)) and callable(getattr(module, "undo", None))
    check(f"{kind} exposes execute+undo", has_both)

print("\n== results: journal round trip (temp file, never the real journal) ==")
with tempfile.TemporaryDirectory() as temporary:
    journal = Path(temporary) / "executions.jsonl"
    check("missing journal reads empty", results.read_executions(path=journal) == [])

    fired = results.ExecutorResult(
        ok=True, kind="github_update", external_id="c1",
        url="https://github.com/sharique2004/adjourn/issues/4",
        human_summary="Commented on #4", mode="live",
        undo_payload={"comment_id": "c1"}, quote="Let's move the cache to Redis.",
        speaker="Priya", meeting_id="m1",
    )
    simulated = results.ExecutorResult.simulated(
        "slack_send", "Would post to #eng",
        rendered_payload={"channel": "#eng", "text": "hi"}, meeting_id="m1",
    )
    broken = results.ExecutorResult.failed("linear_create", "no api key", meeting_id="m1")

    results.append_execution(fired, "github_update:4:cache-layer", path=journal)
    results.append_execution(simulated, "slack_send:eng:hi", path=journal)
    results.append_execution(broken, "linear_create:cache-layer", path=journal)

    check("three records on disk", len(results.read_executions(path=journal)) == 3)
    check("fired keys exclude the failure",
          results.read_fired_dedup_keys(path=journal)
          == {"github_update:4:cache-layer", "slack_send:eng:hi"},
          str(results.read_fired_dedup_keys(path=journal)))

    restored = results.read_execution_results(path=journal)[0]
    check("round trip preserves the quote", restored.quote == fired.quote)
    check("round trip preserves mode", restored.mode == "live")
    check("sim result reports is_simulated", simulated.is_simulated is True)
    check("failed result is not undoable", broken.is_undoable is False)

    results.append_undo(fired, True, "github_update:4:cache-layer", path=journal)
    check("undone key tracked",
          results.read_undone_dedup_keys(path=journal) == {"github_update:4:cache-layer"})

    totals = results.summarize_executions(path=journal)
    check("totals: 3 fired", totals["fired"] == 3, str(totals))
    check("totals: 1 live / 2 sim", totals["live"] == 1 and totals["sim"] == 2, str(totals))
    check("a failure that never sent is badged sim, not live", broken.mode == "sim", broken.mode)
    check("totals: 1 failed", totals["failed"] == 1, str(totals))
    check("totals: 1 undone", totals["undone"] == 1, str(totals))

    batch_one, offset = results.read_records_since(0, path=journal)
    check("tail-f reads all then nothing new", len(batch_one) == 4)
    batch_two, offset_two = results.read_records_since(offset, path=journal)
    check("tail-f second pass is empty", batch_two == [] and offset_two == offset)

    journal.write_text(journal.read_text() + "{ this is not json\n")
    check("malformed line is skipped, not raised",
          len(results.read_executions(path=journal)) == 4)

    filtered = results.read_executions(meeting_id="nope", path=journal)
    check("meeting filter works", filtered == [])

print("\n== orchestrator: pending.json round trip (temp file) ==")
with tempfile.TemporaryDirectory() as temporary:
    pending = Path(temporary) / "pending.json"
    check("missing pending reads empty", orchestrator.read_pending_actions(path=pending) == [])

    action = planner.Action(
        kind="slack_send",
        payload={"channel": "#eng", "text": "heads up", "human_preview": "Slack #eng: heads up"},
        dedup_key="slack_send:eng:heads-up",
        regret_window_s=60,
        quote="I'll ping the team.", speaker="Priya", meeting_id="m1",
    )
    check("countdown action reports reversible", action.is_reversible_on_countdown is True)
    check("action dict round trip",
          planner.Action.from_dict(action.to_dict()).dedup_key == action.dedup_key)

    entry = orchestrator.add_pending_action(action, meeting_title="Eng sync", path=pending)
    check("one pending item", len(orchestrator.read_pending_actions(path=pending)) == 1)
    check("preview carried onto the card", entry.human_preview == "Slack #eng: heads up")
    remaining = entry.seconds_remaining()
    check("countdown is ~60s", 50 <= remaining <= 60, str(remaining))
    check("nothing due yet", orchestrator.due_pending_actions(path=pending) == [])

    future = datetime.now(UTC) + timedelta(seconds=120)
    check("due after the window", len(orchestrator.due_pending_actions(future, path=pending)) == 1)

    orchestrator.add_pending_action(action, path=pending)
    check("re-adding same key does not duplicate",
          len(orchestrator.read_pending_actions(path=pending)) == 1)

    check("cancel succeeds", orchestrator.cancel_pending_action("slack_send:eng:heads-up", pending))
    check("cancelled item no longer due",
          orchestrator.due_pending_actions(future, path=pending) == [])
    check("cancel is idempotent",
          orchestrator.cancel_pending_action("slack_send:eng:heads-up", pending) is False)
    check("unknown key cancels nothing",
          orchestrator.cancel_pending_action("nope", pending) is False)

    orchestrator.clear_pending_actions(path=pending)
    check("clear empties the file", orchestrator.read_pending_actions(path=pending) == [])

print("\n== every contract function is implemented (or honestly stubbed) ==")
# This section used to assert that each function still RAISED NotImplementedError.
# That made it a gate that went red — and then, once run_orchestrator() became a
# real polling loop, one that HUNG FOREVER — precisely as the package got
# finished. Every entry is now implemented, so the check is inverted: each of
# these must be callable and must NOT raise NotImplementedError. Nothing in here
# may block, so anything that polls is called with a bounded stop.
IMPLEMENTED = [
    ("extraction.extract_statements", lambda: extraction.extract_statements([], "t")),
    ("planner.plan", lambda: planner.plan([], None)),
    ("planner.collapse_duplicate_actions", lambda: planner.collapse_duplicate_actions([])),
    ("orchestrator.read_pending_actions", lambda: orchestrator.read_pending_actions()),
    ("orchestrator.order_actions_for_firing", lambda: orchestrator.order_actions_for_firing([])),
    ("watcher.detect_events",
     lambda: importlib.import_module("adjourn.watcher").detect_events({}, None)),
    ("meetingscribe_source.describe_source",
     lambda: importlib.import_module("adjourn.meetingscribe_source").describe_source()),
    ("memory_store.describe_memory",
     lambda: importlib.import_module("adjourn.memory_store").describe_memory()),
]
for label, call in IMPLEMENTED:
    try:
        call()
        check(f"{label} is implemented", True)
    except NotImplementedError as error:
        check(f"{label} is implemented", False, f"still a stub: {error}")
    except Exception as error:
        # A real error (no engine running, no graph) is fine here: the claim is
        # only that the function exists and is not a stub.
        check(f"{label} is implemented", True, f"ran, raised {type(error).__name__}")

print("\n== dispatch degrades instead of crashing ==")
# Every executor is implemented now, so a dispatched slack_send SUCCEEDS — in sim,
# forced at the top of this file. The claim worth testing is no longer "the stub
# is honest" but "a dispatched action comes back as a result, never as an
# exception, and never wearing a LIVE badge when nothing was sent".
sim_result = executors.execute_action(action)
check("dispatch returns a result, not an exception", sim_result.kind == "slack_send")
check("a simulated send is badged sim", sim_result.mode == "sim", sim_result.mode)
check("a simulated send renders the payload it would have sent",
      bool(sim_result.undo_payload.get("payload")), str(sim_result.undo_payload)[:120])
unknown = executors.execute_action(planner.Action(kind="teleport", dedup_key="x"))
check("unknown kind returns a failed result", unknown.ok is False)
check("undo of a failed result returns False",
      executors.undo_result(results.ExecutorResult.failed("slack_send", "x")) is False)
check("email undo of a sim result is honest-true",
      importlib.import_module("adjourn.executors.email_send_executor").undo(
          results.ExecutorResult.simulated("email_send", "s", rendered_payload={})) is True)
check("email undo of a live result is honest-false",
      importlib.import_module("adjourn.executors.email_send_executor").undo(
          results.ExecutorResult(ok=True, kind="email_send", external_id="x", url=None,
                                 human_summary="sent", mode="live")) is False)

print("\n== guards ==")
github = importlib.import_module("adjourn.executors.github_update_executor")
try:
    github.assert_repo_is_allowed("someoneelse/private-repo")
    check("github repo guard blocks other repos", False, "did not raise")
except PermissionError:
    check("github repo guard blocks other repos", True)
github.assert_repo_is_allowed("sharique2004/adjourn")
check("github repo guard permits the demo repo", True)

slack = importlib.import_module("adjourn.executors.slack_send_executor")
try:
    slack.assert_workspace_is_allowed("wellx-ai")
    check("slack workspace guard blocks wellx-ai", False, "did not raise")
except PermissionError:
    check("slack workspace guard blocks wellx-ai", True)

print("\n" + "=" * 60)
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
