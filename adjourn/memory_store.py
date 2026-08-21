"""Memory — what Adjourn knows across meetings.

FalkorDB by default, SQLite when Redis is unreachable, and the SAME interface
either way so no caller has to know which one it got. The demo must survive the
container not being up.

Graph schema (# ported from drift/graph.py — MERGE everywhere so re-ingesting a
meeting is a no-op), extended with the two node types Adjourn needs that Drift
did not:

    (Person)-[:SAID]->(Statement)-[:ABOUT]->(Issue)
    (Statement)-[:IN_MEETING]->(Meeting)
    (Statement)-[:SUPERSEDES]->(Statement)
    (Statement)-[:CAUSED]->(Action)              # what the statement fired
    (Statement)-[:PROMISED]->(Commitment)        # a promise someone made out loud
    (Action)-[:TOUCHED]->(Ticket {system:...})   # the linear/github object it moved

WHY (:Commitment) IS ITS OWN NODE: a promise ("I'll email Div the deck") is not
the same thing as the action that discharges it. The action can fail, be undone,
or be cancelled inside the regret window, and the promise still stands. Keeping
them separate is what lets a later meeting say "you said this last week too" even
though something did technically fire.

Graph name is "adjourn" (config.graph_name()), NOT "drift" — Sharique's Aug 3
hackathon graph stays exactly as it is.

The planner reads this. Only the orchestrator writes to it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import config

BACKEND_FALKOR = "falkor"
BACKEND_SQLITE = "sqlite"

TICKET_SYSTEM_LINEAR = "linear"
TICKET_SYSTEM_GITHUB = "github"

# The statement kinds that represent a promise a human made out loud. These get a
# (:Commitment) node so they can be recalled across meetings.
COMMITMENT_KINDS: frozenset[str] = frozenset(
    {"message_commitment", "email_commitment", "pr_intent", "assignment", "ticket_request"}
)

# Words carrying no signal for issue-title matching. Anything shorter than
# MINIMUM_TERM_LENGTH is dropped too.
# ported from drift/graph.py
STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "nor", "for", "of", "to", "in",
    "on", "at", "by", "with", "from", "about", "into", "over", "under",
    "is", "are", "was", "were", "be", "been", "being", "it", "its", "this",
    "that", "these", "those", "there", "here", "when", "where", "how", "why",
    "we", "our", "you", "your", "they", "their", "he", "she", "him", "her",
    "i", "me", "my", "us", "do", "does", "did", "done", "should", "would",
    "could", "will", "shall", "can", "may", "might", "must", "have", "has",
    "had", "not", "no", "yes", "if", "then", "than", "so", "too", "very",
    "just", "also", "all", "any", "some", "such", "other", "more", "most",
    "new", "get", "got", "use", "using", "need", "needs", "want", "wants",
    "issue", "ticket", "thing", "stuff", "team", "work", "working",
})
MINIMUM_TERM_LENGTH = 3

_TERM_PATTERN = re.compile(r"[a-z0-9]+")


def topic_search_terms(topic: str) -> list[str]:
    """Tokenize a topic key into searchable terms. Shared by both backends.

    # ported from drift/graph.py:find_issue
    Tokenizing on [a-z0-9] runs also strips every RediSearch syntax character
    (- | @ ( ) { } [ ] : ; ~ ^) out of a model-written topic, which is what stops
    a topic like "rate-limiting (v2)" from becoming a malformed query.
    """
    return [
        term
        for term in _TERM_PATTERN.findall((topic or "").lower())
        if len(term) >= MINIMUM_TERM_LENGTH and term not in STOPWORDS
    ]


@dataclass
class PriorCommitment:
    """Something a person said they would do, in an earlier meeting.

    Used by the planner to tell a NEW promise from a repeated one, and by the
    recap to show "you said this last week too".
    """

    segment_id: str
    speaker: str
    topic: str
    claim: str
    kind: str
    meeting_id: str
    meeting_date: str

    def as_history_row(self, meeting_title: str = "") -> dict:
        """The row shape extraction.judge_conflict() expects. # drift/detector.py contract"""
        return {
            "segment_id": self.segment_id,
            "date": self.meeting_date,
            "meeting": meeting_title or self.meeting_id,
            "who": self.speaker,
            "kind": self.kind,
            "text": self.claim,
        }


class MeetingMemory:
    """The memory interface. Both backends implement exactly this.

    Construct via `open_memory()` — never instantiate a backend directly, or the
    automatic fallback stops working.
    """

    backend: str = BACKEND_FALKOR

    # -- lifecycle ----------------------------------------------------------

    def ensure_schema(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # -- writes -------------------------------------------------------------

    def record_meeting(self, meeting_id: str, title: str, date: str) -> None:
        raise NotImplementedError

    def record_statement(self, statement, meeting_id: str) -> None:
        raise NotImplementedError

    def record_action(self, action, result) -> None:
        raise NotImplementedError

    def record_issue(self, number: int, repo: str, title: str, slug: str = "") -> None:
        raise NotImplementedError

    def mark_superseded(self, new_segment_id: str, old_segment_id: str) -> None:
        raise NotImplementedError

    # -- reads --------------------------------------------------------------

    def find_issue_for_topic(self, topic: str) -> int | None:
        raise NotImplementedError

    def find_prior_commitments(self, topic: str, limit: int = 10) -> list[PriorCommitment]:
        raise NotImplementedError

    def known_topics(self, limit: int = 40) -> list[str]:
        """Every topic key memory has ever seen, most-used first.

        Fed back into extraction as the running vocabulary. Drift only carried
        topics WITHIN one meeting, which kept a single transcript self-consistent
        but let the same work item get a fresh name every week — and a topic that
        changes name is a work item memory cannot follow. Seeding the vocabulary
        from memory is what makes "the cache layer" in August the same node as
        "the cache layer" in July, which is the whole basis for noticing that a
        decision changed.
        """
        raise NotImplementedError

    def topic_index_by_reference(self) -> dict[str, str]:
        """Map every concrete handle memory has seen to its settled topic key.

        Keys are ticket identifiers ("MMM-7") and issue handles ("#2"); values
        are the topic key most used with that handle.

        This is the deterministic anchor under topic naming. A model asked to
        name a work item is reliable about WHAT was said and unreliable about
        what to CALL it — across runs the same statement came back as "rate
        limiting", "mmm-7", and "gateway work". A handle like MMM-7 has no such
        ambiguity, so when a statement carries one, the topic is looked up
        rather than trusted. Prompt wording could not fix this; a lookup does.
        """
        raise NotImplementedError

    def has_fired_dedup_key(self, dedup_key: str) -> bool:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    # -- targeted forgetting (the demo reset; never used at runtime) ---------

    def forget_meeting(self, meeting_id: str) -> int:
        """Remove one meeting and everything it said. Returns statements removed.

        Rehearsing writes the demo meeting into memory. Left there, the next run
        finds its own statements in the history, the topic vocabulary is already
        full of the answers, and the conflict beat is judged against a copy of
        itself. Wiping the whole graph would also delete the PRIOR meeting, which
        is the one thing that must survive — so the reset forgets by name.
        """
        raise NotImplementedError

    def forget_actions(self) -> int:
        """Forget every recorded action, so dedup starts from the journal again.

        The journal and memory both remember what fired. Clearing the journal
        alone leaves memory silently suppressing every action on the next run —
        which looks exactly like a broken planner and is not.
        """
        raise NotImplementedError

    # -- convenience shared by both backends --------------------------------

    def ingest(self, statements, meta: dict) -> int:
        """Record a whole meeting's statements. Idempotent; returns how many landed.

        `meta` is {"meeting_id", "title", "date"}. Re-ingesting the same meeting
        is a no-op at the storage layer (MERGE / INSERT OR IGNORE), which is what
        makes the reconcile pass safe to run over a meeting the fast path already
        wrote.
        """
        meeting_id = str(meta.get("meeting_id") or meta.get("id") or "")
        self.record_meeting(meeting_id, str(meta.get("title", "")), str(meta.get("date", "")))
        recorded = 0
        for statement in statements:
            self.record_statement(statement, meeting_id)
            recorded += 1
        return recorded

    def history(self, topic_or_ticket: str, limit: int = 20) -> list[dict]:
        """History rows for a topic, in the shape the conflict judge wants.

        Accepts a topic key ("cache layer") or a ticket handle ("#2", "MMM-7").
        Oldest first, matching drift/detector.judge()'s contract.
        """
        return [
            commitment.as_history_row()
            for commitment in self.find_prior_commitments(topic_or_ticket, limit=limit)
        ]


# --- FalkorDB backend --------------------------------------------------------

# ported from drift/graph.py — every write is a parameterized MERGE so a
# re-ingest is idempotent, every read goes through ro_query.
_MERGE_MEETING = """
MERGE (m:Meeting {id: $meeting_id})
  ON CREATE SET m.title = $title, m.date = $date
  ON MATCH  SET m.title = coalesce($title, m.title), m.date = coalesce($date, m.date)
"""

_MERGE_STATEMENT = """
MERGE (p:Person {name: $speaker})
MERGE (m:Meeting {id: $meeting_id})
MERGE (s:Statement {segment_id: $segment_id})
  ON CREATE SET s.claim = $claim, s.kind = $kind, s.topic = $topic,
                s.quote = $quote, s.source = $source, s.engine = $engine,
                s.linear_identifier = $linear_identifier
  ON MATCH  SET s.claim = $claim, s.quote = coalesce($quote, s.quote),
                s.source = $source,
                s.linear_identifier = coalesce($linear_identifier, s.linear_identifier)
MERGE (p)-[:SAID]->(s)
MERGE (s)-[:IN_MEETING]->(m)
MERGE (t:Topic {key: $topic})
MERGE (s)-[:ABOUT_TOPIC]->(t)
"""

_LINK_STATEMENT_TO_ISSUE = """
MATCH (s:Statement {segment_id: $segment_id})
MERGE (i:Issue {number: $issue_number})
MERGE (s)-[:ABOUT]->(i)
"""

_MERGE_COMMITMENT = """
MATCH (s:Statement {segment_id: $segment_id})
MERGE (c:Commitment {segment_id: $segment_id})
  ON CREATE SET c.claim = $claim, c.kind = $kind, c.topic = $topic,
                c.owner = $owner, c.meeting_id = $meeting_id
MERGE (s)-[:PROMISED]->(c)
"""

_MERGE_ACTION = """
MERGE (a:Action {dedup_key: $dedup_key})
  ON CREATE SET a.kind = $kind, a.mode = $mode, a.ok = $ok,
                a.external_id = $external_id, a.url = $url,
                a.human_summary = $human_summary, a.fired_at = $fired_at
  ON MATCH  SET a.mode = $mode, a.ok = $ok, a.fired_at = $fired_at
WITH a
OPTIONAL MATCH (s:Statement {segment_id: $segment_id})
FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
    MERGE (s)-[:CAUSED]->(a))
"""

_MERGE_TICKET = """
MATCH (a:Action {dedup_key: $dedup_key})
MERGE (t:Ticket {system: $system, identifier: $identifier})
  ON CREATE SET t.url = $url
MERGE (a)-[:TOUCHED]->(t)
"""

_UPSERT_ISSUE = """
MERGE (i:Issue {number: $number})
SET i.repo = $repo, i.title = $title, i.slug = $slug
"""

_SUPERSEDES = """
MATCH (new:Statement {segment_id: $new_id})
MATCH (old:Statement {segment_id: $old_id})
MERGE (new)-[:SUPERSEDES]->(old)
"""

_HISTORY_BY_TOPIC = """
MATCH (p:Person)-[:SAID]->(s:Statement)-[:ABOUT_TOPIC]->(t:Topic {key: $topic}),
      (s)-[:IN_MEETING]->(m:Meeting)
RETURN s.segment_id, p.name, s.topic, s.claim, s.kind, m.id, m.date
ORDER BY m.date ASC, s.segment_id ASC
LIMIT $limit
"""

_HISTORY_BY_ISSUE = """
MATCH (p:Person)-[:SAID]->(s:Statement)-[:ABOUT]->(i:Issue {number: $issue_number}),
      (s)-[:IN_MEETING]->(m:Meeting)
RETURN s.segment_id, p.name, s.topic, s.claim, s.kind, m.id, m.date
ORDER BY m.date ASC, s.segment_id ASC
LIMIT $limit
"""

_FULLTEXT_ISSUE = (
    "CALL db.idx.fulltext.queryNodes('Issue', $term) YIELD node, score "
    "RETURN node.number, score"
)

# The issue this topic has ALREADY been filed against, most-used first. Ties break
# toward the lower issue number so the answer is stable across runs, which the
# planner needs — an unstable issue lookup makes unstable dedup keys.
_REMEMBERED_ISSUE_FOR_TOPIC = """
MATCH (s:Statement)-[:ABOUT_TOPIC]->(t:Topic {key: $topic}),
      (s)-[:ABOUT]->(i:Issue)
RETURN i.number, count(s) AS uses
ORDER BY uses DESC, i.number ASC
LIMIT 1
"""

_KNOWN_TOPICS = """
MATCH (s:Statement)-[:ABOUT_TOPIC]->(t:Topic)
RETURN t.key, count(s) AS uses
ORDER BY uses DESC, t.key ASC
LIMIT $limit
"""

_TOPIC_BY_LINEAR_IDENTIFIER = """
MATCH (s:Statement)-[:ABOUT_TOPIC]->(t:Topic)
WHERE s.linear_identifier IS NOT NULL AND s.linear_identifier <> ''
RETURN s.linear_identifier, t.key, count(s) AS uses
ORDER BY uses DESC, t.key ASC
"""

_TOPIC_BY_ISSUE = """
MATCH (s:Statement)-[:ABOUT]->(i:Issue), (s)-[:ABOUT_TOPIC]->(t:Topic)
RETURN i.number, t.key, count(s) AS uses
ORDER BY uses DESC, t.key ASC
"""

_HAS_ACTION = "MATCH (a:Action {dedup_key: $dedup_key}) RETURN count(a)"

# Targeted forgetting, for the demo reset. Wiping the whole graph would take the
# prior standup with it — and the prior standup is the entire reason the conflict
# beat has anything to compare against.
_FORGET_MEETING_STATEMENTS = """
MATCH (s:Statement)-[:IN_MEETING]->(m:Meeting {id: $meeting_id})
OPTIONAL MATCH (s)-[:PROMISED]->(c:Commitment)
OPTIONAL MATCH (s)-[:CAUSED]->(a:Action)
DELETE c, a, s
"""

_FORGET_MEETING = "MATCH (m:Meeting {id: $meeting_id}) DELETE m"

_FORGET_ALL_ACTIONS = "MATCH (a:Action) DELETE a"

_COUNT_MEETING_STATEMENTS = """
MATCH (s:Statement)-[:IN_MEETING]->(m:Meeting {id: $meeting_id})
RETURN count(s)
"""

_ISSUE_NUMBER_PATTERN = re.compile(r"^#?(\d+)$")


class FalkorMeetingMemory(MeetingMemory):
    """FalkorDB backend. Container `falkordb-test` on localhost:6379, graph "adjourn"."""

    backend = BACKEND_FALKOR

    def __init__(self, host: str | None = None, port: int | None = None,
                 graph_name: str | None = None) -> None:
        from falkordb import FalkorDB

        self.host = host or config.falkor_host()
        self.port = port or config.falkor_port()
        self.graph_name = graph_name or config.graph_name()
        self._database = FalkorDB(host=self.host, port=self.port)
        self._graph = self._database.select_graph(self.graph_name)
        # Force a round trip now, so an unreachable Redis fails HERE, inside
        # open_memory()'s try block, rather than on the first write mid-demo.
        self._read("MATCH (n) RETURN count(n) LIMIT 1")

    # -- plumbing -----------------------------------------------------------

    def _read(self, query: str, params: dict | None = None) -> list:
        """ro_query returning [] when the graph key does not exist yet.

        # ported from drift/graph.py:_ro
        """
        try:
            return self._graph.ro_query(query, params or {}).result_set
        except Exception as error:  # fresh DB: no graph key until the first write
            if "empty key" in str(error).lower():
                return []
            raise

    def _write(self, query: str, params: dict | None = None) -> None:
        self._graph.query(query, params or {})

    # -- lifecycle ----------------------------------------------------------

    def ensure_schema(self) -> None:
        """Full-text index on Issue.title (idempotent; backfills existing nodes)."""
        # ported from drift/graph.py:ensure_indexes
        try:
            self._graph.query("CREATE FULLTEXT INDEX FOR (i:Issue) ON (i.title)")
            print(f"[memory] falkor: full-text index on Issue.title created ({self.graph_name})")
        except Exception as error:
            if "already" not in str(error).lower():
                raise

    def close(self) -> None:
        self._graph = None
        self._database = None

    # -- writes -------------------------------------------------------------

    def record_meeting(self, meeting_id: str, title: str, date: str) -> None:
        self._write(_MERGE_MEETING, {"meeting_id": meeting_id, "title": title, "date": date})

    def record_statement(self, statement, meeting_id: str) -> None:
        entity_refs = _entity_refs_dict(statement)
        self._write(_MERGE_STATEMENT, {
            "segment_id": statement.segment_id,
            "speaker": statement.speaker or "Unknown",
            "meeting_id": meeting_id,
            "claim": statement.claim,
            "kind": statement.kind,
            "topic": statement.topic or "untopiced",
            "quote": statement.quote or "",
            "source": getattr(statement, "source", "live"),
            "engine": getattr(statement, "engine", "fixtures"),
            "linear_identifier": entity_refs.get("linear_identifier"),
        })
        issue_number = entity_refs.get("issue_number")
        if issue_number is not None:
            self._write(_LINK_STATEMENT_TO_ISSUE, {
                "segment_id": statement.segment_id,
                "issue_number": int(issue_number),
            })
        if statement.kind in COMMITMENT_KINDS:
            self._write(_MERGE_COMMITMENT, {
                "segment_id": statement.segment_id,
                "claim": statement.claim,
                "kind": statement.kind,
                "topic": statement.topic or "untopiced",
                "owner": entity_refs.get("person") or statement.speaker or "Unknown",
                "meeting_id": meeting_id,
            })

    def record_action(self, action, result) -> None:
        self._write(_MERGE_ACTION, {
            "dedup_key": getattr(action, "dedup_key", "") or "",
            "kind": getattr(action, "kind", "") or "",
            "mode": getattr(result, "mode", "sim"),
            "ok": bool(getattr(result, "ok", False)),
            "external_id": getattr(result, "external_id", None) or "",
            "url": getattr(result, "url", None) or "",
            "human_summary": getattr(result, "human_summary", "") or "",
            "fired_at": getattr(result, "fired_at", "") or "",
            "segment_id": getattr(action, "segment_id", "") or "",
        })
        system, identifier = _ticket_handle(action, result)
        if identifier:
            self._write(_MERGE_TICKET, {
                "dedup_key": getattr(action, "dedup_key", "") or "",
                "system": system,
                "identifier": identifier,
                "url": getattr(result, "url", None) or "",
            })

    def record_issue(self, number: int, repo: str, title: str, slug: str = "") -> None:
        self._write(_UPSERT_ISSUE, {
            "number": int(number), "repo": repo, "title": title, "slug": slug or "",
        })

    def mark_superseded(self, new_segment_id: str, old_segment_id: str) -> None:
        self._write(_SUPERSEDES, {"new_id": new_segment_id, "old_id": old_segment_id})
        print(f"[memory] falkor: {new_segment_id} SUPERSEDES {old_segment_id}")

    # -- reads --------------------------------------------------------------

    def find_issue_for_topic(self, topic: str) -> int | None:
        """The issue this topic is about: what memory REMEMBERS first, then a guess.

        # ported from drift/graph.py:find_issue (the full-text half)
        Each surviving term gets a '*' appended because RediSearch matches whole
        tokens — a bare "oauth" would miss "OAuth2".

        WHY THE REMEMBERED LINK COMES FIRST. Prefix search only runs one
        direction: "cache*" matches the token "cache" in "Add cache layer for
        transcript ingestion", but "caching*" does not. The extractor names that
        work item "cache layer" on some runs and "caching" on others — the topic
        is the one field a model is free to rephrase — so on a "caching" run the
        title search returned None and every statement that did not SAY an issue
        number out loud silently produced no card. That is how the demo's
        reassignment beat disappeared: beat 5 says "issue two" and survived,
        beat 9 does not name a number and died, with no error and nothing on the
        board to explain the absence.

        The graph already knew the answer. A statement about this topic has been
        filed against issue #2 before, so ask memory what it did last time and
        only fall back to guessing from the title when it has never seen the
        topic. Same principle as topic_index_by_reference: when a lookup is
        available, look it up rather than trusting the model's wording.
        """
        explicit = _ISSUE_NUMBER_PATTERN.match((topic or "").strip())
        if explicit:
            return int(explicit.group(1))
        remembered = self._remembered_issue_for_topic(topic)
        if remembered is not None:
            return remembered
        best_number: int | None = None
        best_score = float("-inf")
        for term in topic_search_terms(topic):
            try:
                rows = self._read(_FULLTEXT_ISSUE, {"term": term + "*"})
            except Exception:  # no index yet / malformed term — treat as a miss
                continue
            for number, score in rows:
                if number is not None and score > best_score:
                    best_score = score
                    best_number = int(number)
        return best_number

    def _remembered_issue_for_topic(self, topic: str) -> int | None:
        """The issue a statement about this topic was already filed against, or None."""
        key = (topic or "").strip()
        if not key:
            return None
        try:
            rows = self._read(_REMEMBERED_ISSUE_FOR_TOPIC, {"topic": key})
        except Exception:  # noqa: BLE001 — a cold graph just means "nothing remembered"
            return None
        for number, _uses in rows:
            if number is not None:
                return int(number)
        return None

    def find_prior_commitments(self, topic: str, limit: int = 10) -> list[PriorCommitment]:
        handle = (topic or "").strip()
        explicit = _ISSUE_NUMBER_PATTERN.match(handle)
        if explicit:
            rows = self._read(_HISTORY_BY_ISSUE, {
                "issue_number": int(explicit.group(1)), "limit": int(limit),
            })
        else:
            rows = self._read(_HISTORY_BY_TOPIC, {"topic": handle, "limit": int(limit)})
        return [
            PriorCommitment(
                segment_id=segment_id, speaker=speaker or "", topic=row_topic or "",
                claim=claim or "", kind=kind or "", meeting_id=meeting_id or "",
                meeting_date=meeting_date or "",
            )
            for segment_id, speaker, row_topic, claim, kind, meeting_id, meeting_date in rows
        ]

    def known_topics(self, limit: int = 40) -> list[str]:
        rows = self._read(_KNOWN_TOPICS, {"limit": int(limit)})
        return [str(topic) for topic, _ in rows if topic and topic != "untopiced"]

    def topic_index_by_reference(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for identifier, topic, _ in self._read(_TOPIC_BY_LINEAR_IDENTIFIER):
            if identifier and topic and topic != "untopiced":
                index.setdefault(str(identifier).upper(), str(topic))
        for number, topic, _ in self._read(_TOPIC_BY_ISSUE):
            if number is not None and topic and topic != "untopiced":
                index.setdefault(f"#{int(number)}", str(topic))
        return index

    def has_fired_dedup_key(self, dedup_key: str) -> bool:
        rows = self._read(_HAS_ACTION, {"dedup_key": dedup_key})
        return bool(rows) and int(rows[0][0]) > 0

    def stats(self) -> dict:
        counts: dict[str, int] = {"backend": BACKEND_FALKOR}
        for label in ("Person", "Meeting", "Topic", "Issue", "Statement", "Commitment", "Action", "Ticket"):
            rows = self._read(f"MATCH (n:{label}) RETURN count(n)")
            counts[label.lower() + "s"] = int(rows[0][0]) if rows else 0
        nodes = self._read("MATCH (n) RETURN count(n)")
        edges = self._read("MATCH ()-[r]->() RETURN count(r)")
        counts["nodes"] = int(nodes[0][0]) if nodes else 0
        counts["edges"] = int(edges[0][0]) if edges else 0
        return counts

    def forget_meeting(self, meeting_id: str) -> int:
        rows = self._read(_COUNT_MEETING_STATEMENTS, {"meeting_id": meeting_id})
        removed = int(rows[0][0]) if rows else 0
        self._write(_FORGET_MEETING_STATEMENTS, {"meeting_id": meeting_id})
        self._write(_FORGET_MEETING, {"meeting_id": meeting_id})
        return removed

    def forget_actions(self) -> int:
        rows = self._read("MATCH (a:Action) RETURN count(a)")
        removed = int(rows[0][0]) if rows else 0
        self._write(_FORGET_ALL_ACTIONS)
        return removed

    def wipe(self) -> None:
        """Delete the whole `adjourn` graph. Used by the demo reset, never at runtime."""
        try:
            self._graph.delete()
        except Exception as error:
            if "empty key" not in str(error).lower():
                raise
        self._graph = self._database.select_graph(self.graph_name)


# --- SQLite backend ----------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id TEXT PRIMARY KEY, title TEXT, date TEXT);
CREATE TABLE IF NOT EXISTS statements (
    segment_id TEXT PRIMARY KEY, meeting_id TEXT, speaker TEXT, topic TEXT,
    claim TEXT, kind TEXT, quote TEXT, source TEXT, engine TEXT,
    issue_number INTEGER, linear_identifier TEXT, person TEXT,
    deadline_text TEXT, percent INTEGER);
CREATE TABLE IF NOT EXISTS commitments (
    segment_id TEXT PRIMARY KEY, meeting_id TEXT, topic TEXT, kind TEXT,
    claim TEXT, owner TEXT);
CREATE TABLE IF NOT EXISTS issues (
    number INTEGER PRIMARY KEY, repo TEXT, title TEXT, slug TEXT);
CREATE TABLE IF NOT EXISTS actions (
    dedup_key TEXT PRIMARY KEY, kind TEXT, segment_id TEXT, mode TEXT,
    ok INTEGER, external_id TEXT, url TEXT, human_summary TEXT, fired_at TEXT);
CREATE TABLE IF NOT EXISTS tickets (
    system TEXT, identifier TEXT, url TEXT, dedup_key TEXT,
    PRIMARY KEY (system, identifier));
CREATE TABLE IF NOT EXISTS supersedes (
    new_segment_id TEXT, old_segment_id TEXT,
    PRIMARY KEY (new_segment_id, old_segment_id));
CREATE INDEX IF NOT EXISTS statements_topic ON statements(topic);
CREATE INDEX IF NOT EXISTS statements_issue ON statements(issue_number);
"""


class SqliteMeetingMemory(MeetingMemory):
    """SQLite fallback at state/memory.sqlite3. Same interface, no daemon required.

    Not a lesser memory — the same questions get the same answers, just without
    the graph traversal. The only real difference is topic matching: Falkor uses
    a full-text index, this uses LIKE over the terms, which is coarser but has
    never been the thing that decided a demo.
    """

    backend = BACKEND_SQLITE

    def __init__(self, database_path=None) -> None:
        self.database_path = Path(database_path or config.sqlite_memory_path())
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.database_path))
        self._connection.row_factory = sqlite3.Row

    # -- lifecycle ----------------------------------------------------------

    def ensure_schema(self) -> None:
        self._connection.executescript(_SQLITE_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # -- writes -------------------------------------------------------------

    def record_meeting(self, meeting_id: str, title: str, date: str) -> None:
        self._connection.execute(
            "INSERT INTO meetings (meeting_id, title, date) VALUES (?, ?, ?) "
            "ON CONFLICT(meeting_id) DO UPDATE SET title=excluded.title, date=excluded.date",
            (meeting_id, title, date),
        )
        self._connection.commit()

    def record_statement(self, statement, meeting_id: str) -> None:
        entity_refs = _entity_refs_dict(statement)
        self._connection.execute(
            "INSERT INTO statements (segment_id, meeting_id, speaker, topic, claim, kind, "
            "quote, source, engine, issue_number, linear_identifier, person, deadline_text, percent) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(segment_id) DO UPDATE SET claim=excluded.claim, source=excluded.source",
            (
                statement.segment_id, meeting_id, statement.speaker or "Unknown",
                statement.topic or "untopiced", statement.claim, statement.kind,
                statement.quote or "", getattr(statement, "source", "live"),
                getattr(statement, "engine", "fixtures"),
                entity_refs.get("issue_number"), entity_refs.get("linear_identifier"),
                entity_refs.get("person"), entity_refs.get("deadline_text"),
                entity_refs.get("percent"),
            ),
        )
        if statement.kind in COMMITMENT_KINDS:
            self._connection.execute(
                "INSERT OR IGNORE INTO commitments (segment_id, meeting_id, topic, kind, claim, owner) "
                "VALUES (?,?,?,?,?,?)",
                (
                    statement.segment_id, meeting_id, statement.topic or "untopiced",
                    statement.kind, statement.claim,
                    entity_refs.get("person") or statement.speaker or "Unknown",
                ),
            )
        self._connection.commit()

    def record_action(self, action, result) -> None:
        dedup_key = getattr(action, "dedup_key", "") or ""
        self._connection.execute(
            "INSERT INTO actions (dedup_key, kind, segment_id, mode, ok, external_id, url, "
            "human_summary, fired_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(dedup_key) DO UPDATE SET mode=excluded.mode, ok=excluded.ok, "
            "fired_at=excluded.fired_at",
            (
                dedup_key, getattr(action, "kind", ""), getattr(action, "segment_id", ""),
                getattr(result, "mode", "sim"), int(bool(getattr(result, "ok", False))),
                getattr(result, "external_id", None) or "", getattr(result, "url", None) or "",
                getattr(result, "human_summary", "") or "", getattr(result, "fired_at", "") or "",
            ),
        )
        system, identifier = _ticket_handle(action, result)
        if identifier:
            self._connection.execute(
                "INSERT OR REPLACE INTO tickets (system, identifier, url, dedup_key) VALUES (?,?,?,?)",
                (system, identifier, getattr(result, "url", None) or "", dedup_key),
            )
        self._connection.commit()

    def record_issue(self, number: int, repo: str, title: str, slug: str = "") -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO issues (number, repo, title, slug) VALUES (?,?,?,?)",
            (int(number), repo, title, slug or ""),
        )
        self._connection.commit()

    def mark_superseded(self, new_segment_id: str, old_segment_id: str) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO supersedes (new_segment_id, old_segment_id) VALUES (?,?)",
            (new_segment_id, old_segment_id),
        )
        self._connection.commit()
        print(f"[memory] sqlite: {new_segment_id} SUPERSEDES {old_segment_id}")

    # -- reads --------------------------------------------------------------

    def find_issue_for_topic(self, topic: str) -> int | None:
        """Best token-prefix match of the topic's terms against issue titles and slugs.

        Scores by how many of the topic's terms prefix-match a WORD in the title;
        ties break toward the lower issue number so the result is deterministic,
        which the planner needs — an unstable issue lookup makes dedup keys
        unstable, and unstable dedup keys double-fire on the reconcile pass.

        Matching is per-token, not substring, for two reasons. It mirrors what
        RediSearch does for the Falkor backend (`term*` matches whole tokens), so
        both backends give the same answer to the same question — the property
        this whole class exists to preserve. And a substring search genuinely
        gets it wrong: the topic "rate limiting" substring-matches "Migrate auth
        service to OAuth2", which would file the rate-limiting comment on the
        auth issue.
        """
        explicit = _ISSUE_NUMBER_PATTERN.match((topic or "").strip())
        if explicit:
            return int(explicit.group(1))
        # What memory REMEMBERS beats what the title search can guess. Prefix
        # matching runs one direction only ("cache*" hits "cache layer",
        # "caching*" hits nothing), and the extractor rephrases topics between
        # runs, so a title-only lookup silently dropped every card whose
        # statement did not say an issue number out loud. See the Falkor
        # docstring — both backends must answer this question identically.
        remembered = self._remembered_issue_for_topic(topic)
        if remembered is not None:
            return remembered
        terms = topic_search_terms(topic)
        if not terms:
            return None
        best_number: int | None = None
        best_score = 0
        for row in self._connection.execute("SELECT number, title, slug FROM issues ORDER BY number ASC"):
            haystack_tokens = _TERM_PATTERN.findall(f"{row['title'] or ''} {row['slug'] or ''}".lower())
            score = sum(
                1 for term in terms
                if any(token.startswith(term) for token in haystack_tokens)
            )
            if score > best_score:
                best_score = score
                best_number = int(row["number"])
        return best_number

    def _remembered_issue_for_topic(self, topic: str) -> int | None:
        """The issue a statement about this topic was already filed against, or None.

        Most-used issue wins; ties break toward the lower number so the answer is
        stable run to run, exactly as the Falkor query orders it.
        """
        key = (topic or "").strip()
        if not key:
            return None
        row = self._connection.execute(
            "SELECT issue_number, COUNT(*) AS uses FROM statements "
            "WHERE topic = ? AND issue_number IS NOT NULL "
            "GROUP BY issue_number ORDER BY uses DESC, issue_number ASC LIMIT 1",
            (key,),
        ).fetchone()
        return int(row["issue_number"]) if row and row["issue_number"] is not None else None

    def find_prior_commitments(self, topic: str, limit: int = 10) -> list[PriorCommitment]:
        handle = (topic or "").strip()
        explicit = _ISSUE_NUMBER_PATTERN.match(handle)
        if explicit:
            rows = self._connection.execute(
                "SELECT s.segment_id, s.speaker, s.topic, s.claim, s.kind, s.meeting_id, "
                "COALESCE(m.date, '') AS date FROM statements s "
                "LEFT JOIN meetings m ON m.meeting_id = s.meeting_id "
                "WHERE s.issue_number = ? ORDER BY date ASC, s.segment_id ASC LIMIT ?",
                (int(explicit.group(1)), int(limit)),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT s.segment_id, s.speaker, s.topic, s.claim, s.kind, s.meeting_id, "
                "COALESCE(m.date, '') AS date FROM statements s "
                "LEFT JOIN meetings m ON m.meeting_id = s.meeting_id "
                "WHERE s.topic = ? ORDER BY date ASC, s.segment_id ASC LIMIT ?",
                (handle, int(limit)),
            ).fetchall()
        return [
            PriorCommitment(
                segment_id=row["segment_id"], speaker=row["speaker"] or "",
                topic=row["topic"] or "", claim=row["claim"] or "", kind=row["kind"] or "",
                meeting_id=row["meeting_id"] or "", meeting_date=row["date"] or "",
            )
            for row in rows
        ]

    def known_topics(self, limit: int = 40) -> list[str]:
        rows = self._connection.execute(
            "SELECT topic, count(*) AS uses FROM statements "
            "WHERE topic IS NOT NULL AND topic != '' AND topic != 'untopiced' "
            "GROUP BY topic ORDER BY uses DESC, topic ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [row["topic"] for row in rows]

    def topic_index_by_reference(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for row in self._connection.execute(
            "SELECT linear_identifier AS handle, topic, count(*) AS uses FROM statements "
            "WHERE linear_identifier IS NOT NULL AND linear_identifier != '' "
            "AND topic != 'untopiced' GROUP BY handle, topic ORDER BY uses DESC, topic ASC"
        ):
            index.setdefault(str(row["handle"]).upper(), row["topic"])
        for row in self._connection.execute(
            "SELECT issue_number, topic, count(*) AS uses FROM statements "
            "WHERE issue_number IS NOT NULL AND topic != 'untopiced' "
            "GROUP BY issue_number, topic ORDER BY uses DESC, topic ASC"
        ):
            index.setdefault(f"#{int(row['issue_number'])}", row["topic"])
        return index

    def has_fired_dedup_key(self, dedup_key: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM actions WHERE dedup_key = ? LIMIT 1", (dedup_key,)
        ).fetchone()
        return row is not None

    def stats(self) -> dict:
        counts: dict = {"backend": BACKEND_SQLITE, "database": str(self.database_path)}
        for table, label in (
            ("meetings", "meetings"), ("statements", "statements"),
            ("commitments", "commitments"), ("issues", "issues"),
            ("actions", "actions"), ("tickets", "tickets"),
        ):
            row = self._connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            counts[label] = int(row["n"]) if row else 0
        row = self._connection.execute(
            "SELECT count(DISTINCT speaker) AS n FROM statements"
        ).fetchone()
        counts["persons"] = int(row["n"]) if row else 0
        row = self._connection.execute(
            "SELECT count(DISTINCT topic) AS n FROM statements"
        ).fetchone()
        counts["topics"] = int(row["n"]) if row else 0
        return counts

    def forget_meeting(self, meeting_id: str) -> int:
        row = self._connection.execute(
            "SELECT count(*) AS n FROM statements WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()
        removed = int(row["n"]) if row else 0
        for table in ("statements", "commitments"):
            self._connection.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (meeting_id,))
        self._connection.execute("DELETE FROM meetings WHERE meeting_id = ?", (meeting_id,))
        self._connection.commit()
        return removed

    def forget_actions(self) -> int:
        row = self._connection.execute("SELECT count(*) AS n FROM actions").fetchone()
        removed = int(row["n"]) if row else 0
        self._connection.execute("DELETE FROM actions")
        self._connection.commit()
        return removed

    def wipe(self) -> None:
        """Drop every row. Used by the demo reset, never at runtime."""
        for table in ("meetings", "statements", "commitments", "issues",
                      "actions", "tickets", "supersedes"):
            self._connection.execute(f"DELETE FROM {table}")
        self._connection.commit()


# --- shared helpers ----------------------------------------------------------


def _entity_refs_dict(statement) -> dict:
    """entity_refs as a plain dict, whatever shape the caller's Statement is in."""
    refs = getattr(statement, "entity_refs", None)
    if refs is None:
        return {}
    if isinstance(refs, dict):
        return refs
    to_dict = getattr(refs, "to_dict", None)
    return to_dict() if callable(to_dict) else {}


def _ticket_handle(action, result) -> tuple[str, str]:
    """(system, identifier) for the ticket an action touched, or ("", "").

    linear_* actions own a Linear ticket; github_update and pull_request_stub own
    a GitHub one. The identifier comes from the executor's result when it landed,
    and from the action payload when it did not — so a sim-mode run still records
    WHICH ticket it would have moved.
    """
    kind = getattr(action, "kind", "") or ""
    payload = getattr(action, "payload", None) or {}
    external_id = getattr(result, "external_id", None) or ""
    if kind.startswith("linear"):
        identifier = external_id or str(payload.get("linear_identifier") or "")
        return TICKET_SYSTEM_LINEAR, identifier
    if kind in {"github_update", "pull_request_stub"}:
        identifier = external_id or str(payload.get("issue_number") or "")
        return TICKET_SYSTEM_GITHUB, identifier
    return "", ""


def open_memory(backend: str | None = None) -> MeetingMemory:
    """Open memory on the configured backend, falling back to SQLite automatically.

    Order: requested backend (or config.memory_backend()) -> on any connection
    failure, log loudly and return SqliteMeetingMemory. This function never
    raises: losing memory should degrade the demo, not end it.

    The fallback is LOUD on purpose. A silent downgrade to SQLite would leave
    Sharique telling founders a FalkorDB story over a SQLite file.
    """
    requested = (backend or config.memory_backend()).lower()
    if requested == BACKEND_FALKOR:
        try:
            memory = FalkorMeetingMemory()
            memory.ensure_schema()
            print(f"[memory] falkor: connected to {memory.host}:{memory.port}, graph {memory.graph_name!r}")
            return memory
        except Exception as error:  # noqa: BLE001 — any connection problem falls back
            print("=" * 72)
            print(f"[memory] FALKORDB UNREACHABLE ({error.__class__.__name__}: {error})")
            print("[memory] FALLING BACK TO SQLITE — the graph story is not live this run.")
            print(f"[memory] start it with:  docker start falkordb-test")
            print("=" * 72)
    memory = SqliteMeetingMemory()
    memory.ensure_schema()
    print(f"[memory] sqlite: {memory.database_path}")
    return memory


def describe_memory() -> dict:
    """Backend and counts, for logs and the board footer. Never raises."""
    try:
        memory = open_memory()
    except Exception as error:  # noqa: BLE001 — describe must not be the thing that breaks
        return {"backend": "unavailable", "error": f"{error.__class__.__name__}: {error}"}
    try:
        return memory.stats()
    finally:
        memory.close()


if __name__ == "__main__":
    print(json.dumps(describe_memory(), indent=2, default=str))
