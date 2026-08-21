/**
 * The shared data contract. The Mac side writes these shapes into FalkorDB
 * Cloud; this web surface only ever reads them. Field names here must match
 * `ExecutorResult` on the Mac exactly — do not rename anything without
 * changing the executor first.
 */

export const EXECUTOR_KINDS = [
  'github_update',
  'linear_create',
  'linear_move',
  'pull_request_stub',
  'pr_review_suggestion',
  'slack_send',
  'email_send',
  'calendar_hold',
  'recap_page',
] as const;

export type ExecutorKind = (typeof EXECUTOR_KINDS)[number];

export const STATEMENT_KINDS = [
  'decision',
  'update',
  'assignment',
  'question',
  'ticket_request',
  'progress_report',
  'message_commitment',
  'email_commitment',
  'deadline',
  'pr_intent',
] as const;

export type StatementKind = (typeof STATEMENT_KINDS)[number];

/** Statement kinds that count as a promise someone owes. Drives /ledger. */
export const COMMITMENT_KINDS: StatementKind[] = [
  'message_commitment',
  'email_commitment',
  'assignment',
  'deadline',
];

export type ExecutionMode = 'live' | 'sim';

/** One flattened (:Execution) node — an ExecutorResult as mirrored to the graph. */
export interface ExecutionRecord {
  ok: boolean;
  kind: ExecutorKind | string;
  external_id: string;
  url: string;
  human_summary: string;
  mode: ExecutionMode;
  /** Stored in the graph as a JSON string; parsed back to a dict on read. */
  undo_payload: Record<string, unknown>;
  quote: string;
  speaker: string;
  meeting_id: string;
  fired_at: string;
}

export interface MeetingRecord {
  id: string;
  title: string;
  date: string;
}

export interface StatementRecord {
  id: string;
  kind: StatementKind | string;
  text: string;
  speaker: string;
  /** (Statement)-[:ABOUT]->(Issue) — the issue key, when there is one. */
  issue: string | null;
  meeting_id: string;
  said_at: string;
  /** Ids of statements this one SUPERSEDES. */
  supersedes: string[];
  /** Ids of statements that SUPERSEDE this one. Non-empty means stale. */
  superseded_by: string[];
}

export interface MeetingGroup {
  meeting: MeetingRecord;
  executions: ExecutionRecord[];
}

export interface CommitmentEntry {
  statement: StatementRecord;
  meeting: MeetingRecord | null;
}

export interface PersonLedger {
  person: string;
  open_count: number;
  superseded_count: number;
  entries: CommitmentEntry[];
}

/** Where the bytes on screen came from. Rendered honestly in the footer. */
export type DataSource = 'cloud' | 'demo';

export interface Sourced<T> {
  source: DataSource;
  /** Present only when the cloud read failed and the snapshot took over. */
  reason?: string;
  generated_at: string;
  data: T;
}

export interface BoardPayload {
  groups: MeetingGroup[];
  execution_count: number;
}

export interface MeetingPayload {
  meeting: MeetingRecord | null;
  executions: ExecutionRecord[];
  statements: StatementRecord[];
  commitments: PersonLedger[];
}

export interface LedgerPayload {
  people: PersonLedger[];
  meetings: MeetingRecord[];
}
