"""Take Adjourn's writing back off Adjourn's repository. Idempotent.

    python -m adjourn.tools.scrub_demo_world --rehearsal --dry-run
    python -m adjourn.tools.scrub_demo_world --rehearsal
    python -m adjourn.tools.scrub_demo_world --teardown

TWO SCOPES, and the difference is what you want the repo to look like afterwards.

  --rehearsal (the default)  Removes everything Adjourn WROTE — issue comments,
      inline review comments, the `from-meeting` and `decision-changed` labels,
      and any plan block it stitched into an issue body. Leaves the world
      standing: the four roadmap issues and the standing prop PR are scenery,
      not output, and rebuilding them between takes wastes a minute and changes
      the pull request number. This is the between-takes reset.

  --teardown  Everything above, then closes the prop pull request, deletes its
      branch, and closes the roadmap issues. Use it when the demo is over, or
      before re-seeding from scratch. It CLOSES rather than deletes: closing is
      reversible and deletion is not, and a repository whose history has been
      edited is a repository nobody can audit.

WHAT IT WILL NOT DO. A submitted pull-request review cannot be deleted through
the GitHub API at all (`DELETE /pulls/{n}/reviews/{id}` is legal only while the
review is still PENDING). The one legal retraction — blank the body while an
inline comment is still attached, then delete the comments, and let GitHub
garbage-collect the empty husk — is order-dependent and belongs to the executor
that created the review, which does it in `undo()` and verifies with a follow-up
GET. This script reports any review it finds and cannot remove rather than
pretending the PR is clean. See PR_REVIEW_PROP.md.

SAFETY. Every call goes through `github_world.target_repo()`, which refuses any
repository outside `config.ALLOWED_GITHUB_REPOS`. There is no `--repo` flag, on
purpose: a script whose job is deletion should not take the target as an
argument.
"""

from __future__ import annotations

import argparse
import sys

from .github_world import (
    PROP_BRANCH,
    WorldError,
    gh,
    gh_json,
    is_missing,
    target_repo,
)

ADJOURN_LABELS = ("from-meeting", "decision-changed")
PLAN_BLOCK_OPEN = "<!-- adjourn:plan -->"
PLAN_BLOCK_CLOSE = "<!-- /adjourn:plan -->"


def _all_issues_and_pulls(repo: str) -> list[dict]:
    """Every issue and pull request, open or closed. One paginated read."""
    records = gh_json(
        "api", "--paginate", f"repos/{repo}/issues?state=all&per_page=100"
    ) or []
    assert isinstance(records, list)
    return [record for record in records if isinstance(record, dict)]


def _delete(repo: str, endpoint: str, *, dry_run: bool) -> bool:
    """DELETE one thing. A 404 counts as success — the end state is what matters."""
    if dry_run:
        return True
    try:
        gh("api", "-X", "DELETE", f"repos/{repo}/{endpoint}")
        return True
    except WorldError as error:
        if is_missing(error):
            return True
        print(f"    could not delete {endpoint}: {error}")
        return False


def scrub_comments(repo: str, records: list[dict], *, dry_run: bool) -> list[str]:
    """Delete every issue comment and inline review comment in the repo."""
    log: list[str] = []
    for record in records:
        number = record["number"]
        comments = gh_json("api", f"repos/{repo}/issues/{number}/comments") or []
        assert isinstance(comments, list)
        for comment in comments:
            if _delete(repo, f"issues/comments/{comment['id']}", dry_run=dry_run):
                log.append(f"{'would delete' if dry_run else 'deleted'} comment on "
                           f"#{number} ({comment.get('user', {}).get('login')})")
        if not record.get("pull_request"):
            continue
        review_comments = gh_json("api", f"repos/{repo}/pulls/{number}/comments") or []
        assert isinstance(review_comments, list)
        for comment in review_comments:
            if _delete(repo, f"pulls/comments/{comment['id']}", dry_run=dry_run):
                log.append(f"{'would delete' if dry_run else 'deleted'} inline comment "
                           f"on #{number} ({comment.get('path')})")
    return log


def report_reviews(repo: str, records: list[dict]) -> list[str]:
    """Name any submitted review still on a PR. This script cannot remove them."""
    log: list[str] = []
    for record in records:
        if not record.get("pull_request"):
            continue
        number = record["number"]
        reviews = gh_json("api", f"repos/{repo}/pulls/{number}/reviews") or []
        assert isinstance(reviews, list)
        for review in reviews:
            log.append(
                f"STUCK: review {review['id']} ({review.get('state')}) on #{number} "
                "— GitHub does not allow a submitted review to be deleted; "
                "only the executor's own undo() can retract one, and only in order"
            )
    return log


def scrub_labels(repo: str, records: list[dict], *, dry_run: bool) -> list[str]:
    """Remove the two labels Adjourn applies. Leaves `roadmap` alone — it is scenery."""
    log: list[str] = []
    for record in records:
        present = {label["name"] for label in record.get("labels", [])}
        for label in ADJOURN_LABELS:
            if label not in present:
                continue
            if _delete(repo, f"issues/{record['number']}/labels/{label}", dry_run=dry_run):
                log.append(f"{'would strip' if dry_run else 'stripped'} "
                           f"{label} from #{record['number']}")
    return log


def scrub_plan_blocks(repo: str, records: list[dict], *, dry_run: bool) -> list[str]:
    """Cut any adjourn:plan region back out of an issue body.

    The executor restores the previous body from its own undo payload, which is
    the correct path. This is the sweep for a body whose undo record is gone —
    after a journal reset, say. It removes the marked region and nothing else,
    so a body a human edited around the block survives.
    """
    log: list[str] = []
    for record in records:
        body = record.get("body") or ""
        if PLAN_BLOCK_OPEN not in body:
            continue
        head, _, rest = body.partition(PLAN_BLOCK_OPEN)
        _, _, tail = rest.partition(PLAN_BLOCK_CLOSE)
        cleaned = (head.rstrip() + "\n" + tail.lstrip()).strip() + "\n"
        if dry_run:
            log.append(f"would cut the plan block out of #{record['number']}")
            continue
        gh("api", "-X", "PATCH", f"repos/{repo}/issues/{record['number']}",
           "-F", "body=@-", stdin=cleaned)
        log.append(f"cut the plan block out of #{record['number']}")
    return log


def teardown(repo: str, records: list[dict], *, dry_run: bool) -> list[str]:
    """Close the prop, delete its branch, close the roadmap issues."""
    log: list[str] = []
    for record in records:
        if record.get("state") != "open":
            continue
        number = record["number"]
        kind = "PR" if record.get("pull_request") else "issue"
        if dry_run:
            log.append(f"would close {kind} #{number} — {record.get('title')}")
            continue
        gh("api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=closed")
        log.append(f"closed {kind} #{number} — {record.get('title')}")

    if dry_run:
        log.append(f"would delete branch {PROP_BRANCH}")
        return log
    try:
        gh("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{PROP_BRANCH}")
        log.append(f"deleted branch {PROP_BRANCH}")
    except WorldError as error:
        log.append(
            f"branch {PROP_BRANCH} already gone"
            if is_missing(error) else f"could not delete {PROP_BRANCH}: {error}"
        )
    return log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--rehearsal", action="store_true",
                       help="remove what Adjourn wrote; leave the world standing (default)")
    scope.add_argument("--teardown", action="store_true",
                       help="also close the prop and the roadmap issues, and delete the branch")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be removed, remove nothing")
    args = parser.parse_args(argv)

    try:
        repo = target_repo()
    except WorldError as error:
        print(f"REFUSED: {error}")
        return 2

    scope_name = "TEARDOWN" if args.teardown else "REHEARSAL"
    print(f"target repo   {repo}")
    print(f"scope         {scope_name}"
          f"{'  (dry run — nothing will be removed)' if args.dry_run else ''}")
    print()

    try:
        records = _all_issues_and_pulls(repo)
        print(f"found {len(records)} issues and pull requests")
        for line in scrub_comments(repo, records, dry_run=args.dry_run):
            print(f"  comments  {line}")
        for line in scrub_labels(repo, records, dry_run=args.dry_run):
            print(f"  labels    {line}")
        for line in scrub_plan_blocks(repo, records, dry_run=args.dry_run):
            print(f"  bodies    {line}")
        for line in report_reviews(repo, records):
            print(f"  reviews   {line}")
        if args.teardown:
            for line in teardown(repo, records, dry_run=args.dry_run):
                print(f"  teardown  {line}")
    except WorldError as error:
        print()
        print(f"FAILED: {error}")
        return 1

    print()
    print("clean." if not args.dry_run else "dry run complete — nothing was removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
