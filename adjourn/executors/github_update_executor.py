"""github_update — Drift-style what-changed comment on an issue.

Payload shape:
    {
      "issue_number": 4,
      "repo": "sharique2004/adjourn",     # must be in config.ALLOWED_GITHUB_REPOS
      "comment_markdown": "...",            # optional; built from the fields below if absent
      "before": "...",                      # prior decision, for the what-changed table
      "after": "...",                       # new decision
      "what_changed": "scope narrowed to reads",
      "meeting_title": "Eng sync",
      "meeting_date": "2026-08-21",
      "timestamp": "12:03",                 # mm:ss inside the meeting, for the excerpt
      "labels": ["from-meeting"],           # optional, added after the comment
      "plan_markdown": "...",               # optional; rewrites the marker block
      "create_issue": false,                # true => file a NEW issue instead of commenting
      "title": "Add Redis cache layer",     # required when create_issue is true
      "body_markdown": "...",               # optional body for the new issue
      "human_preview": "Comment on #4: cache layer moved to Redis"
    }

Live transport: the authenticated `gh` CLI (ported from drift/github_tools.py).
Bodies go over STDIN via `-F body=@-` so markdown is never shell-interpolated,
and `gh api` is used rather than `gh issue ...` because it returns JSON with
.number and .html_url.

The plan block is a marker-delimited region in the issue body:
    <!-- adjourn:plan -->  ...  <!-- /adjourn:plan -->
Only that region is rewritten; the rest of the body is never touched. (drift used
<!-- drift:plan -->; Adjourn uses its own marker so both can coexist on an issue.)

Undo: delete the comment via DELETE /repos/{repo}/issues/comments/{id}, and
remove any label this action added. The plan block is restored from the "before"
snapshot captured in undo_payload. An issue this action CREATED is closed rather
than deleted — closing is reversible, deletion is not.

SAFETY: live writes are allowed ONLY against config.ALLOWED_GITHUB_REPOS. Assert
it before the transport call, not after. Clean up test comments when verifying.

Secrets: none — `gh` is already authenticated as sharique2004 (scopes:
gist, read:org, repo). NO workflow scope, so never touch .github/workflows.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING

from .. import config, results, secrets_store

if TYPE_CHECKING:
    from ..planner import Action

ACTION_KIND = "github_update"
REQUIRED_SECRETS: tuple[str, ...] = ()  # gh CLI carries its own auth

PLAN_BLOCK_OPEN = "<!-- adjourn:plan -->"
PLAN_BLOCK_CLOSE = "<!-- /adjourn:plan -->"

LABELS = (
    ("from-meeting", "0E8A16", "Posted by Adjourn from a meeting"),
    ("decision-changed", "D93F0B", "A meeting changed a prior decision on this issue"),
)

GITHUB_CLI_TIMEOUT_SECONDS = 30

_github_cli_ready: bool | None = None


# --- pure rendering ---------------------------------------------------------


def _one_line(value: object) -> str:
    """Collapse whitespace and newlines into one tidy line. # ported from drift/guild_trigger.py"""
    return " ".join(str(value or "").split())


def _table_cell(value: object) -> str:
    """One-line, pipe-safe text for a GFM table cell. # ported from drift/guild_trigger.py"""
    return _one_line(value).replace("|", "\\|")


def build_comment_markdown(action: Action) -> str:
    """Render the what-changed comment: quote, speaker, what changed, provenance.

    Pure string building — no network. The meeting quote is kept verbatim; that
    quote is the whole credibility of the comment.

    A payload carrying both `before` and `after` renders the Drift-style
    [!IMPORTANT] banner with a before/after table. Anything else renders a
    quieter [!NOTE] update. Either way the collapsible excerpt carries the
    verbatim sentence, and the footer says where it came from.
    """
    payload = action.payload
    supplied = payload.get("comment_markdown")
    if supplied:
        return str(supplied)

    before = _table_cell(payload.get("before", ""))
    after = _table_cell(payload.get("after", "") or action.quote)
    what_changed = _one_line(payload.get("what_changed", ""))
    meeting = _one_line(payload.get("meeting_title", "")) or "an untitled meeting"
    meeting_date = _one_line(payload.get("meeting_date", ""))
    speaker = _one_line(action.speaker) or "someone in the room"
    quote = _one_line(action.quote)
    timestamp = _one_line(payload.get("timestamp", ""))
    where = f"**{meeting}**" + (f" ({meeting_date})" if meeting_date else "")

    lines: list[str] = []
    if before:
        lines += [
            "> [!IMPORTANT]",
            "> **Decision changed in a later meeting.**",
            f"> The plan recorded on this issue was revised during {where}.",
            "",
            "|            | Decision |",
            "| ---------- | -------- |",
            f"| **Before** | {before} |",
            f"| **After**  | {after} |",
            "",
        ]
    else:
        lines += [
            "> [!NOTE]",
            f"> **Updated from {where}.**",
            f"> {after}" if after else "> A meeting touched this issue.",
            "",
        ]
    if what_changed:
        lines += [f"**What changed:** `{what_changed}`", ""]
    if quote:
        excerpt = f"> **{speaker}:** {quote}"
        if timestamp:
            excerpt = f"> [{timestamp}] **{speaker}:** {quote}"
        lines += [
            "<details>",
            "<summary>Meeting excerpt</summary>",
            "",
            excerpt,
            "",
            "</details>",
            "",
        ]
    from .. import work_brief

    brief = work_brief.as_markdown(payload)
    if brief:
        lines += [brief]
    lines += [
        "---",
        "",
        f"_Meeting: {meeting}"
        + (f", {meeting_date}" if meeting_date else "")
        + " · Written by Adjourn the moment the meeting ended · "
        # PRECISE, because this sits directly beneath a verbatim quote on a
        # public issue. "The transcript never left this machine" printed three
        # lines under an excerpt OF that transcript is the single most quotable
        # thing in the product, and it is not true. What is true: the recording
        # and the full transcript stayed on the Mac, and the one sentence above
        # is the only text that travelled. Same wording as the recap footer.
        "the audio and the full transcript stayed on that Mac — the sentence "
        "quoted above is the only text that travelled_",
    ]
    return "\n".join(lines) + "\n"


def build_issue_body_markdown(action: Action) -> str:
    """Body for an issue Adjourn FILES (create_issue payloads). Pure."""
    payload = action.payload
    supplied = payload.get("body_markdown")
    if supplied:
        return str(supplied)
    meeting = _one_line(payload.get("meeting_title", "")) or "an untitled meeting"
    speaker = _one_line(action.speaker) or "someone in the room"
    quote = _one_line(action.quote)
    lines = [
        f"Filed by Adjourn from **{meeting}**.",
        "",
    ]
    if quote:
        lines += [f"> **{speaker}:** {quote}", ""]
    plan = _one_line(payload.get("plan_markdown", ""))
    if plan:
        lines += [PLAN_BLOCK_OPEN, plan, PLAN_BLOCK_CLOSE, ""]
    lines += [
        "---",
        "",
        "_Created automatically when the meeting ended. Nothing here was invented — "
        "the quote above is what was actually said._",
    ]
    return "\n".join(lines) + "\n"


def rewrite_plan_block(body_markdown: str, plan_markdown: str) -> str:
    """Replace (or append) the adjourn:plan marker region in an issue body. Pure.

    # ported from drift/github_tools.py update_plan_block()
    """
    body = body_markdown or ""
    block = f"{PLAN_BLOCK_OPEN}\n{plan_markdown}\n{PLAN_BLOCK_CLOSE}"
    if PLAN_BLOCK_OPEN in body and PLAN_BLOCK_CLOSE in body:
        head, rest = body.split(PLAN_BLOCK_OPEN, 1)
        _, tail = rest.split(PLAN_BLOCK_CLOSE, 1)
        return head + block + tail
    return (body.rstrip() + "\n\n" if body.strip() else "") + block


def assert_repo_is_allowed(repo: str) -> None:
    """Guard: refuse to write to any repo outside the allowlist. Implemented on purpose."""
    if repo not in config.ALLOWED_GITHUB_REPOS:
        raise PermissionError(
            f"refusing live GitHub write to {repo!r}; "
            f"allowed: {sorted(config.ALLOWED_GITHUB_REPOS)}"
        )


# --- gh transport helpers (shared with pull_request_stub_executor) -----------


def run_github_cli(*args: str, stdin: str | None = None) -> str:
    """Run `gh <args>` and return stdout. Raises RuntimeError with stderr on failure.

    Two things here are not in the original: the child gets an explicit minimal
    environment rather than inheriting this one, and its argv is checked for
    secret values before it runs.

    `gh` needs HOME (its credentials live in the keychain under it) and PATH.
    It does not need the Slack token, the Linear key or the FalkorDB password,
    and a process that does not have them cannot leak them — not through `ps -E`,
    not through a crash report, not through some future change that gives the
    CLI a way to read its own environment. The argv check is the same rule
    applied to the other place a secret can end up visible to `ps`.

    # ported from drift/github_tools.py gh()
    """
    argv = ["gh", *args]
    secrets_store.assert_no_secret_in_argv(argv)
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        input=stdin,
        timeout=GITHUB_CLI_TIMEOUT_SECONDS,
        env=config.child_process_environment(),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"gh failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed.stdout


def is_already_gone(error: Exception) -> bool:
    """True when a DELETE failed only because the thing was already deleted.

    Undo has to be IDEMPOTENT. Two cards from one meeting can both carry the
    `from-meeting` label they each added to issue #2; undoing the first removes
    it, and undoing the second then gets a 404 for a label that is already in the
    state the caller wanted. Reporting that as a refusal is a false alarm — the
    board shows a red toast for an issue that is, in fact, exactly as clean as
    requested. A 404 on a DELETE is the desired end state, not a failure.
    """
    text = str(error).lower()
    return "404" in text or "not found" in text


def is_github_cli_ready() -> bool:
    """True when `gh` exists on PATH and is authenticated. Checked once per process.

    A missing or logged-out `gh` is not an error — it is sim mode, which is a demo
    that still works rather than a traceback in front of founders.
    """
    global _github_cli_ready
    if _github_cli_ready is not None:
        return _github_cli_ready
    if shutil.which("gh") is None:
        _github_cli_ready = False
        return False
    try:
        completed = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=GITHUB_CLI_TIMEOUT_SECONDS,
        )
        _github_cli_ready = completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        _github_cli_ready = False
    return _github_cli_ready


def decide_github_mode(label: str) -> str:
    """"live" or "sim" for a gh-backed executor, decided once and said out loud."""
    mode = secrets_store.decide_mode(REQUIRED_SECRETS, label=label)
    if mode == "live" and not is_github_cli_ready():
        print(f"[{label}] SIM mode — `gh` is missing or not authenticated")
        return "sim"
    return mode


def ensure_labels_exist(repo: str) -> None:
    """Create (or update) Adjourn's labels. `--force` makes this idempotent.

    # ported from drift/github_tools.py ensure_labels()
    """
    for name, color, description in LABELS:
        try:
            run_github_cli(
                "label", "create", name,
                "--repo", repo,
                "--color", color,
                "--description", description,
                "--force",
            )
        except (RuntimeError, subprocess.SubprocessError) as error:
            print(f"[{ACTION_KIND}] label {name!r} could not be ensured: {error}")


def read_issue_body(repo: str, issue_number: int) -> str:
    """Current markdown body of an issue ('' when empty). # ported from drift/github_tools.py"""
    output = run_github_cli("api", f"repos/{repo}/issues/{issue_number}")
    return json.loads(output).get("body") or ""


class IssueNotVerified(RuntimeError):
    """The target issue could not be shown to exist inside the allowlisted repo."""


def verify_issue_target(repo: str, issue_number: int) -> dict:
    """Confirm the issue exists AND lives in `repo`. Returns its API record.

    The issue number is the one part of a live public write that the MODEL
    chooses: `entity_refs.issue_number` comes out of extraction, from a number
    somebody said out loud. The repo allowlist bounds which repository can be
    written to; nothing bounded which issue inside it, so a misheard "forty-two"
    used to become a comment on whatever #42 happened to be, and a number that
    matched nothing became a raw `gh` error on a red card after the attempt.

    So: look first. One GET, before the comment is composed and long before it is
    posted. Three things are checked, because GitHub will cheerfully answer for
    two of them:

      * the issue exists at all (a 404 becomes a readable refusal, not a stack
        trace);
      * `repository_url` really ends in the allowlisted repo — `gh api` follows
        a rename or a transfer, so asking for an issue in the allowed repo is
        not by itself proof of having reached it;
      * it is not locked, which would make the comment fail anyway.

    A pull request IS an issue as far as this API is concerned, and that is
    deliberate — GitHub numbers issues and PRs in one space, and commenting on
    the conversation of a PR is a legitimate thing a meeting can decide to do.
    """
    try:
        raw = run_github_cli("api", f"repos/{repo}/issues/{issue_number}")
    except Exception as error:  # noqa: BLE001 — every failure becomes one message
        if is_already_gone(error):
            raise IssueNotVerified(
                f"{repo}#{issue_number} does not exist — refusing to comment on a number "
                "nobody can open"
            ) from error
        raise IssueNotVerified(f"could not read {repo}#{issue_number}: {error}") from error

    try:
        record = json.loads(raw)
    except ValueError as error:
        raise IssueNotVerified(f"unreadable API response for {repo}#{issue_number}") from error

    repository_url = str(record.get("repository_url") or "")
    if not repository_url.endswith(f"/{repo}"):
        raise IssueNotVerified(
            f"{repo}#{issue_number} actually lives in "
            f"{repository_url.rsplit('/repos/', 1)[-1] or 'an unknown repo'} — refusing"
        )
    if record.get("locked"):
        raise IssueNotVerified(f"{repo}#{issue_number} is locked — comments are closed")
    return record


# --- execute ----------------------------------------------------------------


def execute(action: Action) -> results.ExecutorResult:
    """Comment on the issue, add labels, refresh the plan block.

    The comment body is built identically in both modes; the fork to the wire is
    at the bottom of this module. The issue body is captured BEFORE the plan
    block is rewritten so undo can put it back exactly.
    """
    payload = action.payload
    repo = str(payload.get("repo") or config.github_repo())
    creating_issue = bool(payload.get("create_issue"))
    labels = [str(label) for label in (payload.get("labels") or [])]
    plan_markdown = payload.get("plan_markdown")

    mode = decide_github_mode(ACTION_KIND)

    if creating_issue:
        title = _one_line(payload.get("title", "")) or f"Adjourn: {_one_line(action.quote)[:60]}"
        body_markdown = build_issue_body_markdown(action)
        if mode == "sim":
            return _render_simulated(
                action,
                body_markdown,
                {
                    "operation": "create_issue",
                    "repo": repo,
                    "title": title,
                    "labels": labels,
                    "body": body_markdown,
                },
                human_summary=f"filed {repo} issue “{title}”",
            )
        assert_repo_is_allowed(repo)
        ensure_labels_exist(repo)
        try:
            issue_number, issue_url = _create_issue_live(repo, title, body_markdown, labels)
        except Exception as error:  # noqa: BLE001 — a failed post is a red card, not a crash
            return results.ExecutorResult.failed(
                ACTION_KIND, f"could not file {repo} issue: {error}",
                mode="live", quote=action.quote, speaker=action.speaker,
                meeting_id=action.meeting_id,
            )
        return results.ExecutorResult(
            ok=True,
            kind=ACTION_KIND,
            external_id=str(issue_number),
            url=issue_url,
            human_summary=f"filed {repo}#{issue_number} “{title}”",
            mode="live",
            undo_payload={
                "operation": "create_issue",
                "repo": repo,
                "issue_number": issue_number,
            },
            quote=action.quote,
            speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    issue_number = payload.get("issue_number")
    if not issue_number:
        return results.ExecutorResult.failed(
            ACTION_KIND,
            "no issue_number in the payload — the planner should not have routed this here",
            quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
        )
    issue_number = int(issue_number)

    # Look before composing. In live mode the target is checked BEFORE the
    # comment is built, so a refusal costs one GET and reads as a sentence about
    # the issue rather than as a failed write. Sim mode never checks, because sim
    # mode never touches the network — the sim card says what it *would* send,
    # and that is honest whether or not #42 turns out to exist.
    if mode != "sim":
        try:
            assert_repo_is_allowed(repo)
            verify_issue_target(repo, issue_number)
        except (PermissionError, IssueNotVerified) as error:
            print(f"[{ACTION_KIND}] BLOCKED before composing: {error}")
            return results.ExecutorResult.failed(
                ACTION_KIND, str(error),
                quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
            )

    comment_markdown = build_comment_markdown(action)
    summary = _one_line(payload.get("human_preview", "")) or (
        f"commented on {repo}#{issue_number}"
    )

    if mode == "sim":
        rendered = {
            "operation": "comment",
            "repo": repo,
            "issue_number": issue_number,
            "labels": labels,
            "comment": comment_markdown,
        }
        if plan_markdown:
            rendered["plan_block"] = (
                f"{PLAN_BLOCK_OPEN}\n{plan_markdown}\n{PLAN_BLOCK_CLOSE}"
            )
        return _render_simulated(
            action, comment_markdown, rendered,
            human_summary=summary,
            url=f"https://github.com/{repo}/issues/{issue_number}",
            external_id=str(issue_number),
        )

    assert_repo_is_allowed(repo)
    if labels:
        ensure_labels_exist(repo)
    try:
        comment_id, comment_url = _post_comment_live(repo, issue_number, comment_markdown)
    except Exception as error:  # noqa: BLE001
        return results.ExecutorResult.failed(
            ACTION_KIND, f"could not comment on {repo}#{issue_number}: {error}",
            mode="live", quote=action.quote, speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    undo_payload: dict = {
        "operation": "comment",
        "repo": repo,
        "issue_number": issue_number,
        "comment_id": comment_id,
        "labels_added": [],
    }

    for label in labels:
        try:
            run_github_cli(
                "api", f"repos/{repo}/issues/{issue_number}/labels", "-f", f"labels[]={label}",
            )
            undo_payload["labels_added"].append(label)
        except Exception as error:  # noqa: BLE001 — the comment already landed; keep going
            print(f"[{ACTION_KIND}] label {label!r} not added: {error}")

    if plan_markdown:
        try:
            previous_body = read_issue_body(repo, issue_number)
            new_body = rewrite_plan_block(previous_body, str(plan_markdown))
            if new_body != previous_body:
                run_github_cli(
                    "api", "-X", "PATCH", f"repos/{repo}/issues/{issue_number}",
                    "-F", "body=@-", stdin=new_body,
                )
                undo_payload["previous_body"] = previous_body
        except Exception as error:  # noqa: BLE001
            print(f"[{ACTION_KIND}] plan block not rewritten: {error}")

    if undo_payload.get("previous_body") is not None:
        summary = f"{summary} and refreshed the stated plan"

    return results.ExecutorResult(
        ok=True,
        kind=ACTION_KIND,
        external_id=str(comment_id),
        url=comment_url,
        human_summary=summary,
        mode="live",
        undo_payload=undo_payload,
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )


def undo(result: results.ExecutorResult) -> bool:
    """Delete the comment, drop the added labels, restore the previous plan block."""
    if result.is_simulated:
        return True  # nothing left the machine
    undo_payload = result.undo_payload or {}
    repo = str(undo_payload.get("repo") or "")
    if not repo:
        return False
    try:
        assert_repo_is_allowed(repo)
    except PermissionError as error:
        print(f"[{ACTION_KIND}] {error}")
        return False

    issue_number = undo_payload.get("issue_number")
    if undo_payload.get("operation") == "create_issue":
        try:
            run_github_cli(
                "api", "-X", "PATCH", f"repos/{repo}/issues/{issue_number}",
                "-f", "state=closed",
            )
            return True
        except Exception as error:  # noqa: BLE001
            print(f"[{ACTION_KIND}] could not close {repo}#{issue_number}: {error}")
            return False

    reversed_everything = True
    comment_id = undo_payload.get("comment_id")
    if comment_id:
        try:
            run_github_cli("api", "-X", "DELETE", f"repos/{repo}/issues/comments/{comment_id}")
        except Exception as error:  # noqa: BLE001
            if is_already_gone(error):
                print(f"[{ACTION_KIND}] comment {comment_id} was already deleted")
            else:
                print(f"[{ACTION_KIND}] could not delete comment {comment_id}: {error}")
                reversed_everything = False

    for label in undo_payload.get("labels_added") or []:
        try:
            run_github_cli(
                "api", "-X", "DELETE", f"repos/{repo}/issues/{issue_number}/labels/{label}",
            )
        except Exception as error:  # noqa: BLE001
            if is_already_gone(error):
                print(f"[{ACTION_KIND}] label {label!r} was already removed")
            else:
                print(f"[{ACTION_KIND}] could not remove label {label!r}: {error}")
                reversed_everything = False

    previous_body = undo_payload.get("previous_body")
    if previous_body is not None:
        try:
            run_github_cli(
                "api", "-X", "PATCH", f"repos/{repo}/issues/{issue_number}",
                "-F", "body=@-", stdin=str(previous_body),
            )
        except Exception as error:  # noqa: BLE001
            print(f"[{ACTION_KIND}] could not restore the issue body: {error}")
            reversed_everything = False

    return reversed_everything


# --- TRANSPORT SWAP (bottom of the module) ----------------------------------


def _post_comment_live(repo: str, issue_number: int, body_markdown: str) -> tuple[str, str]:
    """One of the two network calls in this module. Returns (comment_id, html_url).

    gh api repos/<repo>/issues/<n>/comments -F body=@-   (body on stdin, never argv)
    """
    assert_repo_is_allowed(repo)
    output = run_github_cli(
        "api", f"repos/{repo}/issues/{issue_number}/comments",
        "-F", "body=@-",
        stdin=body_markdown,
    )
    posted = json.loads(output)
    return str(posted["id"]), posted.get("html_url", "")


def _create_issue_live(
    repo: str,
    title: str,
    body_markdown: str,
    labels: list[str],
) -> tuple[int, str]:
    """The other network call. Returns (issue_number, html_url).

    # ported from drift/github_tools.py create_issue()
    """
    assert_repo_is_allowed(repo)
    args = ["api", f"repos/{repo}/issues", "-f", f"title={title}", "-F", "body=@-"]
    for label in labels:
        args += ["-f", f"labels[]={label}"]
    created = json.loads(run_github_cli(*args, stdin=body_markdown))
    return int(created["number"]), created.get("html_url", "")


def _render_simulated(
    action: Action,
    body_markdown: str,
    rendered_payload: dict,
    *,
    human_summary: str,
    url: str | None = None,
    external_id: str | None = None,
) -> results.ExecutorResult:
    """Sim mode: the exact markdown that would have been posted, unsent."""
    print(f"[{ACTION_KIND}] SIM — not posting to GitHub. Rendered body:\n{body_markdown}")
    return results.ExecutorResult.simulated(
        ACTION_KIND,
        human_summary,
        rendered_payload=rendered_payload,
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
        url=url,
        external_id=external_id,
    )
