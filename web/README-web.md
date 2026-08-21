# Adjourn — web surface

The public, read-only view over the graph the Mac mirrors to FalkorDB Cloud.

The Mac is authoritative. Extraction, execution, the 60-second regret window and
undo all happen locally and keep working with the network down. This app reads
the mirror and can neither fire an action nor reverse one — that is deliberate,
and no page here renders a control that implies otherwise.

## Pages

| Route            | What it shows                                                                             |
| ---------------- | ----------------------------------------------------------------------------------------- |
| `/`              | Landing: what fires when a meeting ends, the eight executor kinds, the topology.           |
| `/board`         | Follow-through board. Execution receipts grouped by meeting, newest first, polled every 5s. |
| `/m/[meetingId]` | Recap: meeting header, executions, statement timeline, per-person commitments.              |
| `/ledger`        | Cross-meeting commitment ledger, per person.                                                |
| `/api/executions`| JSON the board polls. Always 200 — a cloud failure is a `source: "demo"` payload.            |
| `/api/health`    | Deploy check: is this instance on the cloud graph or the snapshot? Reports host, never creds.|

## Environment variables

Set these in the Vercel project (Production + Preview) or in `web/.env.local`
for local work. `.env.example` is the template; nothing secret is committed.

| Variable                  | Required | Default   | Notes                                                                 |
| ------------------------- | -------- | --------- | --------------------------------------------------------------------- |
| `FALKORDB_CLOUD_HOST`     | yes\*    | —         | FalkorDB Cloud hostname. **Absent ⇒ the app runs in demo-data mode.**  |
| `FALKORDB_CLOUD_PORT`     | no       | `6379`    | Cloud instances usually hand out a non-default port — set it.          |
| `FALKORDB_CLOUD_USERNAME` | no       | —         | ACL username. Omit for a password-only instance.                       |
| `FALKORDB_CLOUD_PASSWORD` | yes\*    | —         | ACL password. Never commit it; Vercel env var only.                    |
| `FALKORDB_GRAPH`          | no       | `adjourn` | Graph name. Must match the Mac's `GRAPH_NAME`.                         |

\* Required only for live data. With `FALKORDB_CLOUD_HOST` unset the app serves
`demo-snapshot.json` and tags every page footer `demo data`.

TLS is always on for the cloud connection — there is no plaintext option here.

## Demo-data mode

The app falls back to the bundled snapshot, and says so in the footer, when any
of these happen:

- `FALKORDB_CLOUD_HOST` is not set (the expected state before creds are provisioned);
- the TLS connect fails or exceeds the 2s connect timeout;
- the whole read exceeds the 4s end-to-end deadline, or a query exceeds 2.5s server-side;
- authentication is rejected;
- the graph is reachable but holds no executions and no statements.

The fallback is never silent: the footer shows a grey `demo data` tag, and
`/api/health` reports `source` plus the failure `reason`. Demo rows are
fabricated content about the `adjourn` project — two meetings, twelve
executions covering all eight kinds, seventeen statements and one `SUPERSEDES`
pair.

## Data contract

Written by the Mac, read here verbatim. Renaming a field on either side breaks
the other.

`(:Execution)` — one flattened `ExecutorResult`:
`ok`, `kind`, `external_id`, `url`, `human_summary`, `mode` (`live` | `sim`),
`undo_payload` (JSON string), `quote`, `speaker`, `meeting_id`, `fired_at` (ISO).

Kinds: `github_update`, `linear_create`, `linear_move`, `pull_request_stub`,
`slack_send`, `email_send`, `calendar_hold`, `recap_page`.

Graph shape:

```
(Person)-[:SAID]->(Statement)-[:ABOUT]->(Issue)
(Statement)-[:IN_MEETING]->(Meeting {id, title, date})
(Statement)-[:SUPERSEDES]->(Statement)
(Execution)-[:FROM_MEETING]->(Meeting)
```

Statement kinds: `decision`, `update`, `assignment`, `question`,
`ticket_request`, `progress_report`, `message_commitment`, `email_commitment`,
`deadline`, `pr_intent`. The ledger counts the four commitment kinds:
`message_commitment`, `email_commitment`, `assignment`, `deadline`.

`undo_payload` is stored as a JSON string and parsed on read; it is never
rendered — this surface has no undo.

## Local development

```bash
cd web
npm install
npm run dev        # http://localhost:3000, demo data unless .env.local is filled in
npm run build      # production build
npm run lint       # tsc --noEmit
```

Verified locally on Node v23.11.0 with Next 15.5.

## Deploy notes (Vercel)

- Root directory: `web`. Framework preset: Next.js. No `vercel.json` is needed —
  the defaults are correct for this app.
- Every route that touches data is `force-dynamic` with `Cache-Control:
  no-store`. Nothing about a live board should ever be served from a CDN cache.
- The FalkorDB client speaks the Redis protocol over TCP, so all data routes pin
  `runtime = 'nodejs'`. Do not move them to the edge runtime; the connection will
  not open there.
- `falkordb` is listed in `serverExternalPackages` so the bundler leaves
  `node:net` / `node:tls` alone.
- One TLS connection is opened per request and closed in a `finally`. That is
  intentional for short-lived serverless invocations — a pooled Redis socket
  across cold-started functions is a reliability hazard, not a saving.
- Set the env vars **before** the first production deploy if you want live data
  on day one; otherwise the site deploys honestly in demo mode and starts
  reading the graph as soon as the vars land and the next deploy (or redeploy)
  picks them up.
- After deploying, `curl https://<host>/api/health` — it reports `source`,
  `configured`, the host it is pointed at and the row counts, with no credentials
  in the response.
