/**
 * Pure shaping helpers. Both the cloud reader and the demo snapshot produce the
 * same flat record arrays, then hand them to these functions — so the two paths
 * cannot drift apart in how a board group or a ledger row is built.
 */

import {
  COMMITMENT_KINDS,
  type CommitmentEntry,
  type ExecutionRecord,
  type ExecutionMode,
  type MeetingGroup,
  type MeetingRecord,
  type PersonLedger,
  type StatementRecord,
} from './types';

export function asText(value: unknown, fallback = ''): string {
  if (value === null || value === undefined) return fallback;
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return fallback;
}

export function asBool(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') return ['true', '1', 'yes', 'on'].includes(value.toLowerCase());
  return false;
}

export function asMode(value: unknown): ExecutionMode {
  return asText(value).toLowerCase() === 'live' ? 'live' : 'sim';
}

/** undo_payload crosses the wire as a JSON string. Never throw on bad JSON. */
export function asPayload(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value === 'string' && value.trim()) {
    try {
      const parsed: unknown = JSON.parse(value);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return { raw: value };
    }
  }
  return {};
}

/** Newest first. Records with no fired_at sort last rather than blowing up. */
export function byFiredAtDesc(a: ExecutionRecord, b: ExecutionRecord): number {
  return (b.fired_at || '').localeCompare(a.fired_at || '');
}

export function groupByMeeting(
  executions: ExecutionRecord[],
  meetings: MeetingRecord[],
): MeetingGroup[] {
  const known = new Map(meetings.map((m) => [m.id, m]));
  const buckets = new Map<string, ExecutionRecord[]>();

  for (const execution of [...executions].sort(byFiredAtDesc)) {
    const bucket = buckets.get(execution.meeting_id);
    if (bucket) bucket.push(execution);
    else buckets.set(execution.meeting_id, [execution]);
  }

  const groups: MeetingGroup[] = [];
  for (const [meetingId, rows] of buckets) {
    groups.push({
      meeting: known.get(meetingId) ?? { id: meetingId, title: meetingId, date: '' },
      executions: rows,
    });
  }

  // Meetings order by their most recent execution, which is what a board wants.
  groups.sort((a, b) =>
    (b.executions[0]?.fired_at || '').localeCompare(a.executions[0]?.fired_at || ''),
  );
  return groups;
}

export function isCommitment(statement: StatementRecord): boolean {
  return (COMMITMENT_KINDS as string[]).includes(statement.kind);
}

/**
 * Per-person commitment ledger. A statement that has been SUPERSEDED is still
 * listed — it is history, not a lie — but counted separately from open ones.
 */
export function buildLedger(
  statements: StatementRecord[],
  meetings: MeetingRecord[],
): PersonLedger[] {
  const known = new Map(meetings.map((m) => [m.id, m]));
  const buckets = new Map<string, CommitmentEntry[]>();

  const commitments = statements
    .filter(isCommitment)
    .sort((a, b) => (b.said_at || '').localeCompare(a.said_at || ''));

  for (const statement of commitments) {
    const person = statement.speaker || 'unattributed';
    const entry: CommitmentEntry = {
      statement,
      meeting: known.get(statement.meeting_id) ?? null,
    };
    const bucket = buckets.get(person);
    if (bucket) bucket.push(entry);
    else buckets.set(person, [entry]);
  }

  const people: PersonLedger[] = [];
  for (const [person, entries] of buckets) {
    const superseded = entries.filter((e) => e.statement.superseded_by.length > 0).length;
    people.push({
      person,
      open_count: entries.length - superseded,
      superseded_count: superseded,
      entries,
    });
  }

  people.sort((a, b) => b.open_count - a.open_count || a.person.localeCompare(b.person));
  return people;
}

/** Attach both directions of SUPERSEDES onto the statement records. */
export function linkSupersedes(
  statements: StatementRecord[],
  edges: Array<{ from: string; to: string }>,
): StatementRecord[] {
  const index = new Map(statements.map((s) => [s.id, s]));
  for (const edge of edges) {
    const newer = index.get(edge.from);
    const older = index.get(edge.to);
    if (newer && !newer.supersedes.includes(edge.to)) newer.supersedes.push(edge.to);
    if (older && !older.superseded_by.includes(edge.from)) older.superseded_by.push(edge.from);
  }
  return statements;
}
