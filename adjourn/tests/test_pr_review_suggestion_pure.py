"""Lane 2 regression gate — pr_review_suggestion, with nothing on the wire.

Run from the repo root:
    python -m adjourn.tests.test_pr_review_suggestion_pure

SAFE to run repeatedly and offline. ADJOURN_SIM=1 is forced before the package
is imported, and the executor's two read-only lookups (list open PRs, list a
PR's files) are replaced with fixtures taken verbatim from the real
sharique2004/adjourn PR #22 diff — so this file cannot reach GitHub even if
`gh` is authenticated on the machine running it.

What is checked:
  * the diff parser puts real NEW-file line numbers on '+' and context lines,
    across multiple hunks, and never on a removed line
  * `old_value` concreteness — a hex colour is findable, "green" is not
  * PR matching covers the topic, refuses a weak match, and refuses a tie
  * "#2" is never read out of the hex colour "#2ecc71"
  * exactly-one-match renders a real ```suggestion block with the WHOLE line
  * zero and many matches both fall back to the plain comment, with the reason
  * a topic that matches no open PR is a FAILED result, not a guess
  * the repo allowlist refuses reads as well as writes
  * undo of a simulated result is honest-true (nothing left the machine)
"""

from __future__ import annotations

import os

# Forced BEFORE the package is imported: dotenv does not override a set
# variable, so this holds even though adjourn/.env ships ADJOURN_SIM=0.
os.environ["ADJOURN_SIM"] = "1"
os.environ["ADJOURN_LIVE_KINDS"] = ""
# Deliberately set to a value that USED to arm a direct-commit path, so that the
# inversion section at the bottom can prove the setting is inert: the capability
# was deleted, not defaulted off, and nothing in the tree reads this name.
os.environ["ADJOURN_PR_AUTOCOMMIT_BRANCHES"] = "priya/join-button"

from adjourn import config, results  # noqa: E402
from adjourn.executors import pr_review_suggestion_executor as executor  # noqa: E402
from adjourn.planner import Action  # noqa: E402

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def make_action(payload: dict, **overrides) -> Action:
    return Action(
        kind=executor.ACTION_KIND,
        payload=payload,
        dedup_key=overrides.get("dedup_key", "pr_review_suggestion:test"),
        quote=overrides.get(
            "quote",
            "That join button green is way too loud — take it to #6b7f99 before the demo.",
        ),
        speaker=overrides.get("speaker", "Priya"),
        meeting_id=overrides.get("meeting_id", "20260821-140312"),
        segment_id=overrides.get("segment_id", "L12"),
        source=overrides.get("source", "live"),
    )


# --- fixtures: the real shape the GitHub API returns ------------------------

JOIN_CSS_PATCH = (
    "@@ -0,0 +1,12 @@\n"
    "+:root {\n"
    "+  --bg: #0b0d10;\n"
    "+\n"
    "+  /* Beta CTA accent */\n"
    "+  --join-accent: #2ecc71;\n"
    "+  --join-accent-ink: #08120c;\n"
    "+}\n"
    "+\n"
    "+.btn-join {\n"
    "+  background: var(--join-accent);\n"
    "+  padding: 13px 24px;\n"
    "+}\n"
)

JOIN_HTML_PATCH = (
    "@@ -0,0 +1,3 @@\n"
    "+<button class=\"btn-join\" type=\"submit\">Join the beta</button>\n"
    "+<p class=\"fineprint\">One email when your slot opens.</p>\n"
    "+</main>\n"
)

# Two hunks, a removed line, and a context line — the shapes the parser has to
# survive on a PR that edits an existing file rather than adding one.
EXISTING_FILE_PATCH = (
    "@@ -1,4 +1,4 @@\n"
    " .sidebar {\n"
    "-  padding: 8px;\n"
    "+  padding: 12px;\n"
    " }\n"
    "@@ -40,3 +40,4 @@\n"
    " .footer {\n"
    "+  color: #2ecc71;\n"
    " }\n"
)

OPEN_PULLS = [
    {
        "number": 22,
        "title": "[demo-prop] Add join button to landing page",
        "html_url": "https://github.com/sharique2004/adjourn/pull/22",
        "head": {"ref": "priya/join-button"},
    },
    {
        "number": 19,
        "title": "Bump the Redis client and retune the cache TTL",
        "html_url": "https://github.com/sharique2004/adjourn/pull/19",
        "head": {"ref": "adjourn/cache-layer"},
    },
]

FILES_BY_PULL: dict[int, list[dict]] = {
    22: [
        {"filename": "site/join.css", "patch": JOIN_CSS_PATCH},
        {"filename": "site/join.html", "patch": JOIN_HTML_PATCH},
    ],
    19: [{"filename": "app/cache.py", "patch": "@@ -1,2 +1,2 @@\n-TTL = 60\n+TTL = 300\n"}],
}

network_calls: list[str] = []


def fake_list_open_pull_requests(repo: str) -> list[dict]:
    executor.assert_repo_is_allowed(repo)  # the guard is part of what we are testing
    network_calls.append(f"list_pulls:{repo}")
    return list(OPEN_PULLS)


def fake_list_pull_request_files(repo: str, pull_number: int) -> list[dict]:
    executor.assert_repo_is_allowed(repo)
    network_calls.append(f"list_files:{repo}#{pull_number}")
    return list(FILES_BY_PULL.get(int(pull_number), []))


def refuse_to_write(*args, **kwargs):  # noqa: ANN002, ANN003
    raise AssertionError("a write escaped sim mode")


executor._list_open_pull_requests_live = fake_list_open_pull_requests
executor._list_pull_request_files_live = fake_list_pull_request_files
executor._post_review_live = refuse_to_write
executor._post_plain_comment_live = refuse_to_write
executor.resolution_reads_available = lambda: True


BASE_PAYLOAD = {
    "pr_topic": "join button",
    "change_description": "join button green",
    "old_value": "#2ecc71",
    "new_value": "#6b7f99",
    "file_hint": "join.css",
    "meeting_title": "Eng sync",
    "meeting_date": "2026-08-21",
    "timestamp": "12:03",
}


print("== diff parsing: real line numbers on the post-change file ==")
parsed = executor.parse_patch("site/join.css", JOIN_CSS_PATCH)
check("added file yields one entry per line", len(parsed) == 12, f"got {len(parsed)}")
accent = [line for line in parsed if "#2ecc71" in line.content]
check("the accent line is found exactly once", len(accent) == 1, f"got {len(accent)}")
check(
    "the accent line carries NEW-file line 5",
    accent and accent[0].line == 5,
    f"got {accent[0].line if accent else 'nothing'}",
)
check(
    "the accent line keeps its leading indentation",
    accent and accent[0].content == "  --join-accent: #2ecc71;",
    repr(accent[0].content) if accent else "",
)

multi = executor.parse_patch("site/base.css", EXISTING_FILE_PATCH)
removed = [line for line in multi if "padding: 8px" in line.content]
check("a removed line is never an anchor", not removed)
padding = [line for line in multi if "padding: 12px" in line.content]
check("second hunk restarts numbering", padding and padding[0].line == 2, str(padding))
footer_colour = [line for line in multi if "#2ecc71" in line.content]
check(
    "line numbers in the second hunk come from its own header",
    footer_colour and footer_colour[0].line == 41,
    str(footer_colour),
)
check(
    "context lines are anchorable too",
    any(line.content == " }" or line.content == "}" for line in multi)
    or any(not line.is_added for line in multi),
)


print("\n== concreteness: what is worth going to look for ==")
for value, expected in (
    ("#2ecc71", True),
    ("#fff", True),
    ("12px", True),
    ("--join-accent", True),
    ("Join the beta", True),
    ('"btn-join"', True),
    ("green", False),
    ("it", False),
    ("", False),
    (None, False),
):
    check(f"is_concrete_value({value!r}) is {expected}", executor.is_concrete_value(value) is expected)


print("\n== the hex colour must never be read as a PR number ==")
check(
    "'#2ecc71' does not yield PR #2",
    executor.find_explicit_pull_request_number("make it #2ecc71") is None,
)
check("'#22' does yield 22", executor.find_explicit_pull_request_number("look at #22") == 22)
check(
    "'PR 22' does yield 22",
    executor.find_explicit_pull_request_number("the one in PR 22") == 22,
)


print("\n== PR matching: cover the topic, or refuse ==")
chosen, reason = executor.choose_pull_request("join button", OPEN_PULLS)
check("'join button' picks #22", chosen is not None and chosen["number"] == 22, reason)
chosen, reason = executor.choose_pull_request("cache layer", OPEN_PULLS)
check("'cache layer' picks #19 off the branch name", chosen is not None and chosen["number"] == 19, reason)
chosen, reason = executor.choose_pull_request("the onboarding email copy", OPEN_PULLS)
check("an unrelated topic matches nothing", chosen is None, reason)
check("...and says so honestly", "no open PR matched" in reason, reason)
chosen, reason = executor.choose_pull_request("button", OPEN_PULLS)
check(
    "a one-word topic is allowed only while it is unique",
    chosen is not None and chosen["number"] == 22,
    reason,
)
crowded = OPEN_PULLS + [
    {"number": 25, "title": "Restyle the cancel button", "head": {"ref": "x/cancel-button"}}
]
chosen, reason = executor.choose_pull_request("button", crowded)
check("...and refused the moment a second PR shares the word", chosen is None, reason)
tie = [
    {"number": 30, "title": "Add join button", "head": {"ref": "a/join-button"}},
    {"number": 31, "title": "Fix join button", "head": {"ref": "b/join-button"}},
]
chosen, reason = executor.choose_pull_request("join button", tie)
check("a tie is refused rather than broken", chosen is None, reason)
check("...and names both", "#30" in reason and "#31" in reason, reason)
chosen, reason = executor.choose_pull_request("join button", [])
check("no open PRs at all is handled", chosen is None, reason)


print("\n== path 1: exactly one match renders a real suggestion ==")
network_calls.clear()
result = executor.execute(make_action(dict(BASE_PAYLOAD)))
check("the action succeeds", result.ok, result.human_summary)
check("badged SIM", result.mode == results.MODE_SIM, result.mode)
check("nothing was written", "list_files:sharique2004/adjourn#22" in network_calls, str(network_calls))
check(
    "the human summary reads like a person wrote it",
    result.human_summary == "Suggested change on PR #22: join button green #2ecc71 → #6b7f99",
    result.human_summary,
)
rendered = result.undo_payload["payload"]
check(
    "the endpoint is the reviews endpoint",
    rendered["endpoint"] == "repos/sharique2004/adjourn/pulls/22/reviews",
    rendered["endpoint"],
)
check("event is COMMENT", rendered["body"]["event"] == "COMMENT")
inline = rendered["body"]["comments"][0]
check("anchored to the right file", inline["path"] == "site/join.css", inline["path"])
check("anchored to the right line", inline["line"] == 5, str(inline["line"]))
check("anchored to the RIGHT side", inline["side"] == "RIGHT", inline["side"])
check("the suggestion block is present", "```suggestion" in inline["body"])
check(
    "the suggestion replaces the WHOLE line, indentation intact",
    "\n  --join-accent: #6b7f99;\n" in inline["body"],
    inline["body"],
)
check("the old value is gone from the suggestion", "#2ecc71" not in inline["body"].split("```suggestion")[1].split("```")[0])
check("the verbatim quote is cited", "too loud" in inline["body"], inline["body"])
check("the speaker is named", "Priya" in inline["body"])
check("the meeting is named", "Eng sync" in rendered["body"]["body"])
check("undo of a sim result is honest-true", executor.undo(result) is True)


print("\n== the suggestion line itself ==")
check(
    "build_suggestion_line swaps only the old value",
    executor.build_suggestion_line("  --join-accent: #2ecc71;", "#2ecc71", "#6b7f99")
    == "  --join-accent: #6b7f99;",
)


print("\n== path 2a: zero matches falls back to a plain comment ==")
payload = dict(BASE_PAYLOAD, old_value="#ff0000", file_hint="")
result = executor.execute(make_action(payload))
check("the action still succeeds", result.ok, result.human_summary)
rendered = result.undo_payload["payload"]
check(
    "it targets the PR conversation, not a review",
    rendered["endpoint"] == "repos/sharique2004/adjourn/issues/22/comments",
    rendered["endpoint"],
)
check("no suggestion block is offered", "```suggestion" not in rendered["body"]["body"])
check(
    "and it says why",
    "does not appear anywhere in this PR's diff" in rendered["body"]["body"],
    rendered["body"]["body"],
)
check("the quote still rides along", "too loud" in rendered["body"]["body"])


print("\n== path 2b: several matches is also a refusal to guess ==")
payload = dict(BASE_PAYLOAD, old_value="btn-join", file_hint="")
result = executor.execute(make_action(payload))
rendered = result.undo_payload["payload"]
check("still a plain comment", rendered["endpoint"].endswith("/issues/22/comments"), rendered["endpoint"])
check(
    "the count of candidates is stated",
    "appears on 2 lines" in rendered["body"]["body"],
    rendered["body"]["body"],
)
check(
    "and the candidate lines are named, file and line",
    "site/join.css:9" in rendered["body"]["body"]
    and "site/join.html:1" in rendered["body"]["body"],
    rendered["body"]["body"],
)


print("\n== path 2c: a vague old_value never becomes a suggestion ==")
result = executor.execute(make_action(dict(BASE_PAYLOAD, old_value="green")))
rendered = result.undo_payload["payload"]
check("vague values go to the plain comment", rendered["endpoint"].endswith("/issues/22/comments"))
check("and say they were too vague", "not specific" in rendered["body"]["body"], rendered["body"]["body"])

result = executor.execute(make_action(dict(BASE_PAYLOAD, new_value=None)))
rendered = result.undo_payload["payload"]
check("no new_value means no suggestion", rendered["endpoint"].endswith("/issues/22/comments"))
check(
    "and says the meeting never named a replacement",
    "did not say what to change it to" in rendered["body"]["body"],
    rendered["body"]["body"],
)


print("\n== file_hint narrows, and a wrong hint does not blind the search ==")
located = executor.locate_value(FILES_BY_PULL[22], "btn-join", "join.html")
check("the hint narrows to one file", {line.path for line in located} == {"site/join.html"}, str(located))
located = executor.locate_value(FILES_BY_PULL[22], "#2ecc71", "nonexistent.scss")
check("a hint that matches nothing falls back to every file", len(located) == 1, str(located))


print("\n== an unmatched topic is a FAILED result, never a guess ==")
result = executor.execute(make_action(dict(BASE_PAYLOAD, pr_topic="the onboarding email copy")))
check("not ok", result.ok is False)
check("says no open PR matched", "no open PR matched" in result.human_summary, result.human_summary)
check("a failure that never sent anything is badged SIM", result.mode == results.MODE_SIM)
check("nothing to undo", result.undo_payload == {})

result = executor.execute(make_action(dict(BASE_PAYLOAD, pr_number=999)))
check("an explicit closed/absent PR number fails honestly", result.ok is False)
check("...and names the number", "#999" in result.human_summary, result.human_summary)


print("\n== an explicit pr_number wins over the topic ==")
result = executor.execute(
    make_action(dict(BASE_PAYLOAD, pr_number=22, pr_topic="something else entirely"))
)
check("explicit number is honoured", result.ok and "PR #22" in result.human_summary, result.human_summary)


print("\n== the repo allowlist refuses reads as well as writes ==")
raised = False
try:
    executor.assert_repo_is_allowed("torvalds/linux")
except PermissionError:
    raised = True
check("assert_repo_is_allowed refuses an unlisted repo", raised)
result = executor.execute(make_action(dict(BASE_PAYLOAD, repo="torvalds/linux")))
check("execute refuses it too", result.ok is False, result.human_summary)
check("...before any lookup", "refusing GitHub access" in result.human_summary, result.human_summary)
check(
    "the allowlist is the one in config",
    config_allowed := ("sharique2004/adjourn" in __import__("adjourn.config", fromlist=["x"]).ALLOWED_GITHUB_REPOS),
    str(config_allowed),
)


print("\n== undo refuses what it cannot reverse ==")
check(
    "an undo with no repo is False",
    executor.undo(
        results.ExecutorResult(
            ok=True, kind=executor.ACTION_KIND, external_id="1", url=None,
            human_summary="", mode=results.MODE_LIVE, undo_payload={},
        )
    )
    is False,
)
check(
    "an undo with no review id is False",
    executor.undo(
        results.ExecutorResult(
            ok=True, kind=executor.ACTION_KIND, external_id="1", url=None,
            human_summary="", mode=results.MODE_LIVE,
            undo_payload={"operation": "review", "repo": "sharique2004/adjourn"},
        )
    )
    is False,
)


print("\n== THE COMMIT PATH DOES NOT EXIST (inversion — it must never come back) ==")
# This section is the guard on brief §6 and the out-of-scope line "do not
# auto-commit from meetings". It asserts ABSENCE, because a test that exercises
# a commit path is a test that keeps one alive. The env var below is set to the
# demo prop's own branch at the top of this file: if anybody re-introduces the
# feature, these checks fail rather than quietly start passing on a new path.

import inspect  # noqa: E402

source = inspect.getsource(executor)

for symbol in (
    "branch_opts_into_autocommit",
    "NEVER_AUTOCOMMIT_BRANCHES",
    "COMMIT_MESSAGE_PREFIX",
    "apply_line_change",
    "build_commit_message",
    "build_commit_review_request",
    "build_committed_comment_body",
    "build_committed_review_body",
    "summarize_commit",
    "_commit_and_receipt",
    "_commit_file_live",
    "_read_file_at_ref_live",
    "_undo_commit_and_review",
):
    check(f"executor exposes no {symbol}", not hasattr(executor, symbol))

check("config exposes no pr_autocommit_branches",
      not hasattr(config, "pr_autocommit_branches"))
check("the executor never reads ADJOURN_PR_AUTOCOMMIT_BRANCHES",
      "ADJOURN_PR_AUTOCOMMIT_BRANCHES" not in source)
check("the executor names no contents endpoint (the one-file commit API)",
      "/contents/" not in source, "a contents path survived the deletion")
# The ONE surviving PUT is undo blanking a review body before deleting its
# comment — the reversal, not a write to anybody's code. Pin it by shape so a
# second PUT (to a file, to a branch) cannot slip in unremarked.
put_lines = [line.strip() for line in source.splitlines() if '"PUT"' in line]
check("exactly one PUT survives in the module", len(put_lines) == 1, str(put_lines))
check("...and it targets a review, not a file",
      put_lines and "/reviews/" in put_lines[0] and "contents" not in put_lines[0],
      str(put_lines))

# The demo prop PR #6 is on priya/join-button — the exact branch that used to be
# opted in. It must come back a SUGGESTION.
network_calls.clear()
result = executor.execute(make_action(dict(BASE_PAYLOAD)))
check("the prop branch's action still succeeds", result.ok, result.human_summary)
check("...badged SIM", result.mode == results.MODE_SIM, result.mode)
check("...and the summary SUGGESTS, never commits",
      result.human_summary.startswith("Suggested change on PR #22:"),
      result.human_summary)
check("...the summary contains no commit language",
      "ommitted" not in result.human_summary, result.human_summary)
rendered = result.undo_payload["payload"]
check("the rendered request is a REVIEW post, not a contents PUT",
      rendered["endpoint"].endswith("/reviews") and rendered["method"] == "POST",
      f"{rendered['method']} {rendered['endpoint']}")
check("nothing in the rendered request targets a branch",
      "branch" not in rendered, str(rendered.keys()))
body = rendered["body"]["comments"][0]["body"]
check("the inline comment carries a suggestion block", "```suggestion" in body)
check("...and states plainly that nothing was pushed",
      "nothing was pushed" in body, body)
check("the undo payload is a review undo, not a commit undo",
      result.undo_payload.get("kind", executor.ACTION_KIND) is not None
      and "commit_sha" not in str(result.undo_payload), str(result.undo_payload)[:200])


print("\n== the suggestion path still says nothing was pushed ==")
suggested = executor.execute(make_action(dict(BASE_PAYLOAD)))
suggestion_body = suggested.undo_payload["payload"]["body"]["comments"][0]["body"]
check("the suggestion block is back", "```suggestion" in suggestion_body)
check("...and it still says nothing was pushed", "nothing was pushed" in suggestion_body)
check("...with the precise privacy line, not the absolute one",
      "transcript never left this machine" not in suggestion_body
      and "the sentence quoted above is the only text that travelled" in suggestion_body,
      suggestion_body)


print("\n== no `gh` at all: placeholders, clearly marked, never fiction ==")
executor.resolution_reads_available = lambda: False
result = executor.execute(make_action(dict(BASE_PAYLOAD)))
executor.resolution_reads_available = lambda: True
check("still succeeds in sim", result.ok and result.mode == results.MODE_SIM)
rendered = result.undo_payload["payload"]
check("the anchor is admitted to be unknown", "unresolved" in rendered, str(rendered.keys()))
check("the placeholder is visibly a placeholder", "<line containing #2ecc71>" == rendered["body"]["comments"][0]["line"], str(rendered["body"]["comments"][0]["line"]))


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
