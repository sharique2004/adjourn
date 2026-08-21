"""Configuration for Adjourn — .env values, sane defaults, and path/port discovery.

Everything that another module needs to know about *where things are* lives here,
so no lane has to hardcode a port or a directory. Values come from
`adjourn/.env` (loaded at import), then from the process environment, then from
the defaults in this file.

Secrets do NOT live here. Use `secrets_store.get_secret()` for anything that
would be embarrassing in a screenshot.

Run modules with -m from the repo root:
    python -m adjourn.orchestrator
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# --- package geography ------------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
STATE_DIR = PACKAGE_ROOT / "state"
FIXTURES_DIR = PACKAGE_ROOT / "fixtures"
RECAPS_DIR = PACKAGE_ROOT / "recaps"
ENV_PATH = PACKAGE_ROOT / ".env"

load_dotenv(ENV_PATH)

# --- secret quarantine ------------------------------------------------------
# `load_dotenv` above puts every line of adjourn/.env into os.environ, and
# os.environ is inherited by every child process this package spawns — the
# `claude` CLI, `gh`, `security`, the Swift calendar helper. A credential sitting
# in a third-party process's environment block is readable by `ps -E`, lands in
# crash reports, and is one feature flag away from being readable by a model.
#
# So the secret-shaped variables are lifted straight back OUT of os.environ into
# this module's memory the moment they are loaded. They stay reachable through
# `read_private_setting()`; they stop being reachable through inheritance. This
# is why `secrets_store` reads its ADJOURN_* fallbacks from here rather than from
# os.environ directly, and why `cloud_mirror` parses the .env file itself instead
# of calling load_dotenv a second time (which would undo this).

_PRIVATE_SETTINGS: dict[str, str] = {}

# Exact names that are always quarantined, whatever they look like.
QUARANTINED_NAMES: frozenset[str] = frozenset({
    "FALKORDB_CLOUD_PASSWORD",
    "FALKORDB_CLOUD_USERNAME",
})

# Suffixes that make a name secret-shaped. Anything matching is quarantined too,
# so a key added to .env later is protected without anyone remembering to.
QUARANTINED_SUFFIXES: tuple[str, ...] = (
    "_PASSWORD", "_TOKEN", "_API_KEY", "_SECRET", "_KEY",
)

# Names that end in a quarantined suffix but are not secrets. Keep this short.
QUARANTINE_EXEMPT_NAMES: frozenset[str] = frozenset({
    "LINEAR_TEAM_KEY",  # "SHA" — a team handle, printed on the sim card on purpose
})


def _is_secret_shaped(name: str) -> bool:
    """Should `name` be kept out of every child process's environment?"""
    if name in QUARANTINE_EXEMPT_NAMES:
        return False
    if name in QUARANTINED_NAMES:
        return True
    return name.endswith(QUARANTINED_SUFFIXES)


def _quarantine_secret_environment() -> None:
    """Move secret-shaped variables out of os.environ and into this process.

    Idempotent: calling it again after a new secret is exported picks that one up
    too. Never raises — a hardening step must not be able to stop a demo.
    """
    for name in [key for key in os.environ if _is_secret_shaped(key)]:
        value = os.environ.pop(name, "")
        if value:
            _PRIVATE_SETTINGS[name] = value


_quarantine_secret_environment()


def read_private_setting(name: str, default: str = "") -> str:
    """One secret-shaped setting, read from the quarantine and then os.environ.

    os.environ is still consulted second so a secret exported *after* import (a
    key pasted mid-demo) is picked up; `_quarantine_secret_environment()` sweeps
    it out on the next call.
    """
    quarantined = (_PRIVATE_SETTINGS.get(name) or "").strip()
    if quarantined:
        return quarantined
    return (os.environ.get(name) or "").strip() or default


def child_process_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal environment for a subprocess: no credentials, by construction.

    Pass this as `env=` to every `subprocess.run` that shells out to a third
    party. It carries only what a CLI genuinely needs to function — PATH to find
    binaries, HOME because `gh` and `claude` read their credentials out of dot
    directories under it, and a dumb TERM so nothing tries to draw a progress bar
    into a captured pipe.

    Secret values are never placed on a command line either: argv is world
    readable through `ps`, so secrets travel as environment entries added
    explicitly through `extra`, or over stdin, and never as arguments.
    """
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "TERM": "dumb",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    for name in ("SSL_CERT_FILE", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    if extra:
        environment.update({key: str(value) for key, value in extra.items()})
    return environment

# --- MeetingScribe geography (READ-ONLY — never write into the app directory) -

MEETINGSCRIBE_HOME = Path.home() / ".meetingscribe"
MEETINGSCRIBE_CONFIG_PATH = MEETINGSCRIBE_HOME / "config.json"
RECORDINGS_DIR = MEETINGSCRIBE_HOME / "recordings"
EXECUTIONS_JOURNAL_PATH = MEETINGSCRIBE_HOME / "executions.jsonl"

DEFAULT_MEETINGSCRIBE_PORT = 5005
DEFAULT_MEETINGSCRIBE_HOST = "127.0.0.1"

# --- defaults ---------------------------------------------------------------

DEFAULT_BOARD_PORT = 5117
DEFAULT_REGRET_WINDOW_SECONDS = 60
DEFAULT_GRAPH_NAME = "adjourn"
DEFAULT_FALKOR_HOST = "localhost"
DEFAULT_FALKOR_PORT = 6379
DEFAULT_GITHUB_REPO = "sharique2004/adjourn"
DEFAULT_LINEAR_TEAM_KEY = "SHA"

# The only repo GitHub executors are ever allowed to mutate. Live GitHub calls
# against anything else are a bug — executors must assert against this.
#
# It is Adjourn's OWN repository. That is the point: the roadmap issues it
# comments on and the pull request it reviews are the roadmap and the pull
# request of the thing doing the commenting. A tool that manages its own
# repository is a claim you can check by clicking the link, rather than a
# sandbox somebody built to be impressed by.
ALLOWED_GITHUB_REPOS = frozenset({"sharique2004/adjourn"})

DEFAULT_SLACK_CHANNEL = "#all-test"

# The only Slack channels a live send may reach — the GitHub allowlist's twin.
# Slack's guarantee used to rest on the planner never populating a `channel`
# field and the extractor's whitelist dropping one if the model emitted it: two
# true facts, neither of them a guard. This is the guard. A channel that is not
# in here is refused before the message is composed, in `execute` and in `undo`.
#
# Names only, in both spellings. Slack answers a post with the channel ID (C…)
# rather than the name, and this token has no `channels:read` scope to turn one
# into the other — so `undo` checks the NAME the send recorded next to the id,
# and refuses a record that carries an id with no name attached.
ALLOWED_SLACK_CHANNELS = frozenset({"#all-test", "all-test"})


# --- small readers ----------------------------------------------------------


def read_setting(name: str, default: str = "") -> str:
    """One environment/.env string setting, stripped. Empty string means 'unset'."""
    return (os.environ.get(name) or "").strip() or default


def read_integer_setting(name: str, default: int) -> int:
    """One environment/.env integer setting; falls back to `default` if unparseable."""
    raw = read_setting(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def read_flag(name: str, default: bool = False) -> bool:
    """One environment/.env boolean. '1', 'true', 'yes', 'on' are true."""
    raw = read_setting(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# --- MeetingScribe discovery ------------------------------------------------


def discover_meetingscribe_port() -> int:
    """Port of the local MeetingScribe Flask engine.

    Order: MEETINGSCRIBE_PORT (.env override) -> "port" in
    ~/.meetingscribe/config.json -> 5005. Never raises: a missing or malformed
    config file falls through to the default.
    """
    override = read_integer_setting("MEETINGSCRIBE_PORT", 0)
    if override:
        return override
    try:
        settings = json.loads(MEETINGSCRIBE_CONFIG_PATH.read_text())
        port = settings.get("port")
        if isinstance(port, int) and port > 0:
            return port
        if isinstance(port, str) and port.strip().isdigit():
            return int(port.strip())
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return DEFAULT_MEETINGSCRIBE_PORT


def meetingscribe_host() -> str:
    """Loopback host of the MeetingScribe engine. It is loopback-guarded; keep it that way."""
    return read_setting("MEETINGSCRIBE_HOST", DEFAULT_MEETINGSCRIBE_HOST)


def meetingscribe_base_url() -> str:
    """e.g. 'http://127.0.0.1:5005'. Send NO Origin header to this engine."""
    return f"http://{meetingscribe_host()}:{discover_meetingscribe_port()}"


# --- mode / backends --------------------------------------------------------


def is_simulation_forced() -> bool:
    """ADJOURN_SIM=1 forces every executor into sim mode regardless of secrets."""
    return read_flag("ADJOURN_SIM", False)


def forced_live_action_kinds() -> frozenset[str]:
    """Action kinds that stay LIVE even under ADJOURN_SIM=1.

        ADJOURN_LIVE_KINDS=github_update,pull_request_stub

    This is the demo's actual configuration: everything simulated except the two
    transports that write somewhere safe, public and reversible — a repo that
    exists for this. It is a narrowing of ADJOURN_SIM, never a widening: a kind
    listed here still needs its secrets, still logs the decision out loud, and
    still wears whichever badge is true. An empty list (the default) means
    ADJOURN_SIM=1 means exactly what it says.
    """
    raw = read_setting("ADJOURN_LIVE_KINDS")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def memory_backend() -> str:
    """'falkor' or 'sqlite'. Falkor is the default; memory_store falls back on its own."""
    backend = read_setting("MEMORY_BACKEND", "falkor").lower()
    return backend if backend in {"falkor", "sqlite"} else "falkor"


def graph_name() -> str:
    """FalkorDB graph name. Defaults to 'adjourn' so drift's Aug 3 demo graph stays intact."""
    return read_setting("GRAPH_NAME", DEFAULT_GRAPH_NAME)


def falkor_host() -> str:
    return read_setting("FALKOR_HOST", DEFAULT_FALKOR_HOST)


def falkor_port() -> int:
    return read_integer_setting("FALKOR_PORT", DEFAULT_FALKOR_PORT)


def sqlite_memory_path() -> Path:
    """Fallback memory database, used when FalkorDB is unreachable."""
    return state_directory() / "memory.sqlite3"


# --- action-side settings ---------------------------------------------------


def github_repo() -> str:
    """Adjourn's own repo. Live GitHub writes are allowed here and nowhere else.

    ADJOURN_REPO can still name the repo, but it can only ever name one that is
    already in ALLOWED_GITHUB_REPOS. A stale override in a .env file is a
    plausible way to point a live write at the wrong repository, and an
    environment variable is not the right place to widen a safety boundary — so a
    value outside the allowlist is ignored with a printed complaint rather than
    quietly obeyed.
    """
    override = read_setting("ADJOURN_REPO")
    if not override:
        return DEFAULT_GITHUB_REPO
    if override in ALLOWED_GITHUB_REPOS:
        return override
    print(
        f"[config] ignoring ADJOURN_REPO={override!r} — not in ALLOWED_GITHUB_REPOS "
        f"({sorted(ALLOWED_GITHUB_REPOS)}); using {DEFAULT_GITHUB_REPO}"
    )
    return DEFAULT_GITHUB_REPO


# A meeting never commits code. There is deliberately NO pr_autocommit_branches()
# here and no ADJOURN_PR_AUTOCOMMIT_BRANCHES setting: when a meeting corrects a
# line, pr_review_suggestion_executor leaves a review suggestion on the diff and
# the humans on the PR decide. The capability is absent from the code rather than
# merely defaulted off, so no .env line can turn speech into a push.


def slack_channel() -> str:
    """The one Slack channel live sends may reach. Config only — never a payload.

    Read as an ordinary setting rather than through `secrets_store` because a
    channel name is not a secret; the secret is the token that can post to it.
    The value still has to clear `ALLOWED_SLACK_CHANNELS` before anything is sent.
    """
    return read_setting("SLACK_CHANNEL", DEFAULT_SLACK_CHANNEL)


def linear_team_key() -> str:
    """The Linear team new tickets are filed into, e.g. "SHA".

    Configured rather than hard-coded because the SIM rendering shows it: a
    placeholder reading "<team-id for ENG>" on a workspace whose only team is SHA
    is a small lie on a big screen, and the sim payload's whole job is to be the
    exact thing the live call would have sent.
    """
    return read_setting("LINEAR_TEAM_KEY", DEFAULT_LINEAR_TEAM_KEY).upper()


def regret_window_seconds() -> int:
    """Seconds of countdown when an action kind still uses a regret window.

    Slack and email no longer use this — they wait in Ready to send.
    """
    return read_integer_setting("REGRET_WINDOW_SECONDS", DEFAULT_REGRET_WINDOW_SECONDS)


def extraction_engine() -> str:
    """'claude' (primary), 'gemini' (fallback), or 'fixtures' (the floor)."""
    engine = read_setting("EXTRACTION_ENGINE", "claude").lower()
    return engine if engine in {"claude", "gemini", "fixtures"} else "claude"


def is_demo_fixture_floor_allowed() -> bool:
    """May the LAST-RESORT demo fixture stand in for a meeting that has no fixture?

    Off by default, and it must stay off for a real meeting. A meeting whose own
    fixture exists (the demo, the prior standup) always gets it; this flag only
    governs `demo_statements.json` standing in for a meeting it knows nothing
    about — which would put the demo's promises on a real meeting's board under a
    real person's name. That is the one failure the sim badge cannot excuse.
    """
    return read_flag("ADJOURN_FIXTURE_FLOOR", False)


def board_port() -> int:
    return read_integer_setting("BOARD_PORT", DEFAULT_BOARD_PORT)


# --- state files ------------------------------------------------------------


def executions_journal_path() -> Path:
    """Append-only JSONL of everything Adjourn has executed (and undone)."""
    override = read_setting("EXECUTIONS_JOURNAL")
    return Path(override).expanduser() if override else EXECUTIONS_JOURNAL_PATH


def state_directory() -> Path:
    """Where Adjourn keeps its working files, honouring ADJOURN_STATE_DIR.

    STATE_DIR is the constant; this is the accessor, and the difference matters.
    Anything that reads the constant directly cannot be pointed somewhere else,
    which is how a test suite ended up leaving `state/recap_actions/quiet-meeting.json`
    in the shipped tree: the path it wanted to sandbox was not a function it
    could replace. Read through here.
    """
    override = read_setting("ADJOURN_STATE_DIR")
    return Path(override).expanduser() if override else STATE_DIR


def pending_actions_path() -> Path:
    """Actions inside their regret window, with fire_at timestamps. The board reads this."""
    return state_directory() / "pending.json"


def pipeline_status_path() -> Path:
    """Live extract/plan/watch phases the board guts panel reads. Orchestrator writes this."""
    override = read_setting("ADJOURN_PIPELINE")
    return Path(override).expanduser() if override else state_directory() / "pipeline.json"


def recaps_directory() -> Path:
    """Where recap_page writes local meeting recaps."""
    override = read_setting("RECAP_DIR")
    return Path(override).expanduser() if override else RECAPS_DIR


def ensure_state_directories() -> None:
    """Create state/, fixtures/, recaps/ if missing. Safe to call repeatedly."""
    for directory in (state_directory(), FIXTURES_DIR, recaps_directory()):
        directory.mkdir(parents=True, exist_ok=True)


def describe_configuration() -> dict:
    """Flat snapshot for logs, the board header, and `-m adjourn.config`. No secrets."""
    return {
        "package_root": str(PACKAGE_ROOT),
        "meetingscribe_base_url": meetingscribe_base_url(),
        "recordings_dir": str(RECORDINGS_DIR),
        "executions_journal": str(executions_journal_path()),
        "pending_actions": str(pending_actions_path()),
        "pipeline_status": str(pipeline_status_path()),
        "recaps_dir": str(recaps_directory()),
        "simulation_forced": is_simulation_forced(),
        "memory_backend": memory_backend(),
        "graph_name": graph_name(),
        "falkor": f"{falkor_host()}:{falkor_port()}",
        "github_repo": github_repo(),
        "extraction_engine": extraction_engine(),
        "regret_window_seconds": regret_window_seconds(),
        "board_port": board_port(),
    }


if __name__ == "__main__":
    print(json.dumps(describe_configuration(), indent=2))
