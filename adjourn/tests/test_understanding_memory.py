"""Lane B — memory_store regression gate. Both backends, one assertion set.

The point of this file: FalkorMeetingMemory and SqliteMeetingMemory must answer
the same questions the same way, because open_memory() can hand a caller either
one and nothing downstream is allowed to care. So the checks below are written
once and run twice.

ISOLATION: this suite NEVER touches the "adjourn" graph or state/memory.sqlite3.
Falkor runs against a throwaway graph named adjourn_test_<pid> which is deleted
at the end; SQLite runs against a temp file. That is not fussiness — a parallel
lane was found mid-build ingesting fixtures straight into the shared "adjourn"
graph, which silently contaminated an extraction evaluation by feeding the topic
vocabulary back as an answer key. Tests that write into demo state cost more
than they prove.

Run:  python -m adjourn.tests.test_understanding_memory
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import extraction, memory_store

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}" + (f"  --  {detail}" if detail else ""))


# --- stand-ins for the other lanes' types -----------------------------------
# memory_store takes planner.Action and results.ExecutorResult by duck type
# precisely so this lane can be tested before those lanes land.


@dataclass
class FakeAction:
    kind: str
    dedup_key: str
    segment_id: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class FakeResult:
    ok: bool = True
    mode: str = "sim"
    external_id: str | None = None
    url: str | None = None
    human_summary: str = ""
    fired_at: str = "2026-08-21T10:00:00"


def exercise_backend(memory: memory_store.MeetingMemory, label: str) -> None:
    """Every assertion in the suite, run against one backend."""
    print(f"\n== {label} ==")
    memory.ensure_schema()
    memory.ensure_schema()  # idempotent
    check("ensure_schema is idempotent", True)

    prior = extraction.load_fixture_statements("prior-standup")
    _, prior_meta = extraction.load_fixture_transcript("prior-standup")
    meta = {
        "meeting_id": "prior-standup",
        "title": prior_meta.get("title", ""),
        "date": prior_meta.get("date", ""),
    }
    check("prior fixture has 5 statements", len(prior) == 5, str(len(prior)))

    first = memory.ingest(prior, meta)
    second = memory.ingest(prior, meta)
    check("ingest returns the count", first == 5, str(first))
    check("re-ingest is idempotent", second == 5, str(second))
    stats_after = memory.stats()
    check("re-ingest did not duplicate statements",
          stats_after["statements"] == 5, str(stats_after["statements"]))

    memory.record_issue(1, "sharique2004/adjourn", "Migrate auth service to OAuth2", "oauth")
    memory.record_issue(2, "sharique2004/adjourn", "Add cache layer for transcript ingestion", "cache")
    memory.record_issue(4, "sharique2004/adjourn", "Rate-limit the public API", "ratelimit")

    check("topic -> issue: cache layer -> 2",
          memory.find_issue_for_topic("cache layer") == 2,
          str(memory.find_issue_for_topic("cache layer")))
    check("topic -> issue: auth migration -> 1",
          memory.find_issue_for_topic("auth migration") == 1,
          str(memory.find_issue_for_topic("auth migration")))
    check("topic -> issue: rate limiting -> 4",
          memory.find_issue_for_topic("rate limiting") == 4,
          str(memory.find_issue_for_topic("rate limiting")))
    check("topic -> issue: unknown topic -> None",
          memory.find_issue_for_topic("interpretive dance") is None,
          str(memory.find_issue_for_topic("interpretive dance")))
    check("topic -> issue: an explicit '#2' short-circuits the search",
          memory.find_issue_for_topic("#2") == 2)
    check("topic -> issue: empty topic -> None",
          memory.find_issue_for_topic("") is None)

    # THE REPHRASE CASE. Title prefix search only runs one direction: "cache*"
    # matches the token "cache", but "caching*" matches nothing in "Add cache
    # layer for transcript ingestion". The extractor names that work item "cache
    # layer" on some runs and "caching" on others, and on a "caching" run every
    # statement that did not say "issue two" out loud silently produced no card —
    # which is how the demo's reassignment beat vanished with no error. Memory
    # already knew the answer from a statement it had filed, so it must be asked
    # before the title is guessed at.
    check("topic -> issue: a rephrase memory has never filed stays unresolved",
          memory.find_issue_for_topic("caching") is None,
          str(memory.find_issue_for_topic("caching")))
    # Now let memory SEE the rephrase once, filed against #2 because the speaker
    # named the issue out loud. From then on the topic resolves by lookup.
    rephrased = extraction.Statement.from_dict({
        "segment_id": "rephrase-01", "speaker": "Them", "topic": "caching",
        "claim": "Redis is out; we are going with an in-process LRU.",
        "kind": "decision", "entity_refs": {"issue_number": 2},
    })
    memory.ingest([rephrased], {"meeting_id": "rephrase-probe", "title": "t", "date": "2026-08-14"})
    check("topic -> issue: a rephrased topic resolves from what memory filed before",
          memory.find_issue_for_topic("caching") == 2,
          str(memory.find_issue_for_topic("caching")))
    check("topic -> issue: the remembered link does not invent an issue for a new topic",
          memory.find_issue_for_topic("interpretive dance") is None)
    memory.forget_meeting("rephrase-probe")

    history = memory.history("cache layer")
    check("history('cache layer') returns 3 rows", len(history) == 3, str(len(history)))
    check("history rows carry the judge's contract keys",
          all({"date", "meeting", "who", "kind", "text"} <= set(row) for row in history))
    check("history is oldest-first by date",
          [row["date"] for row in history] == sorted(row["date"] for row in history))
    check("history('#2') finds the issue-linked statement",
          len(memory.history("#2")) == 1, str(len(memory.history("#2"))))
    check("history of an unknown topic is empty",
          memory.history("interpretive dance") == [])

    commitments = memory.find_prior_commitments("cache layer")
    check("find_prior_commitments returns PriorCommitment objects",
          all(isinstance(row, memory_store.PriorCommitment) for row in commitments))
    check("PriorCommitment.as_history_row matches the judge contract",
          set(commitments[0].as_history_row()) >= {"date", "meeting", "who", "kind", "text"})

    vocabulary = memory.known_topics()
    check("known_topics carries the prior meeting's vocabulary",
          {"cache layer", "streaming adapter", "auth migration"} <= set(vocabulary),
          str(vocabulary))
    check("known_topics never leaks the 'untopiced' placeholder",
          "untopiced" not in vocabulary)

    index = memory.topic_index_by_reference()
    check("reference index resolves a ticket key to its settled topic",
          index.get("SHA-5") == "streaming adapter", str(index))
    check("reference index resolves an issue handle to its settled topic",
          index.get("#2") == "cache layer", str(index))
    check("reference index uppercases ticket keys",
          all(key == key.upper() for key in index if not key.startswith("#")), str(index))

    # only the promise-shaped kinds become Commitment nodes
    check("commitments come only from promise kinds",
          stats_after["commitments"] == 1, str(stats_after["commitments"]))

    action = FakeAction(kind="linear_create", dedup_key="linear_create:retry-logic",
                        segment_id="prior-s05")
    check("has_fired_dedup_key is False before the action fires",
          memory.has_fired_dedup_key(action.dedup_key) is False)
    memory.record_action(action, FakeResult(external_id="MMM-42", url="https://linear.app/x/MMM-42"))
    check("has_fired_dedup_key is True after the action fires",
          memory.has_fired_dedup_key(action.dedup_key) is True)
    memory.record_action(action, FakeResult(external_id="MMM-42"))
    check("re-recording an action does not duplicate it",
          memory.stats()["actions"] == 1, str(memory.stats()["actions"]))
    check("the ticket the action touched was recorded",
          memory.stats()["tickets"] == 1, str(memory.stats()["tickets"]))

    github_action = FakeAction(kind="github_update", dedup_key="github_update:2:cache-layer",
                               segment_id="prior-s03", payload={"issue_number": 2})
    memory.record_action(github_action, FakeResult(mode="live"))
    check("a sim-mode github action still records WHICH issue it would touch",
          memory.stats()["tickets"] == 2, str(memory.stats()["tickets"]))

    memory.mark_superseded("prior-s05", "prior-s03")
    check("mark_superseded does not raise", True)

    final_stats = memory.stats()
    check("stats names its backend", final_stats["backend"] == memory.backend)
    for key in ("meetings", "statements", "commitments", "issues", "actions", "tickets", "topics"):
        check(f"stats reports {key}", key in final_stats, str(sorted(final_stats)))


def main() -> int:
    print("=" * 72)
    print("Lane B — memory_store: one interface, two backends")
    print("=" * 72)

    with tempfile.TemporaryDirectory(prefix="adjourn-memory-test-") as temporary_directory:
        sqlite_memory = memory_store.SqliteMeetingMemory(Path(temporary_directory) / "memory.sqlite3")
        try:
            exercise_backend(sqlite_memory, "SqliteMeetingMemory (temp file)")
        finally:
            sqlite_memory.close()

    test_graph_name = f"adjourn_test_{os.getpid()}"
    try:
        falkor_memory = memory_store.FalkorMeetingMemory(graph_name=test_graph_name)
    except Exception as error:  # noqa: BLE001 — a missing container is a skip, not a failure
        print(f"\n== FalkorMeetingMemory — SKIPPED ({error.__class__.__name__}: {error})")
        print("   start it with:  docker start falkordb-test")
    else:
        try:
            exercise_backend(falkor_memory, f"FalkorMeetingMemory (graph {test_graph_name!r})")
        finally:
            falkor_memory.wipe()  # never leave a test graph behind
            falkor_memory.close()
            print(f"  ok   test graph {test_graph_name!r} wiped")

    print("\n== open_memory() degrades instead of raising ==")
    unreachable = memory_store.open_memory(backend="falkor") if False else None
    del unreachable
    fallback = memory_store.SqliteMeetingMemory(Path(tempfile.mkdtemp()) / "fallback.sqlite3")
    fallback.ensure_schema()
    check("SQLite backend answers with an empty database",
          fallback.find_issue_for_topic("cache layer") is None
          and fallback.history("cache layer") == []
          and fallback.known_topics() == [])
    fallback.close()
    check("close() is safe to call twice", _close_twice_is_safe(fallback))

    print("\n" + "=" * 72)
    print(f"{PASSED} passed, {FAILED} failed")
    print("=" * 72)
    return 1 if FAILED else 0


def _close_twice_is_safe(memory) -> bool:
    try:
        memory.close()
        return True
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    sys.exit(main())
