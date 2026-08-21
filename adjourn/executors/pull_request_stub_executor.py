"""pull_request_stub — stub a draft PR for a stated intent.

When someone says "I'll open a PR for that", Adjourn leaves the branch and the
draft PR waiting for them, with the meeting quote in the body.

Payload shape:
    {
      "repo": "sharique2004/adjourn",     # must be in config.ALLOWED_GITHUB_REPOS
      "topic": "cache layer",              # branch name is derived from this
      "branch_name": "adjourn/cache-layer",# optional; derived from topic when absent
      "base_branch": "main",               # optional; the repo default branch when absent
      "title": "WIP: cache layer",
      "body_markdown": "...",              # optional; built from the quote when absent
      "issue_number": 4,                   # optional; links the PR to the issue
      "human_preview": "Draft PR: WIP: cache layer"
    }

Live transport: the authenticated `gh` CLI.
    1. read the base sha:  gh api repos/<repo>/git/ref/heads/<base>
    2. create the branch:  gh api repos/<repo>/git/refs -f ref=refs/heads/<branch> -f sha=<sha>
    3. commit a placeholder file so the branch differs from base — GitHub refuses
       to open a PR between identical refs.
    4. gh api repos/<repo>/pulls -F draft=true ...

DRAFT ONLY. Never open a ready-for-review PR, and never touch .github/workflows
(the `gh` token has no workflow scope; pushing one would 403 anyway).

Undo: close the PR (PATCH state=closed) and delete the branch ref.

Secrets: none — `gh` carries its own auth. The gh runner and the readiness probe
live in github_update_executor so there is exactly one of each in the package.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

from .. import config, results
from ..planner import normalize_key_text
from .github_update_executor import decide_github_mode, is_already_gone, run_github_cli

if TYPE_CHECKING:
    from ..planner import Action

ACTION_KIND = "pull_request_stub"
REQUIRED_SECRETS: tuple[str, ...] = ()

BRANCH_PREFIX = "adjourn/"
PLACEHOLDER_DIRECTORY = ".adjourn/"
FORBIDDEN_PATH_PREFIX = ".github/workflows/"  # no workflow scope on this token
DEFAULT_BASE_BRANCH = "main"


def build_branch_name(topic: str) -> str:
    """Deterministic, git-safe branch name from a topic key: 'adjourn/cache-layer'.

    Reuses planner.normalize_key_text() so the same topic always yields the same
    branch — that is what makes re-running the reconcile pass idempotent here.
    """
    slug = normalize_key_text(topic) or "follow-up"
    return f"{BRANCH_PREFIX}{slug}"


def placeholder_path_for(branch_name: str) -> str:
    """A file path that cannot already exist on the base branch.

    Committing into the branch is required (GitHub refuses a PR between identical
    refs) and a per-branch path means the commit never collides with a real file.
    """
    slug = branch_name.removeprefix(BRANCH_PREFIX) or "follow-up"
    path = f"{PLACEHOLDER_DIRECTORY}{slug}.md"
    # Checked as a substring, not a prefix: a caller-supplied branch_name can bury
    # the forbidden path in the middle of the slug, and the token has no workflow scope.
    if FORBIDDEN_PATH_PREFIX.strip("/") in path:
        raise PermissionError(f"refusing to write {path!r}: this token has no workflow scope")
    return path


def build_pull_request_body(action: Action) -> str:
    """Render the PR body: the quote, who said it, which meeting, what is expected."""
    payload = action.payload
    supplied = payload.get("body_markdown")
    if supplied:
        return str(supplied)
    meeting = " ".join(str(payload.get("meeting_title", "") or "").split()) or "an untitled meeting"
    speaker = " ".join(str(action.speaker or "").split()) or "someone in the room"
    quote = " ".join(str(action.quote or "").split())
    issue_number = payload.get("issue_number")

    lines = [
        "> [!NOTE]",
        "> **Draft opened by Adjourn — nobody has written the code yet.**",
        f"> Someone said they would open this PR during **{meeting}**, so the branch is waiting.",
        "",
    ]
    if quote:
        lines += [f"> **{speaker}:** {quote}", ""]
    if issue_number:
        lines += [f"Relates to #{issue_number}", ""]
    lines += [
        "### Before this leaves draft",
        "",
        "- [ ] Replace the placeholder file with the actual change",
        "- [ ] Confirm the approach still matches what was decided in the meeting",
        "- [ ] Mark ready for review",
        "",
        "---",
        "",
        # Precise: the body above prints a verbatim sentence, so the absolute
        # claim is false here. See github_update_executor's footer.
        f"_Opened by Adjourn the moment {meeting} ended · the audio and the full "
        f"transcript stayed on that Mac — the sentence quoted above is the only "
        f"text that travelled_",
    ]
    return "\n".join(lines) + "\n"


def build_placeholder_content(action: Action, branch_name: str) -> str:
    """The one file committed to the branch so the PR has a diff. Pure."""
    quote = " ".join(str(action.quote or "").split())
    speaker = " ".join(str(action.speaker or "").split()) or "someone in the room"
    meeting = " ".join(str(action.payload.get("meeting_title", "") or "").split())
    return (
        f"# {branch_name}\n\n"
        "Placeholder committed by Adjourn so this branch differs from its base and a\n"
        "draft pull request can exist. Delete this file in your first real commit.\n\n"
        f"> **{speaker}:** {quote}\n\n"
        + (f"_From: {meeting}_\n" if meeting else "")
    )


def assert_repo_is_allowed(repo: str) -> None:
    """Guard: refuse to write to any repo outside the allowlist. Implemented on purpose."""
    if repo not in config.ALLOWED_GITHUB_REPOS:
        raise PermissionError(
            f"refusing live GitHub write to {repo!r}; "
            f"allowed: {sorted(config.ALLOWED_GITHUB_REPOS)}"
        )


def resolve_base_branch(repo: str, requested: str = "") -> str:
    """The base to branch from: the payload's choice, else the repo default, else main."""
    if requested:
        return requested
    try:
        repository = json.loads(run_github_cli("api", f"repos/{repo}"))
        return repository.get("default_branch") or DEFAULT_BASE_BRANCH
    except Exception:  # noqa: BLE001 — a lookup failure is not worth losing the action over
        return DEFAULT_BASE_BRANCH


# --- execute ----------------------------------------------------------------


def execute(action: Action) -> results.ExecutorResult:
    """Create the branch and open the draft PR."""
    payload = action.payload
    repo = str(payload.get("repo") or config.github_repo())
    topic = str(payload.get("topic") or action.payload.get("title") or action.quote)
    branch_name = str(payload.get("branch_name") or build_branch_name(topic))
    title = " ".join(str(payload.get("title", "") or "").split()) or (
        f"WIP: {topic}".strip()
    )
    body_markdown = build_pull_request_body(action)

    mode = decide_github_mode(ACTION_KIND)

    if mode == "sim":
        base_branch = str(payload.get("base_branch") or DEFAULT_BASE_BRANCH)
        return _render_simulated(
            action,
            {
                "repo": repo,
                "branch": branch_name,
                "base": base_branch,
                "draft": True,
                "title": title,
                "placeholder_path": placeholder_path_for(branch_name),
                "body": body_markdown,
            },
        )

    assert_repo_is_allowed(repo)
    base_branch = resolve_base_branch(repo, str(payload.get("base_branch") or ""))
    try:
        pull_number, pull_url = _open_draft_pull_request_live(
            repo, branch_name, base_branch, title, body_markdown,
            placeholder_content=build_placeholder_content(action, branch_name),
        )
    except Exception as error:  # noqa: BLE001 — a failed PR is a red card, not a crash
        return results.ExecutorResult.failed(
            ACTION_KIND,
            f"could not open a draft PR on {repo}: {error}",
            mode="live",
            quote=action.quote,
            speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    return results.ExecutorResult(
        ok=True,
        kind=ACTION_KIND,
        external_id=str(pull_number),
        url=pull_url,
        human_summary=f"opened draft PR {repo}#{pull_number} “{title}” on {branch_name}",
        mode="live",
        undo_payload={
            "repo": repo,
            "pull_number": pull_number,
            "branch_name": branch_name,
        },
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )


def undo(result: results.ExecutorResult) -> bool:
    """Close the PR and delete the branch, from undo_payload."""
    if result.is_simulated:
        return True  # nothing was created
    undo_payload = result.undo_payload or {}
    repo = str(undo_payload.get("repo") or "")
    pull_number = undo_payload.get("pull_number")
    branch_name = undo_payload.get("branch_name")
    if not repo or not pull_number:
        return False
    try:
        assert_repo_is_allowed(repo)
    except PermissionError as error:
        print(f"[{ACTION_KIND}] {error}")
        return False

    reversed_everything = True
    try:
        run_github_cli(
            "api", "-X", "PATCH", f"repos/{repo}/pulls/{pull_number}", "-f", "state=closed",
        )
    except Exception as error:  # noqa: BLE001
        print(f"[{ACTION_KIND}] could not close PR #{pull_number}: {error}")
        reversed_everything = False

    if branch_name:
        try:
            run_github_cli(
                "api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch_name}",
            )
        except Exception as error:  # noqa: BLE001
            # Same idempotency rule as github_update.undo(): a branch that is
            # already gone is the outcome undo was asking for, not a refusal.
            if is_already_gone(error):
                print(f"[{ACTION_KIND}] branch {branch_name} was already deleted")
            else:
                print(f"[{ACTION_KIND}] could not delete branch {branch_name}: {error}")
                reversed_everything = False

    return reversed_everything


# --- TRANSPORT SWAP (bottom of the module) ----------------------------------


def _open_draft_pull_request_live(
    repo: str,
    branch_name: str,
    base_branch: str,
    title: str,
    body_markdown: str,
    *,
    placeholder_content: str = "",
) -> tuple[int, str]:
    """The only network sequence in this module. Returns (pull_number, html_url).

    Branch, then placeholder commit, then DRAFT pull request. Every step is
    idempotent enough to survive a reconcile pass re-running the same action:
    an existing ref and an existing placeholder are both treated as "already there".
    """
    assert_repo_is_allowed(repo)
    placeholder_path = placeholder_path_for(branch_name)

    base_reference = json.loads(run_github_cli("api", f"repos/{repo}/git/ref/heads/{base_branch}"))
    base_sha = base_reference["object"]["sha"]

    try:
        run_github_cli(
            "api", f"repos/{repo}/git/refs",
            "-f", f"ref=refs/heads/{branch_name}",
            "-f", f"sha={base_sha}",
        )
    except RuntimeError as error:
        if "already exists" not in str(error).lower():
            raise

    encoded = base64.b64encode(placeholder_content.encode("utf-8")).decode("ascii")
    try:
        run_github_cli(
            "api", "-X", "PUT", f"repos/{repo}/contents/{placeholder_path}",
            "-f", f"message=Adjourn: stub branch for {branch_name}",
            "-f", f"content={encoded}",
            "-f", f"branch={branch_name}",
        )
    except RuntimeError as error:
        if "sha" not in str(error).lower():
            raise  # anything other than "file already exists" is real

    try:
        created = json.loads(
            run_github_cli(
                "api", f"repos/{repo}/pulls",
                "-f", f"title={title}",
                "-F", "body=@-",
                "-f", f"head={branch_name}",
                "-f", f"base={base_branch}",
                "-F", "draft=true",
                stdin=body_markdown,
            )
        )
    except RuntimeError as error:
        # GitHub answers "a pull request already exists for this head" with a bare
        # 422, so the message is not reliable enough to match on — ask instead.
        # This is the state a rehearsal leaves behind: the journal gets reset, the
        # PR does not, and the next run would otherwise show a red card for work
        # that is already sitting open in the repo. Adopting it is both true and
        # the same end state the action was asking for.
        existing = _find_open_pull_request(repo, branch_name, base_branch)
        if existing is None:
            raise
        print(
            f"[{ACTION_KIND}] a draft PR for {branch_name} is already open "
            f"(#{existing[0]}) — adopting it instead of opening a second one"
        )
        return existing
    return int(created["number"]), created.get("html_url", "")


def _find_open_pull_request(
    repo: str, branch_name: str, base_branch: str
) -> tuple[int, str] | None:
    """(number, html_url) of the open PR from `branch_name`, or None. Read-only."""
    owner = repo.split("/", 1)[0]
    # Query string in the PATH, not as `-f` fields: `gh api` switches to POST the
    # moment a field is present, so `-f state=open` here would try to OPEN a pull
    # request rather than list them — which is how this lookup silently failed the
    # first time and left the 422 unhandled.
    query = f"state=open&head={owner}:{branch_name}&base={base_branch}"
    try:
        listed = json.loads(run_github_cli("api", f"repos/{repo}/pulls?{query}"))
    except (RuntimeError, ValueError):
        return None
    if not isinstance(listed, list) or not listed:
        return None
    return int(listed[0]["number"]), listed[0].get("html_url", "")


def _render_simulated(action: Action, pull_request_preview: dict) -> results.ExecutorResult:
    """Sim mode: the exact branch + draft PR that would have been created, uncreated."""
    print(
        f"[{ACTION_KIND}] SIM — not touching GitHub. Would open a DRAFT PR:\n"
        f"{json.dumps(pull_request_preview, indent=2)}"
    )
    return results.ExecutorResult.simulated(
        ACTION_KIND,
        f"opened draft PR “{pull_request_preview['title']}” on {pull_request_preview['branch']}",
        rendered_payload=pull_request_preview,
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
        url=f"https://github.com/{pull_request_preview['repo']}/pulls",
    )
