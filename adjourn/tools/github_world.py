"""Shared plumbing for the demo-world scripts: `gh`, guards, and the prop patch.

Both `seed_demo_world` and `scrub_demo_world` talk to exactly one repository —
`config.github_repo()`, which must be in `config.ALLOWED_GITHUB_REPOS`. The
allowlist is asserted here, once, at the only place either script can reach the
network from, so neither script has to be trusted to remember.

The patch that builds the prop pull request lives here too. It is expressed as
ANCHORED text insertions rather than as a stored copy of the finished files: the
landing page is somebody else's living code, and a script that overwrites two
files with a snapshot taken hours earlier will silently revert whatever changed
in between. An anchor that has moved is an error you can read; a clobbered file
is one you find out about on stage.
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass

from .. import config

GITHUB_CLI_TIMEOUT_SECONDS = 60

# The one line the demo corrects, and the line it corrects it to. The old hex
# exists ONLY here and in the branch this script builds — it is never spoken in
# the transcript, which is the whole point of the beat (see PR_REVIEW_PROP.md).
PROP_OLD_HEX = "#2ecc71"
PROP_NEW_HEX = "#6b7f99"
PROP_BRANCH = "priya/join-button"
PROP_TITLE = "[demo-prop] Add join button to landing page"


class WorldError(RuntimeError):
    """Something about the repository is not what this script requires."""


# --- guards -----------------------------------------------------------------


def target_repo() -> str:
    """The one repo these scripts may touch. Raises if it is not allowlisted."""
    repo = config.github_repo()
    if repo not in config.ALLOWED_GITHUB_REPOS:
        raise WorldError(
            f"refusing to operate on {repo!r}; "
            f"allowed: {sorted(config.ALLOWED_GITHUB_REPOS)}"
        )
    return repo


# --- gh transport -----------------------------------------------------------


def gh(*args: str, stdin: str | None = None) -> str:
    """Run `gh <args>` and return stdout. Raises WorldError with stderr on failure.

    The child gets `config.child_process_environment()` rather than this
    process's environment: `gh` reads its own credentials out of the keychain
    under HOME and needs nothing else, and a credential that is not in the
    environment cannot be read out of it.
    """
    try:
        completed = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            input=stdin,
            timeout=GITHUB_CLI_TIMEOUT_SECONDS,
            env=config.child_process_environment(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorldError(f"gh could not be run: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise WorldError(f"gh {' '.join(args[:3])} failed: {detail}")
    return completed.stdout


def gh_json(*args: str, stdin: str | None = None) -> object:
    """`gh` with a JSON answer, parsed. Empty output becomes None."""
    output = gh(*args, stdin=stdin).strip()
    return json.loads(output) if output else None


def is_missing(error: Exception) -> bool:
    """True when a `gh` failure was only "that does not exist"."""
    text = str(error).lower()
    return "404" in text or "not found" in text or "no commit found" in text


# --- repository reads -------------------------------------------------------


def repository_is_empty(repo: str) -> bool:
    """True when the repo has no commits yet — nothing to branch from.

    DO NOT ask `repos/{repo}` for `default_branch` and believe a falsy answer.
    GitHub reports the CONFIGURED default branch name ("main") on a repository
    that has never received a commit, so that field is truthy for exactly the
    case this function exists to catch. Measured on sharique2004/adjourn before
    the push: `{"size": 0, "default_branch": "main"}`.

    The honest signal is whether any branch ref exists. On an empty repository
    the refs endpoint answers 409 "Git Repository is empty", so a WorldError
    here is the answer rather than a failure.
    """
    try:
        refs = gh_json("api", f"repos/{repo}/git/refs/heads")
    except WorldError:
        return True
    return not refs


def default_branch(repo: str) -> str:
    """The repo's default branch name, or "main" when it has none yet."""
    record = gh_json("api", f"repos/{repo}")
    assert isinstance(record, dict)
    return str(record.get("default_branch") or "main")


def read_file(repo: str, path: str, ref: str) -> tuple[str, str]:
    """(text, blob_sha) for one file on one ref. Raises WorldError when absent."""
    try:
        record = gh_json("api", f"repos/{repo}/contents/{path}?ref={ref}")
    except WorldError as error:
        raise WorldError(f"{path} does not exist on {ref}: {error}") from error
    assert isinstance(record, dict)
    content = base64.b64decode(record.get("content", "")).decode("utf-8")
    return content, str(record.get("sha", ""))


def write_file(
    repo: str, path: str, ref: str, text: str, blob_sha: str, message: str
) -> None:
    """Commit one file onto `ref`. The blob sha makes this a compare-and-swap."""
    body = {
        "message": message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": ref,
        "sha": blob_sha,
    }
    gh("api", "-X", "PUT", f"repos/{repo}/contents/{path}", "--input", "-",
       stdin=json.dumps(body))


# --- the prop patch ---------------------------------------------------------


@dataclass(frozen=True)
class Patch:
    """One anchored insertion into one file."""

    path: str
    anchor: str
    insertion: str
    where: str = "after"   # "after" the anchor, or "end" of the file

    def apply(self, text: str) -> str:
        """Return `text` with the insertion made. Raises when the anchor is wrong.

        An anchor that appears twice is as bad as one that appears never: the
        script would be guessing which occurrence the author meant, and a demo
        prop built on a guess is a demo prop that breaks the week you forget.
        """
        if self.insertion in text:
            return text  # already applied; these scripts are re-runnable
        if self.where == "end":
            return text.rstrip("\n") + "\n" + self.insertion
        found = text.count(self.anchor)
        if found != 1:
            raise WorldError(
                f"{self.path}: anchor {self.anchor.strip()!r} appears {found} times, "
                "expected exactly 1 — the file has moved on and this patch needs rewriting"
            )
        head, _, tail = text.partition(self.anchor)
        return head + self.anchor + self.insertion + tail


# The green goes in as a single declaration on its own line so the correction is
# a one-line diff with nothing to disambiguate. Every surface that uses it goes
# through `var(--join-accent)`, so changing that one line recolours the whole CTA.
#
# The ink token deliberately reuses the page's existing `--bg` rather than
# naming a second colour. Two hex literals in one diff is one hex literal too
# many: the review executor derives the value it is replacing by finding the
# only colour the PR adds (pr_review_suggestion_executor.derive_old_value), and
# a second one would make it refuse — correctly, and uselessly. A prop that
# introduces exactly one new colour is also a better-behaved patch on its own
# terms, which is why this is the fix rather than loosening the executor.
CSS_TOKEN_INSERTION = """
  /* Beta CTA accent. Green so the signup reads as a "go" rather than another
     amber link — happy to be overruled on that. */
  --join-accent: %s;
  --join-accent-ink: var(--bg);
""" % PROP_OLD_HEX

CSS_RULE_INSERTION = """
/* --- beta CTA ------------------------------------------------------------- */

.btn-join {
  border-color: var(--join-accent);
  background: var(--join-accent);
  color: var(--join-accent-ink);
  font-weight: 600;
}

.btn-join:hover {
  border-color: var(--join-accent);
  filter: brightness(1.08);
}

.btn-join:focus-visible {
  outline-color: var(--join-accent);
}
"""

PAGE_INSERTION = """            <a
              className="btn btn-join"
              href="https://github.com/sharique2004/adjourn"
              target="_blank"
              rel="noreferrer"
            >
              Join the beta
            </a>
"""

PROP_PATCHES: tuple[Patch, ...] = (
    Patch(
        path="web/app/globals.css",
        anchor="  --amber: #e3b341;\n",
        insertion=CSS_TOKEN_INSERTION,
    ),
    Patch(
        path="web/app/globals.css",
        anchor="",
        insertion=CSS_RULE_INSERTION,
        where="end",
    ),
    Patch(
        path="web/app/page.tsx",
        anchor='            <Link className="btn btn-quiet" href="/ledger">\n'
               "              Commitment ledger\n"
               "            </Link>\n",
        insertion=PAGE_INSERTION,
    ),
)

PROP_BODY = """Adds the beta signup CTA we talked about — a `Join the beta` button on the landing hero, plus the accent token it paints itself with.

Small and self-contained on purpose. The button links out for now; wiring it to a real signup endpoint is a follow-up and I'd rather land the surface than block on backend work.

A couple of notes:

- The accent is the one thing here that isn't from the existing palette. I picked a green for the button because I wanted the signup to feel like a "go" rather than another quiet link. It's a single token — `--join-accent` in `web/app/globals.css` — so it's one line to change if you disagree. Happy to be overruled on that.
- Everything else routes through `var(--join-accent)`, so the hover, the focus ring and the fill all move together.

No routing or nav changes. The button is on the landing page and nowhere else yet.
"""


def patched_files(source: dict[str, str]) -> dict[str, str]:
    """Apply every prop patch to `{path: text}`. Raises on a moved anchor."""
    out = dict(source)
    for patch in PROP_PATCHES:
        if patch.path not in out:
            raise WorldError(f"{patch.path} was not supplied to the patcher")
        out[patch.path] = patch.apply(out[patch.path])
    return out


def prop_paths() -> tuple[str, ...]:
    """Every file the prop patch touches, deduplicated, in a stable order."""
    seen: list[str] = []
    for patch in PROP_PATCHES:
        if patch.path not in seen:
            seen.append(patch.path)
    return tuple(seen)
