"""recap_page — write the local recap for the meeting.

The recap is the artifact a human actually keeps: what was decided, what Adjourn
did about it, and the verbatim quote behind every single card. It is written LAST
in a run so it can cite the other executors' results, and rewritten in place
during the reconcile pass with the better final-transcript wording.

Payload shape:
    {
      "meeting_id": "20260821-140312",
      "meeting_title": "Eng sync",
      "statements": [...],            # extraction.Statement dicts
      "executed": [...],              # results.ExecutorResult dicts (or journal records)
      "segment_times": {"L12": 41.2}, # optional; segment_id -> seconds, for mm:ss
      "generated_at": "2026-08-21T14:05:00",
      "human_preview": "Recap: Eng sync — 7 actions"
    }

Live transport: there isn't one. This writes a file to adjourn/recaps/ and that
is the whole point — local-first, no server, openable with a double-click. It
therefore runs "live" even when every other executor is in sim: honest, because
the file really is written.

Undo: restore the previous version from the .bak written on every rewrite, so an
undo after a reconcile pass restores the earlier recap rather than losing it. With
no .bak (the first write), undo deletes the file.

Secrets: none.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
from datetime import datetime
from typing import TYPE_CHECKING

from .. import config, results
from ..planner import normalize_key_text

if TYPE_CHECKING:
    from ..planner import Action

ACTION_KIND = "recap_page"
REQUIRED_SECRETS: tuple[str, ...] = ()

RECAP_FILE_SUFFIX = ".html"
BACKUP_SUFFIX = ".bak"

# Statement kinds that put something on a person's plate. The ledger is built
# from these and nothing else — it is a promise list, not a transcript.
COMMITMENT_KINDS = (
    "assignment",
    "message_commitment",
    "email_commitment",
    "pr_intent",
    "deadline",
    "ticket_request",
)

KIND_LABELS = {
    "github_update": "GitHub",
    "linear_create": "Linear",
    "linear_move": "Linear",
    "pull_request_stub": "Draft PR",
    "slack_send": "Slack",
    "email_send": "Email",
    "calendar_hold": "Calendar",
    "recap_page": "Recap",
}


def recap_path_for(meeting_id: str):
    """adjourn/recaps/<meeting_id>.html — stable, so reconcile rewrites in place."""
    return config.recaps_directory() / f"{meeting_id}{RECAP_FILE_SUFFIX}"


# --- pure rendering ---------------------------------------------------------


def format_minutes_seconds(seconds: object) -> str:
    """41.2 -> '00:41'. Empty string when there is no usable number."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    return f"{total // 60:02d}:{total % 60:02d}"


def build_quote_time_index(payload: dict) -> dict[str, str]:
    """normalized quote -> mm:ss, assembled from the statements and segment times."""
    segment_times = payload.get("segment_times") or {}
    index: dict[str, str] = {}
    for statement in payload.get("statements") or []:
        quote = normalize_key_text(statement.get("quote") or statement.get("claim") or "")
        if not quote:
            continue
        stamp = format_minutes_seconds(
            statement.get("at_seconds", segment_times.get(statement.get("segment_id")))
        )
        if stamp:
            index[quote] = stamp
    return index


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _render_action_card(record: dict, quote_times: dict[str, str]) -> str:
    kind = record.get("kind", "")
    mode = record.get("mode", "sim")
    ok = bool(record.get("ok", False))
    undone = bool(record.get("undone", False))
    dedup_key = record.get("dedup_key", "")
    quote = record.get("quote", "")
    stamp = record.get("at_mmss") or quote_times.get(normalize_key_text(quote), "")
    url = record.get("url")
    status_class = "ok" if ok else "failed"
    if undone:
        status_class += " undone"
    badge_class = "live" if mode == "live" else "sim"

    parts = [
        f'<article class="card {status_class}" data-dedup-key="{_escape(dedup_key)}"'
        f'{" data-undone=\"1\"" if undone else ""}>',
        '  <header class="card-head">',
        f'    <span class="kind">{_escape(KIND_LABELS.get(kind, kind))}</span>',
        f'    <span class="badge {badge_class}">{_escape(mode.upper())}</span>',
        (
            '    <span class="badge failed">FAILED</span>' if not ok else ""
        ),
        ('    <span class="badge undone">UNDONE</span>' if undone else ""),
        "  </header>",
        f'  <p class="summary">{_escape(record.get("human_summary", ""))}</p>',
    ]
    # An undone action's permalink points at a comment that has been deleted, so
    # the URL is printed as TEXT rather than as a link. A recap that still offers
    # a live-looking link into a 404 contradicts the board that struck the card
    # through thirty seconds earlier.
    if url and undone:
        parts.append(f'  <p class="link dead">{_escape(url)} <span class="gone">(removed)</span></p>')
    elif url:
        parts.append(f'  <p class="link"><a href="{_escape(url)}">{_escape(url)}</a></p>')
    if quote:
        speaker = _escape(record.get("speaker", "")) or "unattributed"
        stamp_html = f'<span class="stamp">{_escape(stamp)}</span>' if stamp else ""
        parts += [
            '  <blockquote class="quote">',
            f"    {stamp_html}<span class=\"who\">{speaker}</span>",
            f'    <span class="quote-text" data-dedup-key="{_escape(dedup_key)}">'
            f"{_escape(quote)}</span>",
            "  </blockquote>",
        ]
    parts.append("</article>")
    return "\n".join(part for part in parts if part)


def build_commitment_ledger(payload: dict) -> dict[str, list[dict]]:
    """Per-person promises, in the order they were made. Pure; no model, no guessing.

    A person appears here only because a statement of a committing kind names them
    as the speaker. Nothing is inferred about who "probably" owns something.
    """
    ledger: dict[str, list[dict]] = {}
    for statement in payload.get("statements") or []:
        if statement.get("kind") not in COMMITMENT_KINDS:
            continue
        speaker = (statement.get("speaker") or "").strip() or "Unattributed"
        entity_refs = statement.get("entity_refs") or {}
        ledger.setdefault(speaker, []).append(
            {
                "claim": statement.get("claim", ""),
                "kind": statement.get("kind", ""),
                "topic": statement.get("topic", ""),
                "deadline_text": entity_refs.get("deadline_text", ""),
            }
        )
    return ledger


def _render_ledger(ledger: dict[str, list[dict]]) -> str:
    if not ledger:
        return '<p class="empty">No one promised anything in this meeting.</p>'
    blocks = []
    for person, commitments in sorted(ledger.items()):
        rows = []
        for commitment in commitments:
            deadline = commitment.get("deadline_text")
            due = f' <span class="due">{_escape(deadline)}</span>' if deadline else ""
            rows.append(
                f'    <li><span class="tag">{_escape(commitment["kind"].replace("_", " "))}</span>'
                f"{_escape(commitment['claim'])}{due}</li>"
            )
        blocks.append(
            f'<section class="person">\n'
            f'  <h3>{_escape(person)} <span class="count">{len(commitments)}</span></h3>\n'
            f"  <ul>\n" + "\n".join(rows) + "\n  </ul>\n</section>"
        )
    return "\n".join(blocks)


def _cloud_mirror_clause() -> str:
    """The half-sentence that keeps the privacy footer true.

    `cloud_mirror.EXECUTION_FIELDS` carries `quote`, so when a cloud graph is
    configured one verbatim sentence per fired action is copied off this machine.
    The page used to claim the opposite. It says whichever is actually true now,
    read at render time rather than assumed, because the mirror is a setting.

    An unconfigured mirror (the default, and the state of any cloned checkout)
    adds nothing to the sentence.
    """
    try:
        from .. import cloud_mirror
    except Exception:  # noqa: BLE001 - no falkordb installed is not a mirror
        return ""
    try:
        if not cloud_mirror.is_configured():
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return ", and to the graph behind the public board"


def render_recap_html(payload: dict) -> str:
    """The full recap page as a self-contained HTML string. Pure.

    Dark near-black to match the board. No external CSS and no webfont fetch —
    this file has to open on a plane.
    """
    meeting_title = payload.get("meeting_title") or "Untitled meeting"
    meeting_id = payload.get("meeting_id") or ""
    generated_at = payload.get("generated_at") or datetime.now().isoformat(timespec="seconds")
    executed = list(payload.get("executed") or [])
    quote_times = build_quote_time_index(payload)

    live_count = sum(1 for record in executed if record.get("mode") == "live")
    sim_count = sum(1 for record in executed if record.get("mode") != "live")
    failed_count = sum(1 for record in executed if not record.get("ok", False))
    undone_count = sum(1 for record in executed if record.get("undone", False))
    segment_count = int(payload.get("segment_count") or 0)
    statement_count = len(payload.get("statements") or [])
    mirror_clause = _cloud_mirror_clause()

    # A meeting that produced nothing gets a page that SAYS so, with the numbers.
    # A blank board is indistinguishable from a crashed pipeline; "41 segments,
    # 0 commitments, nothing to do" is the strongest claim this product makes,
    # written down.
    if executed:
        cards = "\n".join(_render_action_card(record, quote_times) for record in executed)
    elif segment_count or statement_count:
        cards = (
            '<p class="empty">Nothing followed from this meeting. '
            f"{segment_count} line{'' if segment_count == 1 else 's'} were spoken and "
            f"{statement_count} carried a commitment or a decision; the table declined "
            "all of them. That is the system working, not the system failing.</p>"
        )
    else:
        cards = '<p class="empty">Nothing fired for this meeting.</p>'
    ledger = _render_ledger(build_commitment_ledger(payload))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(meeting_title)} — Adjourn recap</title>
<style>
  :root {{
    --ink: #0a0b0d;
    --panel: #131416;
    --line: rgba(255, 255, 255, 0.08);
    --text: #ecece8;
    --muted: #8b8f98;
    --live: #4ade80;
    --sim: #b9c0cb;
    --failed: #f0616d;
    --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
    --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, "Segoe UI", sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 3rem 1.25rem 5rem;
    background: var(--ink); color: var(--text);
    font-family: var(--sans); font-size: 15px; line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }}
  main {{ max-width: 60rem; margin: 0 auto; }}
  header.meeting {{ border-bottom: 1px solid var(--line); padding-bottom: 1.5rem; margin-bottom: 2.5rem; }}
  header.meeting h1 {{ font-size: 1.75rem; margin: 0 0 .4rem; letter-spacing: -.02em; font-weight: 600; }}
  .meta {{ color: var(--muted); font-family: var(--mono); font-size: .8rem; }}
  .totals {{ margin-top: 1rem; display: flex; flex-wrap: wrap; gap: .5rem; }}
  .total {{ border: 0; border-radius: 0; padding: 0; font-size: .8rem; color: var(--muted); }}
  .total b {{ color: var(--text); font-weight: 600; }}
  h2 {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .14em; color: var(--muted);
        margin: 3rem 0 1rem; font-weight: 500; }}
  .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
           padding: 1rem 1.15rem; margin-bottom: .85rem; box-shadow: inset 4px 0 0 var(--live); }}
  .card.failed {{ box-shadow: inset 4px 0 0 var(--failed); }}
  .card.undone {{ opacity: .55; box-shadow: inset 4px 0 0 var(--muted); }}
  .card.undone .summary {{ text-decoration: line-through; text-decoration-thickness: 1px; }}
  .badge.undone {{ color: var(--muted); }}
  .link.dead {{ color: var(--muted); font-size: .78rem; word-break: break-all; }}
  .gone {{ font-style: italic; }}
  .card-head {{ display: flex; align-items: center; gap: .55rem; margin-bottom: .5rem; }}
  .kind {{ font-size: .72rem; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }}
  .badge {{ font-size: .7rem; letter-spacing: .08em; padding: 0; border-radius: 0;
            border: 0; }}
  .badge.live {{ color: var(--live); }}
  .badge.sim {{ color: var(--sim); }}
  .badge.failed {{ color: var(--failed); }}
  .summary {{ margin: 0; font-size: 1.05rem; font-weight: 560; letter-spacing: -.015em; }}
  .link a {{ color: var(--muted); font-size: .78rem; text-decoration: none; word-break: break-all; }}
  .link a:hover {{ color: var(--text); }}
  blockquote.quote {{ margin: .8rem 0 0; padding: 0; background: transparent;
                      border: 0; font-family: var(--serif); font-style: italic;
                      font-size: .95rem; color: var(--muted); }}
  .stamp {{ color: var(--muted); margin-right: .5rem; font-family: var(--mono); font-size: .75rem; }}
  .who {{ color: var(--live); margin-right: .5rem; }}
  .card.failed .who {{ color: var(--failed); }}
  .person {{ border-top: 1px solid var(--line); padding-top: 1rem; margin-top: 1rem; }}
  .person h3 {{ font-size: .95rem; margin: 0 0 .5rem; }}
  .count {{ color: var(--muted); font-size: .75rem; margin-left: .35rem; }}
  .person ul {{ margin: 0; padding-left: 1.1rem; }}
  .person li {{ margin-bottom: .35rem; }}
  .tag {{ color: var(--muted); font-size: .7rem; text-transform: uppercase; letter-spacing: .08em;
          margin-right: .5rem; }}
  .due {{ color: var(--sim); }}
  .empty {{ color: var(--muted); font-family: var(--serif); font-style: italic; }}
  footer {{ margin-top: 4rem; padding-top: 1.25rem; border-top: 1px solid var(--line);
            color: var(--muted); font-size: .75rem; }}
</style>
</head>
<body>
<main>
  <header class="meeting">
    <h1>{_escape(meeting_title)}</h1>
    <div class="meta">meeting {_escape(meeting_id)} · recap written {_escape(generated_at)}</div>
    <div class="totals">
      <span class="total"><b>{len(executed)}</b> actions</span>
      <span class="total"><b>{live_count}</b> live</span>
      <span class="total"><b>{sim_count}</b> simulated</span>
      <span class="total"><b>{failed_count}</b> failed</span>
      {f'<span class="total"><b>{undone_count}</b> undone</span>' if undone_count else ""}
      {f'<span class="total"><b>{segment_count}</b> lines spoken</span>' if segment_count else ""}
    </div>
  </header>

  <h2>What Adjourn did</h2>
  {cards}

  <h2>Who owes what</h2>
  {ledger}

  <footer>
    Written locally by Adjourn when this meeting ended. The audio, the full transcript and
    this page never left this machine. Every card above cites the sentence that caused it —
    and that one sentence is the only text that travels: to the service the card wrote to{mirror_clause}.
  </footer>
</main>
</body>
</html>
"""


# --- execute ----------------------------------------------------------------


def _write_atomically(path, text: str) -> bool:
    """Temp file + os.replace, the same discipline meeting.json uses. True if a .bak was kept."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backed_up = False
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + BACKUP_SUFFIX))
        backed_up = True
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return backed_up


def execute(action: Action) -> results.ExecutorResult:
    """Render the recap and write it atomically, backing up any previous version."""
    payload = dict(action.payload or {})
    meeting_id = str(payload.get("meeting_id") or action.meeting_id or "unknown-meeting")
    payload.setdefault("meeting_id", meeting_id)
    path = recap_path_for(meeting_id)

    try:
        document = render_recap_html(payload)
        backed_up = _write_atomically(path, document)
    except Exception as error:  # noqa: BLE001 — a recap failure is a red card, not a crash
        return results.ExecutorResult.failed(
            ACTION_KIND, f"could not write the recap: {error}",
            quote=action.quote, speaker=action.speaker, meeting_id=meeting_id,
        )

    action_count = len(payload.get("executed") or [])
    return results.ExecutorResult(
        ok=True,
        kind=ACTION_KIND,
        external_id=meeting_id,
        url=path.as_uri(),
        human_summary=(
            f"wrote the local recap for “{payload.get('meeting_title', meeting_id)}” "
            f"({action_count} actions)"
        ),
        # Honestly live: the file really is on disk, whatever mode everything else ran in.
        mode="live",
        undo_payload={"path": str(path), "had_previous_version": backed_up},
        quote=action.quote,
        speaker=action.speaker,
        meeting_id=meeting_id,
    )


def undo(result: results.ExecutorResult) -> bool:
    """Restore the .bak if there is one, otherwise delete the recap."""
    if result.is_simulated:
        return True
    undo_payload = result.undo_payload or {}
    raw_path = undo_payload.get("path")
    if not raw_path:
        return False
    from pathlib import Path

    path = Path(raw_path)
    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    try:
        if undo_payload.get("had_previous_version") and backup.exists():
            os.replace(backup, path)
            return True
        if path.exists():
            path.unlink()
        return True
    except OSError as error:
        print(f"[{ACTION_KIND}] could not undo the recap write: {error}")
        return False


def upgrade_quotes_in_recap(meeting_id: str, better_quotes: dict[str, str]) -> bool:
    """Reconcile support: swap live-caption quotes for final-transcript wording.

    Keyed by dedup_key so a quote improves in place instead of appending a second
    copy of the same action. Returns True when the file was rewritten.
    """
    path = recap_path_for(meeting_id)
    if not path.exists() or not better_quotes:
        return False
    try:
        document = path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"[{ACTION_KIND}] could not read the recap: {error}")
        return False

    updated = document
    for dedup_key, better_quote in better_quotes.items():
        pattern = re.compile(
            r'(<span class="quote-text" data-dedup-key="'
            + re.escape(html.escape(dedup_key, quote=True))
            + r'">)(.*?)(</span>)',
            re.DOTALL,
        )
        updated = pattern.sub(
            lambda match: match.group(1) + html.escape(better_quote, quote=True) + match.group(3),
            updated,
        )
    if updated == document:
        return False
    try:
        _write_atomically(path, updated)
    except OSError as error:
        print(f"[{ACTION_KIND}] could not rewrite the recap: {error}")
        return False
    return True


if __name__ == "__main__":
    example = {
        "meeting_id": "demo",
        "meeting_title": "Adjourn recap sample",
        "statements": [],
        "executed": [],
    }
    print(json.dumps({"bytes": len(render_recap_html(example))}, indent=2))
