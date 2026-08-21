"""pr_review_suggestion — a GitHub PR review carrying an inline ```suggestion block.

Someone in the meeting says "the join button green is too loud, take it to
#6b7f99". Adjourn finds the open PR that touched the join button, finds the one
line in its diff that actually contains `#2ecc71`, and leaves a review with a
one-click suggestion on that line. Nobody pushed anything; the author presses
"Commit suggestion" or dismisses it, exactly like a human reviewer's.

Payload shape:
    {
      "pr_number": 22,                    # explicit wins; None => match by topic
      "pr_topic": "join button",          # matched against open PR titles + branches
      "change_description": "join button green",   # for the human summary
      "old_value": "#2ecc71",             # the concrete thing to find in the diff
      "new_value": "#6b7f99",             # what it becomes; None => no suggestion
      "file_hint": "join.css",            # optional; narrows which file to search
      "repo": "sharique2004/adjourn",    # must be in config.ALLOWED_GITHUB_REPOS
      "meeting_title": "Eng sync",
      "meeting_date": "2026-08-21",
      "timestamp": "12:03",               # mm:ss inside the meeting
      "human_preview": "Suggested change on PR #22"
    }

Provenance (quote, speaker, meeting_id) rides on the Action, as everywhere else.

-----------------------------------------------------------------------------
 THE TWO PATHS — restraint over guessing
-----------------------------------------------------------------------------
1. EXACTLY ONE line in the PR's diff contains `old_value` (and `new_value` is
   present)  ->  a REVIEW with one inline comment anchored to that line, whose
   body is a ```suggestion block replacing the whole line with `old_value`
   swapped for `new_value`, plus the verbatim meeting quote.

2. ZERO or MANY matching lines, or a vague `old_value`, or no `new_value`
   ->  a PLAIN comment on the PR conversation stating the requested change in
   words, with the quote, and saying honestly why no line was pointed at.

A `pr_topic` that does not confidently match one open PR is a FAILED result
("no open PR matched ..."), never a guess. The match is scored against titles
and branch names of that ONE repo's open PRs and must both clear a floor and
beat the runner-up; two PRs that look equally likely is the same answer as none.

-----------------------------------------------------------------------------
 WHY SIM MODE READS (deliberate, and narrower than it looks)
-----------------------------------------------------------------------------
Every other executor here can render its sim payload from the action alone.
This one cannot: the exact review body depends on a line of code that lives in
the PR's diff, and a sim card showing an invented suggestion — a diff line
nobody wrote, on a file that may not exist — is precisely the lie the SIM badge
exists to prevent.

So the two READ-ONLY lookups (list open PRs, list a PR's changed files) run in
both modes. They mutate nothing, they are allowlist-guarded like every write,
and they are what makes "the payload is real, only the send is not" literally
true here. The WRITE is still forked at the bottom of the module and nowhere
else. When `gh` is unavailable the reads are skipped and the sim payload falls
back to marked placeholders rather than to fiction.

-----------------------------------------------------------------------------
 UNDO — verified against the live API, and the ORDER is the whole trick
-----------------------------------------------------------------------------
GitHub will NOT delete a submitted review:
    DELETE /repos/{repo}/pulls/{n}/reviews/{id}
        -> 422 "Can not delete a non-pending pull request review"
and it will NOT let you blank the body of a COMMENT review that has no inline
comments left:
    PUT /repos/{repo}/pulls/{n}/reviews/{id}  body=""
        -> 422 "Body required for pull request reviews that are comments..."

But a COMMENT review with an empty body and no comments does not survive at
all — GitHub garbage-collects it, and a subsequent GET returns 404. So undo is:

    1. PUT the review body to ""        (allowed WHILE it still has a comment)
    2. DELETE each inline comment        (DELETE /repos/{repo}/pulls/comments/{id})
    -> the review disappears entirely.

Doing those two in the other order strands an un-removable review on the PR
forever. That is exactly why path 2 posts an ISSUE comment on the PR
conversation rather than a body-only review: a body-only review cannot be taken
back by any means the API offers, and the path that fires when Adjourn is LEAST
sure must be the one that is EASIEST to reverse.

Secrets: none — `gh` carries its own auth. The gh runner, the 404-tolerance
helper and the readiness probe live in github_update_executor so the package has
exactly one of each.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import config, results
from ..planner import normalize_key_text
from .github_update_executor import decide_github_mode, is_already_gone, run_github_cli

if TYPE_CHECKING:
    from ..planner import Action

ACTION_KIND = "pr_review_suggestion"
REQUIRED_SECRETS: tuple[str, ...] = ()  # gh CLI carries its own auth

# --- matching thresholds (tuned to refuse, not to reach) ---------------------

# Fraction of the topic's own words a PR must cover before it is a candidate.
MINIMUM_TOPIC_COVERAGE = 0.6
# ...and at least this many real words have to overlap, so a one-word topic like
# "button" cannot pull in every PR that happens to say "button".
MINIMUM_OVERLAPPING_TOKENS = 2
# The winner must beat the runner-up by this much. Two equally plausible PRs is
# the same answer as no PR at all: we do not pick one and hope.
REQUIRED_MATCH_MARGIN = 0.15

# Words that carry no identifying weight in a PR title or a spoken topic.
STOPWORD_TOKENS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "to", "for", "of", "in", "on", "and", "or", "is", "are",
        "be", "with", "from", "at", "by", "it", "this", "that", "we", "our", "my",
        "wip", "draft", "demo", "prop", "pr", "feat", "fix", "chore",
    }
)

# "#22", "PR 22", "pull request #22". `#(\d+)\b` on purpose: a bare `#(\d+)`
# reads the "#2" out of the hex colour "#2ecc71" and would confidently review
# pull request number two.
EXPLICIT_NUMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"#(\d{1,6})\b"),
    re.compile(r"\bpull\s+request\s+#?(\d{1,6})\b", re.IGNORECASE),
    re.compile(r"\bPR\s+#?(\d{1,6})\b", re.IGNORECASE),
)

# What counts as a value concrete enough to go looking for in a diff.
HEX_COLOUR_PATTERN = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
NUMBER_WITH_UNIT_PATTERN = re.compile(r"^-?\d+(?:\.\d+)?(?:px|rem|em|%|s|ms|vh|vw|pt)$")
CODE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_$@.\-][A-Za-z0-9_$@.\-/]{2,}$")
QUOTED_PATTERN = re.compile(r"""^["'`].+["'`]$""")

DIFF_HUNK_HEADER_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# A review comment lands on the RIGHT (post-change) side of the diff; that is the
# only side a ```suggestion block can rewrite.
DIFF_SIDE = "RIGHT"

MAX_REPORTED_CANDIDATES = 5


@dataclass(frozen=True)
class DiffLine:
    """One line of a PR's diff that exists in the post-change file. Pure data.

    path      — file path in the repo.
    line      — 1-based line number in the NEW file (what the API wants as `line`).
    content   — the line's text, without the leading '+' or ' ' diff marker.
    is_added  — True for a '+' line, False for an unchanged context line.
    """

    path: str
    line: int
    content: str
    is_added: bool


# --- pure helpers -----------------------------------------------------------


def _one_line(value: object) -> str:
    """Collapse whitespace and newlines into one tidy line."""
    return " ".join(str(value or "").split())


def assert_repo_is_allowed(repo: str) -> None:
    """Guard: refuse to touch any repo outside the allowlist. Reads included.

    The other GitHub executors guard their writes with this; this one guards its
    reads too, because "search every open PR for a string somebody said out
    loud" is a fishing expedition the moment it is pointed at a repo nobody
    approved.
    """
    if repo not in config.ALLOWED_GITHUB_REPOS:
        raise PermissionError(
            f"refusing GitHub access to {repo!r}; "
            f"allowed: {sorted(config.ALLOWED_GITHUB_REPOS)}"
        )


def tokenize(text: str) -> set[str]:
    """Identifying words of a title, branch or topic. Pure.

        >>> sorted(tokenize("[demo-prop] Add join button to landing page"))
        ['add', 'button', 'join', 'landing', 'page']
    """
    tokens = normalize_key_text(text).split("-")
    return {token for token in tokens if token and token not in STOPWORD_TOKENS}


def is_concrete_value(value: object) -> bool:
    """Is `old_value` specific enough to go find in a diff? Pure, and strict.

    A hex colour, a `12px`, a `--join-accent`, a quoted string or a multi-word
    phrase can be located. A bare "green" or "the colour" cannot, and pretending
    otherwise is how a suggestion lands on the wrong line.

        >>> [is_concrete_value(v) for v in ("#2ecc71", "12px", "green", "")]
        [True, True, False, False]
    """
    text = _one_line(value)
    if len(text) < 3:
        return False
    if HEX_COLOUR_PATTERN.match(text) or NUMBER_WITH_UNIT_PATTERN.match(text):
        return True
    if QUOTED_PATTERN.match(text):
        return True
    if len(text.split()) >= 2:
        return True  # a phrase is specific enough to search for verbatim
    if CODE_IDENTIFIER_PATTERN.match(text) and re.search(r"[_$@.\-/0-9]|[a-z][A-Z]", text):
        return True
    return False


def find_explicit_pull_request_number(*candidates: object) -> int | None:
    """First '#22'-shaped pull request number in the candidates, in order. Pure."""
    for candidate in candidates:
        text = str(candidate or "")
        for pattern in EXPLICIT_NUMBER_PATTERNS:
            match = pattern.search(text)
            if match:
                return int(match.group(1))
    return None


def score_pull_request(topic: str, title: str, branch: str) -> float:
    """0.0 .. 1.0 — how much of `topic` this PR's title and branch account for. Pure.

    Coverage of the TOPIC (not of the title) on purpose: a long PR title should
    not be penalised for saying more than the meeting did.

    A one-word topic can only ever score by its single word — the floor below is
    `min(2, len(topic))`, not a flat 2 — and it is the MARGIN check in
    `choose_pull_request` that keeps such a topic safe: one word that two open
    PRs both contain is a tie, and a tie is refused.
    """
    wanted = tokenize(topic)
    if not wanted:
        return 0.0
    available = tokenize(title) | tokenize(branch)
    overlap = wanted & available
    if len(overlap) < min(MINIMUM_OVERLAPPING_TOKENS, len(wanted)):
        return 0.0
    return len(overlap) / len(wanted)


def choose_pull_request(topic: str, candidates: list[dict]) -> tuple[dict | None, str]:
    """The one open PR that `topic` means, or (None, why not). Pure and conservative.

    Returns (pull_request, reason). A None first element is a normal, expected
    outcome — the caller turns it into an honest failure, never a guess.
    """
    if not candidates:
        return None, "no pull requests are open"
    if not tokenize(topic):
        return None, f"the topic {_one_line(topic)!r} has no words specific enough to match on"

    scored = sorted(
        (
            (
                score_pull_request(
                    topic,
                    str(candidate.get("title", "")),
                    str((candidate.get("head") or {}).get("ref", "")),
                ),
                candidate,
            )
            for candidate in candidates
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    if best_score < MINIMUM_TOPIC_COVERAGE:
        return None, (
            f"no open PR matched {_one_line(topic)!r} "
            f"(closest was #{best.get('number')} “{_one_line(best.get('title'))}”)"
        )
    runner_up_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score - runner_up_score < REQUIRED_MATCH_MARGIN:
        return None, (
            f"{_one_line(topic)!r} matched #{best.get('number')} and "
            f"#{scored[1][1].get('number')} equally well — refusing to pick one"
        )
    return best, f"matched #{best.get('number')} on {best_score:.0%} of the topic"


def parse_patch(path: str, patch: str) -> list[DiffLine]:
    """Every post-change line of one file's unified diff, with real line numbers. Pure.

    Removed ('-') lines are dropped: they do not exist in the file a suggestion
    would rewrite, so they can never be an anchor.
    """
    lines: list[DiffLine] = []
    new_line_number = 0
    inside_hunk = False
    for raw in (patch or "").splitlines():
        header = DIFF_HUNK_HEADER_PATTERN.match(raw)
        if header:
            new_line_number = int(header.group(1))
            inside_hunk = True
            continue
        if not inside_hunk or raw.startswith("\\"):
            continue  # "\ No newline at end of file" and any preamble
        if raw.startswith("-"):
            continue
        if raw.startswith("+"):
            lines.append(DiffLine(path, new_line_number, raw[1:], True))
            new_line_number += 1
        else:
            lines.append(DiffLine(path, new_line_number, raw[1:] if raw else "", False))
            new_line_number += 1
    return lines


def narrow_files_by_hint(files: list[dict], file_hint: str) -> list[dict]:
    """Files whose path matches `file_hint`, or every file when it matches none. Pure.

    A hint that matches nothing is a wrong hint, not an empty answer — the model
    guessed the filename and missed. Searching everything and saying so beats
    reporting "not found" for a value that is sitting right there.
    """
    hint = _one_line(file_hint).lower()
    if not hint:
        return files
    narrowed = [file for file in files if hint in str(file.get("filename", "")).lower()]
    if narrowed:
        return narrowed
    print(f"[{ACTION_KIND}] file_hint {file_hint!r} matched no file in this PR — searching all of them")
    return files


def locate_value(files: list[dict], old_value: str, file_hint: str = "") -> list[DiffLine]:
    """Every diff line containing `old_value`, after the hint narrows the search. Pure."""
    needle = str(old_value or "")
    if not needle:
        return []
    found: list[DiffLine] = []
    for file in narrow_files_by_hint(files, file_hint):
        path = str(file.get("filename", ""))
        for diff_line in parse_patch(path, str(file.get("patch") or "")):
            if needle in diff_line.content:
                found.append(diff_line)
    return found


def value_shape(value: object) -> str:
    """The kind of thing `value` is, or "" if it is not a locatable kind. Pure.

        >>> [value_shape(v) for v in ("#2ecc71", "12px", "green")]
        ['hex-colour', 'number-with-unit', '']
    """
    text = _one_line(value)
    if HEX_COLOUR_PATTERN.match(text):
        return "hex-colour"
    if NUMBER_WITH_UNIT_PATTERN.match(text):
        return "number-with-unit"
    return ""


SHAPE_SCANNERS = {
    "hex-colour": re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b"),
    "number-with-unit": re.compile(r"-?\d+(?:\.\d+)?(?:px|rem|em|%|s|ms|vh|vw|pt)\b"),
}


def derive_old_value(files: list[dict], new_value: str, file_hint: str = "") -> tuple[str, str]:
    """Infer what is being replaced from the diff itself. Returns (value, reason).

    The demo's own beat is the reason this exists. Somebody says "that green is
    wrong, it should be our slate blue, six B seven F nine nine" — they say the
    NEW colour and they never say the old one, because the old one is on screen
    in front of them. A transcript-only extractor structurally cannot produce
    `old_value`; it is in the diff, not in the room.

    So: scan the PR's added lines for values of the SAME SHAPE as `new_value`
    (one hex colour is being swapped for another) and accept the answer ONLY if
    the whole diff contains exactly one distinct such value. Two candidates is
    ambiguity, and ambiguity here means editing a line nobody talked about, so
    two candidates returns nothing and the caller falls back to the plain PR
    comment. That is the restraint thesis applied to the executor's own guessing.

    Only ADDED lines are scanned. Context lines are code the PR did not touch,
    and a review that rewrites untouched code is a different and worse action.
    """
    shape = value_shape(new_value)
    if not shape:
        return "", "the new value is not a shape Adjourn knows how to find in a diff"

    scanner = SHAPE_SCANNERS[shape]
    target = _one_line(new_value).lower()
    found: dict[str, list[DiffLine]] = {}
    for file in narrow_files_by_hint(files, file_hint):
        path = str(file.get("filename", ""))
        for diff_line in parse_patch(path, str(file.get("patch") or "")):
            if not diff_line.is_added:
                continue
            for hit in scanner.findall(diff_line.content):
                if hit.lower() == target:
                    continue  # already the new value; nothing to change
                found.setdefault(hit, []).append(diff_line)

    if not found:
        return "", f"no {shape} appears on an added line in this PR's diff"
    if len(found) > 1:
        shown = ", ".join(f"`{value}`" for value in sorted(found)[:MAX_REPORTED_CANDIDATES])
        return "", (
            f"this PR adds {len(found)} different {shape} values ({shown}) — "
            "Adjourn did not guess which one the room meant"
        )

    value, lines = next(iter(found.items()))
    if len(lines) > 1:
        return "", (
            f"`{value}` is the only {shape} in the diff but it appears on "
            f"{len(lines)} lines — Adjourn did not guess which one was meant"
        )
    return value, f"read `{value}` out of the diff — the only {shape} this PR adds"


def build_suggestion_line(content: str, old_value: str, new_value: str) -> str:
    """The replacement line: the located line with old swapped for new. Pure.

    The WHOLE line goes in the suggestion block — that is what GitHub replaces —
    so indentation and everything else on the line survive untouched.
    """
    return content.replace(old_value, new_value)


def _excerpt_block(action: Action, timestamp: str = "") -> list[str]:
    """The collapsible verbatim meeting excerpt. Pure. Empty when there is no quote."""
    quote = _one_line(action.quote)
    if not quote:
        return []
    speaker = _one_line(action.speaker) or "someone in the room"
    excerpt = f"> **{speaker}:** {quote}"
    if timestamp:
        excerpt = f"> [{timestamp}] **{speaker}:** {quote}"
    return ["<details>", "<summary>Meeting excerpt</summary>", "", excerpt, "", "</details>", ""]


def _footer(action: Action) -> str:
    """One provenance line, matching github_update's. Pure."""
    payload = action.payload
    meeting = _one_line(payload.get("meeting_title", "")) or "an untitled meeting"
    meeting_date = _one_line(payload.get("meeting_date", ""))
    return (
        f"_Meeting: {meeting}"
        + (f", {meeting_date}" if meeting_date else "")
        + " · Suggested by Adjourn the moment the meeting ended · "
        "nothing was pushed · transcript never left this machine_"
    )


def describe_change(action: Action) -> str:
    """"join button green `#2ecc71` → `#6b7f99`" — the change in one phrase. Pure."""
    payload = action.payload
    description = _one_line(payload.get("change_description", ""))
    old_value = _one_line(payload.get("old_value", ""))
    new_value = _one_line(payload.get("new_value", ""))
    if old_value and new_value:
        pair = f"`{old_value}` → `{new_value}`"
        return f"{description} {pair}".strip()
    return description or "a change discussed in the meeting"


def build_inline_comment_body(action: Action, located: DiffLine) -> str:
    """The inline review comment: what changed, the suggestion block, the quote. Pure."""
    payload = action.payload
    old_value = str(payload.get("old_value") or "")
    new_value = str(payload.get("new_value") or "")
    replacement = build_suggestion_line(located.content, old_value, new_value)
    lines = [
        "**Adjourn — a meeting asked for this change.**",
        "",
        describe_change(action),
        "",
        "```suggestion",
        replacement,
        "```",
        "",
    ]
    lines += _excerpt_block(action, _one_line(payload.get("timestamp", "")))
    lines += ["---", "", _footer(action)]
    return "\n".join(lines) + "\n"


def build_review_body(action: Action, located: DiffLine) -> str:
    """The review's own body — the line the PR timeline shows. Short on purpose. Pure.

    Short because undo blanks it before deleting the inline comment, and because
    everything worth reading is at the line, not in the timeline.
    """
    payload = action.payload
    meeting = _one_line(payload.get("meeting_title", "")) or "an untitled meeting"
    meeting_date = _one_line(payload.get("meeting_date", ""))
    where = meeting + (f" ({meeting_date})" if meeting_date else "")
    return (
        "> [!NOTE]\n"
        f"> **One change requested during {where}.**\n"
        f"> Adjourn left an inline suggestion on `{located.path}` line {located.line}. "
        "Nothing was pushed — commit it or dismiss it like any other review.\n"
    )


def build_plain_comment_body(action: Action, reason: str) -> str:
    """Path 2: the change in words, the quote, and why no line was pointed at. Pure."""
    payload = action.payload
    lines = [
        "> [!NOTE]",
        "> **A meeting asked for a change here, but Adjourn would not guess where.**",
        "",
        f"**Requested:** {describe_change(action)}",
        "",
        f"_{reason}_",
        "",
    ]
    lines += _excerpt_block(action, _one_line(payload.get("timestamp", "")))
    lines += ["---", "", _footer(action)]
    return "\n".join(lines) + "\n"


def build_review_request(
    repo: str,
    pull_number: int,
    action: Action,
    located: DiffLine,
) -> dict:
    """The exact JSON body POSTed to /pulls/{n}/reviews. Pure — this IS the sim payload."""
    return {
        "endpoint": f"repos/{repo}/pulls/{pull_number}/reviews",
        "method": "POST",
        "body": {
            "event": "COMMENT",
            "body": build_review_body(action, located),
            "comments": [
                {
                    "path": located.path,
                    "line": located.line,
                    "side": DIFF_SIDE,
                    "body": build_inline_comment_body(action, located),
                }
            ],
        },
    }


def summarize_suggestion(pull_number: int, action: Action) -> str:
    """"Suggested change on PR #22: join button green #2ecc71 → #6b7f99". Pure."""
    payload = action.payload
    description = _one_line(payload.get("change_description", ""))
    old_value = _one_line(payload.get("old_value", ""))
    new_value = _one_line(payload.get("new_value", ""))
    tail = f"{description} {old_value} → {new_value}".strip()
    return f"Suggested change on PR #{pull_number}: {tail}" if tail else (
        f"Suggested a change on PR #{pull_number}"
    )


# --- read-only resolution (RUNS IN BOTH MODES — see the module docstring) ----


def resolution_reads_available() -> bool:
    """True when the read-only lookups can run. Never raises."""
    from .github_update_executor import is_github_cli_ready

    return is_github_cli_ready()


def resolve_pull_request(
    repo: str,
    payload: dict,
    action: Action,
) -> tuple[dict | None, str]:
    """(pull_request, reason) for this action, or (None, honest reason). Read-only.

    Order: an explicit `pr_number`, then a '#22' spoken in the room but only when
    that number is genuinely open, then the conservative topic match. Anything
    less than confident returns None.
    """
    open_pulls = _list_open_pull_requests_live(repo)
    by_number = {int(pull.get("number", 0)): pull for pull in open_pulls}

    explicit = payload.get("pr_number")
    if explicit:
        number = int(explicit)
        if number in by_number:
            return by_number[number], f"payload named PR #{number}"
        return None, f"PR #{number} is not open on {repo}"

    topic = str(payload.get("pr_topic") or "")
    spoken = find_explicit_pull_request_number(topic, action.quote)
    if spoken and spoken in by_number:
        return by_number[spoken], f"the room said PR #{spoken}"

    return choose_pull_request(topic, open_pulls)


def resolve_location(
    repo: str,
    pull_number: int,
    payload: dict,
) -> tuple[DiffLine | None, str]:
    """(located line, reason) for the requested change, or (None, why not). Read-only."""
    old_value = str(payload.get("old_value") or "")
    new_value = str(payload.get("new_value") or "")
    if not _one_line(new_value):
        return None, "the meeting did not say what to change it to, so no suggestion was made"

    files = _list_pull_request_files_live(repo, pull_number)
    if not files:
        return None, "this PR has no changed files to point at"

    hint = str(payload.get("file_hint") or "")

    # Nobody says the old hex out loud. When the room named only the new value,
    # read the old one out of the diff — but only when the diff answers with
    # exactly one candidate. See derive_old_value.
    if not is_concrete_value(old_value):
        spoken = _one_line(old_value)
        derived, why = derive_old_value(files, new_value, hint)
        if not derived:
            return None, (
                f"the value to change ({spoken or 'unstated'!r}) is not specific "
                f"enough to find in the diff, and {why}"
            )
        print(f"[{ACTION_KIND}] {why}")
        old_value = derived
        payload["old_value"] = derived
        payload["old_value_provenance"] = "diff"

    matches = locate_value(files, old_value, hint)
    if not matches:
        return None, f"`{old_value}` does not appear anywhere in this PR's diff"
    if len(matches) > 1:
        shown = ", ".join(
            f"`{match.path}:{match.line}`" for match in matches[:MAX_REPORTED_CANDIDATES]
        )
        return None, (
            f"`{old_value}` appears on {len(matches)} lines ({shown}) — "
            "Adjourn did not guess which one was meant"
        )
    return matches[0], f"located `{old_value}` at {matches[0].path}:{matches[0].line}"


# --- execute ----------------------------------------------------------------


def execute(action: Action) -> results.ExecutorResult:
    """Resolve the PR, locate the line, and leave a review — or say why it did not."""
    payload = action.payload
    repo = str(payload.get("repo") or config.github_repo())
    mode = decide_github_mode(ACTION_KIND)

    try:
        assert_repo_is_allowed(repo)
    except PermissionError as error:
        return results.ExecutorResult.failed(
            ACTION_KIND, str(error),
            quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
        )

    if not resolution_reads_available():
        return _render_unresolvable_simulation(action, repo)

    try:
        pull_request, match_reason = resolve_pull_request(repo, payload, action)
    except Exception as error:  # noqa: BLE001 — a failed lookup is a red card, not a crash
        return results.ExecutorResult.failed(
            ACTION_KIND, f"could not list open PRs on {repo}: {error}",
            quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
        )

    if pull_request is None:
        return results.ExecutorResult.failed(
            ACTION_KIND, match_reason,
            quote=action.quote, speaker=action.speaker, meeting_id=action.meeting_id,
        )

    pull_number = int(pull_request["number"])
    pull_url = str(pull_request.get("html_url") or f"https://github.com/{repo}/pull/{pull_number}")
    print(f"[{ACTION_KIND}] {match_reason}")

    try:
        located, location_reason = resolve_location(repo, pull_number, payload)
    except Exception as error:  # noqa: BLE001
        located, location_reason = None, f"could not read this PR's diff ({error})"

    if located is None:
        print(f"[{ACTION_KIND}] {location_reason}")
        return _comment_without_suggestion(
            action, repo, pull_number, pull_url, location_reason, mode
        )

    print(f"[{ACTION_KIND}] {location_reason}")
    request = build_review_request(repo, pull_number, action, located)
    summary = _one_line(payload.get("human_preview", "")) or summarize_suggestion(
        pull_number, action
    )

    if mode == "sim":
        return _render_simulated(action, request, human_summary=summary, url=pull_url)

    try:
        review_id, review_url, comment_ids = _post_review_live(
            repo, pull_number, request["body"]
        )
    except Exception as error:  # noqa: BLE001
        return results.ExecutorResult.failed(
            ACTION_KIND, f"could not review {repo}#{pull_number}: {error}",
            mode="live", quote=action.quote, speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    return results.ExecutorResult(
        ok=True,
        kind=ACTION_KIND,
        external_id=str(review_id),
        url=review_url or pull_url,
        human_summary=summary,
        mode="live",
        undo_payload={
            "operation": "review",
            "repo": repo,
            "pull_number": pull_number,
            "review_id": review_id,
            "comment_ids": comment_ids,
            "path": located.path,
            "line": located.line,
        },
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )


def _comment_without_suggestion(
    action: Action,
    repo: str,
    pull_number: int,
    pull_url: str,
    reason: str,
    mode: str,
) -> results.ExecutorResult:
    """Path 2 — the change in words on the PR conversation, and fully reversible."""
    body_markdown = build_plain_comment_body(action, reason)
    summary = (
        f"Commented on PR #{pull_number}: {describe_change(action)} "
        "— no single line to suggest on"
    )
    request = {
        "endpoint": f"repos/{repo}/issues/{pull_number}/comments",
        "method": "POST",
        "body": {"body": body_markdown},
    }

    if mode == "sim":
        return _render_simulated(action, request, human_summary=summary, url=pull_url)

    try:
        comment_id, comment_url = _post_plain_comment_live(repo, pull_number, body_markdown)
    except Exception as error:  # noqa: BLE001
        return results.ExecutorResult.failed(
            ACTION_KIND, f"could not comment on {repo}#{pull_number}: {error}",
            mode="live", quote=action.quote, speaker=action.speaker,
            meeting_id=action.meeting_id,
        )

    return results.ExecutorResult(
        ok=True,
        kind=ACTION_KIND,
        external_id=str(comment_id),
        url=comment_url or pull_url,
        human_summary=summary,
        mode="live",
        undo_payload={
            "operation": "issue_comment",
            "repo": repo,
            "pull_number": pull_number,
            "comment_id": comment_id,
        },
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )


def undo(result: results.ExecutorResult) -> bool:
    """Take the review (or the comment) back. See the docstring: the ORDER matters."""
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

    if undo_payload.get("operation") == "issue_comment":
        comment_id = undo_payload.get("comment_id")
        if not comment_id:
            return False
        try:
            run_github_cli("api", "-X", "DELETE", f"repos/{repo}/issues/comments/{comment_id}")
        except Exception as error:  # noqa: BLE001
            if is_already_gone(error):
                print(f"[{ACTION_KIND}] comment {comment_id} was already deleted")
                return True
            print(f"[{ACTION_KIND}] could not delete comment {comment_id}: {error}")
            return False
        return True

    pull_number = undo_payload.get("pull_number")
    review_id = undo_payload.get("review_id")
    comment_ids = [str(identifier) for identifier in (undo_payload.get("comment_ids") or [])]
    if not pull_number or not review_id:
        return False

    reversed_everything = True

    # 1. Blank the review body FIRST — permitted only while an inline comment is
    #    still attached. Reverse this order and the review is stranded forever.
    try:
        run_github_cli(
            "api", "-X", "PUT", f"repos/{repo}/pulls/{pull_number}/reviews/{review_id}",
            "-f", "body=",
        )
    except Exception as error:  # noqa: BLE001
        if is_already_gone(error):
            print(f"[{ACTION_KIND}] review {review_id} was already gone")
            return True
        print(f"[{ACTION_KIND}] could not blank review {review_id}: {error}")
        reversed_everything = False

    # 2. Then delete the inline comment(s); GitHub drops the now-empty review.
    for comment_id in comment_ids:
        try:
            run_github_cli("api", "-X", "DELETE", f"repos/{repo}/pulls/comments/{comment_id}")
        except Exception as error:  # noqa: BLE001
            if is_already_gone(error):
                print(f"[{ACTION_KIND}] review comment {comment_id} was already deleted")
            else:
                print(f"[{ACTION_KIND}] could not delete review comment {comment_id}: {error}")
                reversed_everything = False

    return reversed_everything and review_is_gone(repo, int(pull_number), str(review_id))


def review_is_gone(repo: str, pull_number: int, review_id: str) -> bool:
    """True when GET on the review 404s — the proof undo actually worked. Read-only."""
    try:
        run_github_cli("api", f"repos/{repo}/pulls/{pull_number}/reviews/{review_id}")
    except Exception as error:  # noqa: BLE001
        return is_already_gone(error)
    print(f"[{ACTION_KIND}] review {review_id} still exists after undo")
    return False


# --- TRANSPORT SWAP (bottom of the module) ----------------------------------


def _list_open_pull_requests_live(repo: str) -> list[dict]:
    """READ-ONLY. Open PRs on the allowlisted repo, newest first.

    The query string goes in the PATH: `gh api` flips to POST the moment a `-f`
    field is present, and `-f state=open` here would try to OPEN a pull request.
    """
    assert_repo_is_allowed(repo)
    listed = json.loads(run_github_cli("api", f"repos/{repo}/pulls?state=open&per_page=100"))
    return listed if isinstance(listed, list) else []


def _list_pull_request_files_live(repo: str, pull_number: int) -> list[dict]:
    """READ-ONLY. The PR's changed files, each with its unified `patch`."""
    assert_repo_is_allowed(repo)
    listed = json.loads(
        run_github_cli("api", f"repos/{repo}/pulls/{pull_number}/files?per_page=100")
    )
    return listed if isinstance(listed, list) else []


def _post_review_live(
    repo: str,
    pull_number: int,
    review_body: dict,
) -> tuple[str, str, list[str]]:
    """The write. Returns (review_id, html_url, inline comment ids).

    The JSON goes over STDIN via `--input -` because the review carries a nested
    `comments` array that `-f key=value` cannot express, and because a markdown
    body must never be shell-interpolated.

    The create response does not carry the inline comment ids, so they are read
    back from /reviews/{id}/comments — undo needs them, and undo is the whole
    reason this executor is allowed to write at all.
    """
    assert_repo_is_allowed(repo)
    created = json.loads(
        run_github_cli(
            "api", f"repos/{repo}/pulls/{pull_number}/reviews",
            "--input", "-",
            stdin=json.dumps(review_body),
        )
    )
    review_id = str(created["id"])
    review_url = created.get("html_url", "")
    try:
        comments = json.loads(
            run_github_cli("api", f"repos/{repo}/pulls/{pull_number}/reviews/{review_id}/comments")
        )
        comment_ids = [str(comment["id"]) for comment in comments]
    except Exception as error:  # noqa: BLE001 — the review landed; undo degrades, not the send
        print(f"[{ACTION_KIND}] could not read back review comment ids: {error}")
        comment_ids = []
    return review_id, review_url, comment_ids


def _post_plain_comment_live(repo: str, pull_number: int, body_markdown: str) -> tuple[str, str]:
    """The other write. A PR-conversation comment; body on stdin, never argv."""
    assert_repo_is_allowed(repo)
    posted = json.loads(
        run_github_cli(
            "api", f"repos/{repo}/issues/{pull_number}/comments",
            "-F", "body=@-",
            stdin=body_markdown,
        )
    )
    return str(posted["id"]), posted.get("html_url", "")


def _render_simulated(
    action: Action,
    request: dict,
    *,
    human_summary: str,
    url: str | None = None,
) -> results.ExecutorResult:
    """Sim mode: the byte-exact request that would have gone out, unsent.

    The suggestion block inside it is real — the line it rewrites was read from
    the actual diff a moment ago.
    """
    print(
        f"[{ACTION_KIND}] SIM — not writing to GitHub. Would POST "
        f"{request['endpoint']}:\n{json.dumps(request['body'], indent=2)}"
    )
    return results.ExecutorResult.simulated(
        ACTION_KIND,
        human_summary,
        rendered_payload=request,
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
        url=url,
    )


def _render_unresolvable_simulation(action: Action, repo: str) -> results.ExecutorResult:
    """Sim mode with no `gh` at all: placeholders, clearly marked, never fiction."""
    payload = action.payload
    topic = _one_line(payload.get("pr_topic", "")) or "the change discussed"
    old_value = _one_line(payload.get("old_value", ""))
    new_value = _one_line(payload.get("new_value", ""))
    request = {
        "endpoint": f"repos/{repo}/pulls/<open PR matching {topic!r}>/reviews",
        "method": "POST",
        "body": {
            "event": "COMMENT",
            "body": build_review_body(
                action, DiffLine("<file containing " + (old_value or "the old value") + ">", 0, "", True)
            ),
            "comments": [
                {
                    "path": _one_line(payload.get("file_hint", "")) or "<file from the PR diff>",
                    "line": "<line containing " + (old_value or "the old value") + ">",
                    "side": DIFF_SIDE,
                    "body": (
                        f"{describe_change(action)}\n\n"
                        "```suggestion\n"
                        f"<the located line, with {old_value or 'the old value'} replaced by "
                        f"{new_value or 'the new value'}>\n"
                        "```\n"
                    ),
                }
            ],
        },
        "unresolved": "`gh` is unavailable, so the PR and the exact line were not read",
    }
    print(
        f"[{ACTION_KIND}] SIM — `gh` unavailable, so the anchor is a placeholder:\n"
        f"{json.dumps(request['body'], indent=2)}"
    )
    return results.ExecutorResult.simulated(
        ACTION_KIND,
        f"Suggested change on the open PR for {topic}"
        + (f": {old_value} → {new_value}" if old_value and new_value else ""),
        rendered_payload=request,
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=action.meeting_id,
    )
