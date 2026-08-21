# Adjourn

**The meeting itself becomes the to-do.** A companion process to MeetingScribe: the
moment a meeting stops, Adjourn extracts what was decided, promised, and reported —
and executes the follow-through.

---

## The one architectural claim

> **A model extracts. A table decides. Nothing else gets a vote.**

`extraction.py` is the only module in this package allowed to call a model. It turns
transcript segments into `Statement` objects and stops there. `planner.py` maps those
statements to `Action` objects through `ROUTING_TABLE` — a plain dict and a handful of
`if`s. **No model ever decides whether to act.** That line is why this can run
autonomously without being alarming, and it is enforced by convention at the top of
`planner.py`.

Three properties hold everywhere:

- **Local-first.** Everything runs on this Mac. The only network calls are the ones an
  executor makes deliberately, at the very bottom of its own module.
- **Deterministic core, AI at the edges.** Same statements in, same actions out.
- **Sim mode through identical code paths.** Every executor runs the same code until the
  final HTTP call or subprocess. With no token, it renders the exact payload it *would*
  have sent and the board badge says `SIM`. The payload is real; only the send is not.

---

## Flow

```
MeetingScribe engine (127.0.0.1:5005, read-only)
        │
   watcher.py ─── /api/status: a NEW jobs key in state "processing"
        │          = the meeting ended, and its id, in one observation
        ▼
 orchestrator.py
        │
        ├── FAST FIRE (~1-2s after stop)
        │     meetingscribe_source.read_live_segments()   ← /api/live snapshot
        │     extraction.extract_statements(source="live")     ← MODEL
        │     planner.plan(statements, memory)                 ← TABLE, no model
        │     executors.execute_action(action)
        │
        └── RECONCILE (when jobs[id].state == "done")
              meetingscribe_source.read_final_segments()  ← meeting.json
              re-extract, re-plan, fire only NEW dedup_keys, upgrade recap quotes
        │
        ▼
  results.append_execution()  →  ~/.meetingscribe/executions.jsonl
  orchestrator pending file    →  adjourn/state/pending.json
  orchestrator pipeline file   →  adjourn/state/pipeline.json
        │
        ▼
  board_server.py — dark board, guts panel, live/sim badge, countdown rings, undo
```

**Dedup is by `planner.build_dedup_key()`, never by `segment_id`** — the live pass and
the final pass number their segments differently, so keying on ids would double-fire
every action. The key is built from meaning (issue number, topic, normalized claim), so
the two passes agree.

**The regret window** is how irreversible sends stay safe. `slack_send` and `email_send`
get `regret_window_s = 60`: the action goes into `state/pending.json` with a `fire_at`
timestamp, the board draws a countdown ring with a cancel button, and only then does it
go out. For email that countdown *is* the undo — `email_send_executor.undo()` returns
`False` and says so, because there is no unsend.

---

## Modules

| File | Owns | State |
|---|---|---|
| `config.py` | paths, ports, defaults, `.env` | **done** |
| `secrets_store.py` | `get_secret()`, `decide_mode()` | **done** |
| `results.py` | `ExecutorResult`, the journal | **done** |
| `planner.py` | `Action`, `ROUTING_TABLE`, `build_dedup_key()` | keys done, routing stub (Lane C) |
| `orchestrator.py` | two-phase loop, `pending.json` | pending file done, loop stub (Lane A) |
| `extraction.py` | `Statement`, claude → gemini → fixtures | stub (Lane B) |
| `watcher.py` | stop-edge detection | stub (Lane A) |
| `meetingscribe_source.py` | read-only transcript access | stub (Lane A) |
| `memory_store.py` | Falkor + SQLite fallback | stub (Lane C) |
| `executors/` | eight executors + lazy registry | registry done, executors stub (Lane D) |

Every stub raises `NotImplementedError` naming the lane that owns it. Nothing silently
returns a fake success.

### Shared contracts other lanes code against

```python
from adjourn.results   import ExecutorResult, append_execution, read_fired_dedup_keys
from adjourn.planner   import Action, ACTION_KINDS, build_dedup_key, choose_regret_window
from adjourn.extraction import Statement, STATEMENT_KINDS
from adjourn.executors import execute_action, undo_result, REGISTRY
from adjourn.orchestrator import read_pending_actions, cancel_pending_action  # board reads these
```

`board_server.py` (Lane E) is a **reader**: `results.read_executions()`,
`results.summarize_executions()`, `results.read_records_since()` for tail-f, and
`orchestrator.read_pending_actions()` for the countdown column. Its undo button calls
`orchestrator.undo_execution(dedup_key)`, which appends an undo record rather than
rewriting history.

---

## Running

Run everything on a virtualenv with `adjourn/requirements.txt` installed.
Intra-package imports are relative, so run modules with `-m` **from the repo root**:

```bash
cd /path/to/adjourn
PY="python"

$PY -m adjourn.config          # print the resolved configuration
$PY -m adjourn.secrets_store   # which secrets exist (booleans only, never values)
$PY -m adjourn.planner         # print the routing table
$PY -m adjourn.results         # running totals from the journal
$PY -m adjourn.watcher         # watch stop edges scroll by
$PY -m adjourn.orchestrator    # the real thing
```

`adjourn/.env` holds non-secret defaults. Secrets go in the Keychain:

```bash
security add-generic-password -a "$USER" -s adjourn.slack_bot_token   -w
security add-generic-password -a "$USER" -s adjourn.linear_api_key    -w
security add-generic-password -a "$USER" -s adjourn.gmail_app_password -w
```

Absent secrets are not an error — they are a mode. `ADJOURN_SIM=1` forces sim everywhere.

---

## Boundaries

- `~/MeetingScribe` is **read-only**. GETs to `/api/status`, `/api/record/status`,
  `/api/live`, and reads under `~/.meetingscribe/recordings`. No mutating POSTs, and
  never `/api/shutdown`. Send no `Origin` header — the engine is loopback-guarded.
- `drift/` is **reference only**. Copy what you need into `adjourn/` with a
  `# ported from drift/<file>` comment. No imports across the package boundary.
- **FalkorDB graph name is `adjourn`**, not `drift` — the Aug 3 hackathon graph stays
  exactly as it is.
- **Live GitHub writes only to `sharique2004/adjourn`** (`config.ALLOWED_GITHUB_REPOS`,
  asserted inside the executors). The `gh` token has no `workflow` scope; never touch
  `.github/workflows`.
- **Never post to the `wellx-ai` Slack workspace** — that is someone's real employer.
  `slack_send_executor.FORBIDDEN_WORKSPACE_NAMES` guards it.
