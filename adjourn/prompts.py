"""Prompts — every word Adjourn ever says to a model, in one file.

Two jobs, two prompts:

    build_extraction_prompt(...)  transcript segments -> Statements
    build_conflict_prompt(...)    a new decision + prior history -> a verdict

Both are engine-agnostic: the same text goes to the Claude CLI (primary) and to
Gemini (fallback), so a wording fix lands on both engines at once. The only
difference between engines is transport — Gemini gets a JSON schema attached to
the request, the Claude CLI is asked for JSON in the prompt body.

WHY THIS FILE EXISTS SEPARATELY: prompts are the one part of the system that is
tuned by reading output, not by reasoning about code. Keeping them out of
extraction.py means the tuning diff is never tangled with the parsing diff.

# extraction rules ported from drift/extractor.py (filler-kill, verbatim
# segment_id, topic normalization, known-topics vocabulary)
# conflict rules ported from drift/detector.py

NOTHING HERE MAKES A DECISION. These prompts ask a model to describe what was
said. Whether any of it becomes an action is planner.py's table, which never
calls a model.
"""

from __future__ import annotations

import json

# --- extraction -------------------------------------------------------------

# Each line is "<kind>: <when to use it>". The routing table in planner.py keys
# off these exact strings, so the vocabulary here and STATEMENT_KINDS there must
# stay in lockstep (tests/test_understanding_extraction.py asserts they do).
KIND_GUIDE: tuple[tuple[str, str], ...] = (
    (
        "decision",
        "a choice or plan is made or CHANGED — approach, tool, owner, scope. "
        "Use this when the team settles on something, especially when it "
        "reverses what was decided before.",
    ),
    (
        "update",
        "a status report on existing work where NOTHING MOVED to a new stage — "
        "including 'on track', 'still in progress', 'nothing merged yet', 'no "
        "news', 'running clean in dev'. A report that the situation is unchanged "
        "is an update, however encouraging it sounds.",
    ),
    (
        "assignment",
        "ownership of a work item is given to or moved onto a named person. "
        "'Priya is taking the adapter'.",
    ),
    (
        "question",
        "an open question raised about a work item that nobody answers in the "
        "same breath.",
    ),
    (
        "ticket_request",
        "someone asks for a ticket / issue / card to be FILED for a piece of "
        "work. 'we need a ticket for the retry logic', 'file that', 'can we "
        "track that somewhere'.",
    ),
    (
        "progress_report",
        "a NAMED, already-tracked item has MOVED — a spoken percentage, a stage "
        "it has moved into ('in review', 'merged', 'done', 'shipped'), or the "
        "speaker CLAIMING THE WORK ('I'm working on SHA-6', 'picking that up "
        "today', 'I've started on it'). Starting is movement: a ticket nobody "
        "had begun is now underway. 'MMM-7 is about eighty percent done, should "
        "be in review tomorrow'. Put the number in entity_refs.percent and the "
        "ticket key in entity_refs.linear_identifier. Requires BOTH a named item "
        "and actual movement: if nothing moved, it is an 'update', not this. A "
        "work claim relayed about somebody else ('Maya said she'd move the card') "
        "is reported speech and produces nothing — see rule 3b(c).",
    ),
    (
        "message_commitment",
        "someone promises to send a CHAT message — Slack, Teams, Discord, 'ping "
        "the channel', 'drop it in #eng'. The promise itself is the statement.",
    ),
    (
        "email_commitment",
        "someone promises to send an EMAIL. 'I'll email Alex the deck'. Put the "
        "recipient in entity_refs.person.",
    ),
    (
        "deadline",
        "a date, day, or time window is SET or MOVED for a work item. 'let's "
        "review Friday', 'push it to the twenty-fifth', 'ship before the demo'. "
        "Put the spoken words in entity_refs.deadline_text EXACTLY as said. Not "
        "this when the date is merely repeated back, joked about, or asked "
        "about, and not this when the date is already carried inside another "
        "statement about the same work.",
    ),
    (
        "pr_intent",
        "someone says they will open, push, or put up a pull request / branch / "
        "patch. 'I'll open a PR for the adapter tonight'.",
    ),
)


def _render_kind_guide() -> str:
    return "\n".join(f'   - "{kind}": {guidance}' for kind, guidance in KIND_GUIDE)


EXTRACTION_INSTRUCTIONS = """You are the Scribe for a meeting titled "{title}".

INPUT: transcript segments below, one JSON object per line, each with segment_id, speaker, and text.

TASK: extract every distinct claim about a work item, a promise, or a date. Reply with ONE JSON object and nothing else, of the shape:

{{"statements": [{{"segment_id": "...", "speaker": "...", "topic": "...", "claim": "...", "kind": "...", "entity_refs": {{}}}}]}}

RULES

1. segment_id: copy it EXACTLY, character for character, from the input segment the claim comes from. Never invent, merge, or alter an id. A statement whose id is not in the list below is thrown away.

2. speaker: the speaker of that segment, copied exactly.

3. Skip filler entirely. Greetings, small talk, jokes, thanks, goodbyes, "can you hear me", and weather produce NO statements. A meeting of pure pleasantries produces an empty list, and that is a correct answer.

   ONE EXCEPTION, and it matters: a promise to send, share, or write something up AFTER the meeting is a real commitment, not meta-talk. "I'll Slack the channel the summary", "I'll send round the notes", "I'll email the deck over" are message_commitment or email_commitment statements. Never drop them.

3b. FOUR KINDS OF SENTENCE LOOK ACTIONABLE AND ARE NOT. Each produces NO statement. This is the most important rule on the page: every one of these was published to a real, public issue tracker by an earlier version of you.

   (a) NEGATED, PROHIBITED, OR DEFERRED. The room saying what it is NOT going to do.
       "And don't email the client yet. I mean it. Not until we actually know whether the numbers hold up."  -> NOTHING. This is a prohibition. It was emitted as kind "decision" and published as a comment reading "Do not email the client about the fix until it is confirmed the numbers hold up." The one sentence in the room that said DO NOT ACT became an action.
       "We could file a ticket for this but honestly let's not."  -> NOTHING.
       "Then we'd probably want a hold on the calendar to revisit. Probably. I'm not asking you to make one."  -> NOTHING.
       BUT: a negated TOOL is still a decision. "We are not using Redis for the ingestion cache. We're going with an in-process LRU on issue two." IS a decision and MUST be extracted. The test is what is being negated: a negated ACTION (file, email, send, open a PR, hold time) is restraint; a negated CHOICE is a decision.

   (b) HYPOTHETICAL OR COUNTERFACTUAL. A world that is not this one.
       "If this were real I'd push a PR for the retry logic right now, but it's not real."  -> NOTHING.
       "Suppose we did do it — what would it even look like?"  -> NOTHING.
       "What if we opened an issue just to track the thinking? Not to do it."  -> NOTHING.

   (c) REPORTED SPEECH — SOMEBODY ELSE'S COMMITMENT, RELAYED. The person speaking is not the person promising, and nobody in the room owes anything.
       "She told me she'd take it. She said she'd file the ticket and loop in legal once she has the first draft."  -> NOTHING. This filed a real ticket in Priya's name.
       "That's what he said in standup. He said he'd push it before the freeze."  -> NOTHING. This opened a real branch and a real draft pull request.
       "Alex also said he'd email the client the new timeline."  -> NOTHING.
       If you emit one of these anyway, put the ORIGINAL committer in `speaker` — never the person relaying it — so the mismatch is visible downstream. "X said he'd", "she told me she'd", "that's what he said", "he mentioned he was going to" are the giveaways. A first-person promise ("I'll take it") and a promise ASSIGNED in the room ("Priya's taking the cache work off Sam") are both real and are NOT this.

   (d) SOMEBODY ELSE'S WORK, SIZED BY THE ROOM. An estimate of how quick, easy or cheap an absent person's task would be is not that person's commitment, and the date attached to it is not a deadline.
       "It's one line in the stylesheet, she can turn that around before Friday without breaking a sweat."  -> NOTHING. This put a calendar hold on a colleague's unpromised work. "can" is not "will": nobody asked her, and she was not in the room to answer.
       "That's a two-hour job for Rob, he could have it done Monday."  -> NOTHING.
       "Honestly the whole thing is a rename, someone could knock it out this afternoon."  -> NOTHING. An unnamed "someone" owes nothing.
       BUT: work ASSIGNED to a person in the room, ACCEPTED by them, or REQUESTED of them is real. "Priya's taking the cache work off Sam" is an assignment. "Can you file a ticket for the flaky screenshot test?" is a ticket_request. The test is whether anybody actually took it on.

4. EXTRACT THE BEAT WHERE IT LANDS, NOT WHERE IT IS RAISED. Every claim gets exactly ONE statement, on the segment that SETTLES it. A question that somebody answers in a later segment is setup — the statement belongs to the answer, and the question itself produces nothing. Emit "question" ONLY for a question that nobody answers anywhere in the transcript.

   Produce NO statement for a segment that merely: echoes, repeats, agrees with, reacts to, or clarifies the wording of something another speaker just said; restates a fact already captured by another statement; or supplies background colour for a claim recorded elsewhere. When two segments carry the same claim, keep the one with the specifics and drop the other.

   THE SAME RULE RUNS BACKWARDS. When a LATER segment in this batch withdraws, rejects, or defers a claim raised in an earlier one, the withdrawal is where the beat settles — so the earlier claim produces NOTHING and the withdrawal produces nothing either. "So the ingest thing is still weird, the latency spikes on the big files." followed by "We could file a ticket for this but honestly let's not" and "I don't want a ticket sitting there rotting for four weeks with my name on it" is a room deciding to do nothing, twice, out loud. It produced a comment on a live issue. Two people declining to record something is as clear as consent gets.

   Do split genuinely different intentions. If one segment both picks an approach and promises to Slack about it, emit two statements with that same segment_id — one "decision" and one "message_commitment".

5. topic: a short lowercase key of 2-4 words naming the underlying WORK ITEM in plain words — "cache layer", "retry logic", "storage adapter", "rate limiting", "demo deck". Normalize aggressively: every mention of the same subject anywhere in the meeting must reuse the IDENTICAL topic key, even when speakers phrase it differently, and even when one of them says a ticket number and the other describes the work.

   A ticket key or issue number is NEVER a topic. "MMM-7" is not a topic; the topic is the work MMM-7 is about. Put the key in entity_refs and describe the work in `topic`.

   Name the work item by WHAT IT IS, in the team's own words. Never name it by its effect, its blocker relationship, or what it unblocks: work on rate limiting is "rate limiting", not "gateway blocker".{known_topics}

6. claim: ONE self-contained sentence that carries the specifics — the tool or approach chosen, the owner, the date, the number. Someone reading only the claim, with no transcript, must be able to act on it. Do not quote filler words back; write the claim in clean prose.

   NEVER STRENGTHEN A MODAL. "can", "could", "might", "we should probably" and "I was thinking of" stay exactly that strong in the claim — never rewrite them into "will", "should", or a bare statement of fact. Making a sentence actionable is not your job; recording what was actually said is. An upgraded modal is how "she can turn that around before Friday" became a scheduled deadline for somebody who never spoke. If the honest claim is too weak to act on, that is the correct outcome and rule 3b probably applies.

   WRITE ABOUT THE WORK, NOT ABOUT WHO SPOKE. The speaker is already recorded in `speaker` and the exact words are already kept verbatim, so a claim that opens with the speaker's name spends its first four words saying nothing: "Priya asked for a ticket to be cut to rotate the webhook signing keys" should be "The webhook signing keys need rotating on a schedule". The exception is a claim that is genuinely ABOUT a person — an assignment, or who a message is going to.

7. kind: pick EXACTLY one from this list.
{kind_guide}

   When two kinds could fit, prefer the more specific one: a promise to send a message is "message_commitment", not "assignment"; a percentage on a named ticket is "progress_report", not "update"; a request to file something is "ticket_request", not "decision".

8. entity_refs: an object holding ONLY handles that were literally spoken. Omit any key you did not hear. Never infer, never guess, never carry a value over from a different segment.
   - issue_number (integer): only when a GitHub issue number is spoken — "issue two", "#2", "number four".
   - linear_identifier (string): only when a ticket key is spoken — "MMM-7", "ENG-114". Uppercase it.
   - person (string): the named human this statement points at — the new owner, or the recipient of a promised message or email.
   - deadline_text (string): FORWARD-LOOKING date words EXACTLY as spoken — when something is due, targeted, or will happen. "Friday", "the twenty-fifth", "tonight", "tomorrow", "before the demo". Capture these on ANY statement whose own segment contains such words, not only on statements of kind "deadline": a promise made "tonight" carries deadline_text "tonight". NEVER capture a date that points backwards — "since Tuesday", "last night", "yesterday", "three weeks ago" are history, not deadlines, and must be omitted.
   - percent (integer): a completion percentage that was spoken — "eighty percent" -> 80.
   - pr_topic (string): 2-4 lowercase words naming the thing being CHANGED on somebody's open pull request, when the room corrects a concrete value in code under review — "join button", "retry backoff", "signup copy".
   - new_value (string): the corrected value, normalized from how it was said. "our slate blue, six B seven F nine nine" -> "#6b7f99". "bump it to thirty seconds" -> "30s". Only when a specific value was actually spoken; a colour NAME is not a value.
   - old_value (string): the value being replaced, ONLY when it was spoken as a concrete value. "that green is wrong" gives you NO old_value — "green" is a description, not a value, and the real hex lives in the diff where the reviewer will find it. Leave it out rather than guessing.

   A PR CORRECTION LOOKS LIKE THIS. "But on Priya's join button PR — pull nineteen — that green is wrong. It should be our slate blue, six B seven F nine nine." -> kind "decision", topic "join button", issue_number 19 (GitHub numbers pull requests and issues in the same space, so a spoken "pull nineteen" is issue_number 19), pr_topic "join button", new_value "#6b7f99", and NO old_value. The number here is an EXAMPLE: read the one that was actually spoken, and if no number was spoken, omit issue_number entirely rather than reusing this one.

9. EVERY STATEMENT YOU EMIT CAUSES SOMETHING TO HAPPEN IN THE REAL WORLD — a comment on a live issue, a ticket, a calendar hold, a message someone receives. A statement you were unsure about becomes an action nobody asked for. So when a segment is borderline, emit nothing. A short, correct list is the goal; completeness is not.

VALID segment_id VALUES FOR THIS BATCH (any other id is a hallucination and will be dropped):
{valid_ids}

SEGMENTS:
{segments}
"""

JSON_ONLY_REMINDER = (
    "\nReply with the JSON object only. No preamble, no explanation, no markdown "
    "fence, no trailing commentary.\n"
)


def build_extraction_prompt(
    segments: list[dict],
    meeting_title: str,
    known_topics: list[str] | None = None,
) -> str:
    """The full extraction prompt for one batch of segments.

    `known_topics` is the running vocabulary from earlier batches. Feeding it
    back is what keeps "the cache thing" in batch 3 under the same topic key as
    "cache layer" in batch 1 — without it, topics fragment and memory stops
    recognising that two meetings are about the same work.
    """
    lines = "\n".join(
        json.dumps(
            {
                "segment_id": segment.get("segment_id", ""),
                "speaker": segment.get("speaker", ""),
                "text": segment.get("text", ""),
            }
        )
        for segment in segments
    )
    valid_ids = ", ".join(str(segment.get("segment_id", "")) for segment in segments)
    known = ""
    if known_topics:
        known = (
            " Topic keys already used earlier in this meeting — reuse them "
            "VERBATIM when the subject matches: " + ", ".join(known_topics) + "."
        )
    return (
        EXTRACTION_INSTRUCTIONS.format(
            title=meeting_title,
            known_topics=known,
            kind_guide=_render_kind_guide(),
            valid_ids=valid_ids,
            segments=lines,
        )
        + JSON_ONLY_REMINDER
    )


def build_repair_prompt(original_prompt: str, validation_error: str) -> str:
    """The one retry: the same prompt with the parser's complaint appended.

    Cheaper and far more reliable than re-asking blind — the model gets to see
    the exact field it got wrong. One retry only; a second failure means the
    engine is having a bad day and we fall to the next engine down.
    """
    return (
        original_prompt
        + "\n\nYOUR PREVIOUS REPLY WAS REJECTED BY THE PARSER:\n"
        + validation_error.strip()
        + "\n\nReturn the corrected JSON object only. Fix the specific problem "
        "named above; keep everything else identical.\n"
    )


# --- conflict judging -------------------------------------------------------

# ported from drift/detector.py — the conflict-vs-refinement boundary below is
# the part that took the most tuning in Drift; changing it changes what the
# board calls a "what changed" card.
CONFLICT_INSTRUCTIONS = """You are the Historian for an engineering team. Decide whether a NEW meeting statement CHANGES something that was already settled about this work item.

PRIOR STATEMENTS about "{topic}", oldest first:
{history}

NEW STATEMENT:
[{date} - {meeting}] {who} ({kind}): {text}

Reply with ONE JSON object and nothing else:

{{"conflict": true|false, "before": "...", "after": "...", "what_changed": "..."}}

conflict=true ONLY when the new statement REVERSES or REPLACES a previous decision — one or more of:
- a different approach or tool than the one previously chosen,
- a different owner than the person previously assigned,
- a moved deadline or target date,
- scope that is cut or materially changed.

These are NOT conflicts. Return conflict=false with before, after and what_changed as empty strings:
- Consistent progress on the existing plan ("benchmarks look good", "on track", work proceeding as decided).
- The plan gaining detail while staying consistent with what was decided.
- Restating, confirming, or agreeing with the same plan in different words.
- A question, or an answer that does not change the plan.
- The first decision on a topic where nothing had been decided before.

If conflict=true:
- before: the prior decision, one sentence.
- after: the new state, one sentence.
- what_changed: a compact delta naming ONLY the dimensions that actually moved, e.g. "approach Redis->in-process LRU; owner Sam->Priya; ship Aug 22->Aug 25".
"""


def build_conflict_prompt(history: list[dict], new_statement: dict, topic: str = "") -> str:
    """The conflict prompt. `history` rows are {date, meeting, who, kind, text}."""
    lines = "\n".join(
        f"{index}. [{row.get('date', '')} - {row.get('meeting', '')}] "
        f"{row.get('who', '')} ({row.get('kind', '')}): {row.get('text', '')}"
        for index, row in enumerate(history, 1)
    )
    return (
        CONFLICT_INSTRUCTIONS.format(
            topic=topic or new_statement.get("topic", "this work item"),
            history=lines,
            date=new_statement.get("date", ""),
            meeting=new_statement.get("meeting", ""),
            who=new_statement.get("who", ""),
            kind=new_statement.get("kind", ""),
            text=new_statement.get("text", ""),
        )
        + JSON_ONLY_REMINDER
    )


def describe_prompts() -> dict:
    """Sizes and vocabulary, for `-m adjourn.prompts` and the board's info panel."""
    return {
        "statement_kinds": [kind for kind, _ in KIND_GUIDE],
        "extraction_prompt_characters": len(EXTRACTION_INSTRUCTIONS),
        "conflict_prompt_characters": len(CONFLICT_INSTRUCTIONS),
    }


if __name__ == "__main__":
    print(json.dumps(describe_prompts(), indent=2))
