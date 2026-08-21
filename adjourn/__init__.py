"""Adjourn — the meeting itself becomes the to-do.

A companion process to MeetingScribe. When a meeting stops, Adjourn extracts what
was decided / promised / reported and executes the follow-through.

Architecture in one line:
    watcher -> meetingscribe_source -> extraction (MODEL) -> planner (TABLE, NO MODEL)
            -> executors (transport-swapped live/sim) -> results journal -> board

Design rules this package exists to enforce:
  * LOCAL-FIRST. Everything runs on this Mac; the only network calls are the ones
    an executor makes deliberately at the very bottom of its own module.
  * DETERMINISTIC CORE, AI ONLY AT THE EDGES. A model extracts statements. A
    deterministic table maps statements to actions. No model ever decides WHETHER
    to act. `planner.py` must never import or call a model.
  * SIM-MODE THROUGH IDENTICAL CODE PATHS. Every executor runs the same code until
    the final HTTP/subprocess call. Sim mode renders the exact payload it would
    have sent and says so on screen.

Run modules with -m from the repo root, e.g.
    cd /path/to/adjourn
    python -m adjourn.orchestrator
Intra-package imports are relative, so running a module by path will not work.
"""

from __future__ import annotations

__all__ = ["PACKAGE_NAME", "VERSION"]

PACKAGE_NAME = "adjourn"
VERSION = "0.1.0"
