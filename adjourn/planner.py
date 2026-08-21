"""Planner — statements in, actions out, by TABLE.

=============================================================================
 NON-NEGOTIABLE: THIS MODULE MUST NEVER CALL A MODEL.
 No genai. No anthropic. No subprocess to the claude CLI. No network at all.
 A model extracts; a table decides. If routing feels like it needs judgement,
 the answer is a better statement `kind` from extraction.py, not an LLM here.
 `plan()` must be a pure function: same statements + same memory -> same actions.
=============================================================================

Contract:
    plan(statements, memory) -> list[Action]

Two supersets on that contract, both keyword-only and both defaulted, so every
existing caller keeps working:

    plan(statements, memory, meeting={...}, conflict_verdicts={...})

`meeting` is {"meeting_id", "title", "date"} — the planner needs the id to key the
recap page and to stamp every Action.meeting_id.

`conflict_verdicts` is {segment_id: {"conflict", "before", "after", "what_changed"}},
computed BY THE ORCHESTRATOR through extraction.judge_conflict() before planning.
That call is a model call and it lives on the extraction side of the line, where
every other model call lives. What arrives here is data. The table still decides
whether a verdict becomes a GitHub comment, and a plan built with no verdicts at
all still routes every statement — the verdict only changes how the comment
READS (a before/after table instead of a quieter note), never whether it fires.

ONE THING RUNS BEFORE THE TABLE: the restraint gate. A statement that extraction
flagged as negated, rejected in the room, or reported speech routes to NOTHING —
no candidate is even considered — and the decline is printed. See restraint_hold()
below; the flags themselves are computed deterministically in extraction.py off
the verbatim transcript, because the claim has already had the modality ironed
out of it by the time it gets here.

`build_dedup_key()` and `normalize_key_text()` are IMPLEMENTED here, because the
orchestrator and the executors must compute byte-identical keys or the reconcile
pass will double-fire. Do not reimplement them anywhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from . import config

if TYPE_CHECKING:  # avoid a hard import cycle; typing only
    from .extraction import Statement
    from .memory_store import MeetingMemory

# --- action vocabulary ------------------------------------------------------

ACTION_KINDS: tuple[str, ...] = (
    "github_update",         # Drift-style what-changed comment + label + plan block
    "linear_create",         # new Linear ticket
    "linear_move",           # move an existing Linear ticket between states
    "pull_request_stub",     # draft PR / branch stub for a stated intent
    "pr_review_suggestion",  # inline ```suggestion review on an open PR
    "slack_send",            # message to a channel or person
    "email_send",            # follow-up email
    "calendar_hold",         # time block for a deadline or commitment
    "recap_page",            # local recap HTML for the meeting
)

# Irreversible sends get a visible countdown; everything else fires immediately.
# The board draws a ring for anything with a non-zero window.
REGRET_WINDOW_BY_KIND: dict[str, int] = {
    "slack_send": config.DEFAULT_REGRET_WINDOW_SECONDS,
    "email_send": config.DEFAULT_REGRET_WINDOW_SECONDS,
}

# THE TABLE. This is the deterministic core of the product: statement kind ->
# candidate action kinds, in priority order. A statement kind with an empty tuple
# is deliberately inert (it informs memory and the recap, but fires nothing).
#
# Every candidate is still subject to its GUARD in build_action_for(): a
# github_update needs a resolvable issue, a calendar_hold needs a deadline phrase
# that resolves to a real date, an email_send needs an address the address book
# can actually produce. A guard returning None is normal and means "the table
# declined" — declining is always safer than acting on a guess.
ROUTING_TABLE: dict[str, tuple[str, ...]] = {
    # pr_review_suggestion goes FIRST and suppresses the github_update behind it
    # (see SUPPRESSED_BY): a correction to a line of code under review belongs on
    # the diff as a one-click suggestion, not as prose on the conversation tab.
    # Its guard is narrow, so a decision that is not a PR correction falls
    # straight through to the comment exactly as it always did.
    "decision": ("pr_review_suggestion", "github_update"),
    # linear_move rides along on `update` because a work claim — "I'm working on
    # SHA-6" — is a sentence the extractor calls an update about as often as it
    # calls it a progress_report, and the ticket should move either way. The move
    # guard is a phrase table that answers None for an update that did not move,
    # so the overwhelming majority of updates still produce only the comment.
    "update": ("github_update", "linear_move"),
    "assignment": ("github_update",),      # ownership moved — before/after comes from memory
    "question": (),                        # inert on purpose: recap and memory only
    "ticket_request": ("linear_create",),  # "we need a ticket for X"
    "progress_report": ("linear_move",),   # a named item that actually moved
    "message_commitment": ("slack_send",),  # 60s regret window
    "email_commitment": ("email_send",),    # 60s regret window
    "deadline": ("calendar_hold",),        # a date someone committed to
    "pr_intent": ("pull_request_stub",),   # "I'll open a PR"
}

# When the key's action is planned for a statement, the listed action kinds are
# skipped FOR THAT STATEMENT. The table fires every candidate it can by design —
# a decision that both moves a ticket and needs a comment should do both — so
# mutual exclusion has to be declared where it is actually meant.
SUPPRESSED_BY: dict[str, tuple[str, ...]] = {
    "pr_review_suggestion": ("github_update",),
}

# Kinds whose GitHub comment renders the loud before/after table rather than the
# quiet note — but only when memory or a verdict actually supplies a "before".
CHANGE_SHAPED_KINDS: frozenset[str] = frozenset({"decision", "assignment", "deadline"})

# A statement about a work item nobody has ever tracked cannot become a comment
# on an issue that does not exist. These are the label names the executor uses.
LABEL_FROM_MEETING = "from-meeting"
LABEL_DECISION_CHANGED = "decision-changed"

DEFAULT_LINEAR_TEAM_KEY = "ENG"
DEFAULT_HOLD_DURATION_MINUTES = 60


@dataclass
class Action:
    """One thing Adjourn intends to do, fully specified before anything is sent.

    kind            — one of ACTION_KINDS.
    payload         — everything the executor needs. Executor-specific; document
                      the shape in that executor's module docstring.
    dedup_key       — deterministic identity of this action. Two plans over the
                      same meeting must produce the same key for the same intent,
                      or the reconcile pass will fire it twice.
    regret_window_s — 0 fires immediately; >0 shows a countdown the user can cancel.
    """

    kind: str
    payload: dict = field(default_factory=dict)
    dedup_key: str = ""
    regret_window_s: int = 0

    # Provenance — carried into ExecutorResult so every card can show its quote.
    quote: str = ""
    speaker: str = ""
    meeting_id: str = ""
    segment_id: str = ""
    source: str = "live"

    @property
    def is_reversible_on_countdown(self) -> bool:
        """True when a human still has time to stop this before it goes out."""
        return self.regret_window_s > 0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "payload": self.payload,
            "dedup_key": self.dedup_key,
            "regret_window_s": self.regret_window_s,
            "quote": self.quote,
            "speaker": self.speaker,
            "meeting_id": self.meeting_id,
            "segment_id": self.segment_id,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Action:
        return cls(
            kind=data["kind"],
            payload=data.get("payload") or {},
            dedup_key=data.get("dedup_key", ""),
            regret_window_s=int(data.get("regret_window_s", 0)),
            quote=data.get("quote", ""),
            speaker=data.get("speaker", ""),
            meeting_id=data.get("meeting_id", ""),
            segment_id=data.get("segment_id", ""),
            source=data.get("source", "live"),
        )


# --- deterministic key construction (IMPLEMENTED — do not duplicate) --------

# Tokenizing on [a-z0-9] runs strips punctuation, quotes, and every character
# that would otherwise make "Ship the cache layer." and "ship cache layer"
# different keys. Ported concept from drift/graph.py find_issue().
_KEY_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_KEY_TEXT_TOKEN_LIMIT = 12


def normalize_key_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, cap at 12 tokens.

    Deterministic and stable across the live pass and the reconcile pass, where
    the same sentence often arrives with different casing and punctuation.

        >>> normalize_key_text("I'll ping the team in Slack -- tonight!")
        'i-ll-ping-the-team-in-slack-tonight'
    """
    tokens = _KEY_TOKEN_PATTERN.findall((text or "").lower())
    return "-".join(tokens[:_KEY_TEXT_TOKEN_LIMIT])


def build_dedup_key(kind: str, *parts: object) -> str:
    """The identity of an action: 'kind:part:part:...'.

    Every part is normalized the same way, empty parts are dropped, so the key is
    a function of MEANING rather than of transcript formatting. Use the most
    stable identifiers available and put them in a fixed order, e.g.

        build_dedup_key("github_update", issue_number, topic)
        build_dedup_key("slack_send", channel, claim_text)
        build_dedup_key("linear_create", topic)

    Deliberately NOT keyed on segment_id: the live pass and the final pass assign
    different segment ids to the same sentence, and keying on them would double-fire.
    """
    normalized = [normalize_key_text(str(part)) for part in parts]
    return ":".join([kind, *[part for part in normalized if part]])


# WHAT GOES IN A DEDUP KEY, and why it is not the same answer for every kind.
#
# Two passes over ONE meeting must produce identical keys, or a promise is kept
# twice. Two DIFFERENT meetings must produce different keys wherever repeating
# the action is the right behaviour, or a later meeting is silently ignored.
#
#   meeting-scoped   github_update, linear_move, calendar_hold, slack_send,
#                    email_send, recap_page
#                    -> next week's standup gets to comment on #2 again, move the
#                       same ticket again, and hold its own time.
#   topic-scoped     linear_create, pull_request_stub
#                    -> the ticket and the branch should exist ONCE. A second
#                       meeting asking for the same ticket must not file a
#                       duplicate, however many times it comes up.
#
# NEVER in a key: the speaker, and never the claim text. Measured on a real
# recording: the live captions called the speaker "Them" and the final transcript
# called the same person "Speaker 1", so a key carrying the speaker made one
# promised Slack message into two queued sends. Diarization is not identity.


def choose_regret_window(kind: str) -> int:
    """Seconds of visible countdown for an action kind. 0 means fire now."""
    if kind not in REGRET_WINDOW_BY_KIND:
        return 0
    # Read the configured value rather than the import-time constant, so
    # REGRET_WINDOW_SECONDS in .env (or a shorter one for the stage) is honoured.
    return config.regret_window_seconds()


# --- small pure helpers -----------------------------------------------------


def _one_line(value: object, limit: int = 0) -> str:
    """Collapse whitespace; optionally truncate on a word boundary with an ellipsis."""
    text = " ".join(str(value or "").split())
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return text


def _entity_reference(statement: Statement, name: str):
    """One entity_refs field, whatever shape the Statement's refs are in."""
    refs = getattr(statement, "entity_refs", None)
    if refs is None:
        return None
    if isinstance(refs, dict):
        return refs.get(name)
    return getattr(refs, name, None)


def _statement_topic(statement: Statement) -> str:
    return _one_line(getattr(statement, "topic", "")).lower()


# Clause separators that introduce a REASON. A ticket title wants the thing to
# do, not the justification for doing it.
_REASON_SEPARATORS: tuple[str, ...] = (", because ", " because ", ", since ", " so that ")

# Trailing relative clauses. The extractor writes a claim as a full sentence —
# "…webhook signature verification on the ingestion endpoint, which has only ever
# been discussed verbally" — and everything from the comma on is justification,
# not the name of the work. Left in, it eats the whole title and the ticket
# arrives ending in an ellipsis on a screen founders are reading.
_TRAILING_CLAUSE_SEPARATORS: tuple[str, ...] = (
    ", which ", ", that ", ", who ", ", where ", ", and ", ", but ", ", so ",
    ", as ", ", it ", ", they ",
)

# How people ASK for a ticket, stripped so the title is the work rather than the
# request for the work. "We need a ticket for the retry logic" is a fine claim
# and a terrible ticket title. Longest first so the longer phrasing wins.
_TICKET_REQUEST_PREFIXES: tuple[str, ...] = (
    "a ticket needs to be filed for",
    "a ticket should be filed for",
    "a ticket needs to be created for",
    "we need to file a ticket for",
    "we need to create a ticket for",
    "someone needs to file a ticket for",
    "let's file a ticket for",
    "lets file a ticket for",
    "we need a ticket for",
    "file a ticket for",
    "create a ticket for",
    "open a ticket for",
    "track",
)


# "...a ticket [to be cut] for|to <THE WORK>" — the work is everything after the
# request. Anchored on the request noun rather than on a fixed opening phrase,
# because the opening is the part the model rewords every run and the noun is not.
_TICKET_REQUEST_TAIL_PATTERN = re.compile(
    r"\b(?:ticket|issue|card)\b\s*"
    r"(?:to\s+be\s+\w+\s+)?"
    r"(?:for|to|about|on|covering|tracking)\s+"
    r"(?P<work>\S.+)$",
    re.IGNORECASE,
)


def _title_from_claim(claim: str, limit: int = 78) -> str:
    """A ticket/PR title from a claim sentence: first clause, no trailing period."""
    text = _one_line(claim).rstrip(".")
    for separator in _REASON_SEPARATORS:
        head, marker, _tail = text.partition(separator)
        if marker:
            text = head.rstrip(" ,;:")
            break
    lowered = text.lower()
    for prefix in _TICKET_REQUEST_PREFIXES:
        if lowered.startswith(prefix + " "):
            text = text[len(prefix) + 1 :]
            break
    else:
        # The model words a ticket request a different way every run ("we need a
        # ticket for X", "no ticket exists yet for X; one needs to be filed",
        # "Priya asked for a ticket to be cut to rotate the signing keys").
        # What is stable is the shape: a request noun, then a preposition, then
        # the work. Keep the work — "Them asks for a ticket to be cut to rotate
        # the webhook signing keys" is a fine sentence and a terrible ticket.
        request = _TICKET_REQUEST_TAIL_PATTERN.search(text)
        if request:
            text = request.group("work")
        else:
            head, marker, tail = text.partition(" for ")
            if marker and "ticket" in head.lower().split(" for ")[0] and len(head.split()) <= 10:
                text = tail
    # Everything after a semicolon, a dash, or a relative pronoun is commentary on
    # the request, not part of the work item's name.
    for tail_separator in ("; ", " — ", " -- ", *_TRAILING_CLAUSE_SEPARATORS):
        text = text.split(tail_separator, 1)[0].rstrip(" ,;:")
    # "the retry logic on the ingestion client" -> "Retry logic on the ingestion client"
    if text.lower().startswith("the "):
        text = text[4:]
    text = text[:1].upper() + text[1:] if text else text
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def summarize_prior(prior) -> str:
    """One line describing a prior statement, for the before/after table."""
    return _one_line(getattr(prior, "claim", "") or "")


# The nouns a person actually says when they mean "I will open a pull request".
# Deliberately nouns and verb phrases, not the extractor's own kind label — the
# label is the model's opinion, these are the words that were spoken.
_PULL_REQUEST_PHRASES = (
    "pull request",
    "pr for",
    "a pr",
    "the pr",
    "open a pr",
    "draft pr",
    "push a branch",
    "push up a branch",
    "cut a branch",
    "branch for",
    "put up a patch",
    "send a patch",
)


# Forward-looking verbs: a beat that says one of these is scheduling a meeting
# with someone, not promising a deliverable by a date.
_REVIEW_PHRASES = (
    "review",
    "walk through",
    "walk it through",
    "sync",
    "check in",
    "catch up",
    "go over",
    "look at it together",
    "regroup",
    "debrief",
    "retro",
)


def _is_review_shaped(statement: Statement) -> bool:
    """True when the words spoken schedule a look-at-it-together, not a due date."""
    spoken = f"{getattr(statement, 'quote', '')} {getattr(statement, 'claim', '')}".lower()
    return any(phrase in spoken for phrase in _REVIEW_PHRASES)


def _names_a_bare_weekday(deadline_text: str, weekday_numbers: dict) -> bool:
    """True when the deadline phrase is a weekday name and carries no other anchor.

    "Friday" and "on Friday" qualify; "next Friday" and "Friday the 28th" already
    say which one they mean and must be left exactly as the executor resolved them.
    """
    text = deadline_text.strip().lower()
    if any(word in text for word in ("next", "this coming", "following", "week")):
        return False
    if any(character.isdigit() for character in text):
        return False
    return any(name in text for name in weekday_numbers)


def _subject_phrase(statement: Statement, fallback: str) -> str:
    """The topic, with any person's name in it title-cased. Pure string handling.

    The topic comes from the model, which writes it in running-sentence case, so
    a subject built straight from it read "Follow-up: deck for div" — a person's
    name, lowercased, in the subject line of a real email.
    """
    phrase = _one_line(_statement_topic(statement) or fallback, 60)
    person = _one_line(_entity_reference(statement, "person") or "")
    if not person:
        return phrase
    pattern = re.compile(rf"\b{re.escape(person)}\b", re.IGNORECASE)
    return pattern.sub(person, phrase)


# --- the restraint gate (M4) ------------------------------------------------
#
# Three shapes of sentence reach the planner looking exactly like work and are
# not work: a negation ("don't email the client yet"), an idea the room threw
# out ("we could file a ticket but let's not"), and somebody else's commitment
# being relayed ("Div said he'd send the deck"). extraction.apply_restraint_flags
# marks them deterministically, off the verbatim transcript. This is where the
# mark is honoured, once, for every action kind at the same time — rather than
# per-builder, where the next builder anybody adds would quietly not have it.
#
# It is a HOLD, not a delete. The statement still reaches build_recap_page and
# still reaches memory: a promise somebody else made is real information and
# belongs in the ledger, attributed to them. What it never reaches is a service.

RESTRAINT_FLAG_REASONS: tuple[tuple[str, str], ...] = (
    ("negated", "negated in the source sentence"),
    ("rejected", "withdrawn or rejected in the room"),
    ("third_party", "reported speech — somebody else's commitment"),
)


def restraint_hold(statement: Statement) -> str:
    """Why this statement fires nothing, or "" when it is free to act. Pure.

    Reads the flags by getattr so a duck-typed stand-in (the tests use one) and
    any Statement built before these fields existed both answer "" and route
    exactly as they did.
    """
    reasons = [
        reason for flag, reason in RESTRAINT_FLAG_REASONS
        if bool(getattr(statement, flag, False))
    ]
    if not reasons:
        return ""
    detail = _one_line(getattr(statement, "restraint_reason", ""))
    joined = "; ".join(reasons)
    return f"{joined} [{detail}]" if detail else joined


def mentions_pull_request(statement: Statement) -> bool:
    """True when the speaker's OWN WORDS name a pull request or a branch.

    A pure substring table over the quote and the claim. The extractor labels a
    statement `pr_intent` more eagerly than a person means it, and this kind
    opens a real branch on a real repo, so the label alone is not enough licence.
    """
    spoken = f"{getattr(statement, 'quote', '')} {getattr(statement, 'claim', '')}".lower()
    return any(phrase in spoken for phrase in _PULL_REQUEST_PHRASES)


# --- memory reads (all read-only, all tolerant of a missing backend) ---------


def resolve_issue_number(statement: Statement, memory: MeetingMemory | None) -> int | None:
    """The GitHub issue this statement is about, or None.

    Order: the number that was actually SPOKEN in this segment, then memory's
    topic->issue index. Never guessed from a neighbouring segment — that is how a
    comment lands on the wrong issue.
    """
    spoken = _entity_reference(statement, "issue_number")
    if spoken:
        try:
            return int(spoken)
        except (TypeError, ValueError):
            pass
    topic = _statement_topic(statement)
    if not topic or memory is None:
        return None
    try:
        return memory.find_issue_for_topic(topic)
    except Exception:  # noqa: BLE001 — memory is an optimization, never a dependency
        return None


def resolve_linear_identifier(statement: Statement, memory: MeetingMemory | None) -> str | None:
    """The Linear ticket this statement is about, or None. Spoken first, then memory.

    A spoken identifier is CANONICALIZED before it is returned, because it lands in
    the dedup key. Speech recognition drops the hyphen — a live caption of this
    demo produced "SHA5" while the final transcript produced "SHA-5" — and two
    spellings of one ticket are two keys, which means the fast pass and the
    reconcile pass would each move it.
    """
    spoken = _entity_reference(statement, "linear_identifier")
    if spoken:
        # Pure regex over a known team-key list. No model, no network.
        from .executors.linear_move_executor import find_identifier

        return find_identifier(spoken) or str(spoken).upper()
    topic = _statement_topic(statement)
    if not topic or memory is None:
        return None
    try:
        index = memory.topic_index_by_reference() or {}
    except Exception:  # noqa: BLE001
        return None
    # topic_index_by_reference is handle -> topic; invert it deterministically,
    # ignoring GitHub handles ("#2") which are not Linear identifiers.
    for handle in sorted(index):
        if handle.startswith("#"):
            continue
        if index[handle] == topic:
            return handle
    return None


def find_prior_statement(
    memory: MeetingMemory | None,
    topic: str,
    kinds: tuple[str, ...],
    *,
    exclude_meeting_id: str = "",
):
    """The most recent statement of one of `kinds` about `topic`, from ANOTHER meeting.

    Excluding the current meeting matters: memory is written before planning, so
    without this the "before" in a what-changed table would be the very statement
    that caused the comment. Refining a decision inside one meeting is not drift.
    """
    if memory is None or not topic:
        return None
    try:
        commitments = memory.find_prior_commitments(topic, limit=25)
    except Exception:  # noqa: BLE001
        return None
    candidates = [
        commitment
        for commitment in commitments
        if commitment.kind in kinds and commitment.meeting_id != exclude_meeting_id
    ]
    return candidates[-1] if candidates else None


# --- the guards: one branch per action kind ---------------------------------


def build_github_update(
    statement: Statement,
    memory: MeetingMemory | None,
    meeting: dict,
    verdict: dict | None,
) -> Action | None:
    """A comment on the issue this statement is about. Declines when no issue resolves."""
    issue_number = resolve_issue_number(statement, memory)
    if not issue_number:
        return None

    topic = _statement_topic(statement)
    kind = getattr(statement, "kind", "")
    claim = _one_line(getattr(statement, "claim", ""))

    before = ""
    after = claim
    what_changed = ""

    if verdict and verdict.get("conflict"):
        # The judge looked at the history and described the change in words. Its
        # before/after is better than anything a table can write, so use it.
        before = _one_line(verdict.get("before", ""))
        after = _one_line(verdict.get("after", "")) or claim
        what_changed = _one_line(verdict.get("what_changed", ""))
    elif kind in CHANGE_SHAPED_KINDS:
        # No verdict (or no conflict): fall back to a purely deterministic diff.
        # A prior statement of the same kind about the same topic, from an earlier
        # meeting, IS the before — no model needed to notice that.
        prior = find_prior_statement(
            memory, topic, (kind,), exclude_meeting_id=str(meeting.get("meeting_id", ""))
        )
        if prior is not None and summarize_prior(prior) != claim:
            before = summarize_prior(prior)
            what_changed = _describe_change(kind, prior, statement)
            if not what_changed:
                # The diff came back empty, which means the fields that CAN be
                # differed deterministically did not actually move. Do not label
                # the issue "decision-changed" and do not print a Before/After
                # table whose two halves say the same thing.
                before = ""

    if before and before.strip().casefold() == after.strip().casefold():
        before = ""  # a table with identical halves is noise on a public issue
        what_changed = ""

    labels = [LABEL_FROM_MEETING] + ([LABEL_DECISION_CHANGED] if before else [])
    # The judge's what_changed is written from the history and is the best line
    # available, so it leads. The deterministic fallback ("plan revised since
    # <date>") is NOT — it is a category, not content — so when it is all we
    # have, the headline says what was actually decided and the generic phrase
    # stays in the comment body where it belongs.
    headline = what_changed if (verdict and verdict.get("conflict")) else claim
    preview = f"Comment on #{issue_number}: {_one_line(headline or claim, 70)}"
    return Action(
        kind="github_update",
        payload={
            "issue_number": int(issue_number),
            "repo": config.github_repo(),
            "before": before,
            "after": after,
            "what_changed": what_changed,
            "meeting_title": _one_line(meeting.get("title", "")),
            "meeting_date": _one_line(meeting.get("date", "")),
            "labels": labels,
            "human_preview": preview,
        },
        # Meeting + issue + statement kind is a COMPLETE identity, and every part
        # of it is structural. `topic` used to be in here and is deliberately gone:
        # it is the one field the extractor is free to rephrase between runs, and
        # a rephrase would post the same what-changed comment on #2 twice.
        dedup_key=build_dedup_key(
            "github_update", meeting.get("meeting_id", ""), f"issue {issue_number}", kind
        ),
    )


def _describe_change(kind: str, prior, statement: Statement) -> str:
    """A short, deterministic 'what changed' line. Field diffs only — no prose model.

    Owners and dates are structured enough to diff by hand; a decision is not, so
    for a decision this says only that the plan was revised and lets the
    before/after table carry the content.
    """
    if kind == "assignment":
        previous_owner = _one_line(getattr(prior, "claim", "")).split(" ", 1)[0].rstrip(",")
        new_owner = _one_line(_entity_reference(statement, "person") or "")
        if previous_owner and new_owner:
            if previous_owner.casefold() == new_owner.casefold():
                # "owner Priya -> Priya" — a real render, on a public issue. The
                # previous owner is read from the first word of the prior claim,
                # so a claim that already leads with the NEW owner's name diffs
                # a name against itself. Nothing changed; say nothing.
                return ""
            return f"owner {previous_owner} -> {new_owner}"
        return "owner changed"
    if kind == "deadline":
        return "target date changed"
    date = _one_line(getattr(prior, "meeting_date", ""))
    return f"plan revised since {date}" if date else "plan revised in a later meeting"


def build_linear_create(
    statement: Statement,
    memory: MeetingMemory | None,
    meeting: dict,
) -> Action | None:
    """File a ticket for work that is tracked nowhere. Declines when it already exists."""
    if resolve_linear_identifier(statement, memory):
        return None  # it already has a ticket; filing a second one is the bug
    topic = _statement_topic(statement)
    claim = _one_line(getattr(statement, "claim", ""))
    title = _title_from_claim(claim)
    if not title:
        return None
    person = _entity_reference(statement, "person")
    return Action(
        kind="linear_create",
        payload={
            "title": title,
            "team_key": config.read_setting("LINEAR_TEAM_KEY", DEFAULT_LINEAR_TEAM_KEY),
            "assignee_name": _one_line(person) if person else "",
            "labels": [LABEL_FROM_MEETING],
            "meeting_title": _one_line(meeting.get("title", "")),
            "human_preview": f"Linear: create ticket “{_one_line(title, 60)}”",
        },
        # Meeting-scoped, like every other kind. An unscoped key put this action
        # in a GLOBAL namespace: has_memory_fired() matches the bare key, so once
        # any meeting had filed the webhook ticket, every later meeting's identical
        # beat was silently dropped — no error, no red card, nothing on the board
        # to explain the absence. Within one meeting the topic stabilizer is
        # demonstrably enough to stop a double-file across the two passes.
        dedup_key=build_dedup_key(
            "linear_create", meeting.get("meeting_id", ""), topic or title
        ),
    )


def build_linear_move(
    statement: Statement,
    memory: MeetingMemory | None,
    meeting: dict,
    *,
    sibling_text: str = "",
) -> Action | None:
    """Move a ticket that actually moved. The percent->state table decides where to."""
    identifier = resolve_linear_identifier(statement, memory)
    if not identifier:
        return None
    # Imported lazily so planner stays importable with no executor dependencies
    # loaded. choose_target_state / coerce_percent are pure table lookups — the
    # no-model rule is intact.
    from .executors.linear_move_executor import choose_target_state, coerce_percent

    percent = coerce_percent(_entity_reference(statement, "percent"))
    # SIBLINGS COUNT. One breath — "I'm about four-fifths through, should be in
    # review tomorrow" — gets split by the multi-claim extractor into a
    # progress_report and a pr_intent, and the review phrase leaves with the half
    # that has no percentage. choose_target_state then saw 80% with no
    # destination phrase, fell past the 90% In Review threshold, and announced a
    # move to In Progress — the state the ticket was already in. The ".2" suffix
    # says these claims are the same sentence, so read them as one.
    spoken = " ".join(
        part
        for part in (
            getattr(statement, "quote", ""),
            getattr(statement, "claim", ""),
            sibling_text,
        )
        if part
    )
    target_state = choose_target_state(percent, spoken)
    if not target_state:
        # A silent no-op here used to be invisible: the room named a ticket, the
        # extractor got it right, and nothing appeared on the board or in the log
        # to say why. Declining is fine; declining quietly is not.
        print(
            f"[planner] declined linear_move: {identifier} was named but nothing in "
            f"{_one_line(spoken, 60)!r} says it moved"
        )
        return None  # the table says it has not moved far enough to move the ticket
    return Action(
        kind="linear_move",
        payload={
            "linear_identifier": identifier,
            "target_state": target_state,
            "percent": percent,
            "title_hint": _statement_topic(statement),
            "meeting_title": _one_line(meeting.get("title", "")),
            "human_preview": f"Linear: {identifier} -> {target_state}",
        },
        dedup_key=build_dedup_key("linear_move", meeting.get("meeting_id", ""), identifier),
    )


def build_pull_request_stub(
    statement: Statement,
    memory: MeetingMemory | None,
    meeting: dict,
) -> Action | None:
    """Leave a draft PR and a branch waiting for someone who said they would open one."""
    topic = _statement_topic(statement)
    if not topic:
        return None
    if not mentions_pull_request(statement):
        # The extractor's pr_intent label is not on its own enough to open a real
        # branch. Under natural speech "I'd say I'm four-fifths through, I'll put
        # it up for review tomorrow" split into a progress report AND a pr_intent,
        # and the pr_intent half produced an unscripted live draft PR named
        # "WIP: streaming adapter" for work nobody asked to be PR'd. A ticket
        # identifier with no PR noun anywhere near it is a progress report.
        return None
    issue_number = resolve_issue_number(statement, memory)
    title = f"WIP: {topic}"
    payload = {
        "repo": config.github_repo(),
        "topic": topic,
        "title": title,
        "meeting_title": _one_line(meeting.get("title", "")),
        "human_preview": f"Draft PR: {title}",
    }
    if issue_number:
        payload["issue_number"] = int(issue_number)
    return Action(
        kind="pull_request_stub",
        payload=payload,
        dedup_key=build_dedup_key(
            "pull_request_stub", meeting.get("meeting_id", ""), topic
        ),
    )


# The words a person says when the thing they are correcting is code that is
# already up for review. Broader than _PULL_REQUEST_PHRASES on purpose — the
# room says "pull twenty-two", not "pull request twenty-two" — and safe to be
# broader because this route ALSO requires a concrete new value before it fires.
_PR_REFERENCE_PHRASES = (
    "pull request", "pull ", " pr ", "pr —", "pr,", "pr.", "the pr", "a pr",
    "pr for", "on the diff", "in review", "under review", "review on",
)

# A filename spoken out loud ("it's in join.css") narrows the diff search. Only
# an extension we would actually find in a PR, so "e.g." and "vs." do not match.
_FILENAME_PATTERN = re.compile(
    r"\b([\w./-]+\.(?:css|scss|js|jsx|ts|tsx|py|rb|go|rs|java|html|json|ya?ml|md|toml))\b",
    re.IGNORECASE,
)


def _spoken_file_hint(statement: Statement) -> str:
    """A filename the room actually said, or "". Pure."""
    spoken = f"{getattr(statement, 'quote', '')} {getattr(statement, 'claim', '')}"
    match = _FILENAME_PATTERN.search(spoken)
    return match.group(1) if match else ""


def mentions_pull_request_under_review(statement: Statement) -> bool:
    """True when the speaker's own words point at a pull request. Pure table."""
    spoken = f" {getattr(statement, 'quote', '')} {getattr(statement, 'claim', '')} ".lower()
    return any(phrase in spoken for phrase in _PR_REFERENCE_PHRASES)


def build_pr_review_suggestion(
    statement: Statement,
    memory: MeetingMemory | None,
    meeting: dict,
) -> Action | None:
    """A review with an inline suggestion on the PR the room just corrected.

    Fires ONLY on the narrow shape it is for: somebody names a pull request and
    gives a CONCRETE new value for something in it — "on Priya's join button PR,
    pull twenty-two, that green is wrong, it should be six B seven F nine nine".
    Three independent conditions have to hold, and the whole route declines if
    any of them does not:

        1. a new value was spoken (entity_refs.new_value). A correction with no
           value is an opinion, and an opinion does not belong on a diff.
        2. something to change is named (entity_refs.pr_topic, else the topic).
        3. the words point at a pull request — a spoken number, or PR language.

    The OLD value is passed through only when it was genuinely spoken. Usually
    it was not: the room says "that green is wrong" and the hex lives in the
    diff. That is the executor's problem to solve honestly (it says which lines
    it could not disambiguate) and not the planner's problem to guess at.
    """
    new_value = _one_line(_entity_reference(statement, "new_value") or "")
    if not new_value:
        return None
    pr_topic = _one_line(_entity_reference(statement, "pr_topic") or "") or _statement_topic(statement)
    if not pr_topic:
        return None
    spoken_number = _entity_reference(statement, "issue_number")
    if not spoken_number and not mentions_pull_request_under_review(statement):
        return None

    old_value = _one_line(_entity_reference(statement, "old_value") or "")
    payload = {
        "pr_number": int(spoken_number) if spoken_number else None,
        "pr_topic": pr_topic,
        "change_description": pr_topic,
        "old_value": old_value or None,
        "new_value": new_value,
        "file_hint": _spoken_file_hint(statement) or None,
        "repo": config.github_repo(),
        "meeting_title": _one_line(meeting.get("title", "")),
        "meeting_date": _one_line(meeting.get("date", "")),
        "human_preview": (
            f"PR review{f' on #{int(spoken_number)}' if spoken_number else ''}: "
            f"{_one_line(pr_topic, 40)} → {new_value}"
        ),
    }
    return Action(
        kind="pr_review_suggestion",
        payload=payload,
        # STRUCTURAL, not prose: the thing being changed and the value it is
        # changing from (or to, when the old value was never spoken). Both are
        # handles rather than sentences, so the live pass and the reconcile pass
        # agree even when the extractor rewords the claim between them. Scoped to
        # the meeting like every other kind, so next week's review of the same PR
        # is allowed to leave its own suggestion instead of being silently eaten.
        dedup_key=build_dedup_key(
            "pr_review_suggestion",
            meeting.get("meeting_id", ""),
            pr_topic,
            old_value or new_value,
        ),
    )


def build_slack_send(
    statement: Statement,
    memory: MeetingMemory | None,
    meeting: dict,
) -> Action | None:
    """Send the message someone promised to send. ONE per channel per meeting.

    The key carries neither the claim text nor the speaker, and both omissions
    were paid for: the two passes word the same promise differently, and — found
    on a real recording — the live captions call a person "Them" while the final
    transcript calls them "Speaker 1". Either one in the key turns one promised
    message into two sends. Two distinct Slack promises in one meeting collapse
    into one card instead; both still appear in the recap's ledger, and
    under-sending is the only safe direction to be wrong in.
    """
    text = _one_line(getattr(statement, "claim", ""))
    if not text:
        return None
    channel = _one_line(statement_channel(statement)) or "default-channel"
    meeting_id = str(meeting.get("meeting_id", ""))
    return Action(
        kind="slack_send",
        payload={
            "text": text,
            "meeting_title": _one_line(meeting.get("title", "")),
            "human_preview": f"Slack: {_one_line(text, 70)}",
        },
        dedup_key=build_dedup_key("slack_send", meeting_id, channel),
        regret_window_s=choose_regret_window("slack_send"),
    )


def statement_channel(statement: Statement) -> str:
    """The channel a message was promised to, when one was actually named.

    Nothing in the current extraction schema carries a channel, so this is always
    "" today and the executor falls back to the configured slack_channel secret.
    It exists so that the day entity_refs grows a channel, the key already knows
    where to find it instead of collapsing two channels into one card.
    """
    return _one_line(_entity_reference(statement, "channel") or "")


def build_email_send(
    statement: Statement,
    memory: MeetingMemory | None,
    meeting: dict,
) -> Action | None:
    """Send the follow-up email someone promised. Declines when no address resolves.

    A name is not an address. If the address book cannot turn "Div" into an
    address, this returns None and the promise shows up in the recap's commitment
    ledger instead — which is honest. Emailing a guessed address is the one
    failure that cannot be apologised for, so the guard runs HERE, at plan time,
    rather than letting a card fail on the board.
    """
    person = _one_line(_entity_reference(statement, "person") or "")
    text = _one_line(getattr(statement, "claim", ""))
    if not text:
        return None
    meeting_id = str(meeting.get("meeting_id", ""))
    action = Action(
        kind="email_send",
        payload={
            "person": person,
            "subject": f"Follow-up: {_subject_phrase(statement, text)}",
            "body_text": text,
            "meeting_title": _one_line(meeting.get("title", "")),
            "human_preview": f"Email {person or 'the room'}: {_one_line(text, 60)}",
        },
        dedup_key=build_dedup_key("email_send", meeting_id, person or "unaddressed"),
        regret_window_s=choose_regret_window("email_send"),
    )
    # Pure, stdlib-only address-book lookup. No SMTP connection is opened here.
    from .executors.email_send_executor import resolve_recipients

    try:
        recipients, _cc = resolve_recipients(action)
    except Exception:  # noqa: BLE001 — an unparseable book is a decline, not a crash
        recipients = []
    if not recipients:
        print(
            f"[planner] declined email_send: no address for {person or 'an unnamed person'} "
            "(set ADJOURN_ADDRESS_BOOK in adjourn/.env)"
        )
        return None
    action.payload["to"] = recipients
    return action


def build_calendar_hold(
    statement: Statement,
    memory: MeetingMemory | None,
    meeting: dict,
) -> Action | None:
    """Block time for a date someone committed to. Declines on an unresolvable phrase.

    Gated on statement KIND, never on the mere presence of deadline_text: a
    pr_intent that says "tonight" and a progress_report that says "tomorrow" both
    carry deadline_text, and holding time for either would fire a hold nobody
    asked for.
    """
    deadline_text = _one_line(_entity_reference(statement, "deadline_text") or "")
    if not deadline_text:
        return None
    # Pure date arithmetic — tables and timedeltas, no model, no network.
    from .executors.calendar_hold_executor import (
        DEFAULT_HOUR,
        DEFAULT_MINUTE,
        WEEKDAY_NUMBERS,
        resolve_deadline,
    )

    starts_at = resolve_deadline(deadline_text)
    if starts_at is None:
        print(f"[planner] declined calendar_hold: cannot resolve {deadline_text!r} to a date")
        return None

    is_review = _is_review_shaped(statement)
    if is_review and _names_a_bare_weekday(deadline_text, WEEKDAY_NUMBERS):
        if starts_at.date() == datetime.now().date():
            # "Let's review Friday" said ON a Friday means NEXT Friday. The
            # executor's same-day reading is correct for a deadline — "get it in
            # by Friday" on a Friday does mean today — but a review is
            # forward-looking by construction, and on demo day (Fri 21 Aug) that
            # reading produced a hold half an hour from now and called it
            # follow-through. Only this shape rolls; _weekday_offset is untouched.
            # Back to the default hour too: the executor had slid this hold past
            # the grace window because 10:00 today was already gone, and that
            # reason expires along with the same-day reading.
            starts_at = (starts_at + timedelta(days=7)).replace(
                hour=DEFAULT_HOUR, minute=DEFAULT_MINUTE, second=0, microsecond=0
            )
            print(
                f"[planner] {deadline_text!r} is today and the beat is a review — "
                f"holding the next one, {starts_at:%a %-d %b}"
            )

    topic = _statement_topic(statement)
    # The verb, not just the topic. Both demo holds are about the cache layer, so
    # a topic-only title rendered two identical cards ("Hold: cache layer") whose
    # only difference was a date buried mid-headline — the most confusing pair on
    # the board. Kind plus spoken verb separates them deterministically.
    verb = "Review" if is_review else ("Ship" if getattr(statement, "kind", "") == "deadline" else "Hold")
    title = f"{verb}: {topic}" if topic else _one_line(getattr(statement, "claim", ""), 60)
    return Action(
        kind="calendar_hold",
        payload={
            "title": title,
            "deadline_text": deadline_text,
            "starts_at": starts_at.isoformat(timespec="seconds"),
            "duration_minutes": DEFAULT_HOLD_DURATION_MINUTES,
            "meeting_title": _one_line(meeting.get("title", "")),
            "human_preview": f"Calendar hold {starts_at:%a %-d %b %H:%M}: {_one_line(title, 50)}",
        },
        # Keyed on the meeting and the RESOLVED date, and on NOTHING the model
        # chose. "the twenty-fifth" and "the 25th" are the same hold; so are two
        # runs where the extractor named the same work "cache layer" once and
        # "caching" the next time. That second case is not hypothetical — it
        # double-fired a hold during rehearsal, because the topic is the one part
        # of a statement a model is free to rephrase. One meeting, one date, one
        # block of time.
        dedup_key=build_dedup_key(
            "calendar_hold", meeting.get("meeting_id", ""), starts_at.date().isoformat(),
        ),
    )


def build_recap_page(statements: list[Statement], meeting: dict) -> Action:
    """The local recap. Fires once per meeting, LAST, and rewrites itself on reconcile.

    `executed` is filled in by the orchestrator immediately before firing, because
    the planner cannot know what the other executors did — it plans, it does not
    watch. Everything else in the payload is known at plan time.
    """
    meeting_id = str(meeting.get("meeting_id", "")) or "unknown-meeting"
    return Action(
        kind="recap_page",
        payload={
            "meeting_id": meeting_id,
            "meeting_title": _one_line(meeting.get("title", "")) or meeting_id,
            "meeting_date": _one_line(meeting.get("date", "")),
            "statements": [_statement_dict(statement) for statement in statements],
            "executed": [],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "human_preview": f"Recap: {_one_line(meeting.get('title', '')) or meeting_id}",
        },
        dedup_key=build_dedup_key("recap_page", meeting_id),
        meeting_id=meeting_id,
    )


def _statement_dict(statement: Statement) -> dict:
    """Statement -> dict, tolerating a duck-typed stand-in (the tests use one)."""
    to_dict = getattr(statement, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(statement, dict):
        return statement
    row = {
        "segment_id": getattr(statement, "segment_id", ""),
        "speaker": getattr(statement, "speaker", ""),
        "topic": getattr(statement, "topic", ""),
        "claim": getattr(statement, "claim", ""),
        "kind": getattr(statement, "kind", ""),
        "quote": getattr(statement, "quote", ""),
    }
    # The recap is where a held-back statement is allowed to live, so it needs to
    # know it was held back and why. Only emitted when set.
    for key in ("negated", "rejected", "third_party", "subject", "restraint_reason"):
        value = getattr(statement, key, None)
        if value:
            row[key] = value
    return row


_BUILDERS = {
    "github_update": build_github_update,
    "linear_create": build_linear_create,
    "linear_move": build_linear_move,
    "pull_request_stub": build_pull_request_stub,
    "pr_review_suggestion": build_pr_review_suggestion,
    "slack_send": build_slack_send,
    "email_send": build_email_send,
    "calendar_hold": build_calendar_hold,
}


# --- contract ---------------------------------------------------------------


def plan(
    statements: list[Statement],
    memory: MeetingMemory | None = None,
    *,
    meeting: dict | None = None,
    conflict_verdicts: dict[str, dict] | None = None,
) -> list[Action]:
    """Map statements to actions. PURE AND DETERMINISTIC — no model, no network.

    `memory` is read-only here: it resolves a topic to an issue number, tells you
    whether a Linear ticket already exists for this work item, and surfaces prior
    commitments. Reads only — the orchestrator does the writing.

    Returns actions in fire order, each with a dedup_key from build_dedup_key()
    and a regret window from choose_regret_window(). Duplicate keys within one
    plan are collapsed before returning, and the recap page is always last.
    """
    meeting = dict(meeting or {})
    verdicts = dict(conflict_verdicts or {})
    meeting_id = str(meeting.get("meeting_id", ""))

    sibling_text_by_statement = build_sibling_text_index(statements or [])

    actions: list[Action] = []
    for statement in statements or []:
        kind = getattr(statement, "kind", "")
        candidates = ROUTING_TABLE.get(kind, ())
        if not candidates:
            continue
        held_back = restraint_hold(statement)
        if held_back:
            # THE PRODUCT IS THE RESTRAINT, and a decline nobody can see is
            # indistinguishable from a pipeline that never ran. Say it out loud.
            print(
                f"[planner] declined {'/'.join(candidates)} on "
                f"{getattr(statement, 'segment_id', '?')}: {held_back}"
            )
            continue
        produced_kinds: set[str] = set()
        for action_kind in candidates:
            if any(action_kind in SUPPRESSED_BY.get(winner, ()) for winner in produced_kinds):
                continue
            action = build_action_for(
                statement,
                action_kind,
                memory,
                meeting=meeting,
                verdict=verdicts.get(_verdict_key(statement)),
                sibling_text=sibling_text_by_statement.get(
                    str(getattr(statement, "segment_id", "")), ""
                ),
            )
            if action is None:
                continue
            produced_kinds.add(action_kind)
            action.quote = getattr(statement, "quote", "") or ""
            action.speaker = getattr(statement, "speaker", "") or ""
            action.segment_id = getattr(statement, "segment_id", "") or ""
            action.source = getattr(statement, "source", "live") or "live"
            action.meeting_id = meeting_id
            actions.append(action)

    # ALWAYS recap, even with nothing to report. A meeting that legitimately
    # yields no follow-through used to write no page and leave the board reading
    # "0 actions" under an empty header — indistinguishable from the orchestrator
    # not running at all, which is the one thing you cannot tell from the stage.
    # "This is a system that mostly does nothing, on purpose" is a claim worth
    # being able to SHOW; one honest empty card is the difference between a
    # confident claim and an apparent hang.
    if meeting_id or statements:
        actions.append(build_recap_page(list(statements or []), meeting))
    return collapse_duplicate_actions(actions)


def build_sibling_text_index(statements: list[Statement]) -> dict[str, str]:
    """segment_id -> the spoken words of the OTHER claims split from that segment.

    The extractor suffixes a second claim from one segment as "<id>.2", so the
    base id is the sentence and the suffixes are its halves. Pure and
    order-independent.
    """
    by_base: dict[str, list[Statement]] = {}
    for statement in statements:
        segment_id = str(getattr(statement, "segment_id", ""))
        if not segment_id:
            continue
        by_base.setdefault(segment_id.split(".", 1)[0], []).append(statement)

    index: dict[str, str] = {}
    for group in by_base.values():
        if len(group) < 2:
            continue
        for statement in group:
            segment_id = str(getattr(statement, "segment_id", ""))
            index[segment_id] = " ".join(
                part
                for other in group
                if other is not statement
                for part in (
                    getattr(other, "quote", ""),
                    getattr(other, "claim", ""),
                )
                if part
            )
    return index


def _verdict_key(statement: Statement) -> str:
    """Verdicts are keyed by segment_id, with the multi-claim suffix stripped."""
    segment_id = str(getattr(statement, "segment_id", ""))
    return segment_id.split(".", 1)[0] if segment_id else ""


def build_action_for(
    statement: Statement,
    action_kind: str,
    memory: MeetingMemory | None = None,
    *,
    meeting: dict | None = None,
    verdict: dict | None = None,
    sibling_text: str = "",
) -> Action | None:
    """Build one Action of `action_kind` from `statement`, or None if the guard fails.

    This is where the table's guard conditions live: a github_update needs a
    resolvable issue, a calendar_hold needs a parseable deadline, an email_send
    needs a resolvable address. Returning None is normal and means "the table
    declined".

    `sibling_text` is the spoken words of the OTHER claims the extractor split out
    of the same segment (the ".2" suffix ties them together). Only linear_move
    reads it, and only to look for a destination phrase — see build_linear_move.
    """
    builder = _BUILDERS.get(action_kind)
    if builder is None:
        return None
    meeting = dict(meeting or {})
    try:
        if action_kind == "github_update":
            return builder(statement, memory, meeting, verdict)
        if action_kind == "linear_move":
            return builder(statement, memory, meeting, sibling_text=sibling_text)
        return builder(statement, memory, meeting)
    except Exception as error:  # noqa: BLE001 — one bad guard must not kill the plan
        print(f"[planner] {action_kind} guard failed on "
              f"{getattr(statement, 'segment_id', '?')}: {error}")
        return None


def collapse_duplicate_actions(actions: list[Action]) -> list[Action]:
    """Keep the first action per dedup_key, preserving order. Deterministic."""
    seen: set[str] = set()
    collapsed: list[Action] = []
    for action in actions:
        key = action.dedup_key
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        collapsed.append(action)
    return collapsed


def describe_routing_table() -> dict:
    """The table, for the README and the board's 'how this decided' panel."""
    return {
        "statement_kinds": list(ROUTING_TABLE),
        "action_kinds": list(ACTION_KINDS),
        "routes": {kind: list(targets) for kind, targets in ROUTING_TABLE.items()},
        "regret_windows": dict(REGRET_WINDOW_BY_KIND),
        "suppressed_by": {kind: list(losers) for kind, losers in SUPPRESSED_BY.items()},
        "restraint_gates": [flag for flag, _reason in RESTRAINT_FLAG_REASONS],
    }


def describe_plan(actions: list[Action]) -> str:
    """A human-readable plan, for logs and for `-m adjourn.planner --demo`."""
    if not actions:
        return "(no actions)"
    lines = []
    for action in actions:
        window = f"  [{action.regret_window_s}s window]" if action.regret_window_s else ""
        lines.append(
            f"  {action.kind:<18} {action.payload.get('human_preview', '')}{window}\n"
            f"      key: {action.dedup_key}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="adjourn.planner")
    parser.add_argument("--demo", action="store_true",
                        help="plan the demo fixture and print the actions (no firing)")
    parser.add_argument("--fixture", default="agi-living-room")
    arguments = parser.parse_args()

    if not arguments.demo:
        print(json.dumps(describe_routing_table(), indent=2))
        raise SystemExit(0)

    from . import extraction, memory_store

    fixture_statements = extraction.load_fixture_statements(arguments.fixture)
    opened_memory = None
    try:
        opened_memory = memory_store.open_memory()
    except Exception as error:  # noqa: BLE001
        print(f"[planner] no memory ({error}) — planning without recall")
    planned = plan(
        fixture_statements,
        opened_memory,
        meeting={"meeting_id": arguments.fixture, "title": arguments.fixture, "date": ""},
    )
    print(f"\n{len(fixture_statements)} statements -> {len(planned)} actions\n")
    print(describe_plan(planned))
    if opened_memory is not None:
        opened_memory.close()
