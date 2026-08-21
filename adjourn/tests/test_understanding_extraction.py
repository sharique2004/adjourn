"""Lane B — extraction + prompts regression gate, with a real scoring mode.

Two halves:

    (default)   offline checks. Deterministic, no network, no model, ~instant.
                These are the ones that run in a pre-demo sanity sweep.

    --live      actually calls the Claude CLI on both demo fixtures and scores
                the result against the hand-written ground truth. Takes a few
                minutes and costs real inference. This is the only honest way to
                know whether the prompt still works, so run it after any edit to
                prompts.py.

SCORING IS ISOLATED ON PURPOSE. The vocabulary fed to the extractor comes from a
throwaway SQLite memory seeded with the PRIOR meeting only — never from the
shared "adjourn" graph. During this build a parallel lane was found ingesting
the living-room-standup ground truth into that graph, which meant known_topics()
handed the extractor the exact topic answer key and the score came back a
meaningless 100%. An evaluation that reads shared mutable state is not an
evaluation.

Run:  python -m adjourn.tests.test_understanding_extraction
      python -m adjourn.tests.test_understanding_extraction --live
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from .. import extraction, memory_store, prompts

PASSED = 0
FAILED = 0

DEMO_MEETING = "living-room-standup"
PRIOR_MEETING = "prior-standup"


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}" + (f"  --  {detail}" if detail else ""))


# --- offline ----------------------------------------------------------------


def test_schema_and_vocabulary() -> None:
    print("\n== schema and vocabulary ==")
    check("10 statement kinds", len(extraction.STATEMENT_KINDS) == 10)
    guide_kinds = tuple(kind for kind, _ in prompts.KIND_GUIDE)
    check("prompts.KIND_GUIDE covers exactly STATEMENT_KINDS",
          set(guide_kinds) == set(extraction.STATEMENT_KINDS),
          str(set(guide_kinds) ^ set(extraction.STATEMENT_KINDS)))
    check("KIND_GUIDE has no duplicate kinds", len(guide_kinds) == len(set(guide_kinds)))

    prompt = prompts.build_extraction_prompt(
        [{"segment_id": "s1", "speaker": "You", "text": "hello"}], "Test Meeting")
    for kind in extraction.STATEMENT_KINDS:
        check(f"prompt names the kind {kind!r}", f'"{kind}"' in prompt)
    check("prompt carries the valid-id allowlist", "s1" in prompt)
    check("prompt names the meeting", "Test Meeting" in prompt)
    check("prompt demands JSON only", "JSON object only" in prompt)

    with_vocabulary = prompts.build_extraction_prompt(
        [{"segment_id": "s1", "speaker": "You", "text": "hello"}], "Test",
        ["cache layer", "rate limiting"])
    check("known_topics are fed back into the prompt",
          "cache layer" in with_vocabulary and "rate limiting" in with_vocabulary)

    repair = prompts.build_repair_prompt(prompt, "kind: unexpected value 'vibes'")
    check("repair prompt keeps the original", prompt[:200] in repair)
    check("repair prompt carries the parser's complaint", "vibes" in repair)


def test_json_rescue() -> None:
    print("\n== JSON rescue (models do not always behave) ==")
    check("plain object", extraction.extract_json_object('{"statements": []}') == {"statements": []})
    check("markdown fence",
          extraction.extract_json_object('```json\n{"a": 1}\n```') == {"a": 1})
    check("prose before and after",
          extraction.extract_json_object('Sure! {"a": 1} Hope that helps.') == {"a": 1})
    check("a brace inside a string cannot fool the depth counter",
          extraction.extract_json_object('{"claim": "use a dict {like this}", "n": 2}')
          == {"claim": "use a dict {like this}", "n": 2})
    check("an escaped quote inside a string is survivable",
          extraction.extract_json_object(r'{"claim": "he said \"ship it\"", "n": 3}')["n"] == 3)
    for bad in ("", "no json here", "{unbalanced"):
        try:
            extraction.extract_json_object(bad)
            check(f"rejects {bad!r}", False, "did not raise")
        except ValueError:
            check(f"rejects {bad!r}", True)


def test_deterministic_post_processing() -> None:
    print("\n== deterministic post-processing (runs on every engine's output) ==")

    def make(segment_id: str, kind: str = "update") -> extraction.Statement:
        return extraction.Statement(segment_id=segment_id, speaker="You",
                                    topic="cache layer", claim="c", kind=kind)

    suffixed = extraction.suffix_multiple_claims([make("s1"), make("s1"), make("s2"), make("s1")])
    check("multi-claim suffixing numbers repeats",
          [s.segment_id for s in suffixed] == ["s1", "s1.2", "s2", "s1.3"],
          str([s.segment_id for s in suffixed]))
    check("suffixing is stable when re-applied",
          [s.segment_id for s in extraction.suffix_multiple_claims(suffixed)]
          == ["s1", "s1.2", "s2", "s1.3"])
    check("base_segment_id strips a suffix", extraction.base_segment_id("s1.3") == "s1")
    check("base_segment_id leaves a bare id alone", extraction.base_segment_id("s1") == "s1")

    kept = extraction.drop_hallucinated_segment_ids(
        [make("s1"), make("s99"), make("s2")], {"s1", "s2"})
    check("hallucinated segment ids are dropped",
          [s.segment_id for s in kept] == ["s1", "s2"], str([s.segment_id for s in kept]))
    kept_suffixed = extraction.drop_hallucinated_segment_ids([make("s1.2")], {"s1"})
    check("a suffixed id is compared by its base", len(kept_suffixed) == 1)
    check("everything is dropped when nothing is valid",
          extraction.drop_hallucinated_segment_ids([make("s1")], set()) == [])

    def with_reference(topic: str, **refs) -> extraction.Statement:
        return extraction.Statement(
            segment_id="s1", speaker="You", topic=topic, claim="c", kind="progress_report",
            entity_refs=extraction.EntityReferences(**refs))

    index = {"MMM-7": "rate limiting", "#2": "cache layer"}
    snapped = extraction.snap_topics_to_known_references(
        [with_reference("gateway work", linear_identifier="MMM-7")], index)
    check("a ticket handle overrides the model's topic",
          snapped[0].topic == "rate limiting", snapped[0].topic)
    snapped = extraction.snap_topics_to_known_references(
        [with_reference("caching thing", issue_number=2)], index)
    check("an issue handle overrides the model's topic",
          snapped[0].topic == "cache layer", snapped[0].topic)
    snapped = extraction.snap_topics_to_known_references(
        [with_reference("mmm-7", linear_identifier="mmm-7")], index)
    check("handle matching is case-insensitive", snapped[0].topic == "rate limiting")
    snapped = extraction.snap_topics_to_known_references(
        [with_reference("brand new work")], index)
    check("a statement with no handle keeps the model's topic",
          snapped[0].topic == "brand new work")
    snapped = extraction.snap_topics_to_known_references(
        [with_reference("unseen", linear_identifier="ZZZ-9")], index)
    check("an unknown handle changes nothing", snapped[0].topic == "unseen")
    check("an empty index is a no-op",
          extraction.snap_topics_to_known_references(
              [with_reference("whatever", linear_identifier="MMM-7")], {})[0].topic == "whatever")

    segments = [{"segment_id": "s1", "speaker": "Them", "text": "the real words"}]
    statements = [extraction.Statement(segment_id="s1", speaker="WRONG", topic="t",
                                       claim="paraphrase", kind="update")]
    extraction.attach_segment_context(statements, segments, extraction.SOURCE_FINAL)
    check("quote comes from the transcript, never the model",
          statements[0].quote == "the real words")
    check("speaker is corrected from the transcript", statements[0].speaker == "Them")
    check("source is stamped", statements[0].source == extraction.SOURCE_FINAL)


def test_fixture_floor() -> None:
    print("\n== the fixtures floor ==")
    for meeting in (PRIOR_MEETING, DEMO_MEETING):
        segments, meta = extraction.load_fixture_transcript(meeting)
        statements = extraction.load_fixture_statements(meeting)
        check(f"{meeting}: transcript loads", len(segments) > 0, str(len(segments)))
        check(f"{meeting}: meta carries id/title/date",
              {"meeting_id", "title", "date"} <= set(meta), str(sorted(meta)))
        check(f"{meeting}: ground truth loads", len(statements) > 0, str(len(statements)))
        segment_ids = {s["segment_id"] for s in segments}
        dangling = [s.segment_id for s in statements if s.segment_id not in segment_ids]
        check(f"{meeting}: every ground-truth id exists in the transcript",
              not dangling, str(dangling))
        quotes = {s["segment_id"]: s["text"] for s in segments}
        mismatched = [s.segment_id for s in statements if quotes[s.segment_id] != s.quote]
        check(f"{meeting}: every ground-truth quote is verbatim", not mismatched, str(mismatched))
        bad_kinds = [s.kind for s in statements if s.kind not in extraction.STATEMENT_KINDS]
        check(f"{meeting}: every kind is in the vocabulary", not bad_kinds, str(bad_kinds))
        check(f"{meeting}: everything is badged as fixtures",
              all(s.engine == extraction.ENGINE_FIXTURES for s in statements))

    demo = extraction.load_fixture_statements(DEMO_MEETING)
    covered = {s.kind for s in demo}
    missing = set(extraction.STATEMENT_KINDS) - covered
    check("the demo meeting exercises ALL TEN statement kinds", not missing, str(sorted(missing)))

    # The last-resort floor is OPT-IN. A real meeting that extracts to nothing must
    # produce an empty board, not the demo meeting's promises under its name.
    check("an unknown meeting does NOT silently borrow the demo fixture",
          extraction.load_fixture_statements("no-such-meeting") == [])
    _floor_was = os.environ.get("ADJOURN_FIXTURE_FLOOR")
    os.environ["ADJOURN_FIXTURE_FLOOR"] = "1"
    try:
        check("ADJOURN_FIXTURE_FLOOR=1 turns the demo floor back on",
              len(extraction.load_fixture_statements("no-such-meeting")) > 0)
    finally:
        if _floor_was is None:
            os.environ.pop("ADJOURN_FIXTURE_FLOOR", None)
        else:
            os.environ["ADJOURN_FIXTURE_FLOOR"] = _floor_was
    check("load_fixture_statements never raises on a missing file",
          isinstance(extraction.load_fixture_statements("/nonexistent/path.json"), list))
    check("load_fixture_transcript never raises on a missing file",
          extraction.load_fixture_transcript("/nonexistent/path.jsonl") == ([], {}))


def test_engine_ladder() -> None:
    print("\n== the engine ladder ==")
    order = extraction.resolve_engine_order()
    check("fixtures is always the last resort", order[-1] == extraction.ENGINE_FIXTURES)
    check("the ladder never repeats an engine", len(order) == len(set(order)))
    check("asking for fixtures skips the models",
          extraction.resolve_engine_order("fixtures") == [extraction.ENGINE_FIXTURES])
    if extraction.is_claude_available():
        check("claude leads when it is installed and configured",
              extraction.resolve_engine_order("claude")[0] == extraction.ENGINE_CLAUDE)
    check("no segments and no fixture engine returns empty, without a model call",
          extraction.extract_statements([], "Empty Meeting") == [])
    check("whitespace-only segments also skip the model",
          extraction.extract_statements(
              [{"segment_id": "live-1", "text": "   "}, {"segment_id": "live-2", "text": ""}],
              "Silent Meeting",
          ) == [])
    fixture_run = extraction.extract_statements(
        [], "Demo", meeting_id=DEMO_MEETING, engine=extraction.ENGINE_FIXTURES)
    # TWELVE since the PR-review beat was merged into the demo meeting, so one
    # bare `--replay` shows every action kind including the inline suggestion.
    check("the fixtures engine works with no segments at all", len(fixture_run) == 12,
          str(len(fixture_run)))


def test_conflict_heuristic() -> None:
    print("\n== the conflict judge's deterministic floor ==")
    history = [{"date": "2026-08-14", "meeting": "MMM Standup", "who": "Them",
                "kind": "decision", "text": "Use Redis for the ingestion cache."}]
    changed = extraction.judge_conflict_with_heuristic(
        history, {"date": "2026-08-21", "meeting": "living room", "who": "Them",
                  "kind": "decision",
                  "text": "We're dropping Redis and going with an in-process LRU instead."})
    check("a reversal is a conflict", changed.conflict is True)
    check("a conflict names what changed", bool(changed.what_changed))
    check("a conflict carries before and after", bool(changed.before) and bool(changed.after))

    consistent = extraction.judge_conflict_with_heuristic(
        history, {"date": "2026-08-21", "meeting": "living room", "who": "Them",
                  "kind": "update", "text": "The Redis benchmark looks good, still on track."})
    check("consistent progress is not a conflict", consistent.conflict is False)

    same_day = extraction.judge_conflict_with_heuristic(
        history, {"date": "2026-08-14", "meeting": "MMM Standup", "who": "Them",
                  "kind": "decision", "text": "Actually we're dropping Redis instead."})
    check("refining a decision inside the SAME meeting is not drift",
          same_day.conflict is False)

    check("no history means no conflict",
          extraction.judge_conflict([], {"text": "anything"}).conflict is False)
    check("judge_conflict returns a verdict badged with its engine",
          extraction.judge_conflict_with_heuristic(history, {"text": "x"}).engine == "heuristic")


# --- live scoring -----------------------------------------------------------


def score_against_ground_truth(produced: list, truth: list) -> dict:
    """Segment-level precision/recall plus field agreement on the overlap."""
    produced_by_id: dict[str, object] = {}
    for statement in produced:
        produced_by_id.setdefault(extraction.base_segment_id(statement.segment_id), statement)
    truth_by_id = {extraction.base_segment_id(s.segment_id): s for s in truth}

    produced_ids = set(produced_by_id)
    truth_ids = set(truth_by_id)
    matched = sorted(produced_ids & truth_ids)

    kinds_right = sum(1 for i in matched if produced_by_id[i].kind == truth_by_id[i].kind)
    topics_right = sum(1 for i in matched if produced_by_id[i].topic == truth_by_id[i].topic)
    refs_right = sum(
        1 for i in matched
        if produced_by_id[i].entity_refs.to_dict() == truth_by_id[i].entity_refs.to_dict()
    )
    return {
        "produced": len(produced_ids),
        "expected": len(truth_ids),
        "matched": len(matched),
        "precision": len(matched) / len(produced_ids) if produced_ids else 0.0,
        "recall": len(matched) / len(truth_ids) if truth_ids else 0.0,
        "kind_accuracy": kinds_right / len(matched) if matched else 0.0,
        "topic_accuracy": topics_right / len(matched) if matched else 0.0,
        "entity_ref_accuracy": refs_right / len(matched) if matched else 0.0,
        "false_positives": sorted(produced_ids - truth_ids),
        "missed": sorted(truth_ids - produced_ids),
        "kind_errors": [
            f"{i}: got {produced_by_id[i].kind}, expected {truth_by_id[i].kind}"
            for i in matched if produced_by_id[i].kind != truth_by_id[i].kind
        ],
        "topic_errors": [
            f"{i}: got {produced_by_id[i].topic!r}, expected {truth_by_id[i].topic!r}"
            for i in matched if produced_by_id[i].topic != truth_by_id[i].topic
        ],
        "entity_ref_errors": [
            f"{i}: got {produced_by_id[i].entity_refs.to_dict()}, "
            f"expected {truth_by_id[i].entity_refs.to_dict()}"
            for i in matched
            if produced_by_id[i].entity_refs.to_dict() != truth_by_id[i].entity_refs.to_dict()
        ],
    }


def build_isolated_memory_context() -> tuple[list[str], dict[str, str]]:
    """(vocabulary, reference index) from the PRIOR meeting only, in a temp database.

    This is the honest demo condition: last week's meeting is in memory, tonight's
    is not. Reading the shared graph instead would leak this meeting's own topics
    back into the prompt.
    """
    directory = Path(tempfile.mkdtemp(prefix="adjourn-eval-"))
    memory = memory_store.SqliteMeetingMemory(directory / "eval.sqlite3")
    memory.ensure_schema()
    _, meta = extraction.load_fixture_transcript(PRIOR_MEETING)
    memory.ingest(extraction.load_fixture_statements(PRIOR_MEETING), {
        "meeting_id": PRIOR_MEETING,
        "title": meta.get("title", ""),
        "date": meta.get("date", ""),
    })
    context = (memory.known_topics(), memory.topic_index_by_reference())
    memory.close()
    return context


def run_live_scoring() -> None:
    print("\n" + "=" * 72)
    print("LIVE — real Claude CLI extraction, scored against hand-written truth")
    print("=" * 72)
    if not extraction.is_claude_available():
        print("  SKIPPED — no claude binary found")
        return

    warm_vocabulary, warm_references = build_isolated_memory_context()
    for meeting, vocabulary, references, label in (
        (PRIOR_MEETING, [], {}, "cold (first meeting, nothing in memory)"),
        (DEMO_MEETING, warm_vocabulary, warm_references,
         "warm (prior meeting only, as on demo night)"),
    ):
        print(f"\n-- {meeting} — {label}")
        if vocabulary:
            print(f"   vocabulary: {vocabulary}")
            print(f"   reference index: {references}")
        segments, meta = extraction.load_fixture_transcript(meeting)
        produced = extraction.extract_statements(
            segments, meta["title"], source=extraction.SOURCE_FINAL,
            meeting_id=meeting, engine=extraction.ENGINE_CLAUDE,
            known_topics=vocabulary or None, reference_topics=references or None,
        )
        check(f"{meeting}: the claude engine actually ran",
              all(s.engine == extraction.ENGINE_CLAUDE for s in produced),
              str({s.engine for s in produced}))
        truth = extraction.load_fixture_statements(meeting)
        report = score_against_ground_truth(produced, truth)
        print(f"   produced {report['produced']} / expected {report['expected']}")
        print(f"   precision            {report['precision']:.0%}")
        print(f"   recall               {report['recall']:.0%}")
        print(f"   kind accuracy        {report['kind_accuracy']:.0%}")
        print(f"   topic accuracy       {report['topic_accuracy']:.0%}")
        print(f"   entity_ref accuracy  {report['entity_ref_accuracy']:.0%}")
        for field_name in ("false_positives", "missed", "kind_errors", "topic_errors",
                           "entity_ref_errors"):
            if report[field_name]:
                print(f"   {field_name}: {report[field_name]}")

        check(f"{meeting}: recall >= 90%", report["recall"] >= 0.9, f"{report['recall']:.0%}")
        check(f"{meeting}: precision >= 80%", report["precision"] >= 0.8, f"{report['precision']:.0%}")
        check(f"{meeting}: kind accuracy >= 90%",
              report["kind_accuracy"] >= 0.9, f"{report['kind_accuracy']:.0%}")
        check(f"{meeting}: no segment_id was hallucinated",
              all(extraction.base_segment_id(s.segment_id)
                  in {seg["segment_id"] for seg in segments} for s in produced))

        if meeting == DEMO_MEETING:
            fired_kinds = {s.kind for s in produced}
            for required in ("decision", "ticket_request", "progress_report", "pr_intent",
                             "message_commitment", "email_commitment", "deadline"):
                check(f"demo beat survives live extraction: {required}",
                      required in fired_kinds, str(sorted(fired_kinds)))

    print("\n-- conflict judge, live")
    directory = Path(tempfile.mkdtemp(prefix="adjourn-judge-"))
    memory = memory_store.SqliteMeetingMemory(directory / "judge.sqlite3")
    memory.ensure_schema()
    _, prior_meta = extraction.load_fixture_transcript(PRIOR_MEETING)
    memory.ingest(extraction.load_fixture_statements(PRIOR_MEETING), {
        "meeting_id": PRIOR_MEETING, "title": prior_meta["title"], "date": prior_meta["date"]})
    history = memory.history("cache layer")
    memory.close()

    demo_truth = {s.segment_id: s for s in extraction.load_fixture_statements(DEMO_MEETING)}
    reversal = demo_truth["lr-s05"]
    verdict = extraction.judge_conflict(history, {
        "date": "2026-08-21", "meeting": "MMM Standup — living room",
        "who": reversal.speaker, "kind": reversal.kind, "text": reversal.claim,
    }, topic="cache layer")
    print(f"   verdict: conflict={verdict.conflict} engine={verdict.engine}")
    print(f"   what_changed: {verdict.what_changed}")
    check("the Redis -> LRU reversal is judged a CONFLICT", verdict.conflict is True)
    check("the conflict says what changed", bool(verdict.what_changed))
    check("the conflict carries before and after", bool(verdict.before) and bool(verdict.after))

    steady = demo_truth["lr-s21"]
    steady_verdict = extraction.judge_conflict(memory_history_for_auth(), {
        "date": "2026-08-21", "meeting": "MMM Standup — living room",
        "who": steady.speaker, "kind": steady.kind, "text": steady.claim,
    }, topic="auth migration")
    print(f"   steady-state verdict: conflict={steady_verdict.conflict}")
    check("steady progress on the auth migration is NOT a conflict",
          steady_verdict.conflict is False, steady_verdict.what_changed)


def memory_history_for_auth() -> list[dict]:
    """The prior meeting's auth-migration history, from a throwaway database."""
    directory = Path(tempfile.mkdtemp(prefix="adjourn-judge2-"))
    memory = memory_store.SqliteMeetingMemory(directory / "judge.sqlite3")
    memory.ensure_schema()
    _, meta = extraction.load_fixture_transcript(PRIOR_MEETING)
    memory.ingest(extraction.load_fixture_statements(PRIOR_MEETING), {
        "meeting_id": PRIOR_MEETING, "title": meta["title"], "date": meta["date"]})
    history = memory.history("auth migration")
    memory.close()
    return history


def main() -> int:
    print("=" * 72)
    print("Lane B — extraction and prompts")
    print("=" * 72)
    test_schema_and_vocabulary()
    test_json_rescue()
    test_deterministic_post_processing()
    test_fixture_floor()
    test_engine_ladder()
    test_conflict_heuristic()
    if "--live" in sys.argv:
        run_live_scoring()
    else:
        print("\n(offline only — pass --live to call the real Claude CLI and score it)")
    print("\n" + "=" * 72)
    print(f"{PASSED} passed, {FAILED} failed")
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
