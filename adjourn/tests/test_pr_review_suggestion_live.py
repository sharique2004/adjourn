"""Lane 2 LIVE gate — pr_review_suggestion against sharique2004/adjourn, for real.

    ADJOURN_LIVE_TEST=1 python -m adjourn.tests.test_pr_review_suggestion_live

Gated behind ADJOURN_LIVE_TEST on purpose: without it this file prints why it
declined and exits 0, so a test sweep never posts a review at somebody.

It targets the open PR whose title starts with "[demo-prop]" — the prop PR the
demo is built around. If none is open it creates its own throwaway PR titled
"[adjourn-test] pr-review target", uses that, and closes it and deletes its
branch on the way out. Either way it removes EVERY review and comment it posted
before it exits, including on the failure paths, and it never touches anything
it did not create.

    ONLY sharique2004/adjourn. The executor's allowlist assertion is exercised
    here too, and it is the same frozenset in config that guards the demo.

What it proves against the real API:
  * the exact-match path posts a review whose inline comment lands on the right
    file and line and carries a working ```suggestion block
  * undo removes that review COMPLETELY — a follow-up GET returns 404
  * the no-match path posts a plain PR comment instead of guessing
  * undo removes that comment too
  * the repo allowlist refuses a repo outside the frozenset
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

os.environ["ADJOURN_SIM"] = "0"  # this file is the live one; be explicit

from adjourn import config, results  # noqa: E402
from adjourn.executors import pr_review_suggestion_executor as executor  # noqa: E402
from adjourn.executors.github_update_executor import run_github_cli  # noqa: E402
from adjourn.planner import Action  # noqa: E402

REPO = "sharique2004/adjourn"
PROP_PREFIX = "[demo-prop]"
FALLBACK_TITLE = "[adjourn-test] pr-review target"
FALLBACK_BRANCH = "adjourn-test/pr-review-target"
FALLBACK_PATH = ".adjourn-test/pr-review-target.css"
POLL_ATTEMPTS = 20
POLL_SECONDS = 30

passed = 0
failed = 0
created_pull: int | None = None
created_branch: str | None = None


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def make_action(payload: dict, quote: str) -> Action:
    return Action(
        kind=executor.ACTION_KIND,
        payload=dict(payload, repo=REPO),
        dedup_key="pr_review_suggestion:live-test",
        quote=quote,
        speaker="Priya",
        meeting_id="live-test",
        segment_id="L1",
    )


def find_prop_pull_request() -> dict | None:
    """The open [demo-prop] PR, polled for up to ten minutes."""
    for attempt in range(1, POLL_ATTEMPTS + 1):
        listed = json.loads(run_github_cli("api", f"repos/{REPO}/pulls?state=open&per_page=100"))
        for pull in listed:
            if str(pull.get("title", "")).startswith(PROP_PREFIX):
                return pull
        if attempt == POLL_ATTEMPTS:
            return None
        print(f"  .. no {PROP_PREFIX} PR yet (attempt {attempt}/{POLL_ATTEMPTS}); waiting {POLL_SECONDS}s")
        time.sleep(POLL_SECONDS)
    return None


def create_fallback_pull_request() -> dict:
    """Our own throwaway target, torn down in cleanup(). Only used if Lane 3's is absent."""
    global created_pull, created_branch
    base_sha = json.loads(run_github_cli("api", f"repos/{REPO}/git/ref/heads/main"))["object"]["sha"]
    try:
        run_github_cli(
            "api", f"repos/{REPO}/git/refs",
            "-f", f"ref=refs/heads/{FALLBACK_BRANCH}", "-f", f"sha={base_sha}",
        )
    except RuntimeError as error:
        if "already exists" not in str(error).lower():
            raise
    created_branch = FALLBACK_BRANCH
    content = (
        "/* adjourn live-test fixture — safe to delete */\n"
        ".join-button {\n"
        "  background: #2ecc71;\n"
        "}\n"
    )
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    try:
        run_github_cli(
            "api", "-X", "PUT", f"repos/{REPO}/contents/{FALLBACK_PATH}",
            "-f", "message=adjourn live test fixture",
            "-f", f"content={encoded}", "-f", f"branch={FALLBACK_BRANCH}",
        )
    except RuntimeError as error:
        if "sha" not in str(error).lower():
            raise
    created = json.loads(
        run_github_cli(
            "api", f"repos/{REPO}/pulls",
            "-f", f"title={FALLBACK_TITLE}", "-f", f"head={FALLBACK_BRANCH}",
            "-f", "base=main", "-F", "draft=true",
            "-f", "body=Throwaway target for the Adjourn pr_review_suggestion live test.",
        )
    )
    created_pull = int(created["number"])
    return created


def cleanup() -> None:
    """Close and delete ONLY what this file created. Never touches Lane 3's PR."""
    if created_pull:
        print(f"\n== cleanup: closing our own PR #{created_pull} ==")
        try:
            run_github_cli("api", "-X", "PATCH", f"repos/{REPO}/pulls/{created_pull}", "-f", "state=closed")
        except Exception as error:  # noqa: BLE001
            print(f"  .. could not close #{created_pull}: {error}")
    if created_branch:
        try:
            run_github_cli("api", "-X", "DELETE", f"repos/{REPO}/git/refs/heads/{created_branch}")
            print(f"  .. deleted branch {created_branch}")
        except Exception as error:  # noqa: BLE001
            print(f"  .. could not delete {created_branch}: {error}")


def residue_on(pull_number: int) -> tuple[int, int]:
    """(reviews, adjourn-authored conversation comments) still on the PR."""
    reviews = json.loads(run_github_cli("api", f"repos/{REPO}/pulls/{pull_number}/reviews"))
    comments = json.loads(run_github_cli("api", f"repos/{REPO}/issues/{pull_number}/comments"))
    mine = [c for c in comments if "Adjourn" in str(c.get("body", ""))]
    return len(reviews), len(mine)


def main() -> int:
    if os.environ.get("ADJOURN_LIVE_TEST") != "1":
        print("declined: set ADJOURN_LIVE_TEST=1 to run the live GitHub test")
        return 0

    print(f"== target: an open PR on {REPO} ==")
    pull = find_prop_pull_request()
    if pull is None:
        print(f"  .. no {PROP_PREFIX} PR appeared; creating our own throwaway target")
        pull = create_fallback_pull_request()
    pull_number = int(pull["number"])
    print(f"  using #{pull_number} “{pull['title']}” ({pull['html_url']})")

    before_reviews, before_comments = residue_on(pull_number)
    print(f"  residue before: {before_reviews} review(s), {before_comments} Adjourn comment(s)")

    # Find a value that really is in this PR's diff exactly once, so the test
    # asserts against the actual prop rather than against an assumption.
    files = executor._list_pull_request_files_live(REPO, pull_number)
    candidates = ["#2ecc71", "--join-accent", "#0b0d10"]
    old_value = ""
    located_expectation = None
    for candidate in candidates:
        found = executor.locate_value(files, candidate)
        if len(found) == 1:
            old_value, located_expectation = candidate, found[0]
            break
    if not old_value:
        print("  !! no single-occurrence value found in this PR's diff; cannot test the exact path")
        cleanup()
        return 1
    print(f"  will suggest on {old_value!r} at {located_expectation.path}:{located_expectation.line}")

    quote = f"That join button green is way too loud — take {old_value} to #6b7f99 before the demo."

    print("\n== path 1: exact match posts a real review with a suggestion ==")
    result = executor.execute(
        make_action(
            {
                "pr_number": pull_number,
                "pr_topic": "join button",
                "change_description": "join button green",
                "old_value": old_value,
                "new_value": "#6b7f99",
                "file_hint": located_expectation.path.split("/")[-1],
                "meeting_title": "Eng sync",
                "meeting_date": "2026-08-21",
                "timestamp": "12:03",
            },
            quote,
        )
    )
    check("the review posted", result.ok, result.human_summary)
    check("badged LIVE", result.mode == results.MODE_LIVE, result.mode)
    check("carries a review id", bool(result.external_id), str(result.external_id))
    check("carries a clickable url", bool(result.url), str(result.url))
    check(
        "the summary names the PR and the change",
        f"PR #{pull_number}" in result.human_summary and "#6b7f99" in result.human_summary,
        result.human_summary,
    )
    print(f"  review: {result.url}")

    review_id = result.external_id
    comment_ids = result.undo_payload.get("comment_ids") or []
    check("undo_payload carries the review id", result.undo_payload.get("review_id") == review_id)
    check("undo_payload carries the comment id(s)", len(comment_ids) == 1, str(comment_ids))

    if result.ok and comment_ids:
        posted = json.loads(run_github_cli("api", f"repos/{REPO}/pulls/comments/{comment_ids[0]}"))
        check(
            "the inline comment landed on the expected file",
            posted.get("path") == located_expectation.path,
            f"{posted.get('path')} != {located_expectation.path}",
        )
        check(
            "the inline comment landed on the expected line",
            posted.get("line") == located_expectation.line,
            f"{posted.get('line')} != {located_expectation.line}",
        )
        check("GitHub kept it on the RIGHT side", posted.get("side") == "RIGHT", str(posted.get("side")))
        body = str(posted.get("body", ""))
        check("the suggestion block survived the round trip", "```suggestion" in body)
        suggestion = body.split("```suggestion", 1)[1].split("```", 1)[0]
        check("the suggestion carries the new value", "#6b7f99" in suggestion, suggestion)
        check("the suggestion no longer carries the old value", old_value not in suggestion, suggestion)
        check(
            "the suggestion is the WHOLE line, not a fragment",
            suggestion.strip("\n") == located_expectation.content.replace(old_value, "#6b7f99"),
            repr(suggestion),
        )
        check("the verbatim quote is on the PR", "too loud" in body)

    print("\n== undo removes the review COMPLETELY ==")
    check("undo reports success", executor.undo(result) is True)
    gone = executor.review_is_gone(REPO, pull_number, str(review_id))
    check("a follow-up GET on the review 404s", gone)

    print("\n== path 1b: the same thing again, resolved by TOPIC and not by number ==")
    result1b = executor.execute(
        make_action(
            {
                "pr_topic": "join button",
                "change_description": "join button green",
                "old_value": old_value,
                "new_value": "#6b7f99",
                "meeting_title": "Eng sync",
                "meeting_date": "2026-08-21",
            },
            quote,
        )
    )
    check("a spoken topic alone found the PR", result1b.ok, result1b.human_summary)
    check(
        "...and found the RIGHT PR",
        result1b.undo_payload.get("pull_number") == pull_number,
        f"{result1b.undo_payload.get('pull_number')} != {pull_number}",
    )
    check(
        "...anchored to the same line as the explicit run",
        result1b.undo_payload.get("line") == located_expectation.line,
        str(result1b.undo_payload.get("line")),
    )
    check("undo removes it", executor.undo(result1b) is True)
    check(
        "and it is gone",
        executor.review_is_gone(REPO, pull_number, str(result1b.external_id)),
    )

    print("\n== a topic that matches nothing open is a failure, not a guess ==")
    missed = executor.execute(
        make_action(
            {
                "pr_topic": "the onboarding email drip campaign",
                "change_description": "subject line",
                "old_value": "#2ecc71",
                "new_value": "#6b7f99",
            },
            "Change the subject line on the drip campaign.",
        )
    )
    check("not ok", missed.ok is False, missed.human_summary)
    check("says no open PR matched", "no open PR matched" in missed.human_summary, missed.human_summary)
    check("nothing was sent, so it is badged SIM", missed.mode == results.MODE_SIM, missed.mode)

    print("\n== path 2: no match posts a plain comment instead of guessing ==")
    result2 = executor.execute(
        make_action(
            {
                "pr_number": pull_number,
                "pr_topic": "join button",
                "change_description": "the disabled state",
                "old_value": "#deadbe",
                "new_value": "#6b7f99",
                "meeting_title": "Eng sync",
                "meeting_date": "2026-08-21",
            },
            "Also fix the disabled state, it's still #deadbe somewhere in there.",
        )
    )
    check("the comment posted", result2.ok, result2.human_summary)
    check("badged LIVE", result2.mode == results.MODE_LIVE, result2.mode)
    check(
        "it is an issue comment, not a review",
        result2.undo_payload.get("operation") == "issue_comment",
        str(result2.undo_payload.get("operation")),
    )
    check(
        "the summary admits there was no line to suggest on",
        "no single line" in result2.human_summary,
        result2.human_summary,
    )
    if result2.ok:
        posted = json.loads(
            run_github_cli("api", f"repos/{REPO}/issues/comments/{result2.undo_payload['comment_id']}")
        )
        body = str(posted.get("body", ""))
        check("no suggestion block was offered", "```suggestion" not in body)
        check("the reason is stated on the PR", "does not appear anywhere" in body, body[:200])
        check("the quote still rides along", "disabled state" in body)
        print(f"  comment: {posted.get('html_url')}")

    print("\n== undo removes the comment too ==")
    check("undo reports success", executor.undo(result2) is True)
    still_there = True
    try:
        run_github_cli("api", f"repos/{REPO}/issues/comments/{result2.undo_payload['comment_id']}")
    except Exception:  # noqa: BLE001
        still_there = False
    check("a follow-up GET on the comment 404s", not still_there)

    print("\n== the repo allowlist refuses anything but the demo repo ==")
    refused = executor.execute(
        Action(
            kind=executor.ACTION_KIND,
            payload={"repo": "torvalds/linux", "pr_topic": "join button", "old_value": "#2ecc71"},
            quote=quote, speaker="Priya", meeting_id="live-test",
        )
    )
    check("refused", refused.ok is False, refused.human_summary)
    check("said why", "refusing GitHub access" in refused.human_summary, refused.human_summary)
    check("torvalds/linux is not in the allowlist", "torvalds/linux" not in config.ALLOWED_GITHUB_REPOS)

    print("\n== the PR is left exactly as we found it ==")
    after_reviews, after_comments = residue_on(pull_number)
    check(
        "no review residue",
        after_reviews == before_reviews,
        f"{before_reviews} -> {after_reviews}",
    )
    check(
        "no comment residue",
        after_comments == before_comments,
        f"{before_comments} -> {after_comments}",
    )
    return 0


try:
    exit_code = main()
finally:
    cleanup()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else exit_code)
