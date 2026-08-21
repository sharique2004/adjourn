"""Replay every rehearsed meeting and assert what Adjourn decided to do.

    python -m adjourn.tools.replay_check              # every scenario
    python -m adjourn.tools.replay_check design-review pr-review-beat
    python -m adjourn.tools.replay_check --verbose

This is the product's central claim expressed as a test you can run: given these
spoken words, exactly these actions and no others. Half the corpus exists to
fire nothing at all.

WHAT IT DOES NOT DO, on purpose: it runs extraction and the planner and stops.
No orchestrator, no journal line, no recap file, no memory ingest, no executor,
no network beyond the extraction engine itself. So it can be run at any time,
including five minutes before a demo, and there is nothing to clean up
afterwards.

The transcripts live in `adjourn/fixtures/regression/`. Four of them were
written by an auditor whose job was to break the restraint thesis, and who
succeeded: `fp-hypothetical` and `fp-reported` between them once produced a
public comment publishing a prohibition as a decision, a Linear ticket filed in
the name of somebody who never spoke, and a real branch with a draft PR. They
are kept because that is what a regression fixture is.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config, extraction, planner

REGRESSION_DIR = config.FIXTURES_DIR / "regression"


@dataclass(frozen=True)
class Scenario:
    """One transcript and what the table is supposed to decide about it."""

    name: str
    path: Path
    why: str
    expect_kinds: tuple[str, ...] = ()          # work actions that MUST be planned
    forbid_kinds: tuple[str, ...] = ()          # work actions that must NOT be
    expect_no_work: bool = False                # nothing at all but the recap
    expect_recap: bool = True                   # a recap always, even from silence
    expect_in_plan: tuple[str, ...] = ()        # substrings of describe_plan()
    exact_kinds: bool = False                   # expect_kinds is the WHOLE list


def fixture(name: str) -> Path:
    return config.FIXTURES_DIR / f"{name}.jsonl"


def regression(name: str) -> Path:
    return REGRESSION_DIR / f"{name}.jsonl"


# The corpus. Ordered restraint-first, because that is the order in which these
# things matter: a beat that does not fire is a bug you can fix on stage, and a
# beat that fires when it should not is a public artifact you cannot.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="fp-hypothetical",
        path=regression("fp-hypothetical"),
        why="negations, counterfactuals and two people explicitly declining to record anything",
        expect_no_work=True,
    ),
    Scenario(
        name="fp-reported",
        path=regression("fp-reported"),
        why="nineteen segments of reported speech; nobody in the room commits to anything",
        expect_no_work=True,
    ),
    Scenario(
        name="fp-social",
        path=regression("fp-social"),
        why="dinner. 'I promise you'll love this place' is not a commitment to anything",
        expect_no_work=True,
    ),
    Scenario(
        name="fp-meta",
        path=regression("fp-meta"),
        why="a meeting ABOUT tickets and PRs and emails, which must produce none of them",
        expect_no_work=True,
    ),
    Scenario(
        name="no-actions",
        path=regression("no-actions"),
        why="small talk — and the recap still fires, so restraint leaves evidence",
        expect_no_work=True,
    ),
    Scenario(
        name="hallway-sync",
        path=regression("hallway-sync"),
        why="two real beats buried in chat: a settled decision and a requested ticket",
        expect_kinds=("github_update", "linear_create"),
    ),
    Scenario(
        name="design-review",
        path=regression("design-review"),
        why="the full spread — a reversed decision, a work claim, a ticket, a draft PR, a date",
        expect_kinds=("github_update", "linear_move", "linear_create", "slack_send"),
        expect_in_plan=("SHA-6",),
    ),
    Scenario(
        name="agi-living-room",
        path=fixture("agi-living-room"),
        why="the demo meeting",
        expect_kinds=("github_update", "linear_create", "slack_send"),
    ),
    Scenario(
        name="pr-review-beat",
        path=fixture("pr-review-beat"),
        why="the PR-review beat — a correction on a colleague's open pull request",
        expect_kinds=("pr_review_suggestion",),
        forbid_kinds=("github_update",),   # suppressed by the review suggestion
        # Nothing else at all. "It's one line in the stylesheet, she can turn
        # that around before Friday" once put a calendar hold on an absent
        # colleague's unpromised work; prompt rule 3b(d) is why it no longer does.
        exact_kinds=True,
    ),
)

BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}


def replay(scenario: Scenario) -> tuple[list, list, float]:
    """(statements, actions, seconds). Extraction + planner, nothing else."""
    segments, meta = extraction.load_fixture_transcript(str(scenario.path))
    meeting_id = meta.get("meeting_id") or scenario.name
    meeting = {
        "meeting_id": meeting_id,
        "title": meta.get("title", meeting_id),
        "date": meta.get("date", ""),
    }
    started = time.monotonic()
    statements = extraction.extract_statements(
        segments,
        meeting["title"],
        source=extraction.SOURCE_FINAL,
        meeting_id=meeting_id,
    )
    elapsed = time.monotonic() - started
    actions = planner.plan(statements, None, meeting=meeting)
    return statements, actions, elapsed


def check(scenario: Scenario, statements: list, actions: list) -> list[str]:
    """Every way this replay disagreed with the scenario. Empty means it passed."""
    failures: list[str] = []
    work = [action for action in actions if action.kind != "recap_page"]
    kinds = {action.kind for action in work}
    plan_text = planner.describe_plan(actions) or ""

    if scenario.expect_recap and not any(a.kind == "recap_page" for a in actions):
        failures.append("no recap_page — restraint with no evidence on the board is "
                        "indistinguishable from a crash")

    if scenario.expect_no_work and work:
        for action in work:
            failures.append(
                f"fired {action.kind} on {action.segment_id or '?'}: "
                f"{(action.quote or '')[:90]}"
            )

    for kind in scenario.expect_kinds:
        if kind not in kinds:
            failures.append(f"expected a {kind} and got none (planned: "
                            f"{sorted(kinds) or 'nothing'})")

    for kind in scenario.forbid_kinds:
        if kind in kinds:
            failures.append(f"planned {kind}, which this beat must not produce")

    if scenario.exact_kinds:
        extra = kinds - set(scenario.expect_kinds)
        if extra:
            failures.append(f"planned {sorted(extra)} on top of the beats this "
                            "scenario is supposed to produce")

    for needle in scenario.expect_in_plan:
        if needle not in plan_text:
            failures.append(f"{needle!r} does not appear anywhere in the plan")

    return failures


def describe(scenario: Scenario, statements: list, actions: list,
             elapsed: float, verbose: bool) -> None:
    work = [action for action in actions if action.kind != "recap_page"]
    held = [s for s in statements
            if any(getattr(s, flag, False) for flag in ("negated", "rejected", "third_party"))]
    print(f"\n{'=' * 72}")
    print(f"{scenario.name}  —  {scenario.why}")
    print(f"{'=' * 72}")
    print(f"  {len(statements)} statement(s) in {elapsed:.1f}s"
          f"{f', {len(held)} held by the restraint gate' if held else ''}"
          f"  ->  {len(work)} work action(s) + {len(actions) - len(work)} recap")
    if verbose:
        for statement in statements:
            flags = ",".join(name for name in ("negated", "rejected", "third_party")
                             if getattr(statement, name, False))
            tag = f"  HELD[{flags}]" if flags else ""
            print(f"    [{statement.segment_id}] {statement.kind}{tag}: {statement.claim[:88]}")
    for action in work:
        payload = action.payload or {}
        detail = (payload.get("human_preview") or payload.get("human_summary")
                  or payload.get("title") or payload.get("claim")
                  or payload.get("body") or action.dedup_key or "")
        print(f"    + {action.kind}: {str(detail)[:88]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="*", help="names to run (default: all)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="print every statement, not just the actions")
    parser.add_argument("--list", action="store_true", help="list the corpus and exit")
    options = parser.parse_args(argv)

    if options.list:
        for scenario in SCENARIOS:
            print(f"  {scenario.name:<18} {scenario.why}")
        return 0

    chosen = SCENARIOS
    if options.scenario:
        unknown = [name for name in options.scenario if name not in BY_NAME]
        if unknown:
            print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"known: {', '.join(BY_NAME)}", file=sys.stderr)
            return 2
        chosen = tuple(BY_NAME[name] for name in options.scenario)

    print(f"engine: {config.extraction_engine()}   "
          f"sim forced: {config.is_simulation_forced()}")
    print(f"replaying {len(chosen)} scenario(s) — extraction and the planner only, "
          "nothing is written")

    verdicts: list[tuple[str, list[str]]] = []
    total = 0.0
    for scenario in chosen:
        if not scenario.path.is_file():
            verdicts.append((scenario.name, [f"transcript missing: {scenario.path}"]))
            continue
        statements, actions, elapsed = replay(scenario)
        total += elapsed
        describe(scenario, statements, actions, elapsed, options.verbose)
        failures = check(scenario, statements, actions)
        verdicts.append((scenario.name, failures))
        for failure in failures:
            print(f"    FAIL  {failure}")

    print(f"\n{'=' * 72}")
    broken = [(name, failures) for name, failures in verdicts if failures]
    for name, failures in broken:
        print(f"FAILED {name}: {len(failures)} problem(s)")
    passed = len(verdicts) - len(broken)
    print(f"{passed}/{len(verdicts)} scenarios as specified "
          f"({total:.1f}s of extraction)")
    print("=" * 72)
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
