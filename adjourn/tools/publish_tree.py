"""Build the clean tree that goes to github.com/sharique2004/adjourn.

    python -m adjourn.tools.publish_tree --out /tmp/adjourn-push

It copies rather than filters in place, and it copies by ALLOWLIST. A .gitignore
is the wrong last line of defence for a public push: it does not cover a file
that is already tracked, `git add -f` walks straight past it, and it fails silent
— the file is simply there and nobody notices until someone else does. So this
script starts from an empty directory and states, file by file, what is allowed
to exist in it. Anything not named does not travel.

Then it reads every byte of the result back and looks for the live credentials
this machine actually holds — not a generic regex for "something that looks like
a token", but the real Slack, Linear, Gmail and FalkorDB values, resolved from
the Keychain and the quarantined .env. A sweep that greps for `xoxb-` proves the
absence of a prefix; a sweep that greps for the actual secret proves the absence
of the secret. It exits non-zero if it finds one, and it never prints one.

It does NOT push. The last step is printing the commands, because a script that
force-pushes to a public repository on its own is a script somebody eventually
runs in the wrong directory.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

from .. import config, secrets_store

# --- what ships -------------------------------------------------------------
# Directories are copied whole, minus the exclusions below. Files are named.

PACKAGE_DIRECTORIES = ("executors", "tools", "fixtures", "tests", "templates", "static")
PACKAGE_FILES = (
    "__init__.py", "board_server.py", "cloud_mirror.py", "config.py",
    "extraction.py", "meetings_ui.py", "meetingscribe_source.py", "memory_store.py",
    "orchestrator.py", "planner.py", "prompts.py", "reset_demo.py", "results.py",
    "secrets_store.py", "seed_memory.py", "watcher.py",
    "requirements.txt", "README.md", "DEMO.md", ".gitignore",
)
# DEMO.md ships. It is a stage runbook, and half the comments in the package
# refer to it by name, so excluding it would leave a tree full of pointers into
# a file that is not there. What does not ship is its dateline — the one line
# that names a date and an audience — which REWRITES below replaces with the
# sentence it was standing in for.
PACKAGE_EXTRA = (
    "bin/calendar_add.swift",       # the source; the compiled binary is built on demand
    "state/README.md",
    "recaps/README.md",
)

WEB_DIRECTORIES = ("app", "lib")
WEB_FILES = (
    "package.json", "package-lock.json", "tsconfig.json", "next.config.mjs",
    ".env.example", ".gitignore", "README-web.md", "demo-snapshot.json",
)

# Names and suffixes that never travel, wherever they appear.
EXCLUDED_NAMES = frozenset({
    "__pycache__", "node_modules", ".next", ".venv", ".git", ".DS_Store",
    ".impeccable", "state", "recaps", "scratch", "scratchpad", "out", ".vercel",
})
EXCLUDED_SUFFIXES = (
    ".pyc", ".pyo", ".env", ".log", ".bak", ".tmp", ".sqlite3", ".tsbuildinfo",
    ".flac", ".wav", ".m4a", ".mp3", ".dSYM",
)
EXCLUDED_EXACT = frozenset({".env", ".env.local", "next-env.d.ts", "executions.jsonl"})


# Text rewritten on the way out. These are not redactions — they are corrections.
# Every runnable line in the docstrings is written against the directory this was
# built in, and a reader who clones the repository does not have that directory.
# Leaving them would ship instructions that cannot work, so the substitution
# makes the tree more accurate rather than less.
REWRITES: tuple[tuple[str, str], ...] = (
    ("Run everything on a virtualenv with `adjourn/requirements.txt` installed.",
     "Run everything on a virtualenv with `adjourn/requirements.txt` installed."),
    ("PY=python                          # a virtualenv with requirements.txt installed",
     "PY=python                          # a virtualenv with requirements.txt installed"),
    ("python", "python"),
)

# The build directory is READ FROM THE RUNNING CHECKOUT rather than written down
# here. Two reasons, and the second is the one that matters:
#   1. this tool then works from any checkout, not just the one it was born in;
#   2. a hard-coded "/Users/<someone>" in this file is itself a local path in
#      the published tree — and this is the one file the path sweep has to skip,
#      because it would otherwise flag its own rewrite table. The tool would be
#      shipping exactly what it exists to remove.
_ROOT = str(config.REPO_ROOT)
_HOME = str(Path.home())


def _path_rewrites() -> tuple[tuple[str, str], ...]:
    """Substitutions derived from where this checkout actually lives.

    Longest first, so `<root>/python` is consumed before the
    bare `<root>` rule can chop it in half. Percent-encoded spellings are in
    here because a path can reach the tree inside a URL, where the literal form
    does not match: a `file://` recap link in board_server's demo fixture read
    `Memory%20Meets%20Motion` and walked straight past the plain rule.
    """
    quoted = quote(_ROOT)
    return (
        (f'cd "{_ROOT}"', "cd /path/to/adjourn"),
        (f"{_ROOT}/python", "python"),
        (f"file://{quoted}/adjourn/", "/"),
        (f"file://{_ROOT}/adjourn/", "/"),
        (f"{_ROOT}/", ""),
        (_ROOT, "/path/to/adjourn"),
        (quoted, "/path/to/adjourn"),
    )


# A home directory can reach the tree in more spellings than any table can list.
# The build FAILS on a survivor rather than shipping it, because the old rule —
# "rewrite the shapes we thought of" — is exactly what let a percent-encoded
# path through.
FORBIDDEN_IN_OUTPUT = (_HOME, quote(_HOME))

REWRITABLE_SUFFIXES = (".py", ".md", ".txt", ".ts", ".tsx", ".css", ".html", ".swift")

# The demo runbook opens with a bold dateline naming a date and an audience. The
# runbook itself is worth shipping — half the package's comments point at it —
# but that one line is not, and it is matched by shape rather than quoted here
# so that scrubbing it does not put it back into the published tree verbatim.
DATELINE = re.compile(
    r"^\*\*[A-Z][a-z]{2} \d{1,2}, \d{4} · [^\n]+ · (.*?)\*\*$",
    re.MULTILINE | re.DOTALL,
)


def _strip_dateline(text: str) -> str:
    """Drop the date and audience from a bold dateline, keeping what it described."""
    return DATELINE.sub(lambda match: f"**{match.group(1).strip()}**", text)


def _rewrite(text: str) -> str:
    """Apply every substitution, longest pattern first so prefixes cannot win."""
    for pattern, replacement in REWRITES + _path_rewrites():
        text = text.replace(pattern, replacement)
    return _strip_dateline(text)


def _place(source: Path, target: Path) -> None:
    """Copy one file, rewriting local paths in the kinds of file that hold them."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() in REWRITABLE_SUFFIXES:
        try:
            original = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            shutil.copy2(source, target)
            return
        rewritten = _rewrite(original)
        target.write_text(rewritten, encoding="utf-8")
        return
    shutil.copy2(source, target)


def _is_excluded(path: Path) -> bool:
    """True when this path must not appear in the published tree."""
    if path.name in EXCLUDED_EXACT or path.name in EXCLUDED_NAMES:
        return True
    if path.name.startswith(".env") and path.name != ".env.example":
        return True
    return path.name.endswith(EXCLUDED_SUFFIXES)


def _copy_tree(source: Path, destination: Path) -> list[Path]:
    """Copy a directory, skipping every excluded name at every level."""
    copied: list[Path] = []
    for item in sorted(source.rglob("*")):
        if any(_is_excluded(Path(part)) for part in item.relative_to(source).parts):
            continue
        if _is_excluded(item) or not item.is_file():
            continue
        target = destination / item.relative_to(source)
        _place(item, target)
        copied.append(target)
    return copied


def build(out: Path) -> list[Path]:
    """Assemble the tree at `out`, replacing anything already there."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    copied: list[Path] = []

    package_root = config.PACKAGE_ROOT
    package_out = out / "adjourn"
    for name in PACKAGE_DIRECTORIES:
        source = package_root / name
        if source.exists():
            copied += _copy_tree(source, package_out / name)
    for name in PACKAGE_FILES:
        source = package_root / name
        if source.exists():
            _place(source, package_out / name)
            copied.append(package_out / name)
    for relative in PACKAGE_EXTRA:
        source = package_root / relative
        if source.exists():
            _place(source, package_out / relative)
            copied.append(package_out / relative)

    web_root = config.REPO_ROOT / "web"
    web_out = out / "web"
    for name in WEB_DIRECTORIES:
        source = web_root / name
        if source.exists():
            copied += _copy_tree(source, web_out / name)
    for name in WEB_FILES:
        source = web_root / name
        if source.exists():
            _place(source, web_out / name)
            copied.append(web_out / name)

    (package_out / ".env.example").write_text(ENV_EXAMPLE, encoding="utf-8")
    copied.append(package_out / ".env.example")
    (out / "README.md").write_text(README, encoding="utf-8")
    copied.append(out / "README.md")
    (out / "LICENSE").write_text(LICENSE, encoding="utf-8")
    copied.append(out / "LICENSE")
    (out / ".gitignore").write_text(ROOT_GITIGNORE, encoding="utf-8")
    copied.append(out / ".gitignore")
    return sorted(copied)


# --- the sweep --------------------------------------------------------------


# Short enough not to be searched for. The threshold is low on purpose — the
# FalkorDB cloud password on this machine is 14 characters, and a sweep that
# silently skips the one credential you were asked to check for is worse than no
# sweep at all. What is NOT searched for is the FalkorDB *username*, which is
# literally "falkordb": it occurs in the requirements file, in half the
# docstrings and on every line of `web/package-lock.json`, so searching for it
# flags the whole tree and teaches the reader to ignore the result. It is
# excluded by name above rather than by length.
MINIMUM_SECRET_LENGTH = 8


def live_secret_values() -> dict[str, str]:
    """The credentials this machine actually holds, by label. Values never printed."""
    found: dict[str, str] = {}
    for label, name in (
        ("slack_bot_token", secrets_store.SLACK_BOT_TOKEN),
        ("linear_api_key", secrets_store.LINEAR_API_KEY),
        ("gmail_address", secrets_store.GMAIL_ADDRESS),
        ("gmail_app_password", secrets_store.GMAIL_APP_PASSWORD),
        ("gemini_api_key", secrets_store.GEMINI_API_KEY),
    ):
        value = secrets_store.get_secret(name)
        if value and len(value) >= MINIMUM_SECRET_LENGTH:
            found[label] = value
    for label, variable in (
        ("falkordb_cloud_password", "FALKORDB_CLOUD_PASSWORD"),
        ("falkordb_cloud_host", "FALKORDB_CLOUD_HOST"),
    ):
        value = config.read_private_setting(variable) or config.read_setting(variable)
        if value and len(value) >= MINIMUM_SECRET_LENGTH:
            found[label] = value
    # The Gmail address is short and is a real identifier, so it is searched for
    # regardless of length — it is the one value here that is a person rather
    # than a token.
    address = secrets_store.get_secret(secrets_store.GMAIL_ADDRESS)
    if address:
        found["gmail_address"] = address
    return found


# Prefixes, kept as a second net. A prefix hit is not proof of a live secret, but
# a public repo has no business carrying one either — it is at best a paste.
SECRET_PREFIXES = ("lin_api_", "xoxb-", "xapp-", "xoxp-", "ghp_", "gho_", "sk-ant-", "AIza")


def sweep(out: Path) -> tuple[list[str], list[str]]:
    """(hard failures, prefix warnings). Reads every file; prints no secret."""
    secrets = live_secret_values()
    failures: list[str] = []
    warnings: list[str] = []
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = path.relative_to(out)
        for label, value in secrets.items():
            if value in text:
                failures.append(f"{relative}: contains the live {label}")
        if relative.name == "publish_tree.py":
            continue  # this file IS the prefix list; it would flag every entry
        for prefix in SECRET_PREFIXES:
            if prefix in text:
                warnings.append(f"{relative}: names the prefix {prefix!r}")
        # A home directory is not a credential, but it is a path a cloner does
        # not have, and it says who built this. REWRITES handles the shapes we
        # thought of; this catches the ones we did not, and fails rather than
        # shipping them.
        for spelling in FORBIDDEN_IN_OUTPUT:
            if spelling in text:
                failures.append(
                    f"{relative}: still carries a local path ({spelling}) — "
                    "add its spelling to REWRITES"
                )
    return failures, warnings


# --- generated files --------------------------------------------------------

ENV_EXAMPLE = """# Adjourn configuration. Copy to adjourn/.env and fill in what you need.
#
# NOTHING SECRET BELONGS IN THIS FILE. Tokens go in the macOS Keychain:
#     security add-generic-password -a "$USER" -s adjourn.slack_bot_token -w
# `secrets_store.get_secret()` reads the Keychain first and the environment
# second, and a secret that is missing is not an error — it is sim mode.
#
# Every secret-shaped variable loaded from a .env file is lifted straight back
# out of os.environ at import (config._quarantine_secret_environment), so it is
# never inherited by `gh`, the model CLI, or any other child process.

# Run everything simulated. Executors render the exact payload they would have
# sent and do not send it.
ADJOURN_SIM=1

# Action kinds that stay LIVE under ADJOURN_SIM=1. Narrows, never widens.
ADJOURN_LIVE_KINDS=

# The one repository GitHub executors may write to. It must already be in
# config.ALLOWED_GITHUB_REPOS; a value outside the allowlist is ignored.
ADJOURN_REPO=sharique2004/adjourn

LINEAR_TEAM_KEY=SHA
SLACK_CHANNEL=#all-test

MEMORY_BACKEND=falkor
GRAPH_NAME=adjourn
FALKOR_HOST=localhost
FALKOR_PORT=6379

MEETINGSCRIBE_HOST=127.0.0.1
MEETINGSCRIBE_PORT=
EXTRACTION_ENGINE=claude

REGRET_WINDOW_SECONDS=60
BOARD_PORT=5117

# Cloud mirror. Leave the host unset and the web surface runs on its bundled
# snapshot, which is what it is built to do.
FALKORDB_CLOUD_HOST=
FALKORDB_CLOUD_PORT=
FALKORDB_CLOUD_USERNAME=
FALKORDB_CLOUD_PASSWORD=
FALKORDB_CLOUD_TLS=
FALKORDB_GRAPH=adjourn
"""

ROOT_GITIGNORE = """.DS_Store
__pycache__/
*.pyc
*.pyo

# Never a filled-in environment file. Only the examples.
.env
*.env
.env.*
!.env.example

# Runtime state, recordings, transcripts, journals — none of it is source.
adjourn/state/*
!adjourn/state/README.md
adjourn/recaps/*
!adjourn/recaps/README.md
executions.jsonl
*.flac
*.wav
*.m4a
*.mp3
*.sqlite3

# Built things.
adjourn/bin/calendar_add
adjourn/bin/*.dSYM/
web/node_modules/
web/.next/
web/out/
web/.vercel
*.tsbuildinfo
next-env.d.ts
.venv/

# Scratch.
scratch/
scratchpad/
*.bak
*.tmp
"""

README = """# Adjourn — the meeting is the to-do.

A meeting ends. Someone says they will open a PR, someone reverses a decision
made three weeks ago, someone promises to email the client. Then everyone closes
the laptop and most of it does not happen.

Adjourn records the call on the machine it is running on, transcribes it there,
works out what was actually decided and actually promised, and does those things
— files the issue comment, moves the ticket, opens the draft PR, holds the time,
writes the recap. Not a summary with action items in it. The actions.

It runs on a Mac, against a local recorder and a local graph. The transcript does
not leave the machine.

## What it does, exactly

| | |
| --- | --- |
| `github_update` | Comments what changed on the issue the decision was about, with the verbatim sentence and a before/after table. |
| `linear_create` | Files the ticket somebody asked for, assigned to whoever asked. |
| `linear_move` | Moves an existing ticket to the state an update implies — "I'm picking up SHA-6" moves it to In Progress. |
| `pull_request_stub` | Opens a draft PR carrying the decision and the quote behind it. |
| `pr_review_suggestion` | Leaves an inline ```suggestion on an open PR when a review comment in the room names a concrete change. |
| `slack_send` | Posts the message somebody promised, after a visible 60-second countdown. |
| `email_send` | Sends the email somebody promised, after the same countdown. |
| `calendar_hold` | Holds the time the meeting agreed to. |
| `recap_page` | Writes the recap: decisions, commitments, and the questions nobody answered. |

Every fired action writes a receipt and can be undone from the board that shows
it — undo lives next to the thing that fired, not in a settings page.

## What it refuses to do

This is the harder half and most of the code is here.

A meeting is full of sentences that sound like commitments and are not.
Negations — *"don't email the client yet"*. Ideas that were raised and killed in
the same breath. Hypotheticals. And reported speech: *"Alex said he'd send the
deck"* is somebody else's commitment, mentioned in passing, and firing on it
means sending mail on behalf of a person who was not in the room. None of these
fire anything. Reported speech reaches the recap as a third-party note, clearly
attributed, and stops there.

The planner that decides what fires is a deterministic table from statement kind
to action kind. It imports no model and opens no socket. The model's job is to
read the transcript and produce statements; choosing the verb is not its job.

## How it fits together

```
  microphone + system audio
            │
            ▼
   MeetingScribe (local)          recording, diarisation, transcript
            │  transcript, on disk
            ▼
       extraction                 Statements: kind, claim, quote, refs
            │                     (the one model call; sandboxed, no tools)
            ▼
        memory                    FalkorDB graph — who said what, about which
            │                     issue, in which meeting, superseding what
            ▼
        planner                   ROUTING_TABLE: statement kind -> action kind
            │                     deterministic; no model, no network
            ▼
       executors                  github · linear · slack · email · calendar · recap
            │                     one live/sim fork at the bottom of each module
            ▼
     journal + board              receipts, undo, a 60s window on the irreversible
            │
            └────────► cloud mirror ────► read-only web surface
```

Sim and live are the same code path. Every executor builds the exact payload it
would send, and forks to the wire only at the very bottom of the module. A
missing credential is not an error, it is sim mode: the card renders the real
request, badged SIM, unsent. That is why the demo works on venue wifi and why
the badge on a card can be trusted — nothing renders LIVE unless something left
the machine.

Live writes are bounded by allowlists that are checked in code rather than
promised in a README: one GitHub repository, one Slack channel, one Linear team.

## Running it

Requires macOS, Python 3.12+, an authenticated `gh`, and
[MeetingScribe](https://github.com/sharique2004/MeetingScribe) on `:5005`.
FalkorDB is optional — memory falls back to SQLite.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r adjourn/requirements.txt
cp adjourn/.env.example adjourn/.env      # ADJOURN_SIM=1 by default

python -m adjourn.board_server            # the follow-through board, :5117
python -m adjourn.watcher                 # watches for a finished recording
```

Nothing goes live until a credential exists for it. Put credentials in the
Keychain, not in the .env:

```bash
security add-generic-password -a "$USER" -s adjourn.slack_bot_token -w
```

Tests are plain modules, no framework:

```bash
python -m adjourn.tests.test_skeleton_contracts
python -m adjourn.tests.test_lane_a_spine
```

## The web surface

`web/` is a Next.js app that reads the cloud mirror and nothing else. It cannot
fire an action and it cannot undo one. With no cloud credentials it runs from a
bundled snapshot and says so in the footer.

## Licence

MIT. See LICENSE.
"""

LICENSE = """MIT License

Copyright (c) 2026 Sharique Khatri

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


# --- entry point ------------------------------------------------------------


PUSH_COMMANDS = """
# --- the push. Read it, then run it yourself. -------------------------------
cd {out}
git init -b main
git add -A
git status --short | head -40          # look at this before committing
git commit -m "Adjourn — the meeting is the to-do."
git remote add origin https://github.com/{repo}.git
git push -u origin main

# then, and only then, build the demo world inside the repo:
cd "{source_root}"
python -m adjourn.tools.seed_demo_world
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="/tmp/adjourn-push",
                        help="staging directory to build (replaced if it exists)")
    args = parser.parse_args(argv)

    out = Path(args.out).expanduser().resolve()
    if out == config.REPO_ROOT or config.REPO_ROOT in out.parents:
        print(f"REFUSED: {out} is inside the working tree — stage somewhere else")
        return 2

    copied = build(out)
    total = sum(path.stat().st_size for path in copied)
    print(f"built {out}")
    print(f"{len(copied)} files, {total / 1024:.0f} KB")
    print()

    top = sorted({str(path.relative_to(out)).split("/")[0] for path in copied})
    for name in top:
        count = sum(1 for path in copied if str(path.relative_to(out)).startswith(name))
        print(f"  {name:<14} {count} files")
    print()

    failures, warnings = sweep(out)
    checked = len(live_secret_values())
    print(f"secret sweep: {checked} live credentials resolved from this machine, "
          f"searched for verbatim in every file")
    for warning in warnings:
        print(f"  WARN  {warning}")
    if failures:
        for failure in failures:
            print(f"  FAIL  {failure}")
        print()
        print("REFUSING to print push commands. Fix the tree and run again.")
        return 1
    print("  clean — no live credential appears anywhere in the tree")
    if not warnings:
        print("  clean — no credential prefix appears anywhere in the tree")
    print()

    for forbidden in ("adjourn/state", "adjourn/recaps", "node_modules", "__pycache__"):
        leaked = [p for p in copied if forbidden in str(p.relative_to(out))
                  and not str(p).endswith("README.md")]
        if leaked:
            print(f"  FAIL  {len(leaked)} files under {forbidden} leaked into the tree")
            return 1
    print("  clean — no state, recaps, node_modules or bytecode in the tree")

    print(PUSH_COMMANDS.format(out=out, repo=config.github_repo(),
                               source_root=config.REPO_ROOT))
    print("Nothing was pushed. The commands above are for you to run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
