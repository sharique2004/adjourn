"""Run every suite in this package, one subprocess each, and report honestly.

    cd "<repo>" && python -m adjourn.tests.run_all

Why subprocesses rather than one import: several suites patch module globals,
set environment variables and repoint config paths. Run in one interpreter they
would contaminate each other and a green bar would mean nothing. One process per
suite is slower and true.

Two suites (`test_pr_review_suggestion_live`, and the `live_engine` tests inside
the meetings suites) reach the network. The live GitHub suite is gated behind
ADJOURN_LIVE_TEST=1 and is NOT run here by default; pass --live to include it.
The meetings suites skip their live tests by themselves when MeetingScribe is
not up, which is why they are safe to run unconditionally.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Ordered cheapest-and-most-foundational first, so the first red line is
# usually the most informative one.
OFFLINE_SUITES = (
    "test_skeleton_contracts",
    "test_understanding_extraction",
    "test_understanding_memory",
    "test_decision_pipeline",
    "test_lane_a_spine",
    "test_lane_c_executors",
    "test_lane_d_comms",
    "test_lane2_shell",
    "test_lane3_guards",
    "test_integration_wiring",
    "test_stage2_integration",
    "test_board_server",
    "test_pr_review_suggestion_pure",
    "test_meetings_ui_views",
    "test_meetings_ui_live",
    "test_cloud_mirror",
)

LIVE_SUITES = (
    "test_pr_review_suggestion_live",
)


def run(module: str, environment: dict) -> tuple[bool, float, str]:
    started = time.monotonic()
    finished = subprocess.run(
        [sys.executable, "-m", f"adjourn.tests.{module}"],
        capture_output=True,
        text=True,
        env=environment,
    )
    elapsed = time.monotonic() - started
    output = (finished.stdout or "") + (finished.stderr or "")
    return finished.returncode == 0, elapsed, output


def summary_line(output: str) -> str:
    """Pull the suite's own count line out of its output, whatever shape it uses."""
    for line in reversed(output.splitlines()):
        text = line.strip()
        if not text:
            continue
        if "passed" in text or "PASSED" in text or "FAILED" in text or text.startswith("OK"):
            return text
        if text.startswith("Ran ") and " test" in text:
            return text
    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="also run the suites that write to real services")
    parser.add_argument("--only", action="append", default=[],
                        help="run just this suite (repeatable)")
    options = parser.parse_args(argv)

    suites = list(OFFLINE_SUITES) + (list(LIVE_SUITES) if options.live else [])
    if options.only:
        suites = [name for name in suites if name in options.only] or options.only

    environment = dict(os.environ)
    if options.live:
        environment["ADJOURN_LIVE_TEST"] = "1"

    failures = []
    total = 0.0
    print(f"running {len(suites)} suite(s) with {sys.executable}\n")
    # THE SUITE MUST NOT LEAVE A PIPELINE STATUS FILE IN THE DEMO TREE.
    #
    # Executors report their own progress into adjourn/state/pipeline.json, and
    # the suites that drive executors directly do that for real — so a run of
    # `tests.run_all` left the file behind holding twenty rows of test internals
    # ("Would post to #all-test — Sam in the meeting…", and the titles of the
    # operator's real recordings, read off this machine's library). That file is
    # what the cold open's THINKING feed renders, so a test run five minutes
    # before the demo put test fixtures on the projector. DEMO.md's own order
    # happens to clear it (§1 runs the suite at step 4 and resets at step 6), but
    # that is luck, not a guarantee.
    #
    # One temp path for the whole run: every child resolves
    # config.pipeline_status_path() to it, the suites that test the file write
    # and read it exactly as before, and the demo tree is never touched.
    with tempfile.TemporaryDirectory(prefix="adjourn-tests-") as scratch:
        environment["ADJOURN_PIPELINE"] = str(Path(scratch) / "pipeline.json")
        for module in suites:
            ok, elapsed, output = run(module, environment)
            total += elapsed
            mark = "ok  " if ok else "FAIL"
            detail = summary_line(output)
            print(f"  {mark} {module:<34} {elapsed:6.1f}s  {detail}")
            if not ok:
                failures.append((module, output))

    print()
    if failures:
        for module, output in failures:
            print("=" * 72)
            print(f"FAILED: {module}")
            print("=" * 72)
            print(output[-6000:])
        print(f"{len(suites) - len(failures)}/{len(suites)} suites passed "
              f"in {total:.1f}s — {len(failures)} FAILED")
        return 1

    print(f"ALL {len(suites)} SUITES PASSED in {total:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
