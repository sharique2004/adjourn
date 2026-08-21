/**
 * Shared presentational pieces. All of these are plain function components with
 * no state — the board's polling wrapper is the only client component in the
 * app, and it renders these.
 *
 * Nothing here offers control. Undo lives on the Mac's local board; this
 * surface must never render a button that looks like it could reverse a fire.
 */

import Link from 'next/link';
import { kindLabel, linkLabel, shortDate, shortTime } from '../../lib/format';
import type {
  DataSource,
  ExecutionRecord,
  MeetingRecord,
  StatementRecord,
} from '../../lib/types';

export function KindChip({ kind }: { kind: string }) {
  return <span className={`chip chip-kind-${kind}`}>{kindLabel(kind)}</span>;
}

export function StatementChip({ kind }: { kind: string }) {
  return <span className={`chip chip-stmt-${kind}`}>{kindLabel(kind)}</span>;
}

export function ModeBadge({ mode, ok }: { mode: 'live' | 'sim'; ok: boolean }) {
  if (!ok) {
    return (
      <span className="badge badge-failed">
        <span className="dot" aria-hidden />
        failed
      </span>
    );
  }
  if (mode === 'live') {
    return (
      <span className="badge badge-live" title="Fired against the real service">
        <span className="dot" aria-hidden />
        live
      </span>
    );
  }
  return (
    <span className="badge badge-sim" title="Simulated — no external write was made">
      sim
    </span>
  );
}

export function ExecutionCard({ execution }: { execution: ExecutionRecord }) {
  const external = execution.url && !execution.url.startsWith('/');
  return (
    <article className="card">
      <div className="card-rail">
        <KindChip kind={execution.kind} />
        <ModeBadge mode={execution.mode} ok={execution.ok} />
        <time className="card-time" dateTime={execution.fired_at}>
          {shortTime(execution.fired_at)} UTC
        </time>
      </div>
      <div className="card-body">
        <p className="card-summary">{execution.human_summary}</p>
        {execution.quote ? (
          <blockquote className="card-quote">
            “{execution.quote}”
            <span className="speaker">{execution.speaker || 'unattributed'}</span>
          </blockquote>
        ) : null}
        <div className="card-refs">
          {execution.external_id ? (
            <span className="id">{execution.external_id}</span>
          ) : null}
          {execution.url ? (
            external ? (
              <a href={execution.url} target="_blank" rel="noreferrer noopener">
                {linkLabel(execution.url)}
              </a>
            ) : (
              <Link href={execution.url}>{linkLabel(execution.url)}</Link>
            )
          ) : null}
        </div>
      </div>
    </article>
  );
}

export function MeetingHeading({
  meeting,
  count,
  linked = true,
}: {
  meeting: MeetingRecord;
  count: number;
  linked?: boolean;
}) {
  return (
    <div className="meeting-head">
      <h2>
        {linked ? <Link href={`/m/${encodeURIComponent(meeting.id)}`}>{meeting.title}</Link> : meeting.title}
      </h2>
      <span className="meta">{shortDate(meeting.date)}</span>
      <span className="meta">
        {count} {count === 1 ? 'execution' : 'executions'}
      </span>
      {linked ? (
        <span className="meta" style={{ marginLeft: 'auto' }}>
          <Link href={`/m/${encodeURIComponent(meeting.id)}`}>recap →</Link>
        </span>
      ) : null}
    </div>
  );
}

export function StatementRow({
  statement,
  showMeeting,
}: {
  statement: StatementRecord;
  showMeeting?: MeetingRecord | null;
}) {
  const superseded = statement.superseded_by.length > 0;
  return (
    <div className={`stmt${superseded ? ' superseded' : ''}`}>
      <div className="stmt-rail">
        <StatementChip kind={statement.kind} />
        <span className="card-time">{shortTime(statement.said_at)} UTC</span>
      </div>
      <div>
        <p className="stmt-text">{statement.text}</p>
        <div className="stmt-meta">
          <span>{statement.speaker || 'unattributed'}</span>
          {statement.issue ? <span>{statement.issue}</span> : null}
          {showMeeting ? (
            <Link href={`/m/${encodeURIComponent(showMeeting.id)}`}>
              {showMeeting.title}
            </Link>
          ) : null}
          {superseded ? (
            <span className="note-superseded">
              superseded by {statement.superseded_by.join(', ')}
            </span>
          ) : null}
          {statement.supersedes.length > 0 ? (
            <span>supersedes {statement.supersedes.join(', ')}</span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function TopBar({ current }: { current: string }) {
  const links: Array<[string, string]> = [
    ['/meetings', 'meetings'],
    ['/board', 'follow-through'],
    ['/ledger', 'ledger'],
  ];
  return (
    <header className="topbar">
      <div className="topbar-inner">
        <Link href="/" className="wordmark">
          adjourn
        </Link>
        <nav className="topnav">
          {links.map(([href, label]) => (
            <Link key={href} href={href} aria-current={href === current ? 'page' : undefined}>
              {label}
            </Link>
          ))}
        </nav>
      </div>
    </header>
  );
}

/**
 * The honest footer. When the cloud mirror could not be read we say "demo data"
 * plainly rather than dressing a snapshot up as live state.
 */
export function Footer({ source, reason }: { source: DataSource; reason?: string }) {
  return (
    <footer className="footer">
      <div className="footer-inner">
        <span>adjourn — the meeting is the to-do</span>
        <span className="spacer" />
        {source === 'demo' ? (
          <span className="tag-demo" title={reason ? `cloud read: ${reason}` : undefined}>
            mirror unreachable
          </span>
        ) : (
          <span>reading falkordb cloud · graph: adjourn</span>
        )}
      </div>
    </footer>
  );
}
