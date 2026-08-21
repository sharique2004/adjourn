/**
 * The single read path for every surface. Try the cloud mirror; on any failure
 * — no credentials, TLS refused, timeout, empty graph — serve the bundled
 * snapshot and mark the payload `demo` so the footer can say so.
 *
 * Server components and API routes both call these, so the board's first paint
 * and its 5s poll always agree about where the data came from.
 */

import type { Dataset } from './demo';
import { cloudConfigured, readCloudDataset } from './graph';

/** No fabricated fallback data — an unreachable mirror renders as empty. */
function emptyDataset(): Dataset {
  return { executions: [], meetings: [], statements: [] };
}
import { buildLedger, byFiredAtDesc, groupByMeeting } from './shape';
import type {
  BoardPayload,
  DataSource,
  LedgerPayload,
  MeetingPayload,
  Sourced,
} from './types';

interface Loaded {
  dataset: Dataset;
  source: DataSource;
  reason?: string;
}

async function load(): Promise<Loaded> {
  if (!cloudConfigured()) {
    return {
      dataset: emptyDataset(),
      source: 'demo',
      reason: 'FALKORDB_CLOUD_HOST is not set',
    };
  }
  try {
    return { dataset: await readCloudDataset(), source: 'cloud' };
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    // Logged, not surfaced verbatim to the browser beyond a short reason string.
    console.warn(`[adjourn] cloud read failed, rendering empty state: ${reason}`);
    return { dataset: emptyDataset(), source: 'demo', reason };
  }
}

function wrap<T>(loaded: Loaded, data: T): Sourced<T> {
  return {
    source: loaded.source,
    reason: loaded.reason,
    generated_at: new Date().toISOString(),
    data,
  };
}

export async function getBoard(): Promise<Sourced<BoardPayload>> {
  const loaded = await load();
  const { executions, meetings } = loaded.dataset;
  return wrap(loaded, {
    groups: groupByMeeting(executions, meetings),
    execution_count: executions.length,
  });
}

export async function getMeeting(meetingId: string): Promise<Sourced<MeetingPayload>> {
  const loaded = await load();
  const { executions, meetings, statements } = loaded.dataset;

  const meeting = meetings.find((m) => m.id === meetingId) ?? null;
  const meetingExecutions = executions
    .filter((e) => e.meeting_id === meetingId)
    .sort(byFiredAtDesc);
  const meetingStatements = statements
    .filter((s) => s.meeting_id === meetingId)
    .sort((a, b) => (a.said_at || '').localeCompare(b.said_at || ''));

  return wrap(loaded, {
    meeting,
    executions: meetingExecutions,
    statements: meetingStatements,
    commitments: buildLedger(meetingStatements, meetings),
  });
}

export async function getLedger(): Promise<Sourced<LedgerPayload>> {
  const loaded = await load();
  const { meetings, statements } = loaded.dataset;
  return wrap(loaded, {
    people: buildLedger(statements, meetings),
    meetings,
  });
}

export interface MeetingSummary {
  meeting: import('./types').MeetingRecord;
  statement_count: number;
  live_count: number;
  sim_count: number;
  /** Distinct executor kinds that fired for this meeting, board order. */
  kinds: string[];
}

export interface MeetingsPayload {
  meetings: MeetingSummary[];
}

/**
 * The Meetings tab of the public mirror: every meeting the Mac has mirrored,
 * newest first, with what each one caused. Recording and live captions run
 * on-device — this page only ever shows what already happened.
 */
export async function getMeetings(): Promise<Sourced<MeetingsPayload>> {
  const loaded = await load();
  const { executions, meetings, statements } = loaded.dataset;

  const summaries = meetings
    .map((meeting) => {
      const fired = executions.filter((e) => e.meeting_id === meeting.id);
      return {
        meeting,
        statement_count: statements.filter((s) => s.meeting_id === meeting.id).length,
        live_count: fired.filter((e) => e.mode === 'live').length,
        sim_count: fired.filter((e) => e.mode === 'sim').length,
        kinds: [...new Set(fired.map((e) => e.kind))],
      };
    })
    .sort((a, b) => (b.meeting.date || '').localeCompare(a.meeting.date || ''));

  return wrap(loaded, { meetings: summaries });
}

/** Meeting ids that exist in the current source — used to 404 honestly. */
export async function knownMeetingIds(): Promise<string[]> {
  const loaded = await load();
  return loaded.dataset.meetings.map((m) => m.id);
}
