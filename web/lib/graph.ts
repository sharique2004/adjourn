/**
 * FalkorDB Cloud reader.
 *
 * The Mac is authoritative and local-first; this process only ever reads the
 * mirror, and only ever with RO queries. Every entry point is wrapped in a hard
 * deadline: a slow or unreachable cloud must degrade to the bundled snapshot
 * fast, never hang a request.
 *
 * Credentials come from the environment only — nothing is committed.
 */

import { FalkorDB, type FalkorDBOptions } from 'falkordb';
import { asBool, asMode, asPayload, asText, linkSupersedes } from './shape';
import type { Dataset } from './demo';
import type { ExecutionRecord, MeetingRecord, StatementRecord } from './types';

const CONNECT_TIMEOUT_MS = 2000;
/** Connect + query + close, end to end. Keeps a hung socket from pinning a request. */
const TOTAL_DEADLINE_MS = 4000;
/** Server-side query cap, so a huge graph cannot stall the deadline. */
const QUERY_TIMEOUT_MS = 2500;
const MAX_EXECUTIONS = 300;
const MAX_STATEMENTS = 600;

export interface CloudConfig {
  host: string;
  port: number;
  username?: string;
  password?: string;
  graph: string;
}

export class NoCredentialsError extends Error {
  constructor() {
    super('FalkorDB Cloud credentials are not configured');
    this.name = 'NoCredentialsError';
  }
}

function env(name: string): string {
  return (process.env[name] ?? '').trim();
}

/** Null when the cloud is not provisioned yet — the expected state before creds land. */
export function cloudConfig(): CloudConfig | null {
  const host = env('FALKORDB_CLOUD_HOST');
  if (!host) return null;

  const port = Number.parseInt(env('FALKORDB_CLOUD_PORT') || '6379', 10);
  return {
    host,
    port: Number.isFinite(port) && port > 0 ? port : 6379,
    username: env('FALKORDB_CLOUD_USERNAME') || undefined,
    password: env('FALKORDB_CLOUD_PASSWORD') || undefined,
    graph: env('FALKORDB_GRAPH') || 'adjourn',
  };
}

export function cloudConfigured(): boolean {
  return cloudConfig() !== null;
}

function deadline<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error instanceof Error ? error : new Error(String(error)));
      },
    );
  });
}

type Row = Record<string, unknown>;
type Reader = (cypher: string, params?: Record<string, string>) => Promise<Row[]>;

/**
 * One TLS connection per request. Serverless functions are short-lived and a
 * pooled Redis socket across invocations is a reliability hazard, not a win.
 */
async function withGraph<T>(work: (read: Reader) => Promise<T>): Promise<T> {
  const config = cloudConfig();
  if (!config) throw new NoCredentialsError();

  const run = async (): Promise<T> => {
    // `reconnectStrategy` is a node-redis socket option that falkordb's own
    // SocketOptions type does not re-declare; it is passed through verbatim.
    // A public read surface should fail over to the snapshot immediately
    // rather than sit in a reconnect loop.
    // FALKORDB_CLOUD_TLS=0 for endpoints that speak plain Redis protocol
    // (verified true of the current free-tier instance); default stays TLS.
    const socket = {
      host: config.host,
      port: config.port,
      tls: (process.env.FALKORDB_CLOUD_TLS ?? '1') !== '0',
      connectTimeout: CONNECT_TIMEOUT_MS,
      reconnectStrategy: false,
    } as unknown as FalkorDBOptions['socket'];

    const db = await FalkorDB.connect({
      socket,
      username: config.username,
      password: config.password,
    });

    try {
      const graph = db.selectGraph(config.graph);
      const read: Reader = async (cypher, params) => {
        const reply = await graph.roQuery<Row>(cypher, {
          params: params ?? {},
          TIMEOUT: QUERY_TIMEOUT_MS,
        });
        return reply.data ?? [];
      };
      return await work(read);
    } finally {
      void db.close().catch(() => undefined);
    }
  };

  return deadline(run(), TOTAL_DEADLINE_MS, 'FalkorDB Cloud read');
}

// --- row mappers ------------------------------------------------------------

function toMeeting(row: Row): MeetingRecord {
  const id = asText(row.id);
  return { id, title: asText(row.title, id), date: asText(row.date) };
}

function toExecution(row: Row): ExecutionRecord {
  return {
    ok: asBool(row.ok),
    kind: asText(row.kind),
    external_id: asText(row.external_id),
    url: asText(row.url),
    human_summary: asText(row.human_summary),
    mode: asMode(row.mode),
    undo_payload: asPayload(row.undo_payload),
    quote: asText(row.quote),
    speaker: asText(row.speaker),
    meeting_id: asText(row.meeting_id),
    fired_at: asText(row.fired_at),
  };
}

function toStatement(row: Row): StatementRecord {
  const issue = asText(row.issue);
  return {
    id: asText(row.id),
    kind: asText(row.kind),
    text: asText(row.text),
    speaker: asText(row.speaker),
    issue: issue || null,
    meeting_id: asText(row.meeting_id),
    said_at: asText(row.said_at),
    supersedes: [],
    superseded_by: [],
  };
}

// --- queries ----------------------------------------------------------------
//
// Property names are read defensively with coalesce(): the Mac writes the
// contract fields, but Person/Issue nodes predate Adjourn (they come from the
// drift graph) and spell their label a couple of different ways.

const MEETINGS_CYPHER = `
MATCH (m:Meeting)
RETURN m.id AS id, m.title AS title, m.date AS date
ORDER BY m.date DESC`;

const EXECUTIONS_CYPHER = `
MATCH (e:Execution)-[:FROM_MEETING]->(m:Meeting)
RETURN e.ok AS ok,
       e.kind AS kind,
       e.external_id AS external_id,
       e.url AS url,
       e.human_summary AS human_summary,
       e.mode AS mode,
       e.undo_payload AS undo_payload,
       e.quote AS quote,
       e.speaker AS speaker,
       coalesce(e.meeting_id, m.id) AS meeting_id,
       e.fired_at AS fired_at
ORDER BY e.fired_at DESC
LIMIT ${MAX_EXECUTIONS}`;

const STATEMENTS_CYPHER = `
MATCH (p:Person)-[:SAID]->(s:Statement)-[:IN_MEETING]->(m:Meeting)
OPTIONAL MATCH (s)-[:ABOUT]->(i:Issue)
RETURN s.id AS id,
       s.kind AS kind,
       coalesce(s.text, s.quote, s.summary, '') AS text,
       coalesce(p.name, p.id, s.speaker, '') AS speaker,
       coalesce(i.key, i.id, i.name, i.title, '') AS issue,
       m.id AS meeting_id,
       coalesce(s.said_at, s.at, s.timestamp, m.date, '') AS said_at
ORDER BY said_at DESC
LIMIT ${MAX_STATEMENTS}`;

const SUPERSEDES_CYPHER = `
MATCH (a:Statement)-[:SUPERSEDES]->(b:Statement)
RETURN a.id AS from_id, b.id AS to_id`;

function toEdges(rows: Row[]): Array<{ from: string; to: string }> {
  return rows
    .map((row) => ({ from: asText(row.from_id), to: asText(row.to_id) }))
    .filter((edge) => edge.from && edge.to);
}

/**
 * Everything the four surfaces need, in one connection. The mirrored graph is
 * small by construction (one meeting produces a handful of nodes), so four
 * bounded reads beat maintaining four separate code paths.
 */
export async function readCloudDataset(): Promise<Dataset> {
  return withGraph(async (read) => {
    const [meetingRows, executionRows, statementRows, edgeRows] = await Promise.all([
      read(MEETINGS_CYPHER),
      read(EXECUTIONS_CYPHER),
      read(STATEMENTS_CYPHER),
      read(SUPERSEDES_CYPHER),
    ]);

    const dataset: Dataset = {
      meetings: meetingRows.map(toMeeting).filter((m) => m.id),
      executions: executionRows.map(toExecution).filter((e) => e.kind),
      statements: linkSupersedes(
        statementRows.map(toStatement).filter((s) => s.id),
        toEdges(edgeRows),
      ),
    };

    // An empty graph renders as an honest empty state — never as fabricated
    // sample data. If the mirror holds nothing, the page says so.
    return dataset;
  });
}
