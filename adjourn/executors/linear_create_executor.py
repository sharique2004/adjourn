"""linear_create — file a Linear ticket for a stated request.

Payload shape:
    {
      "title": "Add Redis cache layer",
      "description_markdown": "...",   # optional; built from the quote when absent
      "team_key": "SHA",               # resolved to a team id at execute time
      "assignee_name": "Priya",        # optional; matched against Linear users
      "labels": ["from-meeting"],      # optional; unknown label names are skipped
      "meeting_title": "Eng sync",
      "human_preview": "Linear: create SHA ticket 'Add Redis cache layer'"
    }

Live transport: GraphQL POST to https://api.linear.app/graphql.

    Authorization: lin_api_xxx        <-- RAW. No "Bearer " prefix. This is the
                                          single most common way to get a 401 here.

    mutation { issueCreate(input: {...}) { success issue { id identifier url } } }

Undo: `issueArchive(id:)` — Linear has no hard delete over the API, and archiving
is the reversible thing a human would actually want.

Secrets: linear_api_key. Absent => sim mode, loudly.

This module also owns `post_linear_graphql()`, the one HTTP call shared with
linear_move_executor, and the team-id cache in adjourn/state/linear_teams.json.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .. import config, results, secrets_store

if TYPE_CHECKING:
    from ..planner import Action

ACTION_KIND = "linear_create"
REQUIRED_SECRETS: tuple[str, ...] = (secrets_store.LINEAR_API_KEY,)

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
HTTP_TIMEOUT_SECONDS = 10
def team_cache_path():
    """Workspace team-key -> team-id cache. A function, not a constant, so that a
    test can point it somewhere disposable — one that could not ended up
    overwriting the real cache with a fabricated team."""
    return config.state_directory() / "linear_teams.json"


def default_team_key() -> str:
    """The team key to file into when the payload does not name one.

    Read through config rather than frozen as a constant so the SIM rendering
    names the team this workspace actually has. It is a function, not a module
    constant, because .env is read at call time.
    """
    return config.linear_team_key()


CREATE_ISSUE_MUTATION = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier url title }
  }
}
"""

ARCHIVE_ISSUE_MUTATION = """
mutation ArchiveIssue($id: String!) {
  issueArchive(id: $id) { success }
}
"""

LIST_TEAMS_QUERY = """
query ListTeams {
  teams(first: 50) {
    nodes { id key name }
  }
}
"""

LIST_LABELS_QUERY = """
query ListLabels {
  issueLabels(first: 250) {
    nodes { id name team { id key } }
  }
}
"""

LIST_USERS_QUERY = """
query ListUsers {
  users(first: 100) {
    nodes { id name displayName email }
  }
}
"""


# --- pure rendering ---------------------------------------------------------


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def build_description_markdown(action: Action) -> str:
    """The ticket description, with the verbatim quote in it. Pure."""
    payload = action.payload
    supplied = payload.get("description_markdown")
    if supplied:
        return str(supplied)
    meeting = _one_line(payload.get("meeting_title", "")) or "an untitled meeting"
    speaker = _one_line(action.speaker) or "someone in the room"
    quote = _one_line(action.quote)
    lines = [f"Filed by Adjourn from **{meeting}**.", ""]
    if quote:
        lines += [f"> **{speaker}:** {quote}", ""]
    lines += [
        "---",
        "",
        "_Created automatically when the meeting ended. The quote above is what was "
        "actually said — nothing here was inferred._",
    ]
    return "\n".join(lines) + "\n"


def build_issue_input(action: Action, team_id: str) -> dict:
    """The IssueCreateInput dict. Pure — built identically in live and sim.

    In sim mode `team_id` is a readable placeholder rather than a real id, so the
    rendered payload shows exactly what would have been sent, gap included.
    """
    payload = action.payload
    issue_input: dict = {
        "teamId": team_id,
        "title": _one_line(payload.get("title", "")) or _one_line(action.quote)[:80],
        "description": build_description_markdown(action),
    }
    assignee_id = payload.get("assignee_id")
    if assignee_id:
        issue_input["assigneeId"] = assignee_id
    label_ids = payload.get("label_ids")
    if label_ids:
        issue_input["labelIds"] = list(label_ids)
    elif payload.get("labels") and str(team_id).startswith("<"):
        # Sim mode: show the labels that WOULD be resolved rather than leaving the
        # rendered mutation quietly unlabelled, which is how the live/sim gap in
        # this field went unnoticed in the first place.
        issue_input["labelIds"] = [
            f"<label-id for {_one_line(name)}>" for name in payload["labels"]
        ]
    return issue_input


# --- id resolution ----------------------------------------------------------


def _read_team_cache() -> dict[str, str]:
    try:
        return json.loads(team_cache_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_team_cache(teams: dict[str, str]) -> None:
    try:
        path = team_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(teams, indent=2))
    except OSError as error:
        print(f"[{ACTION_KIND}] team cache not written: {error}")


def resolve_team_id(team_key: str, api_key: str) -> str | None:
    """Look up a Linear team id from its key ("SHA"). Cached in state/linear_teams.json.

    FAIL-CLOSED. When the configured key is not in the workspace this returns
    None and the caller renders the honest red card ("no Linear team resolved for
    'SHA'"). It used to fall back to the first team the API happened to return,
    which meant a typo in .env, or Linear adding a team, silently filed a real
    ticket into a real stranger's board — the one Linear write with no allowlist
    in front of it, unlike every GitHub write. Wrong ticket in the wrong place is
    worse than no ticket: no ticket is visible on the board and takes ten seconds
    to fix by hand.

    ADJOURN_LINEAR_TEAM_FALLBACK=1 restores the old behaviour for a throwaway
    workspace. It is off by default and should stay off for anything live.
    """
    key = (team_key or default_team_key()).upper()
    cached = _read_team_cache()
    if key in cached:
        return cached[key]

    response = post_linear_graphql(LIST_TEAMS_QUERY, {}, api_key)
    nodes = (((response.get("data") or {}).get("teams") or {}).get("nodes")) or []
    if not nodes:
        return None
    discovered = {str(node["key"]).upper(): node["id"] for node in nodes if node.get("key")}
    _write_team_cache({**cached, **discovered})
    if key in discovered:
        return discovered[key]
    available = ", ".join(sorted(discovered)) or "none"
    if not config.read_flag("ADJOURN_LINEAR_TEAM_FALLBACK", False):
        print(
            f"[{ACTION_KIND}] no team {key!r} in this workspace (has: {available}) — "
            "refusing to file into a team nobody asked for"
        )
        return None
    first = nodes[0]
    print(
        f"[{ACTION_KIND}] no team {key!r} in this workspace — "
        f"ADJOURN_LINEAR_TEAM_FALLBACK is set, filing into {first.get('key')} instead"
    )
    return first["id"]


def resolve_label_ids(label_names: list[str], team_id: str, api_key: str) -> list[str]:
    """Map label NAMES to Linear label ids. Unknown names are skipped, never created.

    Parity with the GitHub path, which stamps every comment it leaves with
    `from-meeting`. Without it a Linear ticket Adjourn filed is only
    distinguishable from a hand-filed one by reading the description, which makes
    bulk cleanup after a rehearsal a manual hunt.

    A team-scoped label wins over a workspace-scoped one of the same name; a name
    the workspace does not have is silently skipped, exactly as the payload
    contract in this module's docstring promises. Creating labels is a write
    nobody asked for, so it does not happen here.
    """
    wanted = [str(name).strip() for name in (label_names or []) if str(name).strip()]
    if not wanted:
        return []
    try:
        response = post_linear_graphql(LIST_LABELS_QUERY, {}, api_key)
    except Exception as error:  # noqa: BLE001 — labels are a nicety, never the ticket
        print(f"[{ACTION_KIND}] labels not resolved ({error}) — filing without them")
        return []
    nodes = (((response.get("data") or {}).get("issueLabels") or {}).get("nodes")) or []
    resolved: list[str] = []
    for name in wanted:
        matches = [
            node for node in nodes
            if str(node.get("name", "")).strip().lower() == name.lower()
        ]
        if not matches:
            print(f"[{ACTION_KIND}] no Linear label named {name!r} — skipped")
            continue
        preferred = next(
            (node for node in matches if ((node.get("team") or {}).get("id")) == team_id),
            matches[0],
        )
        resolved.append(preferred["id"])
    return resolved


def resolve_assignee_id(person_name: str, api_key: str) -> str | None:
    """Match a spoken first name against Linear users. None when it is ambiguous."""
    wanted = _one_line(person_name).lower()
    if not wanted:
        return None
    response = post_linear_graphql(LIST_USERS_QUERY, {}, api_key)
    nodes = (((response.get("data") or {}).get("users") or {}).get("nodes")) or []
    matches = [
        node for node in nodes
        if wanted in str(node.get("name", "")).lower()
        or wanted in str(node.get("displayName", "")).lower()
    ]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        print(f"[{ACTION_KIND}] {person_name!r} matches {len(matches)} Linear users — unassigned")
    return None


# --- execute ----------------------------------------------------------------


def execute(action: Action) -> results.ExecutorResult:
    """Create the ticket. Mode decided once, at the top, via secrets_store.decide_mode()."""
    payload = action.payload
    team_key = str(payload.get("team_key") or default_team_key()).upper()
    mode = secrets_store.decide_mode(REQUIRED_SECRETS, label=ACTION_KIND)

    if mode == "sim":
        issue_input = build_issue_input(action, f"<team-id for {team_key}>")
        return _render_simulated(action, issue_input)

    api_key = secrets_store.get_secret(secrets_store.LINEAR_API_KEY) or ""
    try:
        team_id = resolve_team_id(team_key, api_key)
        if not team_id:
            return results.ExecutorResult.failed(
                ACTION_KIND, f"no Linear team resolved for {team_key!r}",
                mode="sim", quote=action.quote, speaker=action.speaker,
                meeting_id=action.meeting_id,
            )
        issue_input = build_issue_input(action, team_id)
        if "labelIds" not in issue_input and payload.get("labels"):
            label_ids = resolve_label_ids(list(payload.get("labels") or []), team_id, api_key)
            if label_ids:
                issue_input["labelIds"] = label_ids
        assignee_name = _one_line(payload.get("assignee_name", ""))
        if assignee_name and "assigneeId" not in issue_input:
            # Resolved here rather than folded back into action.payload: an executor
            # must not mutate the Action the planner handed it.
            assignee_id = resolve_assignee_id(assignee_name, api_key)
            if assignee_id:
                issue_input["assigneeId"] = assignee_id
        created = _create_issue_live(issue_input, api_key)
    except Exception as error:  # noqa: BLE001 — a failed API call is a red card, not a crash
        return results.ExecutorResult.failed(
            ACTION_KIND, f"Linear issueCreate failed: {error}",
            mode="live", quote=action.quote, speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    identifier = created.get("identifier", "")
    return results.ExecutorResult(
        ok=True,
        kind=ACTION_KIND,
        external_id=created.get("id"),
        url=created.get("url"),
        human_summary=f"filed Linear {identifier} “{created.get('title', '')}”",
        mode="live",
        undo_payload={"issue_id": created.get("id"), "identifier": identifier},
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )


def undo(result: results.ExecutorResult) -> bool:
    """Archive the created issue using undo_payload["issue_id"]."""
    if result.is_simulated:
        return True  # nothing was filed
    issue_id = (result.undo_payload or {}).get("issue_id")
    if not issue_id:
        return False
    api_key = secrets_store.get_secret(secrets_store.LINEAR_API_KEY)
    if not api_key:
        print(f"[{ACTION_KIND}] cannot archive {issue_id}: no linear_api_key")
        return False
    try:
        response = post_linear_graphql(ARCHIVE_ISSUE_MUTATION, {"id": issue_id}, api_key)
    except Exception as error:  # noqa: BLE001
        print(f"[{ACTION_KIND}] archive failed: {error}")
        return False
    return bool((((response.get("data") or {}).get("issueArchive") or {}).get("success")))


# --- TRANSPORT SWAP (bottom of the module) ----------------------------------


def post_linear_graphql(query: str, variables: dict, api_key: str) -> dict:
    """The one Linear HTTP call in the package. linear_move_executor delegates here.

    The header is exactly {"Authorization": api_key} — RAW, no "Bearer " prefix.
    Linear returns HTTP 200 with an "errors" array for a failed operation, so the
    body is checked as well as the status.
    """
    import requests  # imported here so the package still loads without requests

    response = requests.post(
        LINEAR_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Linear HTTP {response.status_code}: {response.text[:300]}")
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"Linear GraphQL error: {json.dumps(body['errors'])[:300]}")
    return body


def _post_graphql_live(query: str, variables: dict, api_key: str) -> dict:
    """Contract alias kept for the stub's signature; delegates to post_linear_graphql()."""
    return post_linear_graphql(query, variables, api_key)


def _create_issue_live(issue_input: dict, api_key: str) -> dict:
    """Send issueCreate and return the created issue node."""
    response = post_linear_graphql(CREATE_ISSUE_MUTATION, {"input": issue_input}, api_key)
    created = (response.get("data") or {}).get("issueCreate") or {}
    if not created.get("success"):
        raise RuntimeError("Linear reported issueCreate success=false")
    return created.get("issue") or {}


def _render_simulated(action: Action, issue_input: dict) -> results.ExecutorResult:
    """Sim mode: the exact IssueCreateInput that would have been sent, unsent."""
    rendered = {
        "url": LINEAR_GRAPHQL_URL,
        "authorization_header": "lin_api_… (RAW, no Bearer prefix)",
        "mutation": CREATE_ISSUE_MUTATION.strip(),
        "variables": {"input": issue_input},
    }
    print(
        f"[{ACTION_KIND}] SIM — not posting to Linear. Rendered mutation:\n"
        f"{json.dumps(rendered['variables'], indent=2)}"
    )
    return results.ExecutorResult.simulated(
        ACTION_KIND,
        f"filed Linear ticket “{issue_input.get('title', '')}”",
        rendered_payload=rendered,
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )
