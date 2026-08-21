"""linear_move — move an existing Linear ticket between workflow states.

Payload shape:
    {
      "linear_identifier": "ENG-142",   # spoken in the meeting, or resolved from memory
      "target_state": "In Progress",    # optional; derived from percent/language when absent
      "percent": 70,                    # optional; from entity_refs.percent
      "title_hint": "cache layer",      # optional; fuzzy fallback when no identifier was said
      "comment_markdown": "...",        # optional; the quote is posted as a comment too
      "meeting_title": "Eng sync",
      "human_preview": "Linear: ENG-142 -> In Progress"
    }

The percent -> state mapping is DETERMINISTIC and lives in this module as a pure
function (`choose_target_state`). No model is consulted about where a ticket goes:

    "in review" / "up for review" / "PR is up"        In Review
    a completion claim ("basically wrapped up",
      "finished", "shipped it this morning")           In Review
    >= 90%                                             In Review
    25% .. 89%                                         In Progress
    a work claim ("I'm working on SHA-6",
      "picking it up today"), from an unstarted
      column only                                      In Progress
    anything else                                      leave it where it is

There is no STATE_DONE, on purpose: a meeting can start work and send it for
review, and a human still closes the ticket. A move that would go DOWN the board
is refused at execute time by is_backward_move().

Live transport: GraphQL POST to https://api.linear.app/graphql with a RAW
`Authorization: lin_api_...` header (no "Bearer " prefix).

    issueUpdate(id: $id, input: {stateId: $stateId}) { success issue { id url } }

Undo: move the issue back to the state captured in undo_payload["previous_state_id"].
That is why execute() reads the current state BEFORE writing the new one.

Secrets: linear_api_key. Absent => sim mode, loudly.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from .. import config, results, secrets_store
from .linear_create_executor import post_linear_graphql

if TYPE_CHECKING:
    from ..planner import Action

ACTION_KIND = "linear_move"
REQUIRED_SECRETS: tuple[str, ...] = (secrets_store.LINEAR_API_KEY,)

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
HTTP_TIMEOUT_SECONDS = 10

# The identifier as a human says it: "ENG-142", "OPS-7".
IDENTIFIER_PATTERN = re.compile(r"\b([A-Z]{2,5})-(\d{1,5})\b")

# Speech recognition eats the hyphen. Saying "SHA-5" out loud into a real meeting
# produced the caption "SHA5" on this machine, and "SHA 5" is just as likely — so
# a ticket the room clearly named would silently fall through to a fuzzy title
# search, or to nothing. This second pattern accepts a space or no separator at
# all, and is only ever trusted when the letters match a team key we KNOW exists
# (see find_identifier). Without that guard it would happily read "MP3", "H264"
# or "OAUTH2" as ticket keys.
LOOSE_IDENTIFIER_TEMPLATE = r"\b({keys})[\s-]?(\d{{1,5}})\b"

STATE_IN_PROGRESS = "In Progress"
STATE_IN_REVIEW = "In Review"
IN_REVIEW_PHRASES = ("in review", "up for review", "pr is up", "ready for review", "review it")

# THE PHRASE TABLE, and why it exists. Before it, a ticket moved only on a
# literal percentage or the literal words "in review". An auditor said, in the
# most ordinary sentence imaginable — "yeah, so I'm basically wrapped up on the
# cache layer one, SHA six" — and the pipeline understood it perfectly
# (kind=progress_report, linear_identifier=SHA-6) and then did nothing at all.
# "Basically done", "finished", and even "done, shipped it this morning" all
# returned None. The demo fixture only worked because it was written with belt
# AND braces ("about eighty percent done ... should be in review tomorrow"),
# which is to say it worked because nobody had improvised yet.
#
# Still a pure table. No model gets a say in where a ticket goes.

# The work is finished or as good as. Lands In Review — NOT done: closing
# somebody's ticket because a sentence sounded confident is a bridge too far,
# and there is deliberately no STATE_DONE in this module.
COMPLETION_PHRASES: tuple[str, ...] = (
    "wrapped up", "wrapped it up", "basically done", "pretty much done",
    "nearly done", "almost done", "all but done", "as good as done",
    "it's done", "its done", "is done", "that's done", "thats done",
    "done and dusted", "finished it", "is finished", "i'm finished",
    "shipped it", "shipped that", "just shipped", "merged it", "already merged",
    "landed it", "it landed", "code complete", "feature complete",
)

# Somebody is CLAIMING the work — it is now underway. Lands In Progress, and
# only ever from an unstarted column (see is_backward_move): "I'm working on
# SHA-6" said about a ticket already in review must not drag it backwards.
WORK_CLAIM_PHRASES: tuple[str, ...] = (
    "working on", "i'm on it", "im on it", "on it now",
    "started on", "started the", "started it", "i've started", "ive started",
    "picking it up", "picking that up", "picking up", "picked it up", "picked that up",
    "digging into", "digging in on", "taking a run at", "taking that on",
    "taking it on", "kicking off", "getting into", "i'll take", "ill take",
    "having a go at",
)

# Where each column sits on the road to done, for the never-move-backwards rule.
STATE_RANK: dict[str, int] = {
    "backlog": 0, "todo": 0, "to do": 0, "unstarted": 0, "triage": 0, "icebox": 0,
    "in progress": 1, "started": 1, "doing": 1, "in dev": 1,
    "in review": 2, "review": 2, "code review": 2, "in qa": 2,
    "done": 3, "completed": 3, "shipped": 3, "closed": 3, "canceled": 3, "cancelled": 3,
}
UNSTARTED_STATES: frozenset[str] = frozenset(
    name for name, rank in STATE_RANK.items() if rank == 0
)

# Synonyms so a workspace that renamed its columns still resolves.
STATE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "in progress": ("in progress", "started", "doing", "in dev"),
    "in review": ("in review", "review", "code review", "in qa"),
    "done": ("done", "completed", "shipped", "closed"),
    "todo": ("todo", "to do", "backlog", "unstarted"),
}

FIND_ISSUE_QUERY = """
query FindIssue($identifier: String!) {
  issue(id: $identifier) {
    id identifier url title
    state { id name }
    team { id key }
  }
}
"""

SEARCH_ISSUES_QUERY = """
query SearchIssues($term: String!) {
  issueSearch(query: $term, first: 5) {
    nodes {
      id identifier url title
      state { id name }
      team { id key }
    }
  }
}
"""

TEAM_STATES_QUERY = """
query TeamStates($teamId: String!) {
  team(id: $teamId) {
    states(first: 50) { nodes { id name type position } }
  }
}
"""

UPDATE_ISSUE_MUTATION = """
mutation MoveIssue($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: {stateId: $stateId}) {
    success
    issue { id identifier url state { id name } }
  }
}
"""

CREATE_COMMENT_MUTATION = """
mutation CommentOnIssue($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
    comment { id url }
  }
}
"""


# --- pure decisions ---------------------------------------------------------


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def known_team_keys() -> tuple[str, ...]:
    """Team keys the loose pattern is allowed to recognise without a hyphen.

    The configured team, plus whatever the team-id cache learned from the
    workspace. Reading the cache rather than the network keeps this pure enough to
    call from the planner and cheap enough to call per statement.
    """
    keys = {config.linear_team_key()}
    try:
        from .linear_create_executor import _read_team_cache

        keys.update(str(key).upper() for key in _read_team_cache())
    except Exception:  # noqa: BLE001 — no cache yet is not an error
        pass
    return tuple(sorted(key for key in keys if key.isalpha() and 2 <= len(key) <= 5))


def find_identifier(*candidates: object) -> str | None:
    """First 'SHA-5'-shaped token in the candidates, in the order given. Pure-ish.

    Two passes over each candidate. The strict pattern (letters, hyphen, digits)
    runs first everywhere. Only if nothing matches does the loose pattern run, and
    only for team keys this workspace actually has — so a caption reading "SHA5"
    or "SHA 5" resolves, while "MP3" and "H264" do not.
    """
    texts = [str(candidate or "").upper() for candidate in candidates]
    for text in texts:
        match = IDENTIFIER_PATTERN.search(text)
        if match:
            return f"{match.group(1)}-{match.group(2)}"

    keys = known_team_keys()
    if not keys:
        return None
    loose = re.compile(LOOSE_IDENTIFIER_TEMPLATE.format(keys="|".join(keys)))
    for text in texts:
        match = loose.search(text)
        if match:
            identifier = f"{match.group(1)}-{match.group(2)}"
            print(f"[{ACTION_KIND}] read {match.group(0)!r} as {identifier} (caption lost the hyphen)")
            return identifier
    return None


def coerce_percent(value: object) -> int | None:
    """70, "70", "70%", 70.0 -> 70. Anything unparseable -> None. Pure."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().rstrip("%")
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def choose_target_state(
    percent: int | None,
    spoken_text: str = "",
    current_state: str | None = None,
) -> str | None:
    """Where a progress report sends a ticket. Deterministic table, never a model.

    Returns None for "leave it alone", which is a real and common answer.

    Precedence, most explicit first:
        1. the words "in review" / "up for review" / "pr is up"  -> In Review
        2. a completion claim ("basically wrapped up", "shipped it")  -> In Review
        3. percent >= 90  -> In Review;  percent >= 25  -> In Progress
        4. a work claim ("I'm working on it", "picking it up")  -> In Progress,
           but ONLY from an unstarted column. A work claim is the weakest signal
           in the table, so it is the one that has to prove the ticket has
           somewhere to go.

    `current_state` is optional and additive: the planner does not know it (the
    ticket has not been fetched yet) and passes None, which reads as "unknown,
    allow it" — the executor re-checks against the real column before writing,
    and is_backward_move() is the thing that actually stops a regression.

        >>> choose_target_state(None, "I'm basically wrapped up on SHA-6")
        'In Review'
        >>> choose_target_state(None, "I am working on this issue")
        'In Progress'
        >>> choose_target_state(None, "I am working on this issue", "In Review")
        >>> choose_target_state(None, "the numbers look fine")
    """
    text = _one_line(spoken_text).lower()
    if any(phrase in text for phrase in IN_REVIEW_PHRASES):
        return STATE_IN_REVIEW
    if any(phrase in text for phrase in COMPLETION_PHRASES):
        return STATE_IN_REVIEW
    if percent is not None:
        if percent >= 90:
            return STATE_IN_REVIEW
        if percent >= 25:
            return STATE_IN_PROGRESS
    if any(phrase in text for phrase in WORK_CLAIM_PHRASES):
        column = _one_line(current_state).lower()
        if not column or column in UNSTARTED_STATES:
            return STATE_IN_PROGRESS
    return None


def is_backward_move(current_state: str | None, target_state: str) -> bool:
    """True when `target_state` would drag a ticket back down the board. Pure.

    An unranked column (a workspace with a bespoke state name) is never treated
    as backwards — refusing to move a ticket because we do not recognise the
    column it is in would be a worse failure than moving it.

        >>> is_backward_move("In Review", "In Progress")
        True
        >>> is_backward_move("Backlog", "In Progress")
        False
    """
    current = STATE_RANK.get(_one_line(current_state).lower())
    target = STATE_RANK.get(_one_line(target_state).lower())
    if current is None or target is None:
        return False
    return target < current


def build_comment_markdown(action: Action, previous_state: str, target_state: str) -> str:
    """The comment posted alongside the move: quote, speaker, meeting. Pure."""
    supplied = action.payload.get("comment_markdown")
    if supplied:
        return str(supplied)
    meeting = _one_line(action.payload.get("meeting_title", "")) or "an untitled meeting"
    speaker = _one_line(action.speaker) or "someone in the room"
    quote = _one_line(action.quote)
    lines = [f"Moved **{previous_state or 'its previous state'} → {target_state}** by Adjourn."]
    if quote:
        lines += ["", f"> **{speaker}:** {quote}"]
    lines += ["", f"_Said during {meeting}; the transcript never left that machine._"]
    return "\n".join(lines) + "\n"


def matches_state_name(candidate_name: str, wanted_name: str) -> bool:
    """Case-insensitive state match, widened by STATE_SYNONYMS. Pure."""
    candidate = _one_line(candidate_name).lower()
    wanted = _one_line(wanted_name).lower()
    if candidate == wanted:
        return True
    for synonyms in STATE_SYNONYMS.values():
        if wanted in synonyms and candidate in synonyms:
            return True
    return False


# --- id resolution ----------------------------------------------------------


def find_issue(identifier: str | None, title_hint: str, api_key: str) -> dict | None:
    """The issue node for an identifier, falling back to a fuzzy title search."""
    if identifier:
        try:
            response = post_linear_graphql(FIND_ISSUE_QUERY, {"identifier": identifier}, api_key)
            issue = (response.get("data") or {}).get("issue")
            if issue:
                return issue
        except Exception as error:  # noqa: BLE001 — fall through to the title search
            print(f"[{ACTION_KIND}] {identifier} not found directly: {error}")

    term = _one_line(title_hint)
    if not term:
        return None
    try:
        response = post_linear_graphql(SEARCH_ISSUES_QUERY, {"term": term}, api_key)
    except Exception as error:  # noqa: BLE001
        print(f"[{ACTION_KIND}] title search for {term!r} failed: {error}")
        return None
    nodes = (((response.get("data") or {}).get("issueSearch") or {}).get("nodes")) or []
    if not nodes:
        return None
    wanted = term.lower()
    for node in nodes:
        title = str(node.get("title", "")).lower()
        if wanted in title or title in wanted:
            return node
    # Deliberately NOT falling back to the top search hit. Moving the wrong ticket
    # in someone's real workspace is worse than moving none, and the recap will
    # still carry the sentence that asked for it.
    print(f"[{ACTION_KIND}] {len(nodes)} loose matches for {term!r} — none close enough to move")
    return None


def resolve_state_id(team_id: str, state_name: str, api_key: str) -> str | None:
    """Map a human state name ("In Progress") to a workflow state id on that team.

    Matches case-insensitively, then through STATE_SYNONYMS, rather than failing
    because a workspace calls its column "Started".
    """
    response = post_linear_graphql(TEAM_STATES_QUERY, {"teamId": team_id}, api_key)
    nodes = ((((response.get("data") or {}).get("team") or {}).get("states") or {}).get("nodes")) or []
    for node in nodes:
        if matches_state_name(node.get("name", ""), state_name):
            return node["id"]
    return None


# --- execute ----------------------------------------------------------------


def execute(action: Action) -> results.ExecutorResult:
    """Move the ticket, capturing the previous state so undo can reverse it."""
    payload = action.payload
    identifier = payload.get("linear_identifier") or find_identifier(action.quote, payload.get("title_hint"))
    percent = coerce_percent(payload.get("percent"))
    target_state = _one_line(payload.get("target_state", "")) or choose_target_state(
        percent, f"{action.quote} {payload.get('title_hint', '')}"
    )

    if not target_state:
        return results.ExecutorResult(
            ok=True,
            kind=ACTION_KIND,
            external_id=identifier,
            url=None,
            human_summary=(
                f"left {identifier or 'the ticket'} where it is — "
                f"{percent if percent is not None else 'the'} progress does not move a column"
            ),
            mode="sim",
            undo_payload={},
            quote=action.quote,
            speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    mode = secrets_store.decide_mode(REQUIRED_SECRETS, label=ACTION_KIND)

    if mode == "sim":
        return _render_simulated(
            action,
            {
                "identifier": identifier or "<unresolved identifier>",
                "target_state": target_state,
                "comment": build_comment_markdown(action, "its previous state", target_state),
            },
        )

    api_key = secrets_store.get_secret(secrets_store.LINEAR_API_KEY) or ""
    try:
        issue = find_issue(identifier, str(payload.get("title_hint") or ""), api_key)
        if not issue:
            return results.ExecutorResult.failed(
                ACTION_KIND,
                f"no Linear issue matched {identifier or payload.get('title_hint')!r}",
                mode="sim", quote=action.quote, speaker=action.speaker,
                meeting_id=action.meeting_id,
            )
        previous_state = issue.get("state") or {}
        if is_backward_move(previous_state.get("name"), target_state):
            # "I'm working on SHA-6" said about a ticket that is already in
            # review is a person describing what they are doing, not a request
            # to un-review it. The planner cannot see the column at plan time;
            # this is the only place that can.
            return results.ExecutorResult(
                ok=True, kind=ACTION_KIND, external_id=issue.get("id"), url=issue.get("url"),
                human_summary=(
                    f"left {issue.get('identifier')} in {previous_state.get('name')} — "
                    f"a move to {target_state} would be a step backwards"
                ),
                mode="sim", undo_payload={}, quote=action.quote, speaker=action.speaker,
                meeting_id=action.meeting_id,
            )
        team_id = (issue.get("team") or {}).get("id", "")
        state_id = resolve_state_id(team_id, target_state, api_key)
        if not state_id:
            return results.ExecutorResult.failed(
                ACTION_KIND,
                f"{issue.get('identifier')} has no workflow state named {target_state!r}",
                mode="sim", quote=action.quote, speaker=action.speaker,
                meeting_id=action.meeting_id,
            )
        if state_id == previous_state.get("id"):
            return results.ExecutorResult(
                ok=True, kind=ACTION_KIND, external_id=issue.get("id"), url=issue.get("url"),
                human_summary=f"{issue.get('identifier')} was already in {target_state}",
                mode="live", undo_payload={}, quote=action.quote, speaker=action.speaker,
                meeting_id=action.meeting_id,
            )
        moved = _move_issue_live(issue["id"], state_id, api_key)
        comment_markdown = build_comment_markdown(
            action, previous_state.get("name", ""), target_state
        )
        _comment_on_issue_live(issue["id"], comment_markdown, api_key)
    except Exception as error:  # noqa: BLE001 — a failed API call is a red card, not a crash
        return results.ExecutorResult.failed(
            ACTION_KIND, f"Linear issueUpdate failed: {error}",
            mode="live", quote=action.quote, speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    return results.ExecutorResult(
        ok=True,
        kind=ACTION_KIND,
        external_id=issue.get("id"),
        url=moved.get("url") or issue.get("url"),
        human_summary=(
            f"moved Linear {issue.get('identifier')} "
            f"{previous_state.get('name', '?')} → {target_state}"
        ),
        mode="live",
        undo_payload={
            "issue_id": issue["id"],
            "identifier": issue.get("identifier"),
            "previous_state_id": previous_state.get("id"),
            "previous_state_name": previous_state.get("name"),
        },
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )


def undo(result: results.ExecutorResult) -> bool:
    """Move the ticket back to undo_payload["previous_state_id"]."""
    if result.is_simulated:
        return True  # the ticket never moved
    undo_payload = result.undo_payload or {}
    issue_id = undo_payload.get("issue_id")
    previous_state_id = undo_payload.get("previous_state_id")
    if not issue_id or not previous_state_id:
        return False
    api_key = secrets_store.get_secret(secrets_store.LINEAR_API_KEY)
    if not api_key:
        print(f"[{ACTION_KIND}] cannot move {issue_id} back: no linear_api_key")
        return False
    try:
        _move_issue_live(issue_id, previous_state_id, api_key)
    except Exception as error:  # noqa: BLE001
        print(f"[{ACTION_KIND}] move back failed: {error}")
        return False
    return True


# --- TRANSPORT SWAP (bottom of the module) ----------------------------------


def _post_graphql_live(query: str, variables: dict, api_key: str) -> dict:
    """The only network call in this module. RAW Authorization header, no "Bearer ".

    Delegates to linear_create_executor.post_linear_graphql() so the header rule
    is written down in exactly one place.
    """
    return post_linear_graphql(query, variables, api_key)


def _move_issue_live(issue_id: str, state_id: str, api_key: str) -> dict:
    response = _post_graphql_live(
        UPDATE_ISSUE_MUTATION, {"id": issue_id, "stateId": state_id}, api_key
    )
    updated = (response.get("data") or {}).get("issueUpdate") or {}
    if not updated.get("success"):
        raise RuntimeError("Linear reported issueUpdate success=false")
    return updated.get("issue") or {}


def _comment_on_issue_live(issue_id: str, body_markdown: str, api_key: str) -> None:
    """Best effort — the move already landed; a failed comment must not undo it."""
    try:
        _post_graphql_live(
            CREATE_COMMENT_MUTATION, {"issueId": issue_id, "body": body_markdown}, api_key
        )
    except Exception as error:  # noqa: BLE001
        print(f"[{ACTION_KIND}] quote comment not posted: {error}")


def _render_simulated(action: Action, move_description: dict) -> results.ExecutorResult:
    """Sim mode: the exact mutation variables that would have been sent, unsent."""
    rendered = {
        "url": LINEAR_GRAPHQL_URL,
        "authorization_header": "lin_api_… (RAW, no Bearer prefix)",
        "mutation": UPDATE_ISSUE_MUTATION.strip(),
        "variables": {
            "id": f"<issue id for {move_description['identifier']}>",
            "stateId": f"<state id for {move_description['target_state']}>",
        },
        "comment": move_description["comment"],
    }
    print(
        f"[{ACTION_KIND}] SIM — not posting to Linear. Would move "
        f"{move_description['identifier']} → {move_description['target_state']}:\n"
        f"{json.dumps(rendered['variables'], indent=2)}"
    )
    return results.ExecutorResult.simulated(
        ACTION_KIND,
        f"moved Linear {move_description['identifier']} → {move_description['target_state']}",
        rendered_payload=rendered,
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
        external_id=move_description["identifier"],
    )
