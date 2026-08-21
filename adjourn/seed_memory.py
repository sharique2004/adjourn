"""Seed memory with the meeting the demo contradicts, and with the issues it talks about.

Adjourn's most interesting claim on stage — "this decision changed since last
week, and here is what moved" — is only true if last week is in memory. This is
the script that puts it there, and it is the last thing to run before a demo.

What it seeds, into BOTH backends so the automatic fallback answers identically:

  1. The GitHub issues of the demo repo. Without these,
     memory.find_issue_for_topic("auth migration") returns None and the update
     beat silently declines — the statement is real, the issue is real, and
     nothing connects them.
  2. The prior standup (2026-08-14), extracted through the REAL pipeline rather
     than loaded from ground truth, so what memory holds is what this machine's
     extractor actually produces. If the extractor is having a bad night, this is
     where you find out — not on stage.

Run:
    python -m adjourn.seed_memory                # both backends
    python -m adjourn.seed_memory --wipe         # reset first
    python -m adjourn.seed_memory --engine fixtures   # no model
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import config, extraction, memory_store

PRIOR_MEETING = "prior-standup"
GITHUB_CLI_TIMEOUT_SECONDS = 20

# Read-only fallback list, used when the gh CLI is unavailable. Kept in sync by
# hand and clearly marked, because a stale issue title here would send a comment
# to the wrong issue — the exact failure find_issue_for_topic exists to avoid.
FALLBACK_ISSUES: tuple[tuple[int, str], ...] = (
    (1, "Migrate auth service to OAuth2"),
    (2, "Add cache layer for transcript ingestion"),
    (3, "Mobile offline mode for field agents"),
    (4, "Rate-limit the public API"),
)


# Topics the pre-flight prints a resolution line for, and whether a GitHub issue
# number is the CORRECT answer for each.
#
# "streaming adapter" resolves to issue None on every clean seed, by design: it
# is Linear SHA-5, not a GitHub issue. The pre-flight used to print that line
# indistinguishably from a real failure, and DEMO.md told the presenter that
# "-> issue None" meant the seed was broken and to re-run --reseed — which burns
# ~20s and a model call, every time, forever, on a machine that is working
# perfectly. Each line now says which answer is the right one.
SEED_TOPIC_EXPECTATIONS: tuple[tuple[str, bool], ...] = (
    ("cache layer", True),
    ("auth migration", True),
    ("streaming adapter", False),
)


def report_topic_resolution(memory, backend: str) -> bool:
    """Print one honest line per seeded topic. True when every expectation held."""
    healthy = True
    for topic, expects_issue in SEED_TOPIC_EXPECTATIONS:
        resolved = memory.find_issue_for_topic(topic)
        if expects_issue and resolved is None:
            healthy = False
            verdict = "WRONG — expected a GitHub issue; re-run --reseed"
        elif expects_issue:
            verdict = "ok"
        elif resolved is None:
            verdict = "ok — this one is a Linear ticket (SHA-5), not a GitHub issue"
        else:
            verdict = f"unexpected — this topic should not resolve to a GitHub issue"
        print(f"[seed] {backend}: {topic!r} -> issue {resolved}  [{verdict}]")
    return healthy


def read_repository_issues(repo: str) -> list[tuple[int, str]]:
    """[(number, title)] for the demo repo's OPEN issues. Read-only `gh` call."""
    try:
        completed = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open",
             "--json", "number,title", "--limit", "50"],
            capture_output=True, text=True, timeout=GITHUB_CLI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"[seed] gh unavailable ({error.__class__.__name__}) — using the fallback list")
        return list(FALLBACK_ISSUES)
    if completed.returncode != 0:
        print(f"[seed] gh failed ({completed.stderr.strip()[:90]}) — using the fallback list")
        return list(FALLBACK_ISSUES)
    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return list(FALLBACK_ISSUES)
    return [(int(row["number"]), str(row["title"])) for row in rows]


def seed_issues(memory, repo: str) -> int:
    """Record every open issue so a topic can resolve to one. Returns how many."""
    issues = read_repository_issues(repo)
    for number, title in issues:
        memory.record_issue(number, repo, title)
    print(f"[seed] {len(issues)} issue(s) recorded from {repo}")
    return len(issues)


def extract_prior_meeting(engine: str | None) -> tuple[list, dict]:
    """The prior standup, through the real extraction path. (statements, meta)."""
    segments, meta = extraction.load_fixture_transcript(PRIOR_MEETING)
    if not segments:
        print(f"[seed] no transcript for {PRIOR_MEETING} — nothing to seed")
        return [], {}
    title = str(meta.get("title") or PRIOR_MEETING)
    statements = extraction.extract_statements(
        segments,
        title,
        source=extraction.SOURCE_FINAL,
        meeting_id=PRIOR_MEETING,
        engine=engine,
    )
    engines = sorted({getattr(statement, "engine", "?") for statement in statements})
    print(
        f"[seed] {len(statements)} statement(s) extracted from {len(segments)} segments "
        f"via {', '.join(engines) or 'nothing'}"
    )
    return statements, meta


def seed_backend(backend: str, statements: list, meta: dict, repo: str, *, wipe: bool) -> dict:
    """Seed one backend and return its stats. Never raises past a failed open."""
    try:
        memory = memory_store.open_memory(backend)
    except Exception as error:  # noqa: BLE001
        print(f"[seed] {backend}: could not open ({error}) — skipped")
        return {}
    try:
        if wipe:
            memory.wipe()
            memory.ensure_schema()
            print(f"[seed] {backend}: wiped")
        seed_issues(memory, repo)
        recorded = memory.ingest(statements, {
            "meeting_id": str(meta.get("meeting_id") or PRIOR_MEETING),
            "title": str(meta.get("title") or PRIOR_MEETING),
            "date": str(meta.get("date") or ""),
        })
        stats = memory.stats()
        print(f"[seed] {backend}: {recorded} statement(s) ingested — {stats}")
        print(f"[seed] {backend}: known topics -> {memory.known_topics()}")
        print(f"[seed] {backend}: handle index -> {memory.topic_index_by_reference()}")
        report_topic_resolution(memory, backend)
        return stats
    finally:
        memory.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adjourn.seed_memory")
    parser.add_argument("--wipe", action="store_true",
                        help="delete everything in each backend first (demo reset)")
    parser.add_argument("--backend", choices=("falkor", "sqlite", "both"), default="both")
    parser.add_argument("--engine", choices=("claude", "gemini", "fixtures"), default=None,
                        help="force an extraction engine (default: the configured ladder)")
    arguments = parser.parse_args(argv)

    config.ensure_state_directories()
    repo = config.github_repo()
    statements, meta = extract_prior_meeting(arguments.engine)
    if not statements:
        print("[seed] refusing to seed an empty meeting — memory left as it was")
        return 1

    backends = ("falkor", "sqlite") if arguments.backend == "both" else (arguments.backend,)
    for backend in backends:
        print(f"\n--- {backend} ---")
        seed_backend(backend, statements, meta, repo, wipe=arguments.wipe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
