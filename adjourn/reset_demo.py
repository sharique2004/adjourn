"""Reset the demo to a clean board. Run this right before you present.

What it clears (all local, all reversible by simply running the demo again):

    ~/.meetingscribe/executions.jsonl   the journal the board renders
    adjourn/state/pending.json          anything mid-countdown
    adjourn/state/pipeline.json         guts-panel phases
    adjourn/state/recap_actions/*.json  parked recap actions
    adjourn/state/watcher.json          so the watcher primes fresh
    adjourn/state/engine_launch.json    last MeetingScribe launch, for the UI pill
    adjourn/recaps/*.html               previously written recaps
    adjourn/state/holds/*.ics           previously written calendar holds

THE JOURNAL IS ARCHIVED, NEVER DELETED. ~/.meetingscribe/executions.jsonl holds
the comment ids, branch names and Slack timestamps that Undo needs; deleting it
makes every live write of that rehearsal permanently unreversible. So it is
renamed to a timestamped sibling (executions.20260821-014233.jsonl) and left
there. If you find a stray comment on the demo repo tomorrow morning, the handle
that removes it is still on disk.

AND IT REFUSES TO RUN WHILE LIVE WRITES ARE STILL OUT THERE. If the journal
holds actions that went LIVE and were never undone, reset stops and lists them:
those are real comments on a real issue, real tickets, real messages, and the
runbook's own verification step ("issue #2 has no comments") fails out loud in
front of the room if they are still up. Undo them from the board, or pass
--force, which prints exactly what it is orphaning before it archives.

What it does NOT touch, ever: ~/MeetingScribe, the recordings directory, drift/,
the drift graph, or anything on GitHub. External cleanup is a human step — this
script will not delete anything it did not write.

Memory is left alone by default, because seeding it takes a model call and the
prior standup is the whole reason the conflict beat works. Pass --reseed to wipe
and re-seed it through adjourn.seed_memory.

    python -m adjourn.reset_demo
    python -m adjourn.reset_demo --reseed
    python -m adjourn.reset_demo --check      # report, change nothing
    python -m adjourn.reset_demo --force      # archive anyway, loudly
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from . import config, results

# The meetings a rehearsal writes into memory. The PRIOR standup is deliberately
# not here: it is the seed, and forgetting it would quietly disarm the conflict
# beat while leaving everything looking fine.
DEFAULT_REHEARSAL_MEETINGS: tuple[str, ...] = ("living-room-standup", "pr-review-beat")

# Entity labels a rehearsal creates and a reset must sweep when they are left
# edgeless. Issue is NOT in this set: the prior-standup seed legitimately holds
# edgeless Issue nodes, and deleting those corrupts the baseline the conflict
# beat reads from.
ORPHAN_SWEEP_LABELS: tuple[str, ...] = ("Topic", "Person", "Ticket")


def clear_file(path, label: str) -> None:
    """Delete one file if it exists, and say so either way."""
    try:
        if path.exists():
            path.unlink()
            print(f"[reset] cleared {label}: {path}")
        else:
            print(f"[reset] {label} already clean")
    except OSError as error:
        print(f"[reset] could not clear {label}: {error}")


def clear_directory_contents(directory, patterns: tuple[str, ...], label: str) -> None:
    """Delete matching files inside one directory. Never recurses, never removes the dir."""
    if not directory.exists():
        print(f"[reset] {label} already clean")
        return
    removed = 0
    for pattern in patterns:
        for path in directory.glob(pattern):
            try:
                path.unlink()
                removed += 1
            except OSError as error:
                print(f"[reset] could not remove {path}: {error}")
    print(f"[reset] cleared {removed} file(s) from {label}: {directory}")


# --- the undo guard ----------------------------------------------------------


# Kinds whose "live" write is a file inside adjourn/ that this very script then
# deletes. A live recap_page is a local page in adjourn/recaps — the reset clears
# it two steps below, so blocking on it would refuse every clean run over a file
# reset is about to remove anyway. calendar_hold is deliberately NOT here: its
# live mode puts an event in Calendar.app, which is outside anything reset can
# reach, so an un-undone hold really does need a human.
LOCALLY_CLEANED_KINDS: frozenset[str] = frozenset({"recap_page"})


def unreversed_live_actions(path=None) -> list[dict]:
    """Live executions in the journal that nobody has undone. The reason to refuse.

    "Live" means it left this machine: a comment on a public issue, a ticket in
    someone's tracker, a message in a channel, an event in a calendar. A sim row
    is a rendered payload and has nothing to take back, so it never blocks a
    reset; neither does a local artifact this script is about to delete itself.

    One entry per dedup_key, newest first — an artifact written three times is
    one thing left behind, not three.
    """
    journal = path or config.executions_journal_path()
    if not journal.exists():
        return []
    undone = results.read_undone_dedup_keys(path=journal)
    newest: dict[str, dict] = {}
    for record in results.read_executions(path=journal):
        if record.get("record_type", results.RECORD_TYPE_EXECUTION) != results.RECORD_TYPE_EXECUTION:
            continue
        if not record.get("ok") or record.get("mode") != results.MODE_LIVE:
            continue
        if record.get("kind") in LOCALLY_CLEANED_KINDS:
            continue
        key = (record.get("dedup_key") or "").strip()
        if key and key in undone:
            continue
        newest[key or f"row-{len(newest)}"] = record
    return list(newest.values())


def describe_orphaned_action(record: dict) -> str:
    """One line naming what would be left behind, with the handle that reverses it."""
    kind = record.get("kind") or "unknown"
    summary = (record.get("human_summary") or "").strip() or kind
    key = (record.get("dedup_key") or "").strip() or "(no dedup key)"
    url = (record.get("url") or "").strip()
    line = f"  {kind:<20} {summary}"
    if url:
        line += f"\n{'':<24}{url}"
    line += f"\n{'':<24}undo with: POST /undo/{key}"
    return line


def archive_journal(path=None) -> bool:
    """Rename the journal to a timestamped sibling instead of deleting it.

    Reset used to unlink it. The journal is the ONLY place the undo payloads
    live — comment ids, branch refs, Slack message timestamps — so deleting it
    turned every live write of that rehearsal into something only a human with
    the GitHub UI could reverse. Archiving costs nothing and keeps undo possible
    for as long as the file is on disk.
    """
    journal = path or config.executions_journal_path()
    if not journal.exists():
        print("[reset] the executions journal is already clean")
        return False
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archived = journal.with_name(f"{journal.stem}.{stamp}{journal.suffix}")
    try:
        journal.rename(archived)
    except OSError as error:
        print(f"[reset] could not archive the journal: {error}")
        return False
    print(f"[reset] archived the executions journal -> {archived}")
    print("[reset] (undo handles survive in that file; nothing was destroyed)")
    return True


def report_live_actions(stranded: list[dict], *, forcing: bool) -> None:
    headline = (
        "[reset] --force: ORPHANING these live actions — nothing on the board can undo them "
        "after this point:"
        if forcing
        else "[reset] REFUSING: these actions went LIVE and were never undone:"
    )
    print(headline)
    for record in stranded:
        print(describe_orphaned_action(record))
    if not forcing:
        print(
            "\n[reset] Undo them from the board (each card's Undo button), or run again with "
            "--force.\n[reset] The journal holding their undo handles has NOT been touched."
        )


# --- memory ------------------------------------------------------------------


def forget_rehearsal_memory(meeting_ids: list[str]) -> None:
    """Drop the rehearsal's own traces from BOTH backends, keeping the seed intact.

    Three things have to go or the next run is not a clean run:

      * the demo meeting's statements, which would otherwise sit in the history
        the conflict judge reads and in the vocabulary the extractor is handed;
      * every recorded action, because memory dedups by dedup_key as well as the
        journal does. Clearing the journal alone leaves memory quietly suppressing
        the entire plan, which looks precisely like a planner that stopped working;
      * the ENTITY NODES those statements created. forget_meeting drops statements
        and their edges but not the Topic/Person/Ticket nodes hanging off them, and
        those topics are fed straight back into extraction as vocabulary — an audit
        measured a post-reset run starting with "21 known topic(s)" instead of 3,
        primed with junk from an unrelated rehearsal. The sweep below removes only
        nodes of those three labels that have no edges left.
    """
    from . import memory_store

    for backend in ("falkor", "sqlite"):
        try:
            memory = memory_store.open_memory(backend)
        except Exception as error:  # noqa: BLE001
            print(f"[reset] {backend}: unavailable ({error}) — skipped")
            continue
        try:
            for meeting_id in meeting_ids:
                removed = memory.forget_meeting(meeting_id)
                print(f"[reset] {backend}: forgot {removed} statement(s) from {meeting_id!r}")
            print(f"[reset] {backend}: forgot {memory.forget_actions()} recorded action(s)")
            swept = sweep_orphan_entities(memory, backend)
            if swept:
                print(f"[reset] {backend}: swept {swept} edgeless Topic/Person/Ticket node(s)")
            print(f"[reset] {backend}: topics still known -> {memory.known_topics()}")
        except Exception as error:  # noqa: BLE001
            print(f"[reset] {backend}: could not forget ({error})")
        finally:
            memory.close()


def sweep_orphan_entities(memory, backend: str) -> int:
    """Delete Topic/Person/Ticket nodes with no edges left. Returns how many went.

    Best effort and label-restricted by design: a memory backend that cannot
    answer the query reports zero rather than raising, and Issue is never in the
    sweep set (see ORPHAN_SWEEP_LABELS).
    """
    forget_orphans = getattr(memory, "forget_orphan_entities", None)
    if callable(forget_orphans):
        try:
            return int(forget_orphans(ORPHAN_SWEEP_LABELS) or 0)
        except Exception as error:  # noqa: BLE001
            print(f"[reset] {backend}: orphan sweep unavailable ({error})")
            return 0
    # Graph backends only. SQLite derives its topic and person lists from the
    # statements table (memory_store.py:890-907), so removing the statements
    # already removes the vocabulary — there is nothing left dangling to sweep.
    read = getattr(memory, "_read", None)
    write = getattr(memory, "_write", None)
    if not callable(read) or not callable(write):
        return 0
    condition = " OR ".join(f"n:{label}" for label in ORPHAN_SWEEP_LABELS)
    try:
        rows = read(f"MATCH (n) WHERE ({condition}) AND NOT (n)--() RETURN count(n)")
        orphans = int(rows[0][0]) if rows else 0
        if orphans:
            write(f"MATCH (n) WHERE ({condition}) AND NOT (n)--() DELETE n")
    except Exception as error:  # noqa: BLE001
        print(f"[reset] {backend}: orphan sweep skipped ({error})")
        return 0
    return orphans


# --- entry point -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adjourn.reset_demo")
    parser.add_argument("--reseed", action="store_true",
                        help="wipe and re-seed memory instead (costs one model call)")
    parser.add_argument("--meeting", action="append", default=None,
                        metavar="MEETING_ID",
                        help="a meeting to forget from memory (repeatable; "
                             "defaults to the demo meetings)")
    parser.add_argument("--force", action="store_true",
                        help="archive the journal even while live actions are un-undone "
                             "(prints exactly what it is orphaning)")
    parser.add_argument("--check", action="store_true",
                        help="report what a reset would refuse over, and change nothing")
    arguments = parser.parse_args(argv)
    meetings_to_forget = arguments.meeting or list(DEFAULT_REHEARSAL_MEETINGS)

    stranded = unreversed_live_actions()

    if arguments.check:
        if stranded:
            report_live_actions(stranded, forcing=False)
            print(f"\n[reset] --check: {len(stranded)} live action(s) would block a reset.")
            return 1
        print("[reset] --check: no un-undone live actions — a reset would run clean.")
        return 0

    if stranded and not arguments.force:
        report_live_actions(stranded, forcing=False)
        return 2
    if stranded:
        report_live_actions(stranded, forcing=True)

    config.ensure_state_directories()
    archive_journal()
    clear_file(config.pending_actions_path(), "pending countdowns")
    clear_file(config.pipeline_status_path(), "pipeline status")
    clear_file(config.state_directory() / "watcher.json", "watcher state")
    # A launch record from a rehearsal describes a launch that is no longer
    # happening. The board renders it, so a stale one is a status pill telling
    # the room about an engine start from forty minutes ago.
    clear_file(config.state_directory() / "engine_launch.json", "engine launch status")
    clear_directory_contents(config.recaps_directory(), ("*.html", "*.bak"), "recaps")
    clear_directory_contents(config.state_directory() / "holds", ("*.ics",), "calendar holds")
    clear_directory_contents(
        config.state_directory() / "recap_actions", ("*.json", "*.json.tmp"), "parked recap actions"
    )

    if arguments.reseed:
        from . import seed_memory

        print("\n[reset] re-seeding memory")
        code = seed_memory.main(["--wipe"])
        # The re-seed runs the REAL extract -> plan -> execute path to derive
        # last week's receipts, and that path reports itself to the pipeline
        # status file the same way a live meeting does. Clearing before the
        # re-seed is therefore not enough: the seeder refills it on its way
        # out, and the cold open then renders EXTRACTION "running · batch 2/2"
        # with a stalled bar and the seeder's own statements in the Thinking
        # feed, for a pass that ended before the room walked in. Clear it
        # again on the way out, so the first thing a stranger sees is a rail
        # that is honestly idle.
        print()
        clear_file(config.pipeline_status_path(), "pipeline status (post-reseed)")
        clear_file(config.state_directory() / "watcher.json", "watcher state (post-reseed)")
        return code

    forget_rehearsal_memory(meetings_to_forget)
    print("\n[reset] memory kept (the prior standup survived) — pass --reseed to rebuild it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
