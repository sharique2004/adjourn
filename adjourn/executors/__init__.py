"""Executor registry — Action.kind -> the module that performs it.

Modules are imported LAZILY. A missing dependency in one executor (no falkordb,
no requests, whatever) must not stop the other seven from working, so nothing is
imported until the moment it is actually dispatched.

Every executor module exposes exactly two functions:

    execute(action: Action) -> ExecutorResult
    undo(result: ExecutorResult) -> bool

and follows the TRANSPORT SWAP rule: identical code path all the way down, with
the live/sim fork at the very BOTTOM of the module, immediately around the HTTP
call or subprocess. Sim mode renders the exact payload it would have sent — the
payload is real, only the send is not.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING

from .. import results

if TYPE_CHECKING:
    from ..planner import Action

# Action.kind -> module name inside this package. The keys here must stay exactly
# in sync with planner.ACTION_KINDS.
REGISTRY: dict[str, str] = {
    "github_update": "github_update_executor",
    "linear_create": "linear_create_executor",
    "linear_move": "linear_move_executor",
    "pull_request_stub": "pull_request_stub_executor",
    "pr_review_suggestion": "pr_review_suggestion_executor",
    "slack_send": "slack_send_executor",
    "email_send": "email_send_executor",
    "calendar_hold": "calendar_hold_executor",
    "recap_page": "recap_page_executor",
}

_loaded: dict[str, ModuleType] = {}


def load_executor(kind: str) -> ModuleType:
    """Import and cache the executor module for `kind`. Raises KeyError if unknown."""
    if kind in _loaded:
        return _loaded[kind]
    if kind not in REGISTRY:
        raise KeyError(f"no executor registered for action kind {kind!r}")
    module = importlib.import_module(f"{__name__}.{REGISTRY[kind]}")
    _loaded[kind] = module
    return module


def execute_action(action: Action) -> results.ExecutorResult:
    """Dispatch one action to its executor.

    Never raises: an executor that blows up becomes a failed ExecutorResult, which
    the board renders as a red card. One broken integration must not end a demo.
    """
    try:
        module = load_executor(action.kind)
    except KeyError as error:
        return results.ExecutorResult.failed(
            action.kind,
            f"no executor for {action.kind!r}: {error}",
            quote=action.quote,
            speaker=action.speaker,
            meeting_id=action.meeting_id,
        )
    try:
        return module.execute(action)
    except NotImplementedError as error:
        return results.ExecutorResult.failed(
            action.kind,
            f"not implemented yet: {error}",
            quote=action.quote,
            speaker=action.speaker,
            meeting_id=action.meeting_id,
        )
    except Exception as error:  # noqa: BLE001 — a demo must degrade, not crash
        return results.ExecutorResult.failed(
            action.kind,
            f"{action.kind} failed: {error}",
            quote=action.quote,
            speaker=action.speaker,
            meeting_id=action.meeting_id,
        )


def undo_result(result: results.ExecutorResult) -> bool:
    """Dispatch an undo. False when the executor is unknown, unreachable, or refuses.

    A FAILED result is never dispatched: nothing happened, so there is nothing to
    reverse, and answering True would put an "undone" line in the journal for an
    action that never took place. The board's undo affordance and this guard have
    to agree, and this is the one both go through.
    """
    if not result.ok:
        return False
    try:
        module = load_executor(result.kind)
    except KeyError:
        return False
    try:
        return bool(module.undo(result))
    except Exception as error:  # noqa: BLE001
        print(f"[executors] undo of {result.kind} failed: {error}")
        return False


def registered_kinds() -> tuple[str, ...]:
    """Every action kind this package can execute."""
    return tuple(REGISTRY)
