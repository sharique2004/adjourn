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
  3. That same prior standup into the PUBLIC FalkorDB Cloud graph, so the web
     surface's Meetings tab is never a blank page before the demo. Bounded and
     best-effort: an unreachable cloud queues into the mirror backlog and the
     local seed is unaffected. Only the thin receipt travels — segment_id, kind,
     speaker, meeting_id — never the quote and never the transcript.

Run:
    python -m adjourn.seed_memory                # both + cloud
    python -m adjourn.seed_memory --wipe         # reset first
    python -m adjourn.seed_memory --engine fixtures   # no model
    python -m adjourn.seed_memory --no-cloud     # local only
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

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


# Topics the pre-flight prints a resolution line for.
#
# THE EXPECTATION IS DERIVED, NOT DECLARED, and that is the whole fix. This used
# to be a hand-written table saying that "streaming adapter" must resolve to NO
# GitHub issue, on the true-at-the-time grounds that it is Linear SHA-5. Then the
# demo world moved into Adjourn's own repo, and seed_demo_world's ROADMAP started
# filing a GitHub issue for that ticket too — #3, "Migrate transcript ingestion to
# the streaming adapter". The topic began resolving correctly to 3 and the
# pre-flight called its own healthy seed "unexpected", every run, for two days.
#
# A hand-maintained mirror of another module's data drifts the moment that module
# changes. So the answer comes from the ROADMAP itself now, which also buys a
# strictly better check: not "should this resolve at all" but "which NUMBER should
# it resolve to". That catches the failure that actually costs a demo — every card
# firing correctly onto the wrong issue — which the boolean version could not see.
SEED_TOPIC_PROBES: tuple[str, ...] = ("cache layer", "auth migration", "streaming adapter")


def expected_issue_numbers() -> dict[str, int | None]:
    """{probe topic: the issue number the seeded roadmap gives it}. Never raises.

    Issue numbers are creation order in ROADMAP, 1-based, which is the same
    contract seed_demo_world's own ordering comment relies on.
    """
    try:
        from .tools.seed_demo_world import ROADMAP
    except Exception as error:  # noqa: BLE001 — a pre-flight must not be a hard import
        print(f"[seed] could not read the roadmap ({error}) — reporting resolutions only")
        return {}
    expected: dict[str, int | None] = {}
    for topic in SEED_TOPIC_PROBES:
        needle = topic.casefold()
        expected[topic] = next(
            (
                number
                for number, entry in enumerate(ROADMAP, start=1)
                if needle in str(entry.get("title", "")).casefold()
            ),
            None,
        )
    return expected


def report_topic_resolution(memory, backend: str) -> bool:
    """Print one honest line per seeded topic. True when every expectation held."""
    healthy = True
    expected = expected_issue_numbers()
    for topic in SEED_TOPIC_PROBES:
        resolved = memory.find_issue_for_topic(topic)
        wanted = expected.get(topic)
        if topic not in expected:
            verdict = "resolved (no roadmap to check it against)"
        elif wanted is None and resolved is None:
            verdict = "ok — the roadmap files no GitHub issue for this one"
        elif wanted is None:
            healthy = False
            verdict = (
                f"WRONG — the roadmap files no issue for this topic, so #{resolved} "
                f"is a stale row; re-run --reseed"
            )
        elif resolved is None:
            healthy = False
            verdict = f"WRONG — expected issue #{wanted}; re-run --reseed"
        elif int(resolved) != int(wanted):
            healthy = False
            verdict = (
                f"WRONG — expected issue #{wanted} ({topic!r} is roadmap entry {wanted}); "
                f"every card on this topic would land on the wrong issue"
            )
        else:
            verdict = "ok"
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


def seed_prior_receipts(statements: list, meta: dict) -> int:
    """Write the prior standup's receipts into an ARCHIVED journal. Never raises.

    WHY. Follow-through's cold open must not be a blank pulse. The board falls
    back to `last_adjourned` when there are no live cards, and it reads that from
    the newest ARCHIVED journal before it falls back to memory. Straight after a
    reset there is no such journal with anything in it, so the first thing a
    judge sees is "0 actions · Adjourned. Waiting for the next meeting." — a
    working system doing its best impression of a dead one.

    NOTHING HERE IS INVENTED, which is the only reason this is allowed to exist.
    The receipts are produced by running the SEEDED statements through the REAL
    planner and the REAL executors, in SIM. The payloads are exactly what those
    executors would send; the sends did not happen; every row is badged `sim` on
    the board, which is what that badge is for. Fabricating plausible-looking
    receipts for a meeting instead of deriving them is the precise lie this
    codebase's sim mode exists to prevent, so we derive them.

    They go to an ARCHIVED file (`executions.<stamp>.jsonl`) and never to the
    live journal: a record in the live journal is a CARD on Follow-through, and
    last week's standup must be history behind the cold open, not today's work.
    """
    from . import executors, orchestrator, planner, results

    meeting = {
        "meeting_id": str(meta.get("meeting_id") or PRIOR_MEETING),
        "title": str(meta.get("title") or PRIOR_MEETING),
        "date": str(meta.get("date") or ""),
    }
    journal = config.executions_journal_path()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archived = journal.with_name(f"{journal.stem}.{stamp}{journal.suffix}")

    # Forced for the duration of this call, and restored in the finally: the
    # seeder must never depend on how .env happens to be set, and must never
    # leave the process in sim by accident for whatever runs next.
    previous_sim = os.environ.get("ADJOURN_SIM")
    os.environ["ADJOURN_SIM"] = "1"
    written = 0
    try:
        actions = planner.plan(statements, memory=None, meeting=meeting)
        for action in actions or []:
            # The recap writes a real HTML file; it is not a receipt of anything
            # that left the machine, and re-rendering last week's is noise.
            if action.kind == "recap_page":
                continue
            action.payload.setdefault("meeting_title", meeting["title"])
            result = executors.execute_action(action)
            results.append_execution(
                result, action.dedup_key,
                extra={"source": "seed", "segment_id": action.segment_id},
                path=archived,
            )
            written += 1
    except Exception as error:  # noqa: BLE001 — a blank cold open is bad; a failed seed is worse
        print(f"[seed] could not derive prior receipts ({error}) — memory seed is unaffected")
    finally:
        if previous_sim is None:
            os.environ.pop("ADJOURN_SIM", None)
        else:
            os.environ["ADJOURN_SIM"] = previous_sim

    if written:
        print(
            f"[seed] {written} simulated receipt(s) for {meeting['title']!r} "
            f"-> {archived.name} (the never-blank cold open)"
        )
        # Keep the orchestrator's rail honest about where the board's opening
        # shot came from, rather than leaving a stale run's rows behind it.
        orchestrator.report_pipeline_event(
            stage="executor", tone="note", label="seed",
            text=f"{written} prior receipt(s) archived for {meeting['title']}",
        )
    else:
        print("[seed] no prior receipts derived — the cold open will fall back to memory")
    return written


def mirror_prior_meeting(statements: list, meta: dict) -> bool:
    """Put the seeded prior standup into the PUBLIC graph too. Bounded, best effort.

    WHY THE SEED HAS TO REACH THE CLOUD. The public web surface reads the mirror,
    not the local graph, and its Meetings tab is the first page a link out of this
    demo lands on. A reset wipes memory and reseeds it locally, so the local graph
    is right and the public one is a blank page — the demo's own history exists on
    a laptop and nowhere a founder can click.

    WHAT ACTUALLY TRAVELS is unchanged and worth restating, because this is the
    one call that puts meeting content on somebody else's server: cloud_mirror's
    STATEMENT_FIELDS carries segment_id, kind, speaker and meeting_id. Not the
    quote, not the claim, not the transcript. Audio and transcripts never leave
    the Mac — only the receipts you see on the board are mirrored, and a seeded
    Statement node is the thinnest receipt there is.

    Never raises, never blocks past cloud_mirror's own ~8s budget, and a failure
    queues into state/mirror_backlog.jsonl exactly like a live write would.
    """
    try:
        from . import cloud_mirror
    except Exception as error:  # noqa: BLE001
        print(f"[seed] cloud mirror unavailable ({error}) — local seed is unaffected")
        return False
    if not cloud_mirror.is_configured():
        print("[seed] cloud mirror is not configured — skipping the public graph")
        return False

    rows = [
        statement.to_dict() if hasattr(statement, "to_dict") else dict(statement)
        for statement in (statements or [])
    ]
    payload = {
        "meeting_id": str(meta.get("meeting_id") or PRIOR_MEETING),
        "id": str(meta.get("meeting_id") or PRIOR_MEETING),
        "title": str(meta.get("title") or PRIOR_MEETING),
        "date": str(meta.get("date") or ""),
    }
    target = cloud_mirror.describe_target()
    sent = cloud_mirror.mirror_meeting(payload, rows)
    if sent:
        print(
            f"[seed] cloud: mirrored {payload['title']!r} with {len(rows)} statement(s) "
            f"-> graph {target.get('graph')} on {target.get('host')}"
        )
    else:
        print(
            f"[seed] cloud: could not reach {target.get('host')} — "
            f"{cloud_mirror.backlog_depth()} write(s) queued in the backlog"
        )
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adjourn.seed_memory")
    parser.add_argument("--wipe", action="store_true",
                        help="delete everything in each backend first (demo reset)")
    parser.add_argument("--backend", choices=("falkor", "sqlite", "both"), default="both")
    parser.add_argument("--engine", choices=("claude", "gemini", "fixtures"), default=None,
                        help="force an extraction engine (default: the configured ladder)")
    parser.add_argument("--no-cloud", action="store_true",
                        help="seed the local backends only; leave the public graph alone")
    parser.add_argument("--no-receipts", action="store_true",
                        help="skip the archived SIM receipts that keep the cold open non-blank")
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

    if not arguments.no_receipts:
        print("\n--- prior receipts ---")
        seed_prior_receipts(statements, meta)

    if not arguments.no_cloud:
        print("\n--- cloud ---")
        mirror_prior_meeting(statements, meta)
    return 0


if __name__ == "__main__":
    sys.exit(main())
