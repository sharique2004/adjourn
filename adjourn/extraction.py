"""Extraction — the ONLY place in Adjourn where a model runs.

A model reads transcript segments and returns Statements. That is all it does.
It does not decide whether to act, what to send, or to whom — `planner.py` does
that with a table. Keeping the model on this side of the line is the whole
architectural claim of the product.

Engine order (config.extraction_engine(), with an availability probe under it):
    1. "claude"   — the Claude CLI, sandboxed exactly like MeetingScribe does it.
    2. "gemini"   — gemini-3.5-flash, schema-locked (ported from drift/extractor.py).
    3. "fixtures" — canned statements from adjourn/fixtures/. The floor. Always works.

Every engine goes through the SAME post-processing — anti-hallucination filter,
multi-claim suffixing, verbatim-quote attachment, speaker correction — so a
statement from the fixtures floor is indistinguishable downstream from one the
Claude CLI produced. That is the sim-mode discipline applied to understanding:
identical code path, honest badge (`Statement.engine`).

Contract:
    extract_statements(segments, meeting_title, source="live"|"final") -> list[Statement]

Segment shape (produced by meetingscribe_source):
    {"segment_id": "L12", "speaker": "You", "track": "mic",
     "start": 41.2, "end": 46.8, "text": "..."}

This module also carries the conflict judge (drift's Historian), because it is
the second and last model call in the system and belongs on the same side of the
line: it DESCRIBES a change between two statements. Whether that description
becomes a GitHub comment is, again, the planner's table.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from . import config, prompts, secrets_store

# --- statement schema -------------------------------------------------------

# Extended beyond drift's four kinds. Every kind here must have a row in
# planner.ROUTING_TABLE, or the planner will silently drop it.
STATEMENT_KINDS: tuple[str, ...] = (
    "decision",             # a choice or plan is made or changed
    "update",               # status on existing work
    "assignment",           # ownership of work moves onto a person
    "question",             # an open question raised
    "ticket_request",       # "let's file a ticket for X"
    "progress_report",      # "X is 70% done", "the migration is halfway"
    "message_commitment",   # "I'll ping the team in Slack"
    "email_commitment",     # "I'll email the vendor tonight"
    "deadline",             # "by Friday", "before the demo"
    "pr_intent",            # "I'll open a PR for that"
)

SOURCE_LIVE = "live"    # from /api/live captions — fast path, fires within seconds
SOURCE_FINAL = "final"  # from meeting.json — reconcile path, better text and speakers

ENGINE_CLAUDE = "claude"
ENGINE_GEMINI = "gemini"
ENGINE_FIXTURES = "fixtures"

# Gemini keeps drift's 8, where the batch size was about schema-lock reliability
# and topic-vocabulary control on a free-tier model. Claude now matches it.
# MEASURED, not guessed: a 24-segment batch took ~90s on this machine while an
# 8-segment batch takes ~15s, so the per-call overhead never dominated the way a
# bigger batch assumed it would. Small batches plus real parallelism is the fast
# combination — a 28-segment meeting went from 113s to under 40s on this change.
CLAUDE_BATCH_SIZE = 8
GEMINI_BATCH_SIZE = 8  # ported from drift/extractor.py

# Independent CLI calls, run at once. Measured: one call is ~15s regardless of
# batch size, so a 40-minute meeting (14 batches) is 3.5 minutes serial and under
# a minute at four workers. Four is deliberate — each worker is a `claude`
# process with its own sandbox, and more of them starts costing more in process
# startup than it saves in wall clock.
CLAUDE_PARALLEL_BATCHES = 4

# How many topics memory must already know before the first batch stops running
# alone to seed the vocabulary. Below this, the meeting is the only source of
# topic names and the seeding round trip earns its cost.
MINIMUM_SEEDED_VOCABULARY = 3

GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_RETRY_DELAYS = (8, 16, 32)  # ported from drift/extractor.py — 429 backoff
CLAUDE_MODEL = "sonnet"
# A batch that has not answered in 75s is not going to. With --setting-sources ""
# the whole 28-segment demo transcript extracts in ~25s across four batches, and
# a single batch measures ~12s; 240s only ever bought four minutes of silence in
# front of an audience before producing nothing. 75s still leaves room for the
# one repair retry inside a gap a person can talk through.
CLAUDE_TIMEOUT_SECONDS = int(config.read_integer_setting("ADJOURN_CLAUDE_TIMEOUT_SECONDS", 75))

# Where `claude` lives when it is not on PATH. Order matters: PATH wins, then a
# user install, then Homebrew (it is at /opt/homebrew/bin/claude on this Mac).
CLAUDE_BINARY_FALLBACK_PATHS: tuple[str, ...] = (
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "/opt/homebrew/opt/claude-code/bin/claude",
)


@dataclass
class EntityReferences:
    """Concrete handles the planner needs to route a statement. All optional.

    Only ever populated from what was ACTUALLY said. The model must not infer an
    issue number, a person, or a date that is not in the words.
    """

    issue_number: int | None = None
    linear_identifier: str | None = None
    person: str | None = None
    deadline_text: str | None = None
    percent: int | None = None
    # --- pull-request review handles (additive; every consumer that predates
    # them keeps working because to_dict() still omits None). A room that
    # corrects a concrete value on someone's open PR — "that green is wrong, it
    # should be our slate blue, six B seven F nine nine" — carries the WHAT
    # ("join button"), the NEW value, and the OLD value only when the old value
    # was genuinely said out loud. The old value usually lives in the diff and
    # not in the transcript, and inventing it here would be a guess that lands
    # as a code suggestion on someone's pull request.
    pr_topic: str | None = None
    old_value: str | None = None
    new_value: str | None = None

    def to_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def from_dict(cls, data: dict | None) -> EntityReferences:
        data = data or {}
        return cls(
            issue_number=data.get("issue_number"),
            linear_identifier=data.get("linear_identifier"),
            person=data.get("person"),
            deadline_text=data.get("deadline_text"),
            percent=data.get("percent"),
            pr_topic=data.get("pr_topic"),
            old_value=data.get("old_value"),
            new_value=data.get("new_value"),
        )


@dataclass
class Statement:
    """One distinct claim about a work item, tied to the segment it came from.

    segment_id  — copied verbatim from the input segment. Never invented. The
                  anti-hallucination filter drops anything with an unknown id.
                  Multiple claims from one segment get ".2", ".3" suffixes.
    speaker     — display name from the segment.
    topic       — short lowercase key naming the work item, reused verbatim
                  across the meeting ("cache layer", "oauth migration").
    claim       — one self-contained sentence with the specifics in it.
    kind        — one of STATEMENT_KINDS.
    entity_refs — concrete handles (issue number, person, deadline text, ...).
    quote       — the verbatim segment text, carried through to ExecutorResult.quote.
    source      — "live" or "final".
    engine      — which engine produced this: "claude", "gemini", or "fixtures".
                  The understanding-side equivalent of the live/sim badge: the
                  board can say where a card's reasoning came from, and a demo
                  running on the fixtures floor never pretends otherwise.

    THE RESTRAINT FIELDS (negated / rejected / third_party / subject /
    restraint_reason) are ADDITIVE and all default to falsy, so every consumer
    written before them keeps working and to_dict() emits them only when one is
    actually set. They exist because the product's thesis is that most talk
    should produce nothing, and the three ways a sentence LOOKS actionable
    without BEING actionable are: it is negated ("don't email the client yet"),
    it was rejected in the room ("we could file a ticket but let's not"), or it
    is somebody else's commitment being relayed ("Div said he'd send the deck").
    A statement carrying any of them is recap-and-memory only — see the gate at
    the top of planner.plan().
    """

    segment_id: str
    speaker: str
    topic: str
    claim: str
    kind: str
    entity_refs: EntityReferences = field(default_factory=EntityReferences)
    quote: str = ""
    source: str = SOURCE_LIVE
    engine: str = ENGINE_FIXTURES

    negated: bool = False
    rejected: bool = False
    third_party: bool = False
    subject: str = ""          # who the claim is ABOUT, when that is not the speaker
    restraint_reason: str = ""  # the cue that fired, for the decline log and the recap

    @property
    def fires_no_work(self) -> bool:
        """True when this statement may inform the recap and memory and NOTHING else."""
        return bool(self.negated or self.rejected or self.third_party)

    def to_dict(self) -> dict:
        payload = {
            "segment_id": self.segment_id,
            "speaker": self.speaker,
            "topic": self.topic,
            "claim": self.claim,
            "kind": self.kind,
            "entity_refs": self.entity_refs.to_dict(),
            "quote": self.quote,
            "source": self.source,
            "engine": self.engine,
        }
        # Emitted only when set, so a statement with nothing to flag serializes
        # byte-identically to the way it did before these fields existed.
        for key, value in (
            ("negated", self.negated),
            ("rejected", self.rejected),
            ("third_party", self.third_party),
            ("subject", self.subject),
            ("restraint_reason", self.restraint_reason),
        ):
            if value:
                payload[key] = value
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> Statement:
        return cls(
            segment_id=data["segment_id"],
            speaker=data.get("speaker", ""),
            topic=data.get("topic", ""),
            claim=data.get("claim", ""),
            kind=data.get("kind", "update"),
            entity_refs=EntityReferences.from_dict(data.get("entity_refs")),
            quote=data.get("quote", ""),
            source=data.get("source", SOURCE_LIVE),
            engine=data.get("engine", ENGINE_FIXTURES),
            negated=bool(data.get("negated", False)),
            rejected=bool(data.get("rejected", False)),
            third_party=bool(data.get("third_party", False)),
            subject=data.get("subject", "") or "",
            restraint_reason=data.get("restraint_reason", "") or "",
        )


# --- validation models (the model's reply is untrusted until it passes here) --


class EntityReferencesModel(BaseModel):
    """Pydantic mirror of EntityReferences, used to validate a model's reply."""

    issue_number: int | None = None
    linear_identifier: str | None = None
    person: str | None = None
    deadline_text: str | None = None
    percent: int | None = None
    pr_topic: str | None = None
    old_value: str | None = None
    new_value: str | None = None


class StatementModel(BaseModel):
    segment_id: str
    speaker: str = ""
    topic: str = ""
    claim: str
    kind: Literal[
        "decision", "update", "assignment", "question", "ticket_request",
        "progress_report", "message_commitment", "email_commitment",
        "deadline", "pr_intent",
    ]
    entity_refs: EntityReferencesModel = Field(default_factory=EntityReferencesModel)


class ExtractionModel(BaseModel):
    """Wrapper for the engine's response: {"statements": [...]}."""

    statements: list[StatementModel] = Field(default_factory=list)


class VerdictModel(BaseModel):
    """Conflict judge reply. Ported from drift/detector.py."""

    conflict: bool = False
    before: str = ""
    after: str = ""
    what_changed: str = ""


# --- engine availability ----------------------------------------------------


def find_claude_binary() -> str | None:
    """Absolute path to the `claude` CLI, or None.

    PATH first (`shutil.which`), then the usual install locations. Mirrors
    MeetingScribe's own lookup so both agree about whether Claude is installed —
    a disagreement there is exactly the bug MeetingScribe's find_claude()
    docstring warns about.
    """
    found = shutil.which("claude")
    if found:
        return found
    for candidate in CLAUDE_BINARY_FALLBACK_PATHS:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def is_claude_available() -> bool:
    """True when the Claude CLI binary exists. Does not check that it is signed in."""
    return find_claude_binary() is not None


def is_gemini_available() -> bool:
    """True when a Gemini key is provisioned and the SDK imports."""
    if not secrets_store.has_secret(secrets_store.GEMINI_API_KEY):
        return False
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_engine_order(preferred: str | None = None) -> list[str]:
    """Engines to try, in order, given config and what is actually installed.

    The preferred engine goes first if it is usable; the rest follow in the
    fixed claude -> gemini -> fixtures order. Fixtures are ALWAYS last and are
    never filtered out — they are the floor, and the floor cannot be missing.
    """
    preferred = (preferred or config.extraction_engine()).lower()
    usable = {
        ENGINE_CLAUDE: is_claude_available(),
        ENGINE_GEMINI: is_gemini_available(),
        ENGINE_FIXTURES: True,
    }
    order = [engine for engine in (ENGINE_CLAUDE, ENGINE_GEMINI) if usable.get(engine)]
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    elif preferred == ENGINE_FIXTURES:
        order = []
    return [*order, ENGINE_FIXTURES]


# --- JSON rescue ------------------------------------------------------------

_JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json_object(text: str) -> dict:
    """First balanced {...} in a reply, tolerant of fences and stray prose.

    # ported concept from ~/MeetingScribe/summarize.py:_extract_json
    String-aware, so a brace inside a quoted claim cannot fool the depth
    counter — which matters here because claims routinely contain code and
    punctuation. Raises ValueError when there is no parseable object.
    """
    text = str(text).strip()
    fenced = _JSON_FENCE_PATTERN.search(text)
    if fenced:
        text = fenced.group(1)
    # EVERY candidate brace, not just the first. Committing to text.find("{") made
    # a single brace anywhere in a preamble fatal for the whole batch — and a
    # preamble is exactly what a model produces when it wants to comment before
    # answering ("I looked at my context {note: ...} ... {\"statements\": []}").
    # A batch-1 failure falls all the way through to fixtures, and a real
    # recording has no fixture, so one stray brace used to mean an empty board.
    failure: Exception | None = None
    for start in _brace_positions(text):
        try:
            return _parse_balanced_object(text, start)
        except (ValueError, json.JSONDecodeError) as error:
            failure = error
            continue
    raise ValueError(f"no parseable JSON object in the reply ({failure})")


def _brace_positions(text: str) -> list[int]:
    """Every index of "{" in the text, in order. Cheap; replies are small."""
    positions = []
    index = text.find("{")
    while index != -1:
        positions.append(index)
        index = text.find("{", index + 1)
    if not positions:
        raise ValueError("no JSON object in the reply")
    return positions


def _parse_balanced_object(text: str, start: int) -> dict:
    """Parse the balanced {...} beginning at `start`. String-aware."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : index + 1])
    raise ValueError("unbalanced JSON in the reply")


# --- the Claude CLI transport ------------------------------------------------


def run_claude_prompt(prompt: str, *, timeout_seconds: int = CLAUDE_TIMEOUT_SECONDS) -> str:
    """One sandboxed `claude -p` call. Returns the model's raw reply text.

    # ported from ~/MeetingScribe/summarize.py:1447 (claude_sandbox) and :1531
    # (_summarize_claude)

    The transcript is untrusted input: anyone in the meeting can say anything,
    and a document read aloud can seed the text. Five things keep the CLI inert:

        --tools ""            no built-in tools at all
        --strict-mcp-config   ignore the user's own MCP servers
        --mcp-config <file>   ...and use this {"mcpServers": {}} instead
        --setting-sources ""  load NO settings file, so no hook can run
        cwd=<empty temp dir>  no project CLAUDE.md to inherit

    WHY --setting-sources IS LOAD-BEARING, not belt-and-braces: an empty cwd
    does NOT suppress ~/.claude/settings.json. This machine registers a
    UserPromptSubmit hook and a Stop hook there, and both fired on every batch —
    the first ran a MongoDB Atlas $vectorSearch over the prompt (i.e. over the
    meeting transcript) and injected past-session "decisions already made" back
    into the extractor's context; the second uploaded distilled memories after
    the call. That falsified the "the transcript never left this machine" line
    the executors print onto public GitHub comments, AND contaminated the
    conflict beat by feeding the extractor an answer that did not come from
    Adjourn's own graph. Loading no setting sources closes both. It also more
    than halves the wall clock (63.7s -> 25.4s over the 28-segment demo
    transcript) because there is no retrieval round trip per batch.

    NOT --bare: it forces ANTHROPIC_API_KEY auth and breaks OAuth sign-in.

    ORDERING TRAP: --tools and --mcp-config are VARIADIC and swallow every
    following non-flag argument, so they must come LAST and the prompt must go
    on STDIN. Passing the prompt positionally makes the CLI eat it as another
    config path and exit 1 with empty stdout.
    """
    binary = find_claude_binary()
    if binary is None:
        raise RuntimeError("claude CLI not found on PATH or in the usual locations")
    with tempfile.TemporaryDirectory(prefix="adjourn-claude-") as temporary_directory:
        mcp_config_path = Path(temporary_directory) / "mcp.json"
        mcp_config_path.write_text('{"mcpServers": {}}', encoding="utf-8")
        sandbox_cwd = Path(temporary_directory) / "cwd"  # config stays outside the cwd
        sandbox_cwd.mkdir()
        argv = [
            binary, "-p", "--setting-sources", "",
            "--output-format", "json", "--model", CLAUDE_MODEL,
            "--tools", "", "--strict-mcp-config", "--mcp-config", str(mcp_config_path),
        ]
        completed = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(sandbox_cwd),
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        last_line = detail.splitlines()[-1] if detail else "unknown error"
        raise RuntimeError(f"claude CLI exited {completed.returncode}: {last_line}")
    envelope = json.loads(completed.stdout)
    return str(envelope.get("result") or "")


def _validate_extraction(reply_text: str) -> ExtractionModel:
    """Parse and pydantic-validate one engine reply, or raise with a usable message."""
    payload = extract_json_object(reply_text)
    return ExtractionModel.model_validate(payload)


def extract_with_claude_cli(
    segments: list[dict],
    meeting_title: str,
    *,
    known_topics: list[str] | None = None,
    timeout_seconds: int = CLAUDE_TIMEOUT_SECONDS,
) -> list[Statement]:
    """Primary engine: the Claude CLI, sandboxed the way MeetingScribe does it.

    The first batch runs alone to seed the topic vocabulary; the rest run
    CONCURRENTLY (CLAUDE_PARALLEL_BATCHES workers), because a CLI call costs the
    same ~15s whatever is in it and a real meeting is many batches. On a parse or
    validation failure the batch is retried ONCE with the parser's complaint
    appended (build_repair_prompt) — a targeted retry fixes a malformed reply far
    more often than a blind one. A second failure skips that batch; a failure of
    the FIRST batch raises and the caller falls to the next engine down.
    """
    return _extract_in_batches(
        segments,
        meeting_title,
        known_topics=known_topics,
        batch_size=CLAUDE_BATCH_SIZE,
        engine=ENGINE_CLAUDE,
        call_engine=lambda prompt: run_claude_prompt(prompt, timeout_seconds=timeout_seconds),
        parallel_workers=CLAUDE_PARALLEL_BATCHES,
    )


# --- the Gemini transport ---------------------------------------------------


_gemini_client = None


def _get_gemini_client():
    """Create the Gemini client on first use. # ported from drift/extractor.py"""
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        key = secrets_store.get_secret(secrets_store.GEMINI_API_KEY)
        _gemini_client = genai.Client(api_key=key) if key else genai.Client()
    return _gemini_client


def run_gemini_prompt(prompt: str, schema: dict) -> str:
    """One schema-constrained generate_content call with 429 backoff (8/16/32s).

    # ported from drift/extractor.py:_generate_json
    Free tier is roughly 10 requests per minute, so batches are serialized and
    never fanned out — a parallel burst just spends the whole quota on retries.
    """
    from google.genai import errors, types

    attempt = 0
    while True:
        try:
            response = _get_gemini_client().models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=schema,
                ),
            )
            return response.text
        except errors.ClientError as error:
            code = getattr(error, "code", None) or getattr(error, "status_code", None)
            if code != 429 or attempt >= len(GEMINI_RETRY_DELAYS):
                raise
            delay = GEMINI_RETRY_DELAYS[attempt]
            attempt += 1
            print(f"[scribe] rate limited (429) — retry {attempt}/{len(GEMINI_RETRY_DELAYS)} in {delay}s")
            time.sleep(delay)


def extract_with_gemini(
    segments: list[dict],
    meeting_title: str,
    *,
    known_topics: list[str] | None = None,
) -> list[Statement]:
    """Fallback engine: gemini-3.5-flash, schema-constrained.

    # ported from drift/extractor.py — BATCH_SIZE 8, response_json_schema,
    # 429 backoff at 8/16/32s, serial batches.
    Key comes from secrets_store.get_secret("gemini_api_key").
    """
    schema = ExtractionModel.model_json_schema()
    return _extract_in_batches(
        segments,
        meeting_title,
        known_topics=known_topics,
        batch_size=GEMINI_BATCH_SIZE,
        engine=ENGINE_GEMINI,
        call_engine=lambda prompt: run_gemini_prompt(prompt, schema),
    )


# --- the shared batch loop (both engines run through this) -------------------


def _report_batch_progress(index: int, total: int, lines: int) -> None:
    """Push one batch boundary onto the board's rail. Never raises.

    Imported LAZILY because orchestrator imports this module; a module-level
    import would be a cycle. Wrapped whole because the rail is a nicety and
    extraction is not — a status file must never take a meeting down with it.

    This is the call that makes the brief's "rail must move, not freeze idle"
    literally true: it fires from inside the loop at each batch boundary, so the
    bar advances while the model is still working, rather than jumping from
    empty to full when the pass ends.
    """
    try:
        from . import orchestrator

        # MERGE, do not replace. report_pipeline swaps the whole extraction
        # block, so building a fresh status here would blank `source` — the
        # live/final/fixture label the board prints next to the bar — for the
        # entire time the bar is actually moving.
        current = orchestrator.read_pipeline_status().extraction
        current.phase = orchestrator.STAGE_RUNNING
        current.detail = f"extracting… batch {index} of {total}"
        current.batch_index = index
        current.batch_total = total
        orchestrator.report_pipeline(extraction=current)
        orchestrator.report_pipeline_event(
            stage="extraction",
            tone="note",
            label="batch",
            text=f"batch {index}/{total} · {lines} lines",
        )
    except Exception:  # noqa: BLE001 — the rail is never worth a failed meeting
        pass


def _report_statement(statement: Statement) -> None:
    """One rail row per statement as it lands. Never raises.

    The restraint rows matter as much as the action rows — "eighteen of
    twenty-eight lines produced nothing at all" is the product — so a negated,
    rejected or third-party statement is emitted as `ignore` with the reason
    rather than dropped from the narration.
    """
    try:
        from . import orchestrator

        orchestrator.report_pipeline_event(
            stage="extraction",
            tone="ignore" if statement.fires_no_work else "act",
            label=statement.kind or "statement",
            text=statement.claim or statement.quote,
        )
    except Exception:  # noqa: BLE001
        pass


def _run_one_batch(
    batch: list[dict],
    meeting_title: str,
    seen_topics: list[str],
    *,
    engine: str,
    call_engine,
    label: str,
) -> list[Statement]:
    """One batch through the engine, validated, filtered. Raises nothing the caller cares about."""
    # The model sees the DISPLAY name, never the mic-track label. Otherwise it
    # writes the label into its own prose — "You will Slack the channel the
    # summary" — and that sentence is the body of the message that actually goes
    # to Slack, where "You" means nobody. Mapping it here rather than after the
    # fact means the claim is right at the source; the segment list itself is
    # untouched, so quote and speaker still come from the raw transcript.
    labelled = [
        {**segment, "speaker": speaker_display_name(segment.get("speaker", ""))}
        for segment in batch
    ]
    prompt = prompts.build_extraction_prompt(labelled, meeting_title, seen_topics)
    print(f"[scribe] {engine}: batch {label} ({len(batch)} segments)")
    try:
        extraction = _validate_extraction(call_engine(prompt))
    except (ValueError, ValidationError, json.JSONDecodeError) as error:
        print(f"[scribe] {engine}: batch {label} reply rejected ({error.__class__.__name__}) — one repair retry")
        repair_prompt = prompts.build_repair_prompt(prompt, str(error))
        extraction = _validate_extraction(call_engine(repair_prompt))
    valid_ids = {str(segment.get("segment_id", "")) for segment in batch}
    produced = [
        Statement(
            segment_id=model_statement.segment_id,
            speaker=model_statement.speaker,
            topic=model_statement.topic,
            claim=model_statement.claim,
            kind=model_statement.kind,
            entity_refs=EntityReferences.from_dict(
                model_statement.entity_refs.model_dump(exclude_none=True)
            ),
            engine=engine,
        )
        for model_statement in extraction.statements
    ]
    # The filter runs per batch, against THAT batch's ids — an id from an
    # earlier batch is just as much a hallucination as an invented one.
    return drop_hallucinated_segment_ids(produced, valid_ids)


def _extract_in_batches(
    segments: list[dict],
    meeting_title: str,
    *,
    known_topics: list[str] | None,
    batch_size: int,
    engine: str,
    call_engine,
    parallel_workers: int = 1,
) -> list[Statement]:
    """Batched extraction, running topic vocabulary, one repair retry per batch.

    # ported concept from drift/extractor.py:extract
    The two engines differ only in `call_engine`; every rule that shapes the
    output — batching, vocabulary carry-over, the anti-hallucination filter —
    is identical, so switching engines mid-demo cannot change the semantics of
    what lands on the board.

    PARALLELISM (claude only, `parallel_workers` > 1). One Claude CLI call takes
    ~15s on this machine no matter how small the batch, so a forty-minute meeting
    serialized is a ten-minute wait — and the fast fire is the whole demo moment.
    The batches are independent, so they run concurrently and the results are
    reassembled in transcript order: identical output, a quarter of the clock.

    The FIRST batch still runs alone, because its topics seed the vocabulary the
    rest of the meeting reuses. Losing that carry-over entirely is how one work
    item ends up with four names. Gemini stays strictly serial — free tier, ~10
    RPM, and a 429 storm is slower than doing it properly.
    """
    seen_topics: list[str] = list(known_topics or [])
    collected: list[Statement] = []
    batches = [segments[index : index + batch_size] for index in range(0, len(segments), batch_size)]
    if not batches:
        return []

    # Batches can finish out of order under the thread pool, so the number the
    # rail shows is how many have COMPLETED, not which one was submitted last.
    # That is the honest reading of a progress bar and the only one that never
    # goes backwards on screen.
    completed = 0
    total_batches = len(batches)

    def mark_batch_done(lines: int) -> None:
        nonlocal completed
        completed += 1
        _report_batch_progress(completed, total_batches, lines)

    def absorb(kept: list[Statement]) -> None:
        collected.extend(kept)
        for statement in kept:
            if statement.topic and statement.topic not in seen_topics:
                seen_topics.append(statement.topic)
            _report_statement(statement)

    started_at = time.monotonic()
    # Publish the denominator before the first call so the bar exists (at 0/N)
    # during the longest single wait of the whole pass rather than appearing
    # only once the first batch has already come back.
    _report_batch_progress(0, total_batches, len(segments))
    # The first batch runs alone only when it has a vocabulary job to do. With a
    # seeded memory the topic names are ALREADY settled (and snapped to handles
    # afterwards regardless), so making every other batch wait on one call buys
    # nothing and costs the fast path a whole round trip.
    seed_serially = parallel_workers <= 1 or len(seen_topics) < MINIMUM_SEEDED_VOCABULARY
    if seed_serially:
        absorb(_run_one_batch(
            batches[0], meeting_title, seen_topics,
            engine=engine, call_engine=call_engine, label=f"1/{len(batches)}",
        ))
        mark_batch_done(len(batches[0]))
        remaining = batches[1:]
    else:
        print(f"[scribe] {engine}: vocabulary already seeded ({len(seen_topics)} topics) — "
              "every batch runs in parallel")
        remaining = batches
    if remaining and parallel_workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        workers = min(parallel_workers, len(remaining))
        print(f"[scribe] {engine}: {len(remaining)} more batches across {workers} workers")
        vocabulary = list(seen_topics)  # frozen: every parallel batch sees the same one
        with ThreadPoolExecutor(max_workers=workers) as pool:
            first_label = len(batches) - len(remaining) + 1
            futures = [
                pool.submit(
                    _run_one_batch, batch, meeting_title, vocabulary,
                    engine=engine, call_engine=call_engine,
                    label=f"{number}/{len(batches)}",
                )
                for number, batch in enumerate(remaining, first_label)
            ]
            for future, batch in zip(futures, remaining, strict=False):
                # In submission order — transcript order is preserved.
                try:
                    absorb(future.result())
                except Exception as error:  # noqa: BLE001 — one bad batch is not the meeting
                    print(f"[scribe] {engine}: a batch failed ({error.__class__.__name__}: {error}) — skipped")
                # Counted either way: a skipped batch is finished, and a bar that
                # stalls on a failure reads as a hang rather than as a loss.
                mark_batch_done(len(batch))
    else:
        for number, batch in enumerate(remaining, len(batches) - len(remaining) + 1):
            try:
                absorb(_run_one_batch(
                    batch, meeting_title, seen_topics,
                    engine=engine, call_engine=call_engine, label=f"{number}/{len(batches)}",
                ))
            except Exception as error:  # noqa: BLE001
                print(f"[scribe] {engine}: a batch failed ({error.__class__.__name__}: {error}) — skipped")
            mark_batch_done(len(batch))

    print(
        f"[scribe] {engine}: {len(collected)} statements from {len(segments)} segments "
        f"in {time.monotonic() - started_at:.1f}s"
    )
    return collected


# --- the fixtures floor ------------------------------------------------------


def fixture_path_for(meeting_id: str) -> Path | None:
    """Locate the ground-truth fixture for a meeting, or None.

    Lookup order, all under adjourn/fixtures/:
        <meeting_id>.fixtures.json   (the drift convention: sits next to the
                                      transcript of the same name)
        <meeting_id>.json
        demo_statements.json         (the last-resort floor, so a cold machine
                                      with no keys still fills the whole board)

    A meeting_id that is already a path to an existing file is used directly,
    which is what makes `-m adjourn.extraction <path>` work.
    """
    direct = Path(meeting_id).expanduser()
    if direct.is_file():
        return direct
    fixtures_directory = config.FIXTURES_DIR
    stem = Path(meeting_id).name
    for candidate in (
        fixtures_directory / f"{stem}.fixtures.json",
        fixtures_directory / f"{stem}.json",
    ):
        if candidate.is_file():
            return candidate
    # The last-resort floor is OPT-IN (ADJOURN_FIXTURE_FLOOR=1). A real meeting
    # that extracts to nothing must produce an empty board, not the demo
    # meeting's promises wearing that meeting's name and that speaker's face.
    floor = fixtures_directory / "demo_statements.json"
    if floor.is_file() and config.is_demo_fixture_floor_allowed():
        print(f"[scribe] fixtures: falling back to the demo floor for {meeting_id!r} "
              "(ADJOURN_FIXTURE_FLOOR=1)")
        return floor
    return None


def load_fixture_transcript(meeting_id: str) -> tuple[list[dict], dict]:
    """Read a fixture transcript .jsonl into (segments, meta). Never raises.

    The .jsonl format is drift's, extended to the segment shape
    meetingscribe_source produces: one {"type": "meta", ...} line followed by
    {"type": "segment", "segment_id", "speaker", "track", "start", "end", "text"}
    lines. This is what lets a rehearsal replay the demo meeting through the
    exact same extraction path a live recording takes — no special-case branch,
    so nothing about the demo is only true in rehearsal.
    """
    direct = Path(meeting_id).expanduser()
    path = direct if direct.is_file() else config.FIXTURES_DIR / f"{Path(meeting_id).name}.jsonl"
    if not path.is_file():
        print(f"[scribe] fixtures: no transcript at {path}")
        return [], {}
    segments: list[dict] = []
    meta: dict = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            print(f"[scribe] fixtures: skipped malformed line {line_number} of {path.name}")
            continue
        if row.get("type") == "meta":
            meta = row
        elif row.get("type") == "segment":
            segments.append(row)
    return segments, meta


def load_fixture_statements(meeting_id: str) -> list[Statement]:
    """The floor: canned statements from adjourn/fixtures/.

    Accepts either a bare JSON list of Statement dicts or the drift-style
    {"statements": [...]} wrapper. This path must never raise — a broken fixture
    logs and returns an empty list, because an empty board beats a traceback in
    front of founders.
    """
    path = fixture_path_for(meeting_id)
    if path is None:
        print(f"[scribe] fixtures: no fixture found for {meeting_id!r} — returning nothing")
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"[scribe] fixtures: could not read {path} ({error.__class__.__name__}) — returning nothing")
        return []
    rows = payload.get("statements", []) if isinstance(payload, dict) else payload
    statements: list[Statement] = []
    for row in rows or []:
        try:
            statement = Statement.from_dict(row)
        except (KeyError, TypeError) as error:
            print(f"[scribe] fixtures: skipped a malformed row ({error.__class__.__name__})")
            continue
        statement.engine = ENGINE_FIXTURES
        statements.append(statement)
    print(f"[scribe] fixtures: {len(statements)} statements loaded from {path.name}")
    return statements


# --- deterministic post-processing (no model, runs on every engine's output) --


_SUFFIX_PATTERN = re.compile(r"\.\d+$")


def base_segment_id(segment_id: str) -> str:
    """Strip a ".2"/".3" multi-claim suffix back to the real segment id."""
    return _SUFFIX_PATTERN.sub("", str(segment_id))


def suffix_multiple_claims(statements: list[Statement]) -> list[Statement]:
    """Give repeated segment_ids ".2", ".3" suffixes so each claim keys uniquely.

    # ported concept from drift/extractor.py
    Deterministic — no model. The order of the input list decides the numbering,
    so extraction order must stay stable (it does: batches are serial and each
    batch preserves the engine's reply order).

    Mutates in place and returns the same list, so callers can chain.
    """
    seen_counts: dict[str, int] = {}
    for statement in statements:
        base = base_segment_id(statement.segment_id)
        seen_counts[base] = seen_counts.get(base, 0) + 1
        occurrence = seen_counts[base]
        statement.segment_id = base if occurrence == 1 else f"{base}.{occurrence}"
    return statements


def drop_hallucinated_segment_ids(
    statements: list[Statement],
    valid_segment_ids: set[str],
) -> list[Statement]:
    """Anti-hallucination filter: keep only statements whose segment_id really exists.

    # ported concept from drift/extractor.py
    Compares against the ids of the batch the statement came from, stripping any
    ".2" suffix first. A model that invents an id has invented the claim too, and
    an invented claim would become a real GitHub comment — this filter is the
    only thing standing between a hallucination and an executor.
    """
    kept: list[Statement] = []
    for statement in statements:
        if base_segment_id(statement.segment_id) in valid_segment_ids:
            kept.append(statement)
        else:
            print(f"[scribe] dropped statement with unknown segment_id {statement.segment_id!r}")
    return kept


def snap_topics_to_known_references(
    statements: list[Statement],
    reference_topics: dict[str, str],
) -> list[Statement]:
    """Rename a topic to the one memory already uses for that ticket or issue.

    PURELY DETERMINISTIC — a dict lookup, no model, no network. Safe to call on
    any engine's output, including the fixtures floor.

    `reference_topics` comes from memory.topic_index_by_reference(): {"MMM-7":
    "rate limiting", "#2": "cache layer"}.

    WHY THIS EXISTS: across repeated live runs the same MMM-7 progress report
    came back with topic "rate limiting", "mmm-7", and "gateway work". The model
    is consistently right about what was said and inconsistently right about what
    to call it, and a topic that changes name between runs is a work item memory
    cannot follow — the conflict judge would never see last week's decision.
    Prompt wording narrowed the spread but could not close it, because naming is
    a genuinely free choice. A handle like MMM-7 is not a free choice, so where
    one exists it decides the name and the model's suggestion is discarded.

    Statements with no handle keep whatever the model called them; there is
    nothing more authoritative to appeal to.
    """
    if not reference_topics:
        return statements
    normalized = {str(key).upper(): value for key, value in reference_topics.items()}
    for statement in statements:
        handles = []
        if statement.entity_refs.linear_identifier:
            handles.append(str(statement.entity_refs.linear_identifier).upper())
        if statement.entity_refs.issue_number is not None:
            handles.append(f"#{int(statement.entity_refs.issue_number)}")
        for handle in handles:
            settled = normalized.get(handle)
            if settled and settled != statement.topic:
                print(f"[scribe] topic snapped for {handle}: {statement.topic!r} -> {settled!r}")
                statement.topic = settled
                break
    return statements


# Words that carry no identity. "the cache layer" and "cache layer" are one topic.
_TOPIC_STOPWORDS = frozenset(
    "a an the of for on in to and or our their its this that with we us".split()
)
_TOPIC_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_MINIMUM_STEM_LENGTH = 4


def stem_topic_token(token: str) -> str:
    """Crudely stem ONE token so 'cache' and 'caching' land on the same string.

    Suffix stripping only — no dictionary, no model, and deliberately conservative:
    it never shortens a token below four characters, so 'ring' and 'ping' survive
    intact instead of collapsing into each other.
    """
    stem = token
    for suffix in ("ing", "ers", "ed", "es", "er", "s"):
        if stem.endswith(suffix) and len(stem) - len(suffix) >= _MINIMUM_STEM_LENGTH:
            stem = stem[: -len(suffix)]
            break
    if stem.endswith("e") and len(stem) - 1 >= _MINIMUM_STEM_LENGTH:
        stem = stem[:-1]
    return stem


def topic_stem_set(topic: str) -> frozenset[str]:
    """The identity of a topic as a set of stems, stopwords removed."""
    tokens = _TOPIC_TOKEN_PATTERN.findall((topic or "").lower())
    return frozenset(
        stem_topic_token(token) for token in tokens if token not in _TOPIC_STOPWORDS
    )


def snap_topics_to_known_vocabulary(
    statements: list[Statement],
    known_topics: list[str] | tuple[str, ...] | None,
) -> list[Statement]:
    """Collapse a freshly invented topic onto the name memory already uses for it.

    PURELY DETERMINISTIC — suffix stripping and set containment, no model, no
    network. Runs AFTER snap_topics_to_known_references(), which is the stronger
    signal: a spoken handle always wins over a name match.

    WHY THIS EXISTS: handles only cover statements that mention a ticket. During
    rehearsal the Friday-review beat — which mentions no handle at all — came back
    as "caching" on one run and "cache layer" on the next, and the two runs then
    disagreed about whether that hold had already been placed. Prompting for a
    stable vocabulary narrows the spread; it does not close it, because naming is
    a free choice and a model makes free choices freely.

    The rule is containment, not similarity: a candidate snaps only when one
    topic's stems are a SUBSET of the other's. {cach} is inside {cach, layer}, so
    "cache layer" merges into a known "caching". Two topics that merely overlap —
    {cach, layer} against {cach, invalidat} — do not merge, because neither
    contains the other. Ties break to the SHORTEST known topic, then
    alphabetically, so the answer never depends on the order memory returned.

    BE HONEST ABOUT THE FAILURE MODE: a known topic that is a SINGLE token, like
    "caching", is a subset of everything containing that token, so it will also
    absorb "cache invalidation". That is a deliberate trade. Over-merging shows up
    as one work item where a careful reader wanted two; under-merging shows up as
    memory losing track of a decision, a conflict that goes unjudged, and an
    action that fires twice because the two runs disagreed about its name. The
    second failure is the one that breaks the product, so the rule leans toward
    merging. Nothing here decides WHETHER to act — only what to call the thing.
    """
    vocabulary = [topic for topic in (known_topics or []) if topic]
    if not vocabulary:
        return statements
    indexed = sorted(
        ((topic, topic_stem_set(topic)) for topic in dict.fromkeys(vocabulary)),
        key=lambda pair: (len(pair[0]), pair[0]),
    )
    for statement in statements:
        current = statement.topic or ""
        if not current or any(current == topic for topic, _ in indexed):
            continue
        stems = topic_stem_set(current)
        if not stems:
            continue
        for topic, known_stems in indexed:
            if not known_stems:
                continue
            if stems <= known_stems or known_stems <= stems:
                print(f"[scribe] topic merged into the known vocabulary: {current!r} -> {topic!r}")
                statement.topic = topic
                break
    return statements


def attach_segment_context(statements: list[Statement], segments: list[dict], source: str) -> list[Statement]:
    """Fill quote/speaker/source from the transcript itself, not from the model.

    The model is allowed to be wrong about who spoke; the transcript is not. So
    the speaker is overwritten from the segment whenever the segment has one, and
    `quote` — the verbatim line the board shows under every action card — always
    comes from the transcript. A card's quote can therefore never be a
    paraphrase, which is exactly the property that makes the board auditable.
    """
    by_id = {str(segment.get("segment_id", "")): segment for segment in segments}
    for statement in statements:
        segment = by_id.get(base_segment_id(statement.segment_id))
        if segment is not None:
            statement.quote = str(segment.get("text", "") or "")
            speaker = str(segment.get("speaker", "") or "")
            if speaker:
                statement.speaker = speaker
        statement.source = source
    return statements


# --- restraint: negation, rejection, reported speech (NO MODEL) --------------
#
# THE PRODUCT IS THE RESTRAINT. Everything below is deterministic string work
# over the VERBATIM transcript — never over the claim, which is the model's
# cleaned-up prose and is exactly where modality goes to die ("she said she'd
# file the ticket" becomes "Priya will file a ticket", which is indistinguishable
# from a first-person promise). The words are the evidence; the claim is not.
#
# Three findings from the restraint audit drove the shape of this:
#   * "And don't email the client yet" was published as a DECISION on a live
#     issue. A prohibition became an action.
#   * "So the ingest thing is still weird" was published even though the two
#     next sentences in the room were "let's not" and "I don't want a ticket".
#   * "She told me she'd take it" filed a Linear ticket in Priya's name, on a
#     day Priya was not in the room.
#
# WHY THE CUES ARE SCOPED THE WAY THEY ARE. A bare "not" cannot be a cue: the
# demo's best card comes from "we are not using Redis for the ingestion cache",
# which is a decision, not a refusal to act. What separates the two is the
# OBJECT of the negation — a negated TOOL is a decision, a negated ACTION is
# restraint — so a paired cue only fires when the negation and an
# Adjourn-actionable verb ("file a ticket", "email", "slack", "open a PR",
# "calendar") land in the SAME SENTENCE. That one rule is what lets
# "Can we get a ticket cut for rotating the keys? I don't want it to just live
# in this conversation." keep its ticket: the "don't" sentence names no action.

# Cues that can only mean "we are deciding not to act". Segment-scoped: they
# need no action verb nearby because they cannot mean anything else.
STANDALONE_NEGATION_CUES: tuple[str, ...] = (
    "let's not", "lets not", "let us not",
    "i'm not asking", "i am not asking", "not asking you to",
    "i'm not making", "i am not making",
    "i wasn't going to", "i was not going to",
    "don't write that down", "nobody write that down",
    "don't do anything", "do nothing",
    "nothing today", "not today",
    "hold off", "no need to", "not yet", "not until",
    "never mind", "forget it", "scrap that", "i'd rather not",
)

# Cues that need an action verb in the same sentence before they mean anything.
PAIRED_NEGATION_CUES: tuple[str, ...] = (
    "don't", "dont", "do not", "won't", "will not",
    "we're not", "we are not", "i'm not", "i am not", "not going to",
)

# The verbs Adjourn can actually ACT on. Negating one of these is restraint;
# negating anything else is just a sentence with a "not" in it.
ACTION_VERB_CUES: tuple[str, ...] = (
    "ticket", "issue", "card",
    "email", "e-mail", "mail them", "mail the",
    "slack", "ping", "dm ", "message the", "message them",
    "pull request", "pr for", " pr ", "open a pr", "draft pr",
    "push a branch", "cut a branch",
    "calendar", "invite", "schedule", "book time", "hold on the calendar",
    "comment on",
)

# Talking about a world that is not this one. Unambiguous on their own.
COUNTERFACTUAL_CUES: tuple[str, ...] = (
    "hypothetical", "if this were", "if it were", "if we were",
    "suppose we", "supposing we", "what if we", "let's say we",
    "in theory", "purely academic", "just spitballing",
)

# "Say the migration slips." — a counterfactual only at the START of a sentence.
# Mid-sentence, "do we say the word 'agent' on stage" is an ordinary question,
# and an earlier version of this table read it as a hypothetical and held back
# the two statements either side of it.
SENTENCE_INITIAL_COUNTERFACTUAL_CUES: tuple[str, ...] = (
    "say the ", "say it ", "say we ", "say you ", "imagine ", "pretend ",
)

# How far after a negation cue an action verb still counts as its object.
# "we don't have a product, we have a spam cannon" mentions tickets EARLIER in
# the sentence and is not a refusal to file one; "don't email the client" puts
# its object right there. Forty characters is the difference.
NEGATION_OBJECT_WINDOW = 40

# "X said he'd ...", "she told me she'd ...", "that's what he said".
# Deliberately excludes "I said" and "we said": a person relaying their OWN
# earlier words is still making the commitment.
# NOT case-insensitive, deliberately: the capital is what tells a NAME from an
# ordinary word, and re.IGNORECASE quietly threw that away — it read "Div ALSO
# said he'd..." and attributed the promise to a person called "Also".
_ATTRIBUTION_PATTERN = re.compile(
    r"\b(?:(?P<name>[A-Z][a-z]+)|[Hh]e|[Ss]he|[Tt]hey)"
    r"(?:\s+(?:also|already|just|apparently|definitely|basically|literally|then|even|still))?"
    r"\s+(?:said|says|told\s+(?:me|us|you)|mentioned|promised|volunteered|claimed|reckons?)\b"
)

# Capitalised because a sentence started, not because it is somebody's name.
_NOT_A_NAME: frozenset[str] = frozenset(
    "he she they that thats who and but so then also there this it what when "
    "yeah okay right no yes well anyway everyone nobody someone".split()
)
# ...paired with somebody else's commitment, so "Priya said the numbers look
# good" (a fact she reported) is not mistaken for "Priya said she'd do it".
_THIRD_PARTY_COMMITMENT_PATTERN = re.compile(
    r"\b(?:he|she|they)\s*(?:'d|’d|'ll|’ll|\s+would|\s+will|\s+was\s+going\s+to|"
    r"\s+were\s+going\s+to|\s+is\s+going\s+to|"
    # ...and the same thing in the negative: "she said she didn't want to move
    # it early" is still a report about somebody else's intentions.
    r"\s+(?:did|does|was|were)\s*n[o’']?t\s+(?:want|going|plan))\b",
    re.IGNORECASE,
)
# "That's what he said in standup." — attribution with the commitment in the
# PREVIOUS sentence. Segment-scoped, so the pair still resolves.
_REPORTED_ECHO_PATTERN = re.compile(
    r"\b(?:that'?s|thats)\s+what\s+(?:he|she|they|[A-Z][a-z]+)\s+said\b", re.IGNORECASE
)

# How far forward a withdrawal is allowed to reach. Two segments: "we could file
# a ticket / but let's not / I don't want it rotting with my name on it" is the
# real shape of a room changing its mind, and anything wider starts letting an
# unrelated later "no" veto an earlier decision.
REJECTION_WINDOW_SEGMENTS = 2

_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+|\s+—\s+|\s+--\s+")


def split_sentences(text: str) -> list[str]:
    """Split on sentence enders and em-dashes. Crude on purpose, and pure.

    The cue rules below are sentence-scoped, so the only thing this has to get
    right is not gluing two independent clauses together.
    """
    parts = [part.strip() for part in _SENTENCE_SPLIT_PATTERN.split(text or "")]
    return [part for part in parts if part]


def detect_negation(text: str) -> str:
    """The negation/counterfactual cue in `text`, or "". Pure, deterministic.

        >>> detect_negation("And don't email the client yet.")
        "don't … email"
        >>> detect_negation("we are not using Redis for the ingestion cache")
        ''
    """
    lowered = " ".join(str(text or "").lower().split())
    if not lowered:
        return ""
    for cue in STANDALONE_NEGATION_CUES:
        if cue in lowered:
            return cue
    for cue in COUNTERFACTUAL_CUES:
        if cue in lowered:
            return f"counterfactual: {cue.strip()}"
    for sentence in split_sentences(lowered):
        for cue in SENTENCE_INITIAL_COUNTERFACTUAL_CUES:
            if sentence.startswith(cue):
                return f"counterfactual: {cue.strip()}"
        for cue in PAIRED_NEGATION_CUES:
            start = sentence.find(cue)
            if start < 0:
                continue
            # Only what the negation actually reaches: its own object, not every
            # noun that happens to share the sentence with it.
            object_span = sentence[start : start + len(cue) + NEGATION_OBJECT_WINDOW]
            for verb in ACTION_VERB_CUES:
                if verb in object_span:
                    return f"{cue} … {verb.strip()}"
    return ""


def detect_reported_speech(text: str) -> tuple[bool, str]:
    """(is_reported, subject) for "Div said he'd send the deck". Pure.

    The subject is the attributed NAME when one was spoken, and "" when the
    speaker used a bare pronoun — an unresolved "he" is still reported speech,
    it just cannot be attributed in the ledger.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return False, ""
    attribution = _ATTRIBUTION_PATTERN.search(text)
    echo = _REPORTED_ECHO_PATTERN.search(text)
    if not attribution and not echo:
        return False, ""
    if not (_THIRD_PARTY_COMMITMENT_PATTERN.search(text) or echo):
        return False, ""
    name = (attribution.group("name") if attribution else "") or ""
    if name.lower().strip("'’") in _NOT_A_NAME:
        name = ""
    return True, name


def detect_rejection(following_texts: list[str]) -> str:
    """The cue by which the room withdrew a claim made just before. Pure."""
    for text in following_texts:
        cue = detect_negation(text)
        if cue:
            return cue
    return ""


def apply_restraint_flags(statements: list[Statement], segments: list[dict]) -> list[Statement]:
    """Flag statements the room negated, rejected, or attributed to someone else.

    DETERMINISTIC and idempotent — string tables over the verbatim transcript,
    no model, no network. Runs on EVERY engine's output including the fixtures
    floor, so the floor cannot smuggle past a gate the live path enforces.

    A flag is not a deletion. The statement still reaches the recap and memory,
    where a third party's promise belongs (clearly attributed, owed by them);
    what it does not reach is an executor. planner.plan() is where that is
    enforced, and it says so out loud on the way past.
    """
    order = [str(segment.get("segment_id", "")) for segment in segments or []]
    text_by_id = {
        str(segment.get("segment_id", "")): str(segment.get("text", "") or "")
        for segment in segments or []
    }
    position_by_id = {segment_id: index for index, segment_id in enumerate(order)}

    for statement in statements:
        base = base_segment_id(statement.segment_id)
        spoken = statement.quote or text_by_id.get(base, "")

        cue = detect_negation(spoken)
        if cue:
            statement.negated = True
            statement.restraint_reason = statement.restraint_reason or f"negated in source ({cue})"

        position = position_by_id.get(base)
        if position is not None:
            window = [
                text_by_id.get(order[index], "")
                for index in range(position + 1, min(position + 1 + REJECTION_WINDOW_SEGMENTS, len(order)))
            ]
            rejection = detect_rejection(window)
            if rejection:
                statement.rejected = True
                statement.restraint_reason = (
                    statement.restraint_reason or f"withdrawn in the room ({rejection})"
                )

        is_reported, subject = detect_reported_speech(spoken)
        if is_reported:
            statement.third_party = True
            statement.subject = statement.subject or subject
            statement.restraint_reason = statement.restraint_reason or (
                f"{statement.speaker or 'the speaker'} was relaying "
                f"{subject or 'somebody else'}'s commitment"
            )
    return statements


# --- speaker display names ---------------------------------------------------

# MeetingScribe labels the microphone track "You" and the system track "Them".
# That is right on the recorder's own screen and wrong everywhere else: the
# Slack message that went to #all-test during the audit opened "You in the
# meeting: …", and the recap grouped commitments under a heading called "You".
# Read by anybody except the person who pressed record, "You" is meaningless.
MIC_TRACK_LABELS: frozenset[str] = frozenset({"you", "me", "mic", "self"})
DEFAULT_SPEAKER_NAME = "Sharique"

# "Them" has exactly the same problem in the other direction, and it reads worse
# because it lands mid-sentence: an email card that says "Them will email Div the
# deck" looks like a bug on a projector. The system track is everybody who is not
# holding the microphone, so the honest rendering is a description rather than a
# name — Adjourn genuinely does not know who spoke, and saying so is better than
# inventing a "Priya" who was never identified. When diarisation DOES name a
# speaker, that name arrives here already and passes straight through.
SYSTEM_TRACK_LABELS: frozenset[str] = frozenset({"them", "other", "others", "system"})
DEFAULT_GUEST_NAME = "Guest"
# The name this setting used to have. Still honoured, so a .env or a runbook that
# names the old one keeps working; ADJOURN_GUEST_NAME wins when both are set.
LEGACY_GUEST_SETTING = "ADJOURN_OTHER_SPEAKER_NAME"


def guest_display_name() -> str:
    """The name everyone on the system track is shown under. One place, read live."""
    return (
        config.read_setting("ADJOURN_GUEST_NAME")
        or config.read_setting(LEGACY_GUEST_SETTING)
        or DEFAULT_GUEST_NAME
    )


def speaker_display_name(label: str) -> str:
    """Track placeholders -> readable names; a real name is left alone. Pure-ish.

    Reads its settings at call time (config reads .env at call time), so a
    different presenter needs one line in .env and no code change.

        ADJOURN_SPEAKER_NAME  the person holding the microphone
        ADJOURN_GUEST_NAME    everyone on the far end of the call ("Guest")

    APPLIED IN EXACTLY ONE PLACE and then everywhere downstream: the planner
    copies Statement.speaker onto Action.speaker, so this one pass fixes the
    Slack body, the Linear comment, the GitHub blockquote, the recap ledger AND
    the Statement nodes in both graphs. cloud_mirror calls it again at the write
    boundary, because a seeded or backfilled row can reach the cloud without ever
    having passed through here — which is how the public graph ended up labelling
    a speaker "Them".
    """
    text = str(label or "")
    key = text.strip().lower()
    if key in MIC_TRACK_LABELS:
        return config.read_setting("ADJOURN_SPEAKER_NAME",
                                   DEFAULT_SPEAKER_NAME) or DEFAULT_SPEAKER_NAME
    if key in SYSTEM_TRACK_LABELS:
        return guest_display_name()
    return text


def apply_speaker_display_names(statements: list[Statement]) -> list[Statement]:
    """Rewrite the mic-track label on every statement, once, before planning.

    Done HERE rather than in each executor because Action.speaker is copied from
    Statement.speaker by the planner, so one pass at the source fixes the Slack
    body, the Linear comment, the GitHub blockquote and the recap ledger at once.
    """
    for statement in statements:
        statement.speaker = speaker_display_name(statement.speaker)
        if statement.subject:
            statement.subject = speaker_display_name(statement.subject)
    return statements


# --- the contract -----------------------------------------------------------


def extract_statements(
    segments: list[dict],
    meeting_title: str,
    *,
    source: str = SOURCE_LIVE,
    known_topics: list[str] | None = None,
    reference_topics: dict[str, str] | None = None,
    meeting_id: str = "",
    engine: str | None = None,
) -> list[Statement]:
    """Extract Statements from transcript segments. THE model call lives below here.

    Walks resolve_engine_order() and returns the first engine's output. An engine
    that raises is logged and skipped, never propagated: an extraction failure
    must degrade to fixtures, not kill the demo. An engine that succeeds but
    returns nothing also falls through, because a silent empty board is the same
    failure wearing a nicer hat.

    `known_topics` is the vocabulary from memory, fed to the model as a
    suggestion. `reference_topics` is memory's ticket/issue -> topic index, which
    is applied afterwards as a deterministic override — see
    snap_topics_to_known_references(). One nudges the model; the other does not
    ask it.

    `meeting_id` is only used to find a fixture; extraction itself never needs it.
    """
    if not segments and engine != ENGINE_FIXTURES:
        return []
    for candidate in resolve_engine_order(engine):
        try:
            if candidate == ENGINE_CLAUDE:
                produced = extract_with_claude_cli(segments, meeting_title, known_topics=known_topics)
            elif candidate == ENGINE_GEMINI:
                produced = extract_with_gemini(segments, meeting_title, known_topics=known_topics)
            else:
                produced = load_fixture_statements(meeting_id or meeting_title)
        except Exception as error:  # noqa: BLE001 — any engine failure falls through
            print(f"[scribe] engine {candidate!r} failed ({error.__class__.__name__}: {error}) — falling through")
            continue
        if not produced:
            print(f"[scribe] engine {candidate!r} produced nothing — falling through")
            continue
        if candidate != ENGINE_FIXTURES:
            attach_segment_context(produced, segments, source)
        # Order matters: a spoken handle is authoritative, a name match is only a
        # good guess, so the handle pass runs first and the vocabulary pass only
        # gets the statements it left alone.
        snap_topics_to_known_references(produced, reference_topics or {})
        snap_topics_to_known_vocabulary(produced, known_topics)
        # Restraint runs on every engine, fixtures included: a gate the floor can
        # walk around is not a gate. It reads the verbatim words, so it must run
        # after attach_segment_context() has put them on the statement.
        apply_restraint_flags(produced, segments)
        apply_speaker_display_names(produced)
        suffix_multiple_claims(produced)
        held_back = [statement for statement in produced if statement.fires_no_work]
        if held_back:
            print(
                f"[scribe] {len(held_back)} statement(s) flagged recap-only: "
                + "; ".join(
                    f"{statement.segment_id} ({statement.restraint_reason})"
                    for statement in held_back
                )
            )
        print(f"[scribe] engine {candidate!r} produced {len(produced)} statements (source={source})")
        return produced
    print("[scribe] every engine produced nothing — returning an empty list")
    return []


# --- the conflict judge (drift's Historian) ---------------------------------


@dataclass
class ConflictVerdict:
    """Whether a new statement overturns a prior decision, and what moved.

    `engine` is the same honesty badge Statement carries: "claude", "gemini", or
    "heuristic" when the deterministic floor judged it.
    """

    conflict: bool = False
    before: str = ""
    after: str = ""
    what_changed: str = ""
    engine: str = "heuristic"

    def to_dict(self) -> dict:
        return asdict(self)


# Deterministic floor for the judge. Used when no engine is available, and as
# insurance during the demo. # ported from drift/pipeline.py:heuristic_judge
CHANGE_MARKERS: tuple[str, ...] = (
    "switch", "switching", "instead", "rather than", "reassign", "re-assign",
    "take over", "takes over", "taking over", "hand off", "hands off",
    "no longer", "dropping", "drop redis", "scrap", "scrapping", "replace",
    "replacing", "overkill", "slip", "slips", "slipping", "pushed",
    "pushing back", "move the target", "new target", "new deadline",
    "new owner", "pivot", "changed my mind", "let's not", "we're not",
    "killing", "abandon",
)


def judge_conflict_with_heuristic(history: list[dict], new_statement: dict) -> ConflictVerdict:
    """No-model conflict judge: a change marker plus an older prior statement.

    # ported from drift/pipeline.py:heuristic_judge
    Deliberately conservative — it only fires when the new text contains a word
    that means "we are changing this" AND the prior statement came from a
    different day. Refining a decision made earlier in the SAME meeting is not
    drift, and calling it drift would put a false "what changed" comment on a
    real GitHub issue.
    """
    if not history:
        return ConflictVerdict(conflict=False)
    latest = history[-1]
    if latest.get("date") and latest.get("date") == new_statement.get("date"):
        return ConflictVerdict(conflict=False)
    text = str(new_statement.get("text", "")).lower()
    if not any(marker in text for marker in CHANGE_MARKERS):
        return ConflictVerdict(conflict=False)
    return ConflictVerdict(
        conflict=True,
        before=str(latest.get("text", "")),
        after=str(new_statement.get("text", "")),
        what_changed=(
            f"{latest.get('meeting', 'an earlier meeting')} "
            f"({latest.get('date', '')}) plan revised in "
            f"{new_statement.get('meeting', 'this meeting')}"
        ).strip(),
    )


def judge_conflict_with_claude_cli(
    history: list[dict],
    new_statement: dict,
    topic: str = "",
    *,
    timeout_seconds: int = CLAUDE_TIMEOUT_SECONDS,
) -> ConflictVerdict:
    """Primary judge: one sandboxed Claude CLI call, one repair retry."""
    prompt = prompts.build_conflict_prompt(history, new_statement, topic)
    try:
        verdict = VerdictModel.model_validate(
            extract_json_object(run_claude_prompt(prompt, timeout_seconds=timeout_seconds))
        )
    except (ValueError, ValidationError, json.JSONDecodeError) as error:
        repair_prompt = prompts.build_repair_prompt(prompt, str(error))
        verdict = VerdictModel.model_validate(
            extract_json_object(run_claude_prompt(repair_prompt, timeout_seconds=timeout_seconds))
        )
    return ConflictVerdict(**verdict.model_dump(), engine=ENGINE_CLAUDE)


def judge_conflict_with_gemini(
    history: list[dict],
    new_statement: dict,
    topic: str = "",
) -> ConflictVerdict:
    """Fallback judge: gemini-3.5-flash, schema-locked. # ported from drift/detector.py"""
    prompt = prompts.build_conflict_prompt(history, new_statement, topic)
    reply = run_gemini_prompt(prompt, VerdictModel.model_json_schema())
    verdict = VerdictModel.model_validate(extract_json_object(reply))
    return ConflictVerdict(**verdict.model_dump(), engine=ENGINE_GEMINI)


def judge_conflict(
    history: list[dict],
    new_statement: dict,
    topic: str = "",
    *,
    engine: str | None = None,
) -> ConflictVerdict:
    """Does `new_statement` overturn what this work item's history already settled?

    history rows: {"date", "meeting", "who", "kind", "text"} oldest first,
    EXCLUDING the new statement (drift's contract, kept verbatim).
    new_statement: {"date", "meeting", "who", "kind", "text"}.

    Same engine ladder as extraction, with the deterministic heuristic as the
    floor instead of fixtures. Never raises.
    """
    if not history:
        return ConflictVerdict(conflict=False)
    for candidate in resolve_engine_order(engine):
        if candidate == ENGINE_FIXTURES:
            break
        try:
            if candidate == ENGINE_CLAUDE:
                verdict = judge_conflict_with_claude_cli(history, new_statement, topic)
            else:
                verdict = judge_conflict_with_gemini(history, new_statement, topic)
        except Exception as error:  # noqa: BLE001 — fall to the next judge
            print(f"[historian] {candidate} judge failed ({error.__class__.__name__}) — falling through")
            continue
        print(
            f"[historian] {candidate}: "
            + (f"CONFLICT — {verdict.what_changed}" if verdict.conflict else "no conflict")
        )
        return verdict
    verdict = judge_conflict_with_heuristic(history, new_statement)
    print(
        "[historian] heuristic floor: "
        + (f"CONFLICT — {verdict.what_changed}" if verdict.conflict else "no conflict")
    )
    return verdict


def describe_extraction() -> dict:
    """Engine availability and vocabulary, for logs and the board's info panel."""
    return {
        "configured_engine": config.extraction_engine(),
        "engine_order": resolve_engine_order(),
        "claude_binary": find_claude_binary(),
        "gemini_available": is_gemini_available(),
        "statement_kinds": list(STATEMENT_KINDS),
        "claude_batch_size": CLAUDE_BATCH_SIZE,
        "gemini_batch_size": GEMINI_BATCH_SIZE,
        "speaker_display_name": speaker_display_name("You"),
        "restraint_gates": ["negated", "rejected", "third_party"],
        "rejection_window_segments": REJECTION_WINDOW_SEGMENTS,
    }


if __name__ == "__main__":
    print(json.dumps(describe_extraction(), indent=2))
