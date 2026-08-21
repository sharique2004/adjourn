"""Stage 2 — the integration pass: everything the lanes handed to each other.

Offline, no network, no model. Every path this suite can write to is redirected
into one throwaway sandbox before adjourn is imported, and the last section
asserts that the shipped tree is untouched.

    python -m adjourn.tests.test_stage2_integration
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="adjourn-stage2-"))
(SANDBOX / "state").mkdir()
(SANDBOX / "recaps").mkdir()

# Snapshot the shipped recaps BEFORE adjourn is imported, so the final-state
# contract can say "this suite added nothing" rather than "the operator has
# never held a meeting". A live Stop writes <id>.html here; requiring the
# directory to hold only README made `run_all` fail after every real recording.
_SHIPPED_RECAPS = Path(__file__).resolve().parents[1] / "recaps"
_SHIPPED_RECAP_ACTIONS = Path(__file__).resolve().parents[1] / "state" / "recap_actions"
_RECAPS_BEFORE = frozenset(
    path.name for path in _SHIPPED_RECAPS.iterdir()
) if _SHIPPED_RECAPS.is_dir() else frozenset()
_ACTIONS_BEFORE = frozenset(
    path.name for path in _SHIPPED_RECAP_ACTIONS.iterdir()
) if _SHIPPED_RECAP_ACTIONS.is_dir() else frozenset()

os.environ.update({
    "ADJOURN_SIM": "1",
    "ADJOURN_STATE_DIR": str(SANDBOX / "state"),
    "RECAP_DIR": str(SANDBOX / "recaps"),
    "EXECUTIONS_JOURNAL": str(SANDBOX / "executions.jsonl"),
    "ADJOURN_PIPELINE": str(SANDBOX / "pipeline.json"),
    "EXTRACTION_ENGINE": "fixtures",
    "MEMORY_BACKEND": "sqlite",
    # Firing an action mirrors it off this machine on a daemon thread. Blanking
    # the cloud host makes is_configured() false, so the mirror lands in the
    # sandboxed backlog file rather than in the demo's own cloud graph.
    "FALKORDB_CLOUD_HOST": "",
})

from adjourn import config, extraction, orchestrator, planner, results  # noqa: E402
from adjourn.executors import pr_review_suggestion_executor as review  # noqa: E402
from adjourn.executors import recap_page_executor  # noqa: E402
from adjourn.executors import linear_create_executor  # noqa: E402
from adjourn.tests import minipytest  # noqa: E402
from adjourn.tools import replay_check  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}" + (f"  — {detail}" if detail else ""))


# --- 1. the pytest shim ------------------------------------------------------
#
# Two suites in this package are written in pytest style and drift's venv has no
# pytest, so for a while nobody could honestly claim the suite was green. The
# shim is what closed that, which makes the shim itself load-bearing.

print("\n== the pytest shim answers the surface those suites actually use ==")

check("a fixture is marked and found",
      getattr(minipytest.fixture(lambda: 1), "__minipytest_fixture__", False))

_single = minipytest.mark.parametrize("value", [1, 2, 3])(lambda value: None)
check("parametrize expands one argument",
      [case["value"] for case in _single.__minipytest_params__] == [1, 2, 3])

_pair = minipytest.mark.parametrize("a,b", [(1, 2), (3, 4)])(lambda a, b: None)
check("parametrize expands a tuple of arguments",
      _pair.__minipytest_params__ == [{"a": 1, "b": 2}, {"a": 3, "b": 4}])

_skipped = minipytest.mark.skipif(True, reason="because")(lambda: None)
check("skipif marks", getattr(_skipped, "__minipytest_skip__", "") == "because")
check("a false skipif does not",
      not hasattr(minipytest.mark.skipif(False, reason="x")(lambda: None),
                  "__minipytest_skip__"))

try:
    with minipytest.raises(ValueError):
        raise ValueError("expected")
    _raises_ok = True
except Exception:  # noqa: BLE001
    _raises_ok = False
check("raises swallows the expected exception", _raises_ok)

try:
    with minipytest.raises(ValueError):
        pass
except AssertionError:
    _did_not_raise = True
else:
    _did_not_raise = False
check("raises fails loudly when nothing was raised", _did_not_raise)

try:
    with minipytest.raises(ValueError):
        raise TypeError("different")
except TypeError:
    _wrong_type_propagates = True
else:
    _wrong_type_propagates = False
check("raises lets the WRONG exception through rather than eating it",
      _wrong_type_propagates)

# The teardown fixtures are the reason the shim is trustworthy: a monkeypatch
# that never undid itself would leave config.RECORDINGS_DIR pointing at a
# deleted temp folder for every test after it.
_module = {
    "test_uses_tmp_path": lambda tmp_path: (SEEN.append(tmp_path), None)[1],
}
SEEN: list[Path] = []
_passed, _failed, _ = minipytest.run_module(_module)
check("a tmp_path fixture is supplied", _failed == 0 and len(SEEN) == 1)
check("and it is removed when the test ends", SEEN and not SEEN[0].exists())

_probe = type("Probe", (), {"value": "original"})
os.environ["ADJOURN_SHIM_PROBE"] = "before"


def _test_monkeypatch(monkeypatch):
    monkeypatch.setattr(_probe, "value", "patched")
    monkeypatch.setenv("ADJOURN_SHIM_PROBE", "during")
    monkeypatch.delenv("ADJOURN_SHIM_PROBE_ABSENT", raising=False)
    assert _probe.value == "patched"


minipytest.run_module({"test_monkeypatch": _test_monkeypatch})
check("monkeypatch.setattr is undone after the test", _probe.value == "original")
check("monkeypatch.setenv is undone too",
      os.environ.get("ADJOURN_SHIM_PROBE") == "before")
os.environ.pop("ADJOURN_SHIM_PROBE", None)


def _test_that_fails():
    raise AssertionError("this is supposed to fail")


# Captured, because an unsuppressed "FAIL" line from a deliberately failing
# probe in the middle of a passing run is how somebody reads a green suite as red.
import contextlib  # noqa: E402
import io  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()) as _quiet:
    _p, _f, _s = minipytest.run_module({"test_that_fails": _test_that_fails})
check("a failing test is counted as failed, not swallowed", (_p, _f) == (0, 1))
check("...and the failure is reported with its traceback",
      "AssertionError" in _quiet.getvalue())


# --- 2. deriving the old value out of the diff -------------------------------
#
# Nobody says "two E C C seven one" out loud. They say "that green is wrong".
# The old hex exists only in the PR's diff, so the executor reads it there — but
# only when the diff gives exactly one answer.

print("\n== the reviewer reads the diff for the value nobody said ==")

ONE_COLOUR = [{
    "filename": "web/app/globals.css",
    "patch": (
        "@@ -20,6 +20,9 @@\n"
        "   --amber: #e3b341;\n"
        "+  --join-accent: #2ecc71;\n"
        "+  --join-accent-ink: var(--bg);\n"
        " \n"
        "   --mono: monospace;\n"
    ),
}]

_value, _why = review.derive_old_value(ONE_COLOUR, "#6b7f99")
check("the one colour the PR adds is found", _value == "#2ecc71", f"{_value!r} — {_why}")
check("and it says where it got it", "only hex-colour" in _why, _why)

check("a context line's colour is NOT a candidate — the PR did not write it",
      "#e3b341" not in _value)

TWO_COLOURS = [{
    "filename": "a.css",
    "patch": "@@ -1,1 +1,3 @@\n+  --one: #2ecc71;\n+  --two: #ff0000;\n x\n",
}]
_value, _why = review.derive_old_value(TWO_COLOURS, "#6b7f99")
check("two candidates is a refusal, not a coin flip", _value == "", _why)
check("and the refusal names them", "#2ecc71" in _why and "#ff0000" in _why, _why)

SAME_COLOUR_TWICE = [{
    "filename": "a.css",
    "patch": "@@ -1,1 +1,3 @@\n+  --one: #2ecc71;\n+  --two: #2ecc71;\n x\n",
}]
_value, _why = review.derive_old_value(SAME_COLOUR_TWICE, "#6b7f99")
check("one colour on two lines is also a refusal — which line did they mean?",
      _value == "", _why)

_value, _why = review.derive_old_value(ONE_COLOUR, "slate blue")
check("a colour NAME is not a shape it can search for", _value == "", _why)

ALREADY_NEW = [{"filename": "a.css", "patch": "@@ -1,1 +1,2 @@\n+  --one: #6b7f99;\n x\n"}]
_value, _why = review.derive_old_value(ALREADY_NEW, "#6b7f99")
check("the new value itself is never proposed as the old one", _value == "", _why)

UNITS = [{"filename": "a.css", "patch": "@@ -1,1 +1,2 @@\n+  timeout: 10s;\n x\n"}]
_value, _why = review.derive_old_value(UNITS, "30s")
check("the same trick works for a number with a unit", _value == "10s", _why)

check("shapes are classified, not guessed",
      [review.value_shape(v) for v in ("#2ecc71", "12px", "green", "")]
      == ["hex-colour", "number-with-unit", "", ""])

check("the whole-line replacement keeps indentation and everything else",
      review.build_suggestion_line("  --join-accent: #2ecc71;", "#2ecc71", "#6b7f99")
      == "  --join-accent: #6b7f99;")

# The prop itself: two anchored patches into web/'s real landing page. If the
# seeder ever introduces a second colour, this is the test that says so.
from adjourn.tools import github_world  # noqa: E402

check("the prop still names exactly one new colour",
      github_world.CSS_TOKEN_INSERTION.count("#") == 1,
      github_world.CSS_TOKEN_INSERTION)
check("...and it is the hex the fixture expects",
      github_world.PROP_OLD_HEX in github_world.CSS_TOKEN_INSERTION)


# --- 3. the recap page tells the truth about where the quote goes ------------

print("\n== the privacy footer says what is actually true ==")

_page = recap_page_executor.render_recap_html(
    {"meeting_title": "T", "meeting_id": "m", "executed": [],
     "segment_count": 28, "statements": []})
check("the audio and the full transcript claim survives",
      "The audio, the full transcript and" in _page)
check("the page no longer claims the quoted sentence stays put either",
      "the only text that travels" in _page)
check("an empty meeting still says what was spoken", "28" in _page)

_clause = recap_page_executor._cloud_mirror_clause()
try:
    from adjourn import cloud_mirror
    _mirror_on = cloud_mirror.is_configured()
except Exception:  # noqa: BLE001
    _mirror_on = False
check("the cloud clause appears exactly when a cloud graph is configured",
      bool(_clause) == bool(_mirror_on), f"clause={_clause!r} configured={_mirror_on}")


# --- 4. state paths are redirectable -----------------------------------------
#
# `recap_action_path` read the config CONSTANT, so no test could point it
# anywhere: that is how `state/recap_actions/quiet-meeting.json` kept appearing
# in the shipped tree, in violation of the final-state contract.

def _cloud_backlog_path() -> Path:
    """Where queued mirror writes land. Its own accessor, for the same reason.

    cloud_mirror imports nothing else from adjourn on purpose, so it reads
    ADJOURN_STATE_DIR itself rather than calling config.state_directory().
    """
    from .. import cloud_mirror
    return cloud_mirror.backlog_path()


print("\n== every state path can be pointed somewhere disposable ==")

check("state_directory honours the override",
      config.state_directory() == SANDBOX / "state", str(config.state_directory()))
for label, path in (
    ("the parked recap action", orchestrator.recap_action_path("probe")),
    ("pending.json", config.pending_actions_path()),
    ("pipeline.json", config.pipeline_status_path()),
    ("the sqlite memory", config.sqlite_memory_path()),
    ("the linear team cache", linear_create_executor.team_cache_path()),
    ("the executions journal", config.executions_journal_path()),
    ("the cloud-mirror backlog", _cloud_backlog_path()),
):
    check(f"{label} lands in the sandbox", str(path).startswith(str(SANDBOX)), str(path))


# --- 5. undo rewrites the recap, whichever door it came through --------------
#
# It used to happen only inside board_server's route, so the documented CLI
# escape hatch (`--undo <key>`) reversed the action and left the recap page
# still listing it as LIVE with a permalink that now 404s.

print("\n== undo rewrites the recap from the orchestrator, not just the board ==")

MEETING = "stage2-undo-probe"
_statements = [extraction.Statement(
    segment_id="s01", speaker="Sharique", topic="cache layer",
    claim="The ingestion cache drops Redis for an in-process LRU.",
    kind="decision", quote="We are not using Redis for the ingestion cache.",
    entity_refs=extraction.EntityReferences(issue_number=2),
)]
_meeting = {"meeting_id": MEETING, "title": "Undo probe", "date": "2026-08-21"}
_plan = planner.plan(_statements, None, meeting=_meeting)
# fire_actionS, not fire_action: the plural is what parks the recap action under
# state/recap_actions/, and the parked copy is the ONLY thing a later undo can
# re-render the page from. Driving the singular here would test a path the
# product never takes and quietly pass while the real one broke.
_fired = orchestrator.fire_actions(_plan, meeting_title=_meeting["title"], segment_count=12)
_recap_file = config.recaps_directory() / f"{MEETING}.html"
check("the recap action was parked for a later process to find",
      orchestrator.recap_action_path(MEETING).is_file(),
      str(orchestrator.recap_action_path(MEETING)))
check("the probe wrote a recap", _recap_file.is_file(), str(_recap_file))

_work = [a for a in _plan if a.kind != "recap_page"]
check("the probe fired something undoable", bool(_work), str([a.kind for a in _plan]))

_before = _recap_file.read_text(encoding="utf-8")
check("the recap listed the action before the undo",
      "UNDONE" not in _before)

_undone = orchestrator.undo_execution(_work[0].dedup_key)
check("the undo succeeded", _undone is True)
_after = _recap_file.read_text(encoding="utf-8")
check("...and the orchestrator rewrote the page by itself", _before != _after)
check("the undone row is struck through", "UNDONE" in _after)

_rows = results.read_executions()
check("history was appended to, never rewritten",
      any(r.get("record_type") == "undo" for r in _rows))
check("the undo payload is still on disk, so a reset cannot strand it",
      any(r.get("undo_payload") for r in _rows))

# Undoing the recap itself must not immediately write it back.
_recap_action = next(a for a in _plan if a.kind == "recap_page")
orchestrator.undo_execution(_recap_action.dedup_key)
check("undoing the recap removes it and does not resurrect it",
      not _recap_file.exists(), str(_recap_file))


# --- 6. the replay corpus is intact ------------------------------------------

print("\n== the restraint corpus ships with the package ==")

for scenario in replay_check.SCENARIOS:
    check(f"{scenario.name}: transcript present", scenario.path.is_file(), str(scenario.path))

_restraint = [s for s in replay_check.SCENARIOS if s.expect_no_work]
check("five scenarios exist whose correct answer is 'do nothing'",
      len(_restraint) >= 5, str(len(_restraint)))
check("every one of them still expects a recap",
      all(s.expect_recap for s in _restraint))
check("the PR beat asserts it fires NOTHING but the review",
      any(s.name == "pr-review-beat" and s.exact_kinds for s in replay_check.SCENARIOS))

# Rule 3b(d) is the prompt half of that assertion; the transcript line it was
# written for is in the corpus, so the two cannot drift apart silently.
from adjourn import prompts  # noqa: E402

_rules = prompts.EXTRACTION_INSTRUCTIONS
check("the prompt names four non-actionable shapes, not three",
      "FOUR KINDS OF SENTENCE" in _rules)
check("...and the fourth is somebody else's work, sized by the room",
      "SIZED BY THE ROOM" in _rules)
check("the modal rule is stated",
      "NEVER STRENGTHEN A MODAL" in _rules)
_beat = (config.FIXTURES_DIR / "pr-review-beat.jsonl").read_text(encoding="utf-8")
check("the line rule 3b(d) was written for is still in the transcript",
      "she can turn that around before Friday" in _beat)


# --- 7. and the shipped tree is untouched ------------------------------------

print("\n== nothing was written outside the sandbox ==")

check("this suite added no recap files to the shipped tree",
      frozenset(path.name for path in config.RECAPS_DIR.iterdir()) == _RECAPS_BEFORE
      if config.RECAPS_DIR.is_dir() else not _RECAPS_BEFORE,
      str(sorted(path.name for path in config.RECAPS_DIR.iterdir()) if config.RECAPS_DIR.is_dir() else []))
_parked_dir = config.STATE_DIR / "recap_actions"
_actions_after = frozenset(path.name for path in _parked_dir.iterdir()) if _parked_dir.exists() else frozenset()
check("this suite added no parked recap actions to the shipped tree",
      _actions_after == _ACTIONS_BEFORE, str(sorted(_actions_after - _ACTIONS_BEFORE)))
check("the real executions journal was never touched by this suite",
      not config.EXECUTIONS_JOURNAL_PATH.exists()
      or MEETING not in config.EXECUTIONS_JOURNAL_PATH.read_text(encoding="utf-8"))
_backlog = config.STATE_DIR / "mirror_backlog.jsonl"
check("this suite left no probe rows in the cloud-mirror backlog",
      not _backlog.exists() or MEETING not in _backlog.read_text(encoding="utf-8"))

shutil.rmtree(SANDBOX, ignore_errors=True)
check("the sandbox is cleaned up after itself", not SANDBOX.exists())

print(f"\n{'=' * 72}")
print(f"{PASSED} passed, {FAILED} failed")
print("=" * 72)
raise SystemExit(1 if FAILED else 0)
