"""Seed the demo world inside Adjourn's own repository. Idempotent.

    python -m adjourn.tools.seed_demo_world --dry-run
    python -m adjourn.tools.seed_demo_world

WHAT IT BUILDS, and why it is the same repo the code lives in:

  * Four roadmap issues mirroring the Linear board's SHA-5 … SHA-8. The demo
    walks a meeting into a decision about one of them and Adjourn comments on
    the issue; having them mirror real Linear tickets is what lets the same
    sentence move a ticket AND annotate an issue without either being staged.
  * The two labels Adjourn applies: `from-meeting`, `decision-changed`.
  * One standing pull request — `priya/join-button` → the default branch —
    that puts a green "Join the beta" button on the landing page. The demo
    corrects its colour. It is scenery: never merged, and the only thing that
    changes about it between rehearsals is that Adjourn's review gets wiped off.

  The roadmap issues Adjourn writes on are Adjourn's roadmap. The pull request
  it reviews is a pull request against Adjourn's own landing page. That is not
  set dressing that happens to be convenient — it is the strongest available
  claim that the thing works, because every link in the demo goes somewhere a
  sceptic can read.

RUN ORDER. The repository is EMPTY until the orchestrator pushes the curated
tree. This script cannot create a branch off a repo with no commits, so:

    1. orchestrator pushes            (publish_tree prints the commands)
    2. this script, live
    3. rehearse; `scrub_demo_world --rehearsal` between takes

`--dry-run` works against the empty repo TODAY. It validates every anchor
against the local working tree — which is the tree that will be pushed — proves
the old hex appears nowhere yet, checks the labels and issues it would create,
and prints the patched files. If the dry run is clean, the live run has nothing
left to discover.

FIXTURE SYNC. The prop's pull request number is not knowable before it exists,
and three files have to agree about it or the beat silently mis-targets: the
spoken line in the transcript fixture, the ground truth beside it, and
PR_REVIEW_PROP.md. After creating the PR this script rewrites all three to the
number it actually got, including the spoken form ("pull five"), and says so.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .. import config
from .github_world import (
    PROP_BRANCH,
    PROP_BODY,
    PROP_NEW_HEX,
    PROP_OLD_HEX,
    PROP_TITLE,
    WorldError,
    default_branch,
    gh,
    gh_json,
    patched_files,
    prop_paths,
    read_file,
    repository_is_empty,
    target_repo,
    write_file,
)

FIXTURES_DIR = config.FIXTURES_DIR
BEAT_TRANSCRIPT = FIXTURES_DIR / "pr-review-beat.jsonl"
BEAT_GROUND_TRUTH = FIXTURES_DIR / "pr-review-beat.fixtures.json"
# The demo meeting, which now carries the same beat — one bare `--replay` has to
# show the inline suggestion alongside every other kind, so the number lives here
# too and this script keeps both copies honest.
DEMO_TRANSCRIPT = FIXTURES_DIR / "agi-living-room.jsonl"
DEMO_GROUND_TRUTH = FIXTURES_DIR / "agi-living-room.fixtures.json"
PROP_DOC = FIXTURES_DIR / "PR_REVIEW_PROP.md"

# --- the roadmap ------------------------------------------------------------
# Titles are the Linear board's, verbatim. The mirror is the point: an issue
# whose title has drifted from its ticket is a mirror of nothing.

# ORDER IS LOAD-BEARING. GitHub numbers issues in creation order, and two of
# these numbers are spoken out loud in the demo's own transcripts:
#
#   prior-standup  "I went through the numbers on ISSUE TWO and Redis is the
#                   right call"        -> #2 must be the cache layer
#   prior-standup  "ISSUE ONE is on track. The dev environment has been
#                   authenticating end to end"  -> #1 must be the auth migration
#
# Memory builds its topic -> issue index from those spoken numbers, not from
# GitHub, so a repo seeded in a different order does not break the wiring — it
# does something worse. Every card still fires, and every one of them lands on
# an issue whose title has nothing to do with it. A founder who clicks through
# reads a comment about the auth migration on a ticket about streaming ingestion.
#
# The auth migration is the one entry with no Linear mirror: it is finished work
# that the prior standup refers to, and the SHA board only carries live tickets.
ROADMAP: tuple[dict[str, str], ...] = (
    {
        "linear": "",
        "state": "",
        "title": "Auth migration: move service-to-service calls onto short-lived tokens",
        "body": "Replace the shared static credential between the ingestion "
                "service and the graph with per-service short-lived tokens.\n\n"
                "Running clean in dev. Kept open until it has soaked in "
                "production for a full week.",
    },
    {
        "linear": "SHA-6",
        "state": "Backlog",
        "title": "Cache layer for meeting graph queries",
        "body": "Multi-hop Cypher history queries recompute on every conflict check; "
                "add a per-meeting cache.\n\n"
                "The conflict check walks the same subgraph once per statement. For a "
                "long meeting that is the same traversal several dozen times, and it "
                "shows up as latency in the one place the product cannot afford it: "
                "between the recording stopping and the first card appearing.",
    },
    {
        "linear": "SHA-5",
        "state": "In Progress",
        "title": "Migrate transcript ingestion to the streaming adapter",
        "body": "Move batch ingestion to the incremental adapter so live captions "
                "and final transcripts share one code path.\n\n"
                "Today the live caption stream and the final transcript are read by "
                "two different code paths, which means every fix has to be made "
                "twice and the second one gets forgotten.",
    },
    {
        "linear": "SHA-7",
        "state": "Todo",
        "title": "Retry logic for executor failures",
        "body": "Failed executor calls currently drop; add bounded retry with jitter "
                "and dead-letter to the audit journal.\n\n"
                "A failed send is currently a red card and nothing else. The card is "
                "honest, but the commitment behind it is still real and nobody is "
                "holding it. Bounded retry, then a dead letter somebody can see.",
    },
    {
        "linear": "SHA-8",
        "state": "Backlog",
        "title": "Rate-limit the public board API",
        "body": "The read-only web surface polls unauthenticated; add per-IP limits.\n\n"
                "It reads a mirror and can neither fire an action nor undo one, so the "
                "blast radius is a bill rather than a breach — but an unauthenticated "
                "polling endpoint with no ceiling is a bill waiting to be run up.",
    },
)

ROADMAP_MARKER = "<!-- adjourn:roadmap -->"

LABELS: tuple[tuple[str, str, str], ...] = (
    ("from-meeting", "0E8A16", "Posted by Adjourn from a meeting"),
    ("decision-changed", "D93F0B", "A meeting changed a prior decision on this issue"),
    ("roadmap", "1D76DB", "Mirrors a ticket on the Linear board"),
)

NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
TENS_WORDS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
)


def spoken_number(value: int) -> str:
    """"22" -> "twenty-two". The transcript fixture is a record of speech.

    A transcript that reads "pull 22" is a transcript nobody spoke, and the
    quote in that file is printed verbatim on the board under a person's name.
    Bounded to 1..99 because a demo prop numbered in the hundreds means the repo
    has a history this script is not entitled to assume.
    """
    if not 1 <= value <= 99:
        raise WorldError(f"cannot spell {value} — expected a PR number between 1 and 99")
    if value < 20:
        return NUMBER_WORDS[value]
    tens, ones = divmod(value, 10)
    return TENS_WORDS[tens] + (f"-{NUMBER_WORDS[ones]}" if ones else "")


# --- issues -----------------------------------------------------------------


def existing_issues(repo: str) -> dict[str, dict]:
    """{title: issue record} for every issue in the repo, open or closed."""
    records = gh_json(
        "api", "--paginate", f"repos/{repo}/issues?state=all&per_page=100"
    ) or []
    assert isinstance(records, list)
    return {
        str(record.get("title", "")): record
        for record in records
        if isinstance(record, dict) and not record.get("pull_request")
    }


def ensure_labels(repo: str, *, dry_run: bool) -> list[str]:
    """Create or update Adjourn's labels. `--force` makes it re-runnable."""
    done = []
    for name, color, description in LABELS:
        if dry_run:
            done.append(f"would ensure label {name}")
            continue
        gh("label", "create", name, "--repo", repo, "--color", color,
           "--description", description, "--force")
        done.append(f"label {name}")
    return done


def issue_body(entry: dict[str, str]) -> str:
    """The body of one roadmap issue, marked so the scrub can recognise it.

    An entry with no `linear` key is a GitHub-only issue — work with no live
    ticket behind it. It still carries the marker so the scrub finds it; it just
    does not claim a mirror it does not have.
    """
    if entry["linear"]:
        footer = (
            f"Mirrors `{entry['linear']}` on the Linear board "
            f"(currently **{entry['state']}**). Adjourn keeps the two in step: a "
            f"decision in a meeting comments here and moves the ticket there."
        )
    else:
        footer = (
            "Tracked here only — this one has no ticket on the Linear board. "
            "Adjourn comments on it when a meeting says something about it."
        )
    return f"{entry['body']}\n\n---\n\n{ROADMAP_MARKER}\n{footer}\n"


def ensure_roadmap(repo: str, *, dry_run: bool) -> list[str]:
    """File the four roadmap issues, skipping any that already exist by title."""
    present = {} if dry_run else existing_issues(repo)
    done = []
    for entry in ROADMAP:
        title = entry["title"]
        if title in present:
            done.append(f"issue #{present[title]['number']} already there — {title}")
            continue
        if dry_run:
            done.append(f"would file — {title}  ({entry['linear']}, {entry['state']})")
            continue
        payload = {"title": title, "body": issue_body(entry), "labels": ["roadmap"]}
        record = gh_json("api", f"repos/{repo}/issues", "--input", "-",
                         stdin=json.dumps(payload))
        assert isinstance(record, dict)
        done.append(f"filed #{record['number']} — {title}")
    return done


# --- the prop pull request --------------------------------------------------


def find_prop_pull_request(repo: str) -> dict | None:
    """The open PR on the prop branch, if one is already standing."""
    records = gh_json("api", f"repos/{repo}/pulls?state=open&per_page=100") or []
    assert isinstance(records, list)
    for record in records:
        if isinstance(record, dict) and record.get("head", {}).get("ref") == PROP_BRANCH:
            return record
    return None


def local_sources() -> dict[str, str]:
    """The prop's target files, read from the working tree that will be pushed."""
    out = {}
    for path in prop_paths():
        local = config.REPO_ROOT / path
        if not local.exists():
            raise WorldError(f"{local} is missing — the prop patches a file that is not here")
        out[path] = local.read_text(encoding="utf-8")
    return out


def assert_old_hex_is_unique(patched: dict[str, str]) -> list[str]:
    """The old hex must appear exactly once in the pull request's own diff.

    The demo says "that green is wrong" and the executor goes looking for the
    green — inside the files the PR touches, which is the only place it looks.
    If the string occurs twice there, the executor is right to refuse to guess
    and the beat becomes a card explaining why it did nothing: correct, and not
    what anyone came to watch. So it is checked before the branch is built
    rather than discovered during it.

    Two checks, and the difference between them matters:

      * exactly one occurrence across the PR's own files — the load-bearing one;
      * zero occurrences anywhere else under `web/`, so no other stylesheet on
        the site can be mistaken for the button's colour by a human reading over
        somebody's shoulder.

    Occurrences in `adjourn/` are counted and reported but not refused. The
    executor, its tests and PR_REVIEW_PROP.md all name the hex on purpose: they
    are the record of what the diff contains, not the diff.
    """
    root = config.REPO_ROOT
    in_diff = sum(text.count(PROP_OLD_HEX) for text in patched.values())
    if in_diff != 1:
        offenders = {path: text.count(PROP_OLD_HEX) for path, text in patched.items()}
        raise WorldError(
            f"{PROP_OLD_HEX} appears {in_diff} times in the PR's own files ({offenders}); "
            "the demo needs exactly one so the correction has nothing to disambiguate"
        )

    skip_parts = {".git", "node_modules", ".next", "__pycache__", ".venv", "state"}
    elsewhere_in_web: list[str] = []
    elsewhere_in_package = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or set(path.parts) & skip_parts:
            continue
        if path.suffix.lower() not in {
            ".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".json",
            ".md", ".jsonl", ".txt", ".yml", ".yaml",
        }:
            continue
        relative = str(path.relative_to(root))
        if relative in patched or not relative.startswith(("web/", "adjourn/")):
            continue
        try:
            count = path.read_text(encoding="utf-8").count(PROP_OLD_HEX)
        except (OSError, UnicodeDecodeError):
            continue
        if not count:
            continue
        if relative.startswith("web/"):
            elsewhere_in_web += [relative] * count
        else:
            elsewhere_in_package += count

    if elsewhere_in_web:
        raise WorldError(
            f"{PROP_OLD_HEX} also appears in {sorted(set(elsewhere_in_web))} — "
            "the site must have exactly one green, and it must be the one in the PR"
        )
    return [
        f"{PROP_OLD_HEX} appears once in the PR's diff and nowhere else under web/",
        f"({elsewhere_in_package} mentions in adjourn/ — executor, tests and prop doc, "
        "which describe the diff rather than being it)",
    ]


def build_prop(repo: str, *, dry_run: bool) -> tuple[list[str], int | None]:
    """Create the branch, commit the patch, open the PR. Returns (log, pr_number)."""
    log: list[str] = []
    sources = local_sources()
    patched = patched_files(sources)
    log.append(f"anchors resolved in {len(prop_paths())} files")
    log += assert_old_hex_is_unique(patched)

    if dry_run:
        log.append(f"would create branch {PROP_BRANCH} and open “{PROP_TITLE}”")
        return log, None

    standing = find_prop_pull_request(repo)
    if standing:
        log.append(f"prop already standing — PR #{standing['number']}")
        return log, int(standing["number"])

    base = default_branch(repo)
    head_sha = gh_json("api", f"repos/{repo}/git/ref/heads/{base}")
    assert isinstance(head_sha, dict)
    sha = head_sha["object"]["sha"]

    try:
        gh("api", f"repos/{repo}/git/refs", "-f", "ref=refs/heads/" + PROP_BRANCH,
           "-f", f"sha={sha}")
        log.append(f"branch {PROP_BRANCH} created off {base}@{sha[:7]}")
    except WorldError as error:
        if "already exists" not in str(error).lower():
            raise
        log.append(f"branch {PROP_BRANCH} already existed")

    # Re-read from the branch rather than trusting the local copy: the pushed
    # tree is authoritative once it is pushed, and a compare-and-swap on the blob
    # sha means a file that changed underneath us fails loudly instead of being
    # silently reverted.
    remote_sources, shas = {}, {}
    for path in prop_paths():
        remote_sources[path], shas[path] = read_file(repo, path, PROP_BRANCH)
    remote_patched = patched_files(remote_sources)
    for path in prop_paths():
        if remote_patched[path] == remote_sources[path]:
            log.append(f"{path} already patched on the branch")
            continue
        write_file(repo, path, PROP_BRANCH, remote_patched[path], shas[path],
                   "Add join-the-beta button to the landing page")
        log.append(f"committed {path}")

    record = gh_json(
        "api", f"repos/{repo}/pulls", "--input", "-",
        stdin=json.dumps({"title": PROP_TITLE, "head": PROP_BRANCH,
                          "base": base, "body": PROP_BODY, "draft": False}),
    )
    assert isinstance(record, dict)
    number = int(record["number"])
    log.append(f"opened PR #{number} — {record.get('html_url')}")
    return log, number


# --- fixture sync -----------------------------------------------------------


def _rewrite_pr_number(text: str, repo: str, pr_number: int, quote: str) -> str:
    """Every spelling of the prop's number, inside one slice of one file. Pure."""
    # A lambda replacement, because the quote contains characters (backslashes,
    # in principle) that re.sub would read as group references in a template.
    text = re.sub(r'"quote": ".*?"',
                  lambda _m: f'"quote": {json.dumps(quote, ensure_ascii=False)}',
                  text, count=1)
    text = re.sub(r'"issue_number": \d+', f'"issue_number": {pr_number}', text)
    text = re.sub(r'"pr_number": \d+', f'"pr_number": {pr_number}', text)
    text = re.sub(r"PR #\d+", f"PR #{pr_number}", text)
    text = re.sub(r"https://github\.com/[\w\-]+/[\w\-]+/pull/\d+",
                  f"https://github.com/{repo}/pull/{pr_number}", text)
    text = re.sub(r"[\w\-]+/[\w\-]+#\d+", f"{repo}#{pr_number}", text)
    return text


def _statement_span(document: str, segment_id: str) -> tuple[int, int]:
    """(start, end) of the JSON object holding `segment_id`, by brace matching.

    NEEDED BECAUSE THE BEAT NOW LIVES IN TWO FIXTURES. pr-review-beat.fixtures
    .json holds exactly one statement, so a whole-file rewrite of every
    `"issue_number": N` was safe there. agi-living-room.fixtures.json holds
    twelve, and one of them is the Redis decision on issue #2 — a whole-file
    rewrite would renumber THAT to the pull request and point the demo's opening
    comment at a PR. So the rewrite is scoped to the one object.
    """
    marker = document.find(f'"segment_id": "{segment_id}"')
    if marker < 0:
        raise WorldError(f"could not find statement {segment_id!r} in the ground truth")
    start = document.rfind("{", 0, marker)
    if start < 0:
        raise WorldError(f"{segment_id!r} is not inside a JSON object")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(document)):
        character = document[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1
    raise WorldError(f"the object holding {segment_id!r} is not closed")


# The PR-review beat lives in TWO transcripts: its own, and the demo meeting it
# was merged into so one bare `--replay` shows every action kind. Both say "pull
# six" out loud and both have a ground truth whose `quote` must stay byte-identical
# to the spoken line — so both are rewritten from the same string, or they drift
# and the beat silently mis-targets in exactly one of them.
#
# `scope` says how much of the ground-truth file the number rewrite may touch:
#   "file"      — the file is about this beat and nothing else (pr-review-beat)
#   "statement" — rewrite only the one statement object (agi-living-room, whose
#                 other statements carry real issue numbers of their own)
BEAT_FIXTURES: tuple[tuple[Path, str, Path, str, str], ...] = (
    (BEAT_TRANSCRIPT, "prb-s05", BEAT_GROUND_TRUTH, "prb-s05", "file"),
    (DEMO_TRANSCRIPT, "agi-s32", DEMO_GROUND_TRUTH, "agi-s32", "statement"),
)


def sync_fixtures(repo: str, pr_number: int, *, dry_run: bool) -> list[str]:
    """Point every transcript, ground truth and the prop doc at the real PR.

    Five files, one number, and they all have to agree. The spoken form matters
    as much as the digits: the transcript segments are records of a sentence, and
    each ground truth's `quote` must stay byte-identical to its own, so both are
    rewritten from the same string.
    """
    spoken = spoken_number(pr_number)
    spoken_phrase = f"pull {spoken}"
    log: list[str] = []
    changes: list[tuple[Path, str, str]] = []

    for transcript_path, transcript_segment, truth_path, truth_segment, scope in BEAT_FIXTURES:
        # The transcript is JSONL. Parse it to find the spoken line rather than
        # pattern-matching raw bytes, then splice the NEW text in by replacing
        # the old text's JSON encoding — so the file keeps its exact formatting
        # and only the sentence changes.
        transcript = transcript_path.read_text(encoding="utf-8")
        old_text = new_text = ""
        for line in transcript.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("segment_id") == transcript_segment:
                old_text = str(row.get("text", ""))
                new_text = re.sub(r"pull [a-z\-]+(?= — that green)", spoken_phrase, old_text)
                break
        if not old_text:
            raise WorldError(
                f"could not find segment {transcript_segment} in {transcript_path.name}"
            )
        if spoken_phrase not in new_text:
            raise WorldError(
                f"{transcript_segment} does not contain a spoken pull-request number "
                f"to rewrite: {old_text!r}"
            )
        transcript_out = transcript.replace(
            json.dumps(old_text, ensure_ascii=False)[1:-1],
            json.dumps(new_text, ensure_ascii=False)[1:-1],
        )
        changes.append((transcript_path, transcript, transcript_out))

        ground_truth = truth_path.read_text(encoding="utf-8")
        if scope == "file":
            ground_truth_out = _rewrite_pr_number(ground_truth, repo, pr_number, new_text)
        else:
            start, end = _statement_span(ground_truth, truth_segment)
            ground_truth_out = (
                ground_truth[:start]
                + _rewrite_pr_number(ground_truth[start:end], repo, pr_number, new_text)
                + ground_truth[end:]
            )
        changes.append((truth_path, ground_truth, ground_truth_out))

    doc = PROP_DOC.read_text(encoding="utf-8")
    doc_out = doc
    doc_out = re.sub(r"https://github\.com/[\w\-]+/[\w\-]+/pull/\d+",
                     f"https://github.com/{repo}/pull/{pr_number}", doc_out)
    doc_out = re.sub(r"repos/[\w\-]+/[\w\-]+/(issues|pulls)/\d+",
                     lambda m: f"repos/{repo}/{m.group(1)}/{pr_number}", doc_out)
    doc_out = re.sub(r"--repo [\w\-]+/[\w\-]+", f"--repo {repo}", doc_out)
    doc_out = re.sub(r"PR #\d+", f"PR #{pr_number}", doc_out)
    doc_out = re.sub(r"pull twenty-two", spoken_phrase, doc_out)
    doc_out = re.sub(r'"pull [a-z\-]+"', f'"{spoken_phrase}"', doc_out)
    doc_out = re.sub(r"\bpr (view|edit|close) \d+", lambda m: f"pr {m.group(1)} {pr_number}",
                     doc_out)

    changes.append((PROP_DOC, doc, doc_out))
    for path, before, after in changes:
        if before == after:
            log.append(f"{path.name} already agrees")
            continue
        if dry_run:
            log.append(f"would rewrite {path.name} for PR #{pr_number} (“{spoken_phrase}”)")
            continue
        path.write_text(after, encoding="utf-8")
        log.append(f"rewrote {path.name} for PR #{pr_number} (“{spoken_phrase}”)")

    if not dry_run:
        for _t, _ts, truth_path, truth_segment, _scope in BEAT_FIXTURES:
            document = json.loads(truth_path.read_text(encoding="utf-8"))  # must still parse
            spoken_lines = {
                str(row.get("segment_id")): str(row.get("text", ""))
                for row in _read_jsonl_rows(_t)
            }
            for statement in document.get("statements", []):
                if statement.get("segment_id") != truth_segment:
                    continue
                if statement.get("quote") != spoken_lines.get(truth_segment):
                    raise WorldError(
                        f"{truth_path.name}: the {truth_segment} quote no longer matches "
                        f"the sentence in {_t.name}"
                    )
            log.append(f"{truth_path.name} parses, and its quote matches {_t.name}")
    return log


def _read_jsonl_rows(path: Path) -> list[dict]:
    """Every parseable JSON object in a .jsonl file. Malformed lines are skipped."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# --- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="validate everything, write nothing, anywhere")
    parser.add_argument("--skip-fixture-sync", action="store_true",
                        help="leave the fixtures alone even after opening the PR")
    args = parser.parse_args(argv)

    try:
        repo = target_repo()
    except WorldError as error:
        print(f"REFUSED: {error}")
        return 2

    print(f"target repo   {repo}")
    print(f"mode          {'DRY RUN — nothing will be written' if args.dry_run else 'LIVE'}")
    print()

    try:
        empty = repository_is_empty(repo)
    except WorldError as error:
        print(f"cannot read the repository: {error}")
        return 2
    print(f"repository is {'EMPTY — the tree has not been pushed yet' if empty else 'populated'}")
    if empty and not args.dry_run:
        print()
        print("REFUSED: there is nothing to branch from. Push the curated tree first —")
        print("  python -m adjourn.tools.publish_tree --out /tmp/adjourn-push")
        print("then run this again.")
        return 2
    print()

    try:
        for line in ensure_labels(repo, dry_run=args.dry_run):
            print(f"  labels    {line}")
        for line in ensure_roadmap(repo, dry_run=args.dry_run):
            print(f"  roadmap   {line}")
        prop_log, number = build_prop(repo, dry_run=args.dry_run)
        for line in prop_log:
            print(f"  prop      {line}")
        if args.dry_run:
            expected = len(ROADMAP) + 1
            print(f"  prop      the PR will be #{expected} on an empty repo "
                  f"(spoken: “pull {spoken_number(expected)}”)")
            for line in sync_fixtures(repo, expected, dry_run=True):
                print(f"  fixtures  {line}")
        elif number is not None and not args.skip_fixture_sync:
            for line in sync_fixtures(repo, number, dry_run=False):
                print(f"  fixtures  {line}")
    except WorldError as error:
        print()
        print(f"FAILED: {error}")
        return 1

    print()
    print(f"the correction the demo makes: {PROP_OLD_HEX} -> {PROP_NEW_HEX}")
    if args.dry_run:
        print("dry run clean. Nothing was written to GitHub or to disk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
