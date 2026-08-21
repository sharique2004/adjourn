"""Lane 1 — the decision pipeline: restraint, work claims, deadlines, PR review.

Every check here is OFFLINE and DETERMINISTIC. No model, no network, no journal,
no state directory. The point of this file is that the four gates the product
depends on can be verified in under a second, by anybody, on a plane.

WHERE THE CASES COME FROM. Almost every string below is a verbatim line from an
audit transcript, not something invented to pass. The four that are labelled
FP1..FP4 are the four false positives an auditor drove into live services — a
prohibition published as a decision, an observation the room twice refused to
record, and two of somebody else's promises that opened a real ticket and a real
draft PR. They are the regression suite; if one of them ever fires again, the
product's central claim is false and this file says so out loud.

    python -m adjourn.tests.test_decision_pipeline
"""

from __future__ import annotations

import sys
from datetime import datetime

from .. import extraction, planner
from ..executors.calendar_hold_executor import _weekday_offset, resolve_deadline
from ..executors.linear_move_executor import (
    STATE_IN_PROGRESS,
    STATE_IN_REVIEW,
    choose_target_state,
    is_backward_move,
)

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


def statement(**overrides) -> extraction.Statement:
    """A Statement with sane defaults; overrides win. entity_refs takes a dict."""
    refs = overrides.pop("entity_refs", {})
    base = {
        "segment_id": "s1", "speaker": "Sharique", "topic": "cache layer",
        "claim": "a claim", "kind": "update", "quote": "",
    }
    base.update(overrides)
    return extraction.Statement(entity_refs=extraction.EntityReferences.from_dict(refs), **base)


def plan_one(statement_object, **meeting) -> list:
    """Plan ONE statement with no memory, and drop the always-on recap."""
    meeting = {"meeting_id": "t", "title": "T", "date": "2026-08-21", **meeting}
    return [
        action for action in planner.plan([statement_object], None, meeting=meeting)
        if action.kind != "recap_page"
    ]


# --- 1. the restraint gate --------------------------------------------------

# Verbatim from /private/tmp/.../scratchpad/fp-hypothetical.jsonl and
# fp-reported.jsonl — the transcripts the restraint auditor wrote from scratch.
NEGATED_LINES: tuple[str, ...] = (
    "And don't email the client yet. I mean it. Not until we actually know whether the numbers hold up.",  # FP2
    "Yeah. We could file a ticket for this but honestly let's not — it's going to fix itself when we move off the old bucket next month.",
    "Agreed. I don't want a ticket sitting there rotting for four weeks with my name on it.",
    "Right. If this were real I'd push a PR for the retry logic right now, but it's not real, it's a Tuesday and it's a staging bucket.",
    "Ha. Suppose we did do it — what would it even look like? Exponential backoff, cap at thirty seconds?",
    "Something like that. Purely hypothetically. Nobody write that down.",
    "What if we opened an issue just to track the thinking? Not to do it. Just so it exists.",
    "Then we'd probably want a hold on the calendar to revisit. Probably. I'm not asking you to make one.",
    "Deal. But not today. Nothing today.",
)

REPORTED_LINES: tuple[tuple[str, str], ...] = (
    ("Div said he'd send the deck. He said end of day yesterday, so, you know, sometime this week.", "Div"),
    ("He said he'd use the one from the offsite and fix the pricing slide himself.", ""),
    ("She told me she'd take it. She said she'd file the ticket and loop in legal once she has the first draft.", ""),  # FP3
    ("That's what he said in standup. He said he'd push it before the freeze.", ""),  # FP4
    ("Technically. Div also said he'd email the client the new timeline, which I'll believe when I see it.", "Div"),
    ("And Maya said she'd move the onboarding card to in progress once the copy is signed off.", "Maya"),
    ("And Rob mentioned he was going to open a PR for the SSO change, right?", "Rob"),
)

# Lines that LOOK like the ones above and are real work. Every one of these is a
# card the demo depends on, and each was chosen because it contains the exact
# token that would trip a lazier rule: "we are not", "don't", "no", "I said".
MUST_STILL_FIRE: tuple[str, ...] = (
    "I know, and that's a fair point, it just doesn't outweigh the latency. So — decision, and I want this "
    "written down: we are not using Redis for the ingestion cache. We're going with an in-process LRU on "
    "issue two. Redis stays for the queue, that doesn't change.",
    "No. We keep talking about it and it lives nowhere. We need a ticket for webhook signature verification "
    "on the ingestion endpoint.",
    "It's not on fire, nobody's leaked it, but it should be a thing we do on a schedule. Can we get a ticket "
    "cut for rotating the webhook signing keys? I don't want it to just live in this conversation.",
    "Yeah, I'll slack the team the notes so everyone knows the Redis thing changed. Don't want someone "
    "building against the old decision.",
    "Priya's taking the cache work off Sam. She's the one who found the sixty megabytes, so she should finish it.",
    "And I'll email Div the deck after this. He's asked for it twice now and I keep forgetting.",
    "I know we said Redis last week, but I've been staring at the profile for two days and I don't think "
    "that's right anymore.",
)


def test_restraint_detection() -> None:
    print("\n== restraint: the three shapes that look like work and are not ==")
    for line in NEGATED_LINES:
        check(f"negation/counterfactual caught: {line[:46]!r}",
              bool(extraction.detect_negation(line)))
    for line, expected_subject in REPORTED_LINES:
        reported, subject = extraction.detect_reported_speech(line)
        check(f"reported speech caught: {line[:46]!r}", reported)
        check(f"...attributed to {expected_subject or '(pronoun)'}", subject == expected_subject,
              f"got {subject!r}")
    for line in MUST_STILL_FIRE:
        check(f"NOT held back: {line[:46]!r}",
              not extraction.detect_negation(line)
              and not extraction.detect_reported_speech(line)[0])


def test_rejection_window() -> None:
    print("\n== restraint: a claim the room withdrew two lines later ==")
    # FP1, verbatim: the observation fired a live GitHub comment even though the
    # next two things anybody said were "let's not" and "I don't want a ticket".
    segments = [
        {"segment_id": "L1", "speaker": "You",
         "text": "So the ingest thing is still weird. The latency spikes on the big files."},
        {"segment_id": "L2", "speaker": "Priya",
         "text": "Yeah. We could file a ticket for this but honestly let's not — it's going to fix "
                 "itself when we move off the old bucket next month."},
        {"segment_id": "L3", "speaker": "You",
         "text": "Agreed. I don't want a ticket sitting there rotting for four weeks with my name on it."},
    ]
    subject = statement(segment_id="L1", kind="update", topic="ingest latency",
                        claim="The ingest process still has latency spikes on large files.",
                        quote=segments[0]["text"])
    extraction.apply_restraint_flags([subject], segments)
    check("the withdrawn claim is flagged rejected", subject.rejected)
    check("the reason names the cue", "let's not" in subject.restraint_reason)
    check("it fires nothing", plan_one(subject) == [])

    # ...and the window does not reach a segment that merely says "hold on".
    calm = [
        {"segment_id": "a", "speaker": "Them",
         "text": "Push it to the twenty-fifth. She needs the weekend to rip the Redis client out."},
        {"segment_id": "b", "speaker": "You",
         "text": "Works. Hold on — webhook signature verification. Did anyone ever actually file that?"},
    ]
    deadline = statement(segment_id="a", kind="deadline", quote=calm[0]["text"],
                         entity_refs={"deadline_text": "the twenty-fifth"})
    extraction.apply_restraint_flags([deadline], calm)
    check("'hold on' is not a withdrawal", not deadline.rejected)


def test_restraint_gate_blocks_every_route() -> None:
    print("\n== restraint: the gate covers every action kind at once ==")
    routes = {
        "ticket_request": {"topic": "security questionnaire"},
        "pr_intent": {"topic": "sso change", "quote": "He said he'd push a branch before the freeze."},
        "message_commitment": {"topic": "deck"},
        "email_commitment": {"topic": "deck", "entity_refs": {"person": "Div"}},
        "deadline": {"topic": "deck", "entity_refs": {"deadline_text": "Friday"}},
        "progress_report": {"topic": "onboarding card",
                            "entity_refs": {"linear_identifier": "SHA-6", "percent": 95}},
        "decision": {"topic": "cache layer", "entity_refs": {"issue_number": 2}},
    }
    for kind, extra in routes.items():
        free = statement(kind=kind, claim="Somebody does the thing.", **extra)
        check(f"{kind} fires when nothing is flagged", len(plan_one(free)) >= 1,
              "the fixture itself must be actionable or the gate proves nothing")
        for flag in ("negated", "rejected", "third_party"):
            held = statement(kind=kind, claim="Somebody does the thing.", **extra)
            setattr(held, flag, True)
            held.restraint_reason = f"test: {flag}"
            check(f"{kind} fires NOTHING when {flag}", plan_one(held) == [])


def test_gate_tolerates_statements_without_the_fields() -> None:
    print("\n== restraint: the gate is additive, not required ==")

    class OldStatement:  # no restraint fields at all — a pre-existing consumer
        segment_id, speaker, topic, kind = "s1", "Them", "cache layer", "decision"
        claim, quote, source = "Use an in-process LRU.", "use an LRU", "final"
        entity_refs = {"issue_number": 2}

    check("a statement with no restraint fields still routes",
          len(plan_one(OldStatement())) == 1)
    check("restraint_hold answers '' for it", planner.restraint_hold(OldStatement()) == "")


def test_statement_round_trip() -> None:
    print("\n== restraint: the flags survive to_dict/from_dict, and stay invisible when unset ==")
    plain = statement()
    check("an unflagged statement serializes exactly as before",
          set(plain.to_dict()) == {"segment_id", "speaker", "topic", "claim", "kind",
                                   "entity_refs", "quote", "source", "engine"})
    flagged = statement(kind="ticket_request")
    flagged.third_party, flagged.subject = True, "Priya"
    flagged.restraint_reason = "Sam was relaying Priya's commitment"
    restored = extraction.Statement.from_dict(flagged.to_dict())
    check("third_party round-trips", restored.third_party)
    check("subject round-trips", restored.subject == "Priya")
    check("restraint_reason round-trips", "relaying" in restored.restraint_reason)
    check("fires_no_work agrees", restored.fires_no_work)


# --- 2. work claims move tickets --------------------------------------------

STATE_CASES: tuple[tuple[str, int | None, str | None, str | None], ...] = (
    # spoken text, percent, current state, expected target
    ("Yeah, so I'm basically wrapped up on the cache layer one, SHA six.", None, "Backlog", STATE_IN_REVIEW),
    ("SHA-6 is basically done", None, None, STATE_IN_REVIEW),
    ("SHA-6 is finished", None, None, STATE_IN_REVIEW),
    ("SHA-6 is done, shipped it this morning", None, None, STATE_IN_REVIEW),
    ("I'll put SHA-6 up for review tomorrow", None, None, STATE_IN_REVIEW),
    ("I'm about eighty percent done with it, should be in review tomorrow", 80, None, STATE_IN_REVIEW),
    ("it's about ninety percent there", 90, None, STATE_IN_REVIEW),
    ("I am working on this issue", None, "Backlog", STATE_IN_PROGRESS),
    ("I'm working on SHA-6", None, "Todo", STATE_IN_PROGRESS),
    ("picking it up today", None, "Backlog", STATE_IN_PROGRESS),
    ("I'm picking up SHA-6 today", None, None, STATE_IN_PROGRESS),
    ("started on it this morning", None, "Unstarted", STATE_IN_PROGRESS),
    ("digging into the eviction policy now", None, "Backlog", STATE_IN_PROGRESS),
    ("about halfway", 50, None, STATE_IN_PROGRESS),
    # ...and the answers that must stay None.
    ("I am working on this issue", None, "In Review", None),
    ("picking it up today", None, "Done", None),
    ("the numbers look fine", None, None, None),
    ("nothing to report, which is the report", None, None, None),
    ("barely started, maybe ten percent", 10, None, None),
)


def test_work_claim_table() -> None:
    print("\n== work claims: the phrase table, every phrasing in the audit evidence ==")
    for spoken, percent, current, expected in STATE_CASES:
        got = choose_target_state(percent, spoken, current)
        check(f"{spoken[:44]!r} + {current or 'unknown column'} -> {expected}",
              got == expected, f"got {got!r}")
    check("there is still no STATE_DONE",
          not hasattr(sys.modules["adjourn.executors.linear_move_executor"], "STATE_DONE"))
    check("the old two-argument call still works",
          choose_target_state(None, "should be in review tomorrow") == STATE_IN_REVIEW)


def test_no_backward_moves() -> None:
    print("\n== work claims: a ticket never goes back down the board ==")
    check("In Review -> In Progress is backwards", is_backward_move("In Review", STATE_IN_PROGRESS))
    check("Done -> In Review is backwards", is_backward_move("Done", STATE_IN_REVIEW))
    check("Backlog -> In Progress is forwards", not is_backward_move("Backlog", STATE_IN_PROGRESS))
    check("In Progress -> In Review is forwards", not is_backward_move("In Progress", STATE_IN_REVIEW))
    check("an unrecognised column is never called backwards",
          not is_backward_move("Waiting On Legal", STATE_IN_PROGRESS))
    check("an unknown current state is never called backwards",
          not is_backward_move(None, STATE_IN_REVIEW))


def test_work_claim_routes_end_to_end() -> None:
    print("\n== work claims: the planner actually plans the move ==")
    wrapped = statement(
        kind="progress_report", topic="cache layer",
        claim="The cache layer work on SHA-6 is essentially complete.",
        quote="Yeah, so I'm basically wrapped up on the cache layer one, SHA six.",
        entity_refs={"linear_identifier": "SHA-6"})
    actions = plan_one(wrapped)
    check("a completion claim with no percent plans a linear_move",
          [action.kind for action in actions] == ["linear_move"])
    check("...to In Review", actions[0].payload["target_state"] == STATE_IN_REVIEW)

    claimed = statement(
        kind="update", topic="cache layer",
        claim="Sharique is picking up SHA-6 today.",
        quote="I'm picking up SHA-6 today, should have something by Thursday.",
        entity_refs={"linear_identifier": "SHA-6"})
    kinds = [action.kind for action in plan_one(claimed)]
    check("a work claim the extractor called an 'update' still moves the ticket",
          "linear_move" in kinds, str(kinds))

    idle = statement(
        kind="update", topic="auth migration",
        claim="The auth migration has run clean in dev all week with nothing to report.",
        quote="It's been running clean in dev all week. Nothing to report, which is the report.",
        entity_refs={"linear_identifier": "SHA-8"})
    check("an update where nothing moved plans no move",
          "linear_move" not in [action.kind for action in plan_one(idle)])


# --- 3. deadlines ------------------------------------------------------------


def test_next_weekday() -> None:
    print("\n== deadlines: 'next <weekday>' means next week's one ==")
    thursday = datetime(2026, 8, 20, 9, 0)   # the day the audit ran
    friday = datetime(2026, 8, 21, 9, 0)     # demo day
    cases = (
        (thursday, "next wednesday", "2026-08-26"),
        (thursday, "next monday", "2026-08-24"),
        (thursday, "next friday", "2026-08-28"),
        (thursday, "wednesday", "2026-08-26"),
        (thursday, "friday", "2026-08-21"),
        (friday, "next monday", "2026-08-24"),
        (friday, "next wednesday", "2026-08-26"),
        (friday, "next friday", "2026-08-28"),
        (friday, "friday", "2026-08-21"),     # a bare weekday said ON that day is today
        (datetime(2026, 8, 24, 9, 0), "next monday", "2026-08-31"),   # from a Monday
        (datetime(2026, 8, 23, 9, 0), "next wednesday", "2026-08-26"),  # from a Sunday
    )
    for reference, phrase, expected in cases:
        resolved = resolve_deadline(phrase, reference)
        check(f"{reference:%a %d %b} + {phrase!r} -> {expected}",
              resolved is not None and resolved.date().isoformat() == expected,
              str(resolved))
    check("no weekday named means no offset", _weekday_offset("the twenty-fifth", thursday) is None)


# --- 4. the PR review route --------------------------------------------------


def pr_statement(**overrides) -> extraction.Statement:
    refs = {"issue_number": 22, "pr_topic": "join button", "new_value": "#6b7f99"}
    refs.update(overrides.pop("entity_refs", {}))
    fields = {
        "segment_id": "prb-s05", "kind": "decision", "topic": "join button",
        "claim": "The join button on PR #22 should use the team's slate blue #6b7f99 "
                 "instead of the green it currently ships.",
        "quote": "Yeah, the copy's fine. But on Priya's join button PR — pull twenty-two — that "
                 "green is wrong. It should be our slate blue, six B seven F nine nine.",
    }
    fields.update(overrides)
    return statement(entity_refs=refs, **fields)


def test_pr_review_route() -> None:
    print("\n== pr_review_suggestion: the route, and everything it declines ==")
    check("the kind is in the action vocabulary",
          "pr_review_suggestion" in planner.ACTION_KINDS)
    check("it fires immediately — it is reversible, so no countdown",
          planner.choose_regret_window("pr_review_suggestion") == 0)

    actions = plan_one(pr_statement())
    check("a PR correction plans exactly one action", len(actions) == 1, str([a.kind for a in actions]))
    check("...and it is the review, not a comment on the conversation",
          actions[0].kind == "pr_review_suggestion")
    payload = actions[0].payload
    check("the PR number is the one the room said", payload["pr_number"] == 22)
    check("pr_topic rides through", payload["pr_topic"] == "join button")
    check("the new value rides through", payload["new_value"] == "#6b7f99")
    check("the old value is NOT invented", payload["old_value"] is None)
    check("the repo is the allowlisted one", payload["repo"] == planner.config.github_repo())
    check("the dedup key is structural, not prose",
          actions[0].dedup_key == "pr_review_suggestion:t:join-button:6b7f99",
          actions[0].dedup_key)

    spoken_old = pr_statement(entity_refs={"old_value": "#2ecc71"})
    check("a spoken old value rides through when there IS one",
          plan_one(spoken_old)[0].payload["old_value"] == "#2ecc71")

    named_file = pr_statement(quote="On pull twenty-two, the green in join.css is wrong — make it "
                                    "six B seven F nine nine.")
    check("a filename said out loud becomes the file hint",
          plan_one(named_file)[0].payload["file_hint"] == "join.css")

    no_value = pr_statement(entity_refs={"new_value": None})
    kinds = [action.kind for action in plan_one(no_value)]
    check("no new value means no review — an opinion is not a suggestion",
          "pr_review_suggestion" not in kinds, str(kinds))
    check("...and the ordinary github_update takes over again", kinds == ["github_update"])

    not_a_pr = statement(
        kind="decision", topic="cache layer",
        claim="Use an in-process LRU on issue two instead of Redis.",
        quote="So we're dropping Redis on issue two and going with an in-process LRU instead.",
        entity_refs={"issue_number": 2})
    check("a decision that is not a PR correction is untouched",
          [action.kind for action in plan_one(not_a_pr)] == ["github_update"])


def test_entity_refs_are_additive() -> None:
    print("\n== pr_review_suggestion: the new entity_refs fields are additive ==")
    refs = extraction.EntityReferences.from_dict(
        {"issue_number": 22, "pr_topic": "join button", "new_value": "#6b7f99"})
    check("pr_topic survives from_dict", refs.pr_topic == "join button")
    check("new_value survives from_dict", refs.new_value == "#6b7f99")
    check("old_value defaults to None", refs.old_value is None)
    check("to_dict still omits what was not said",
          set(refs.to_dict()) == {"issue_number", "pr_topic", "new_value"})
    check("the pydantic mirror accepts them too",
          extraction.EntityReferencesModel(pr_topic="x", old_value="y", new_value="z").new_value == "z")
    check("an old five-field payload still loads",
          extraction.EntityReferences.from_dict({"percent": 80}).percent == 80)


# --- 5. speaker attribution --------------------------------------------------


def test_speaker_display_name() -> None:
    print("\n== speaker: a track label is not a person's name ==")
    check("'You' becomes the configured name", extraction.speaker_display_name("You") == "Sharique")
    check("case does not matter", extraction.speaker_display_name("you") == "Sharique")
    # "Guest" — a role, not an invented name. The old default was "A teammate";
    # ADJOURN_GUEST_NAME is the setting now and ADJOURN_OTHER_SPEAKER_NAME is
    # still honoured behind it, so a runbook naming the old one keeps working.
    check("'Them' becomes a readable description, not a name",
          extraction.speaker_display_name("Them") == "Guest")
    check("...and so does the raw system-track label",
          extraction.speaker_display_name("system") == "Guest")
    check("a diarized name is left alone", extraction.speaker_display_name("Priya") == "Priya")
    check("an empty label stays empty", extraction.speaker_display_name("") == "")

    statements = [statement(speaker="You"), statement(speaker="Them")]
    extraction.apply_speaker_display_names(statements)
    check("the pass rewrites the mic track", statements[0].speaker == "Sharique")
    check("...and the system track too, so no card says 'Them will email Div'",
          statements[1].speaker == "Guest")

    # End to end: the label is rewritten ONCE, in extraction's post-processing,
    # and the planner copies whatever it finds — which is how the Slack body, the
    # Linear comment and the recap ledger all get the same name for free.
    carried = statement(speaker="You", kind="message_commitment",
                        claim="Slack the channel the summary once the standup is over.")
    check("the raw statement still carries the mic label", carried.speaker == "You")
    extraction.apply_speaker_display_names([carried])
    check("the Action carries the display name into the executor",
          plan_one(carried)[0].speaker == "Sharique")


# --- 6. linear_create: labels and a fail-closed team ------------------------


def test_linear_create_labels_and_team() -> None:
    print("\n== linear_create: label parity with GitHub, and no stranger's team ==")
    from ..executors import linear_create_executor as linear

    ticket = statement(kind="ticket_request", topic="webhook signing keys",
                       claim="We need a ticket for rotating the webhook signing keys.")
    action = plan_one(ticket)[0]
    check("the planner asks for the from-meeting label",
          action.payload["labels"] == [planner.LABEL_FROM_MEETING])

    rendered = linear.build_issue_input(action, "<team-id for SHA>")
    check("sim mode shows the labels it would resolve",
          rendered.get("labelIds") == ["<label-id for from-meeting>"], str(rendered.get("labelIds")))
    check("live mode does not invent label ids",
          "labelIds" not in linear.build_issue_input(action, "real-team-id"))

    calls: list[str] = []

    def fake_graphql(query: str, variables: dict, api_key: str) -> dict:
        calls.append(query)
        if "issueLabels" in query:
            return {"data": {"issueLabels": {"nodes": [
                {"id": "L-team", "name": "from-meeting", "team": {"id": "T-SHA", "key": "SHA"}},
                {"id": "L-global", "name": "From-Meeting", "team": None},
                {"id": "L-other", "name": "bug", "team": None},
            ]}}}
        return {"data": {"teams": {"nodes": [
            {"id": "T-OTHER", "key": "OTHER", "name": "Someone else"},
        ]}}}

    original = linear.post_linear_graphql
    original_cache = linear._read_team_cache
    original_write = linear._write_team_cache
    try:
        linear.post_linear_graphql = fake_graphql
        linear._read_team_cache = lambda: {}
        # The team cache is a REAL file in adjourn/state/. A test that writes a
        # fabricated team id into it leaves the next live run reading a lie.
        linear._write_team_cache = lambda teams: None
        check("a known label resolves to its id",
              linear.resolve_label_ids(["from-meeting"], "T-SHA", "key") == ["L-team"])
        check("a team-scoped label beats the workspace one",
              linear.resolve_label_ids(["from-meeting"], "T-SHA", "key") == ["L-team"])
        check("an unknown label is skipped, never created",
              linear.resolve_label_ids(["no-such-label"], "T-SHA", "key") == [])
        check("no labels asked for means no lookup at all",
              linear.resolve_label_ids([], "T-SHA", "key") == [])
        check("a missing team FAILS CLOSED rather than filing into a stranger's board",
              linear.resolve_team_id("SHA", "key") is None)
    finally:
        linear.post_linear_graphql = original
        linear._read_team_cache = original_cache
        linear._write_team_cache = original_write


def main() -> int:
    print("=" * 72)
    print("Lane 1 — decision pipeline: restraint, work claims, deadlines, PR review")
    print("=" * 72)
    test_restraint_detection()
    test_rejection_window()
    test_restraint_gate_blocks_every_route()
    test_gate_tolerates_statements_without_the_fields()
    test_statement_round_trip()
    test_work_claim_table()
    test_no_backward_moves()
    test_work_claim_routes_end_to_end()
    test_next_weekday()
    test_pr_review_route()
    test_entity_refs_are_additive()
    test_speaker_display_name()
    test_linear_create_labels_and_team()
    print("\n" + "=" * 72)
    print(f"{PASSED} passed, {FAILED} failed")
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
