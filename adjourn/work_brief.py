"""What a developer actually needs, pulled from what was said.

No model. Filenames, branch names, and a coarse status (In Progress / In Review /
Done) are table lookups over the quote and the claim. The planner stamps them
onto Linear and GitHub payloads so a ticket is not just a paraphrase of the room.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .extraction import Statement

FILENAME_PATTERN = re.compile(
    r"\b([\w./-]+\.(?:css|scss|js|jsx|ts|tsx|py|rb|go|rs|java|html|json|ya?ml|md|toml))\b",
    re.IGNORECASE,
)

# Spoken branches look like priya/join-button, not github.com/org/repo.
BRANCH_PATTERN = re.compile(
    r"\b(?:on\s+(?:the\s+)?|branch\s+)([a-zA-Z][\w.-]*/[\w./-]+)\b"
    r"|\b([a-zA-Z][\w-]+/[\w./-]+)\b"
)

IN_REVIEW = ("in review", "up for review", "under review", "pr is up", "ready for review")
DONE = (
    "it's done", "its done", "is done", "that's done", "thats done",
    "merged it", "already merged", "shipped it", "closed the ticket",
)
IN_PROGRESS = (
    "working on", "in progress", "picking it up", "picking that up",
    "i've started", "ive started", "started on",
)

STATUS_IN_PROGRESS = "In Progress"
STATUS_IN_REVIEW = "In Review"
STATUS_DONE = "Done"


def _spoken(statement: object) -> str:
    quote = str(getattr(statement, "quote", "") or "")
    claim = str(getattr(statement, "claim", "") or "")
    return f"{quote} {claim}".strip()


def spoken_files(text: str) -> list[str]:
    """Filenames the room actually said, in order, unique."""
    seen: list[str] = []
    for match in FILENAME_PATTERN.finditer(text or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def spoken_branch(text: str) -> str:
    """A branch-shaped token the room said, or empty.

    Drops anything whose first segment looks like a host (`github.com/...`).
    """
    for match in BRANCH_PATTERN.finditer(text or ""):
        token = next((part for part in match.groups() if part), "")
        head = token.split("/", 1)[0]
        if "." in head:
            continue
        if head.lower() in {"http", "https", "www"}:
            continue
        if match.start() > 0 and (text or "")[match.start() - 1] == ".":
            continue
        prefix = (text or "")[max(0, match.start() - 16):match.start()].lower()
        if "github.com" in prefix:
            continue
        return token
    return ""


def spoken_status(text: str) -> str:
    """In Progress, In Review, Done, or empty. Explicit phrases win over weaker ones."""
    lowered = " ".join((text or "").lower().split())
    if any(phrase in lowered for phrase in IN_REVIEW):
        return STATUS_IN_REVIEW
    if any(phrase in lowered for phrase in DONE):
        return STATUS_DONE
    if any(phrase in lowered for phrase in IN_PROGRESS):
        return STATUS_IN_PROGRESS
    return ""


def from_statement(statement: Statement | object) -> dict:
    """{files, branch, work_status} — omit-empty at the call site."""
    text = _spoken(statement)
    return {
        "files": spoken_files(text),
        "branch": spoken_branch(text),
        "work_status": spoken_status(text),
    }


def attach(payload: dict, statement: Statement | object) -> dict:
    """Stamp the brief onto an action payload. Empty fields are left off."""
    brief = from_statement(statement)
    if brief["files"]:
        payload["files"] = list(brief["files"])
    if brief["branch"]:
        payload["branch"] = brief["branch"]
    if brief["work_status"]:
        payload["work_status"] = brief["work_status"]
    return payload


def as_markdown(payload: dict | None) -> str:
    """A Work section for a Linear description or GitHub comment. Empty if nothing."""
    payload = payload or {}
    files = [str(name) for name in (payload.get("files") or []) if str(name).strip()]
    branch = str(payload.get("branch") or "").strip()
    status = str(payload.get("work_status") or "").strip()
    if not files and not branch and not status:
        return ""
    lines = ["## Work", ""]
    if status:
        lines.append(f"- **Status:** {status}")
    if branch:
        lines.append(f"- **Branch:** `{branch}`")
    if files:
        lines.append("- **Files:** " + ", ".join(f"`{name}`" for name in files))
    lines.append("")
    return "\n".join(lines)


def from_record(record: dict | None) -> dict:
    """Read a brief back off a journal row (action_payload or the undo wrapper)."""
    record = record or {}
    payload = record.get("action_payload")
    if not isinstance(payload, dict):
        inner = record.get("undo_payload")
        payload = inner.get("payload") if isinstance(inner, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    files = [str(name) for name in (payload.get("files") or []) if str(name).strip()]
    return {
        "files": files,
        "branch": str(payload.get("branch") or "").strip(),
        "work_status": str(payload.get("work_status") or "").strip(),
    }
