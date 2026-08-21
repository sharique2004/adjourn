"""Cloud mirror — pushes the local graph and execution receipts to FalkorDB Cloud.

The Mac is authoritative. This module is a *one-way, best-effort* mirror of what
already happened locally, so the Vercel web surface has something to read. It is
the only place in Adjourn that talks to the cloud graph.

LOCAL-FIRST DISCIPLINE (the rule this module exists to enforce):
  * Every public function catches EVERY exception. Nothing in here ever raises
    into the pipeline. A dead wifi connection must not stall a meeting.
  * Every public function is bounded to `TOTAL_BUDGET_SECONDS` (8s, sized to the
    ~2.6s round trip this instance actually measures). Connect and read timeouts
    are 4s each and they sum to that ceiling, so even a peer that accepts the TCP
    connection and then goes silent cannot overrun it. A mirror_* call opens ONE
    connection and reuses it for both the write and the opportunistic flush, so
    the budget is never spent twice. In the ordinary offline case (no route to
    the host) a call returns in ~4s, on a daemon thread nobody is waiting on.
  * On any failure the payload is appended to `state/mirror_backlog.jsonl` and
    the function returns False. Nothing is lost; it just goes later.
  * `flush_backlog()` replays that file, oldest first, whenever the cloud turns
    out to be reachable. It is called opportunistically at the top of each
    `mirror_*` call and is bounded to `FLUSH_LIMIT` lines per pass.

Graph schema written here (graph name from FALKORDB_GRAPH, default "adjourn"):
    (Person)-[:SAID]->(Statement)-[:ABOUT]->(Issue)
    (Statement)-[:IN_MEETING]->(Meeting {id, title, date})
    (Statement)-[:SUPERSEDES]->(Statement)
    (Execution {...ExecutorResult fields, undo_payload as JSON string})
        -[:FROM_MEETING]->(Meeting)

Idempotency keys: Meeting.id, Statement.segment_id, Person.name, Issue.key, and
Execution on the (meeting_id, kind, fired_at) triple. Mirroring the same meeting
twice is a no-op, which is what makes the backlog safe to replay.

Environment (adjourn/.env is PARSED, but never exported into os.environ — see
`_load_env_once`, and `config._quarantine_secret_environment` for the same rule
applied to the variables python-dotenv has already exported):
    FALKORDB_CLOUD_HOST, FALKORDB_CLOUD_PORT, FALKORDB_CLOUD_USERNAME,
    FALKORDB_CLOUD_PASSWORD, FALKORDB_CLOUD_TLS, FALKORDB_GRAPH (default "adjourn")

TLS defaults on for every host except loopback, so a local container can be used
for integration testing without certificates. FALKORDB_CLOUD_TLS=0 turns it off,
and the free-tier cloud instance this was built against requires that: it does
not answer a TLS handshake at all (measured — a TLS connect times out where a
plaintext one returns in 2.3s). So on that instance the AUTH handshake and every
mirrored row cross the network in the clear. Two consequences, both deliberate:
`uses_tls()` is reported in `describe_target()` and printed by `--probe` so the
transport is never a surprise, and the password is treated as disposable — it
protects a graph holding nothing but a mirror of what is already on the Mac.
Anything genuinely private must not be added to the mirrored field lists while
that is true.

Run from the repo root:
    python -m adjourn.cloud_mirror --probe
    python -m adjourn.cloud_mirror --backfill
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

# --- package geography ------------------------------------------------------
# Deliberately self-contained: this module imports no other adjourn module, so
# it can be built and tested while the rest of the package is still landing.

PACKAGE_ROOT = Path(__file__).resolve().parent
STATE_DIR = PACKAGE_ROOT / "state"
ENV_PATH = PACKAGE_ROOT / ".env"


def state_dir() -> Path:
    """Where the backlog lives, honouring ADJOURN_STATE_DIR.

    A function rather than a constant so a test can point it somewhere
    disposable. It could not, and so a suite running with the cloud host blanked
    — which is how a test says "do not touch the network" — dropped its queued
    writes into the SHIPPED tree as state/mirror_backlog.jsonl, in violation of
    the final-state contract it was trying to respect.

    This module deliberately imports nothing else from adjourn, so it reads the
    variable itself rather than calling config.state_directory().
    """
    override = (os.environ.get("ADJOURN_STATE_DIR") or "").strip()
    return Path(override).expanduser() if override else STATE_DIR


def backlog_path() -> Path:
    """Queued mirror writes that have not reached the cloud yet."""
    return state_dir() / "mirror_backlog.jsonl"

# The local MeetingScribe journal that --backfill replays.
EXECUTIONS_JOURNAL_PATH = Path.home() / ".meetingscribe" / "executions.jsonl"

# --- budgets ----------------------------------------------------------------

# Connect and socket caps sum to the total budget, so even the pathological
# "TCP connects, then the peer goes silent" case cannot exceed TOTAL_BUDGET.
# A mirror_* call opens ONE connection and reuses it for the opportunistic
# flush and the write, so the budget is spent once, not twice.
#
# SIZED TO THE MEASURED ROUND TRIP, not to a round number. `probe()` reports
# ~2.6s to the configured FalkorDB Cloud instance from this machine, so a 1.5s
# connect cap could not complete a single write: nine of eleven mirror writes
# per run were failing straight into the local backlog, and the cloud graph was
# effectively never current. This runs on a daemon thread off the hot path — the
# only thing that ever waits on it is drain_cloud_mirrors() at exit, whose own
# budget scales with the queue — so the cost of a longer cap is nothing a person
# experiences, and the benefit is a graph you can actually put on screen.
CONNECT_TIMEOUT_SECONDS = 4.0
SOCKET_TIMEOUT_SECONDS = 4.0
TOTAL_BUDGET_SECONDS = 8.0
FLUSH_LIMIT = 50

DEFAULT_GRAPH = "adjourn"
DEFAULT_PORT = 6379
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

# The eight kinds an ExecutorResult may carry, and the flat property set the web
# surface reads. `undo_payload` is stored as a JSON string — FalkorDB properties
# are scalars, not maps.
EXECUTION_KINDS = (
    "github_update",
    "linear_create",
    "linear_move",
    "pull_request_stub",
    "slack_send",
    "email_send",
    "calendar_hold",
    "recap_page",
)
EXECUTION_FIELDS = (
    "ok",
    "kind",
    "external_id",
    "url",
    "human_summary",
    "mode",
    "quote",
    "speaker",
    "meeting_id",
    "fired_at",
)

# Statement properties worth carrying to the cloud. Anything else on the dict is
# dropped rather than guessed at, so the cloud schema stays predictable.
STATEMENT_FIELDS = (
    "segment_id",
    "kind",
    "text",
    "speaker",
    "issue",
    "meeting_id",
    "started_at",
    "confidence",
    "assignee",
    "due",
)

STATEMENT_KINDS = (
    "decision",
    "update",
    "assignment",
    "question",
    "ticket_request",
    "progress_report",
    "message_commitment",
    "email_commitment",
    "deadline",
    "pr_intent",
)

# Guards against flush_backlog() -> mirror_*() -> flush_backlog() recursion.
_flush_in_progress = False
_dotenv_loaded = False


# --- environment ------------------------------------------------------------


_ENV_FILE_SETTINGS: dict[str, str] = {}


def _load_env_once() -> None:
    """Parse adjourn/.env exactly once, into THIS MODULE — never into os.environ.

    This used to call `load_dotenv`, which exports every line of the file into
    the process environment, where FALKORDB_CLOUD_PASSWORD is then inherited by
    every child process the package spawns (the `claude` CLI, `gh`, the calendar
    helper) and is readable from outside through `ps -E`. The password this
    module needs is needed by this module and nothing else, so it is read into a
    private dict and stays there. `config` performs the same quarantine on the
    variables `load_dotenv` has already exported by the time it runs.

    Missing file, unreadable file, malformed line: all fine, all silent. This is
    a best-effort mirror, and a parse error here must not stall a meeting.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        text = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            _ENV_FILE_SETTINGS[name] = value


def _setting(name: str, default: str = "") -> str:
    """One setting: the process environment wins, then adjourn/.env, then `default`.

    Presence in os.environ wins even when the value is empty — an explicitly
    blanked variable is how a caller (and every test in test_cloud_mirror) says
    "pretend this is not configured", and falling through to the .env file would
    silently point a test at the real cloud instance.
    """
    if name in os.environ:
        return (os.environ.get(name) or "").strip() or default
    _load_env_once()
    return (_ENV_FILE_SETTINGS.get(name) or "").strip() or default


def cloud_host() -> str:
    return _setting("FALKORDB_CLOUD_HOST")


def cloud_port() -> int:
    raw = _setting("FALKORDB_CLOUD_PORT")
    try:
        return int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def cloud_username() -> str:
    return _setting("FALKORDB_CLOUD_USERNAME")


def cloud_password() -> str:
    return _setting("FALKORDB_CLOUD_PASSWORD")


def graph_name() -> str:
    return _setting("FALKORDB_GRAPH", DEFAULT_GRAPH)


def uses_tls(host: str) -> bool:
    """FALKORDB_CLOUD_TLS overrides ("0"/"1"); otherwise TLS everywhere except
    loopback. The current free-tier cloud endpoint speaks plain Redis protocol,
    so its .env sets FALKORDB_CLOUD_TLS=0."""
    override = _setting("FALKORDB_CLOUD_TLS", "").strip()
    if override in ("0", "1"):
        return override == "1"
    return host.strip().lower() not in LOOPBACK_HOSTS


def is_configured() -> bool:
    """True when a cloud host is set. Without one the web surface runs on demo data."""
    return bool(cloud_host())


def describe_target() -> dict:
    """Non-secret snapshot of where this module would write. Safe to print or log."""
    host = cloud_host()
    return {
        "configured": bool(host),
        "host": host or None,
        "port": cloud_port() if host else None,
        "graph": graph_name(),
        "tls": uses_tls(host) if host else None,
        "username_set": bool(cloud_username()),
        "password_set": bool(cloud_password()),
        "backlog_path": str(backlog_path()),
        "backlog_lines": backlog_depth(),
    }


# --- value coercion ---------------------------------------------------------


def _scalar(value: Any) -> Any:
    """Coerce one value into something FalkorDB can hold as a property.

    Scalars pass through. Maps and lists are JSON-encoded rather than dropped,
    so nothing silently disappears on the way to the cloud.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except Exception:
        return str(value)


def _properties(source: dict, fields: Iterable[str]) -> dict:
    """Pick `fields` off `source`, coerced to graph-safe scalars. Nulls dropped."""
    out = {}
    for field in fields:
        value = _scalar(source.get(field))
        if value is not None:
            out[field] = value
    return out


# --- connection -------------------------------------------------------------


class _Deadline:
    """A wall-clock budget. Keeps any public call from chaining work past ~3s."""

    def __init__(self, budget: float = TOTAL_BUDGET_SECONDS) -> None:
        self._expires_at = time.monotonic() + budget

    @property
    def remaining(self) -> float:
        return self._expires_at - time.monotonic()

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0


def _connect(deadline: _Deadline | None = None):
    """Open a graph handle against the configured cloud (or local) FalkorDB.

    Raises on misconfiguration or connection failure — callers are responsible
    for catching. The handle is lazy: the socket is not touched until a query.
    """
    from falkordb import FalkorDB

    host = cloud_host()
    if not host:
        raise RuntimeError("FALKORDB_CLOUD_HOST is not set")

    connect_timeout = CONNECT_TIMEOUT_SECONDS
    socket_timeout = SOCKET_TIMEOUT_SECONDS
    if deadline is not None:
        connect_timeout = max(0.1, min(connect_timeout, deadline.remaining))
        socket_timeout = max(0.1, min(socket_timeout, deadline.remaining))

    options: dict[str, Any] = {
        "host": host,
        "port": cloud_port(),
        "socket_connect_timeout": connect_timeout,
        "socket_timeout": socket_timeout,
    }
    username = cloud_username()
    password = cloud_password()
    if username:
        options["username"] = username
    if password:
        options["password"] = password
    if uses_tls(host):
        options["ssl"] = True

    return FalkorDB(**options).select_graph(graph_name())


def probe() -> tuple[bool, str]:
    """(reachable, human message). Never raises. Bounded by the standard budget."""
    if not is_configured():
        return False, "not configured — FALKORDB_CLOUD_HOST is unset (demo data mode)"
    host, port = cloud_host(), cloud_port()
    started = time.monotonic()
    try:
        graph = _connect(_Deadline())
        graph.query("RETURN 1")
        elapsed = time.monotonic() - started
        transport = "TLS" if uses_tls(host) else "plaintext"
        return True, f"reachable: {host}:{port} graph={graph_name()} ({transport}) in {elapsed:.2f}s"
    except Exception as error:
        elapsed = time.monotonic() - started
        return False, f"unreachable: {host}:{port} — {type(error).__name__}: {error} after {elapsed:.2f}s"


# --- backlog ----------------------------------------------------------------


def _append_backlog(operation: str, payload: dict) -> None:
    """Append one deferred write. Best-effort: a failure here is swallowed too."""
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"op": operation, "payload": payload, "queued_at": _now_iso()},
            default=str,
        )
        with backlog_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        # If we cannot even write the backlog, the local pipeline still wins.
        pass


def backlog_depth() -> int:
    """How many writes are waiting on the cloud. Never raises."""
    try:
        if not backlog_path().exists():
            return 0
        with backlog_path().open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except Exception:
        return 0


def _read_backlog() -> list[dict]:
    entries = []
    try:
        if not backlog_path().exists():
            return entries
        with backlog_path().open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    # A torn line is dropped rather than blocking the queue.
                    continue
    except Exception:
        return []
    return entries


def _rewrite_backlog(entries: list[dict]) -> None:
    """Atomically replace the backlog with whatever is still pending."""
    try:
        if not entries:
            backlog_path().unlink(missing_ok=True)
            return
        state_dir().mkdir(parents=True, exist_ok=True)
        temporary = backlog_path().with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, default=str) + "\n")
        temporary.replace(backlog_path())
    except Exception:
        pass


def flush_backlog(limit: int = FLUSH_LIMIT, deadline: _Deadline | None = None, graph=None) -> int:
    """Replay up to `limit` queued writes. Returns how many landed. Never raises.

    Stops at the first network failure and keeps that entry (and everything
    after it) so ordering is preserved. Re-entrant calls are no-ops, which is
    what lets the mirror_* functions call this opportunistically.

    `deadline` and `graph` let a caller share its budget and its already-open
    connection, so an opportunistic flush costs no extra connect.
    """
    global _flush_in_progress
    if _flush_in_progress:
        return 0

    entries = _read_backlog()
    if not entries:
        return 0

    _flush_in_progress = True
    if deadline is None:
        deadline = _Deadline()
    replayed = 0
    try:
        if graph is None:
            graph = _connect(deadline)
        for index, entry in enumerate(entries[:limit]):
            if deadline.expired:
                break
            try:
                _dispatch(graph, entry.get("op", ""), entry.get("payload") or {})
            except (_UnknownOperation, ValueError, TypeError, KeyError):
                # Structurally undeliverable (bad or unknown payload). Drop it —
                # retrying forever would wedge every entry behind it. Anything
                # else propagates, because "the cloud is down" must NOT drop data.
                pass
            replayed = index + 1
    except Exception:
        pass
    finally:
        _flush_in_progress = False

    if replayed:
        _rewrite_backlog(entries[replayed:])
    return replayed


class _UnknownOperation(Exception):
    """A backlog line naming an operation this version does not understand."""


def _dispatch(graph, operation: str, payload: dict) -> None:
    if operation == "mirror_meeting":
        _write_meeting(graph, payload.get("meta") or {}, payload.get("statements") or [])
    elif operation == "mirror_execution":
        _write_execution(graph, payload.get("result") or {})
    else:
        raise _UnknownOperation(operation)


# --- writes (these raise; the public wrappers catch) ------------------------

_MEETING_QUERY = """
MERGE (m:Meeting {id: $meeting.id})
SET m += $meeting
WITH m
UNWIND $statements AS st
  MERGE (s:Statement {segment_id: st.segment_id})
  SET s += st.props
  MERGE (s)-[:IN_MEETING]->(m)
  FOREACH (_ IN CASE WHEN st.speaker IS NULL THEN [] ELSE [1] END |
    MERGE (p:Person {name: st.speaker})
    MERGE (p)-[:SAID]->(s))
  FOREACH (_ IN CASE WHEN st.issue IS NULL THEN [] ELSE [1] END |
    MERGE (i:Issue {key: st.issue})
    MERGE (s)-[:ABOUT]->(i))
RETURN count(s)
"""

_MEETING_ONLY_QUERY = """
MERGE (m:Meeting {id: $meeting.id})
SET m += $meeting
RETURN m.id
"""

_SUPERSEDES_QUERY = """
UNWIND $edges AS e
MATCH (a:Statement {segment_id: e.from_id})
MATCH (b:Statement {segment_id: e.to_id})
MERGE (a)-[:SUPERSEDES]->(b)
"""

_EXECUTION_QUERY = """
MERGE (e:Execution {meeting_id: $key.meeting_id, kind: $key.kind, fired_at: $key.fired_at})
SET e += $props
WITH e
MERGE (m:Meeting {id: $key.meeting_id})
MERGE (e)-[:FROM_MEETING]->(m)
RETURN e.kind
"""


def _meeting_payload(meta: dict) -> dict:
    """The Meeting node's properties. `id` is required and is the merge key."""
    meeting_id = _scalar(meta.get("id") or meta.get("meeting_id"))
    if not meeting_id:
        raise ValueError("meeting meta needs an 'id'")
    payload = _properties(meta, ("title", "date"))
    payload["id"] = meeting_id
    return payload


def display_speaker(label: Any) -> Any:
    """"Them" -> ADJOURN_GUEST_NAME, "You" -> the presenter. Never raises.

    THE LAST GATE, and it earns its place by having been missed. Statements that
    go through extraction are relabelled there and arrive here already readable —
    but a SEEDED meeting, a backfill from the journal, or anything reconstructed
    from a stored dict bypasses that pass entirely, and the public graph spent a
    week labelling half its Statement nodes "Them". This module writes to a graph
    strangers can read, so it does the rename itself rather than trusting that
    every caller already did.

    Falls back to the raw label if extraction cannot be imported, because a
    cosmetic rename must never cost a mirror write.
    """
    text = _scalar(label)
    if not isinstance(text, str) or not text.strip():
        return text
    try:
        from .extraction import speaker_display_name

        return speaker_display_name(text)
    except Exception:  # noqa: BLE001
        return text


def _statement_rows(statements: list[dict], meeting_id: Any) -> list[dict]:
    """Shape statements into UNWIND rows. Statements without a segment_id are skipped."""
    rows = []
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        segment_id = _scalar(statement.get("segment_id"))
        if not segment_id:
            continue
        props = _properties(statement, STATEMENT_FIELDS)
        props["segment_id"] = segment_id
        props.setdefault("meeting_id", meeting_id)
        speaker = display_speaker(statement.get("speaker"))
        if speaker:
            props["speaker"] = speaker
        rows.append(
            {
                "segment_id": segment_id,
                "speaker": speaker or None,
                "issue": _scalar(statement.get("issue")) or None,
                "props": props,
            }
        )
    return rows


def _supersedes_rows(statements: list[dict]) -> list[dict]:
    """SUPERSEDES edges, read off each statement's `supersedes` field."""
    edges = []
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        source = _scalar(statement.get("segment_id"))
        if not source:
            continue
        targets = statement.get("supersedes")
        if targets is None:
            continue
        if not isinstance(targets, (list, tuple, set)):
            targets = [targets]
        for target in targets:
            target_id = _scalar(target)
            if target_id and target_id != source:
                edges.append({"from_id": source, "to_id": target_id})
    return edges


def _write_meeting(graph, meta: dict, statements: list[dict]) -> None:
    meeting = _meeting_payload(meta)
    rows = _statement_rows(statements or [], meeting["id"])
    if rows:
        graph.query(_MEETING_QUERY, {"meeting": meeting, "statements": rows})
    else:
        graph.query(_MEETING_ONLY_QUERY, {"meeting": meeting})
    edges = _supersedes_rows(statements or [])
    if edges:
        graph.query(_SUPERSEDES_QUERY, {"edges": edges})


def _write_execution(graph, result: dict) -> None:
    key = {
        "meeting_id": _scalar(result.get("meeting_id")),
        "kind": _scalar(result.get("kind")),
        "fired_at": _scalar(result.get("fired_at")),
    }
    if not key["meeting_id"] or not key["kind"] or not key["fired_at"]:
        raise ValueError("execution needs meeting_id, kind and fired_at to be idempotent")

    props = _properties(result, EXECUTION_FIELDS)
    props.update(key)
    # Same rename as the statement rows: an Execution carries the speaker onto
    # the public board, and "Them" reads as a bug there too.
    speaker = display_speaker(result.get("speaker"))
    if speaker:
        props["speaker"] = speaker
    # undo_payload is a dict on the Mac side; FalkorDB holds scalars only.
    undo = result.get("undo_payload")
    props["undo_payload"] = json.dumps(undo or {}, default=str, sort_keys=True)
    graph.query(_EXECUTION_QUERY, {"key": key, "props": props})


# --- public surface ---------------------------------------------------------


def mirror_meeting(meta: dict, statements: list[dict]) -> bool:
    """Mirror one meeting and its statements. Returns True if the cloud took it.

    Never raises and never blocks past ~3s. On any failure the payload lands in
    state/mirror_backlog.jsonl and the local pipeline carries on unaffected.
    """
    payload = {"meta": meta, "statements": statements}
    if not is_configured():
        _append_backlog("mirror_meeting", payload)
        return False
    try:
        deadline = _Deadline()
        graph = _connect(deadline)
        _write_meeting(graph, meta, statements)
        # Only once the write proved the cloud is up is a flush worth attempting,
        # and it reuses this connection and what is left of this budget.
        flush_backlog(deadline=deadline, graph=graph)
        return True
    except Exception:
        _append_backlog("mirror_meeting", payload)
        return False


def mirror_execution(result_dict: dict) -> bool:
    """Mirror one ExecutorResult as an (:Execution)-[:FROM_MEETING]->(:Meeting).

    Never raises and never blocks past ~3s. Same backlog-on-failure contract as
    `mirror_meeting`.
    """
    payload = {"result": result_dict}
    if not is_configured():
        _append_backlog("mirror_execution", payload)
        return False
    try:
        deadline = _Deadline()
        graph = _connect(deadline)
        _write_execution(graph, result_dict)
        flush_backlog(deadline=deadline, graph=graph)
        return True
    except Exception:
        _append_backlog("mirror_execution", payload)
        return False


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# --- backfill ---------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    """Every well-formed JSON object in a .jsonl file. Missing file returns []."""
    records = []
    try:
        if not path.exists():
            return records
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except Exception:
        return records
    return records


def backfill() -> dict:
    """Replay the local execution journal and any queued backlog into the cloud.

    Returns a small counts dict. Never raises — a backfill against a dead cloud
    just re-queues everything into the backlog.
    """
    counts = {"executions": 0, "mirrored": 0, "failed": 0, "flushed": 0}

    records = _read_jsonl(EXECUTIONS_JOURNAL_PATH)
    counts["executions"] = len(records)
    for record in records:
        # The journal may wrap the result, or be the result itself.
        result = record.get("result") if isinstance(record.get("result"), dict) else record
        if mirror_execution(result):
            counts["mirrored"] += 1
        else:
            counts["failed"] += 1

    # Drain whatever is queued, in bounded passes. The pass cap keeps a backlog
    # file that refuses to shrink (a read-only state dir, say) from spinning.
    for _ in range(100):
        moved = flush_backlog()
        if not moved:
            break
        counts["flushed"] += moved

    counts["backlog_remaining"] = backlog_depth()
    return counts


# --- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adjourn.cloud_mirror",
        description="Mirror the local Adjourn graph and execution receipts to FalkorDB Cloud.",
    )
    parser.add_argument("--probe", action="store_true", help="print connection status and exit")
    parser.add_argument("--backfill", action="store_true", help="replay the execution journal and backlog")
    parser.add_argument("--flush", action="store_true", help="replay the backlog only")
    arguments = parser.parse_args(argv)

    if not (arguments.probe or arguments.backfill or arguments.flush):
        parser.print_help()
        return 2

    if arguments.probe:
        reachable, message = probe()
        print(json.dumps(describe_target(), indent=2))
        print(message)
        return 0 if reachable else 1

    if arguments.flush:
        print(f"flushed {flush_backlog()} entries; {backlog_depth()} remaining")
        return 0

    reachable, message = probe()
    print(message)
    counts = backfill()
    print(json.dumps(counts, indent=2))
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
