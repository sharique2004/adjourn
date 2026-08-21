'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ExecutionCard, MeetingHeading } from '../components/ui';
import { shortTime } from '../../lib/format';
import type { BoardPayload, Sourced } from '../../lib/types';

const POLL_MS = 5000;
/**
 * A board left open on a second screen is usually a background tab. Keep
 * polling there, just far less often — a frozen board that never recovers is
 * worse than a handful of extra requests.
 */
const HIDDEN_POLL_MS = 30000;

/**
 * The board. Server-rendered once, then polled every 5 seconds.
 *
 * Strictly read-only: it renders receipts of things that already happened.
 * There is deliberately no control here — no undo, no retry, no re-fire. Undo
 * lives on the Mac's local board, and a button here would be a lie about what
 * this surface can do.
 */
export default function BoardLive({ initial }: { initial: Sourced<BoardPayload> }) {
  const [payload, setPayload] = useState(initial);
  const [syncedAt, setSyncedAt] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const inFlight = useRef(false);
  const lastPollAt = useRef(0);

  const poll = useCallback(async (force = false) => {
    if (inFlight.current) return;
    const now = Date.now();
    if (!force && document.hidden && now - lastPollAt.current < HIDDEN_POLL_MS) return;

    inFlight.current = true;
    lastPollAt.current = now;

    const controller = new AbortController();
    const abort = setTimeout(() => controller.abort(), POLL_MS - 500);
    try {
      const response = await fetch('/api/executions', {
        cache: 'no-store',
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const next = (await response.json()) as Sourced<BoardPayload>;
      setPayload(next);
      setSyncedAt(next.generated_at);
      setStale(false);
    } catch {
      // Keep the last good render on screen and say so, rather than blanking.
      setStale(true);
    } finally {
      clearTimeout(abort);
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    const timer = setInterval(() => void poll(), POLL_MS);
    const onVisible = () => {
      if (!document.hidden) void poll(true);
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [poll]);

  const { groups, execution_count } = payload.data;
  const live = payload.source === 'cloud' && !stale;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Follow-through</h1>
          <p className="sub">
            {execution_count} {execution_count === 1 ? 'receipt' : 'receipts'} ·{' '}
            {groups.length} {groups.length === 1 ? 'meeting' : 'meetings'} · newest first
          </p>
        </div>
        <div className="right">
          <span className={live ? 'badge badge-live' : 'badge badge-sim'}>
            <span className={live ? 'dot dot-pulse' : 'dot'} aria-hidden />
            {stale ? 'reconnecting' : payload.source === 'cloud' ? 'cloud' : 'snapshot'}
          </span>
          <span>
            polling 5s · synced {shortTime(syncedAt ?? payload.generated_at)} UTC
          </span>
        </div>
      </div>

      {groups.length === 0 ? (
        <p className="empty">No executions mirrored yet.</p>
      ) : (
        groups.map((group) => (
          <section className="meeting-block" key={group.meeting.id}>
            <MeetingHeading meeting={group.meeting} count={group.executions.length} />
            <div className="cards">
              {group.executions.map((execution) => (
                <ExecutionCard
                  key={`${execution.kind}:${execution.external_id}:${execution.fired_at}`}
                  execution={execution}
                />
              ))}
            </div>
          </section>
        ))
      )}

      <p className="empty" style={{ borderBottom: 'none' }}>
        Read-only mirror. Undo and re-fire live on the Mac board.
      </p>
    </>
  );
}
