"""Secrets for Adjourn — one accessor, three sources, honest failure.

    get_secret("slack_bot_token")
        1. macOS Keychain:  security find-generic-password -s adjourn.slack_bot_token -w
        2. environment:     ADJOURN_SLACK_BOT_TOKEN
        3. None             -> the caller enters sim mode, loudly

A missing secret is never an error. It is a mode. Executors call
`decide_mode(...)` and get "sim" back, which is a demo that still works rather
than a traceback in front of founders.

To put a secret in the Keychain (do this yourself, in your own terminal):
    security add-generic-password -a "$USER" -s adjourn.slack_bot_token -w
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from . import config

KEYCHAIN_SERVICE_PREFIX = "adjourn."
ENVIRONMENT_PREFIX = "ADJOURN_"
KEYCHAIN_TIMEOUT_SECONDS = 5

# Canonical secret names, so lanes do not invent three spellings of the same key.
SLACK_BOT_TOKEN = "slack_bot_token"
SLACK_CHANNEL = "slack_channel"
LINEAR_API_KEY = "linear_api_key"
GMAIL_ADDRESS = "gmail_address"
GMAIL_APP_PASSWORD = "gmail_app_password"
GEMINI_API_KEY = "gemini_api_key"
FALKORDB_CLOUD_PASSWORD = "falkordb_cloud_password"

_cache: dict[str, str | None] = {}


def _read_from_keychain(name: str) -> str | None:
    """Ask the macOS Keychain for adjourn.<name>. None if absent or `security` is unavailable."""
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", f"{KEYCHAIN_SERVICE_PREFIX}{name}", "-w"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _read_from_environment(name: str) -> str | None:
    """ADJOURN_<NAME> from the .env quarantine, then the process environment.

    Read through `config.read_private_setting` rather than `os.environ` directly:
    config lifts secret-shaped variables straight back out of os.environ at
    import, so they are not inherited by `gh`, `claude`, `security` or any other
    child process. Reading os.environ here would find nothing and quietly fall to
    sim mode.
    """
    value = config.read_private_setting(f"{ENVIRONMENT_PREFIX}{name.upper()}")
    return value.strip() or None


def get_secret(name: str) -> str | None:
    """The single secret accessor for the whole package. Cached per process.

    Returns the secret string, or None when it is not provisioned anywhere. A
    None return is the signal to run in sim mode — never a reason to raise.
    """
    if name in _cache:
        return _cache[name]
    value = _read_from_keychain(name) or _read_from_environment(name)
    _cache[name] = value
    return value


def child_process_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A credential-free environment for a subprocess. See config for the reasoning.

    Re-exported here because the modules that spawn children (`extraction`, the
    `gh` transport, the calendar helper) already import this module for the
    secrets themselves, and the two rules belong next to each other: secrets do
    not travel in the environment of a child, and they do not travel in its argv.
    """
    return config.child_process_environment(extra)


def assert_no_secret_in_argv(argv: Sequence[str]) -> None:
    """Raise if any known secret's VALUE appears in a command line.

    argv is world-readable through `ps`. A secret belongs on stdin or in an
    explicit environment entry, never as an argument — and "never" is worth one
    cheap assertion at the call site rather than a comment asking people to
    remember. Only already-resolved secrets are compared, so this reads nothing
    new out of the Keychain.
    """
    values = {value for value in _cache.values() if value and len(value) >= 8}
    if not values:
        return
    for index, argument in enumerate(argv):
        text = str(argument)
        for value in values:
            if value in text:
                raise ValueError(
                    f"refusing to run: a secret value appears in argv[{index}] — "
                    "pass it on stdin or through an explicit env entry instead"
                )


def has_secret(name: str) -> bool:
    """True when `name` resolves to a non-empty value."""
    return get_secret(name) is not None


def forget_cached_secrets() -> None:
    """Drop the process cache so a secret added mid-demo is picked up on the next read."""
    _cache.clear()


def missing_secret_names(required: Sequence[str]) -> list[str]:
    """The subset of `required` that is not provisioned, in the order given."""
    return [name for name in required if not has_secret(name)]


def decide_mode(required: Sequence[str], *, label: str = "") -> str:
    """Return "live" or "sim" for an executor, and say out loud why.

    "sim" when ADJOURN_SIM=1 is set, or when any required secret is missing.
    Every executor calls this exactly once, at the top, and threads the result
    through to ExecutorResult.mode so the board badge cannot lie.
    """
    prefix = f"[{label}] " if label else "[secrets] "
    if config.is_simulation_forced():
        # ADJOURN_LIVE_KINDS carves specific action kinds back out of a blanket
        # ADJOURN_SIM=1 — the demo runs everything simulated except GitHub. It can
        # only ever narrow the exception: the secrets still have to be there, and
        # the decision is still printed.
        if label and label in config.forced_live_action_kinds():
            missing = missing_secret_names(required)
            if missing:
                print(f"{prefix}SIM mode — in ADJOURN_LIVE_KINDS but no secret for: "
                      f"{', '.join(missing)}")
                return "sim"
            print(f"{prefix}ADJOURN_SIM=1, but {label} is in ADJOURN_LIVE_KINDS — running LIVE")
            return "live"
        print(f"{prefix}ADJOURN_SIM=1 — running in SIM mode")
        return "sim"
    missing = missing_secret_names(required)
    if missing:
        print(f"{prefix}SIM mode — no secret for: {', '.join(missing)}")
        return "sim"
    return "live"


def describe_secret_availability() -> dict[str, bool]:
    """Which known secrets are provisioned. Values are booleans — never the secrets."""
    return {
        name: has_secret(name)
        for name in (
            SLACK_BOT_TOKEN,
            SLACK_CHANNEL,
            LINEAR_API_KEY,
            GMAIL_ADDRESS,
            GMAIL_APP_PASSWORD,
            GEMINI_API_KEY,
        )
    }


if __name__ == "__main__":
    import json

    print(json.dumps(describe_secret_availability(), indent=2))
