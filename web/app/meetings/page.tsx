import type { Metadata } from 'next';
import Link from 'next/link';
import { Footer, KindChip, TopBar } from '../components/ui';
import { getMeetings } from '../../lib/read';
import { plural, shortDate } from '../../lib/format';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

export const metadata: Metadata = {
  title: 'Meetings — Adjourn',
  description: 'Every meeting the Mac has mirrored, and what each one caused.',
};

export default async function MeetingsPage() {
  const result = await getMeetings();
  const { meetings } = result.data;

  const statementTotal = meetings.reduce((sum, m) => sum + m.statement_count, 0);
  const liveTotal = meetings.reduce((sum, m) => sum + m.live_count, 0);
  const simTotal = meetings.reduce((sum, m) => sum + m.sim_count, 0);

  return (
    <>
      <TopBar current="/meetings" />
      <main className="shell">
        <div className="page-head">
          <div>
            <h1>Meetings</h1>
            <p className="sub">
              Recording and live captions run on the Mac. This is the mirror — each meeting, and
              what it caused.
            </p>
          </div>
          <div className="right">
            {plural(meetings.length, 'meeting')} · {plural(statementTotal, 'statement')} ·{' '}
            {liveTotal + simTotal} actions{liveTotal > 0 ? ` (${liveTotal} live)` : ''}
          </div>
        </div>

        {meetings.length === 0 ? (
          <p className="empty">
            Nothing mirrored yet. Adjourn a meeting on the Mac and it appears here.
          </p>
        ) : (
          meetings.map(({ meeting, statement_count, live_count, sim_count, kinds }) => (
            <section className="meeting-block" key={meeting.id}>
              <div className="meeting-head">
                <h2>
                  <Link href={`/m/${meeting.id}`}>{meeting.title || meeting.id}</Link>
                </h2>
                <span className="meta">
                  {shortDate(meeting.date)} · {plural(statement_count, 'statement')} ·{' '}
                  {plural(live_count + sim_count, 'action')}
                  {live_count > 0 ? ` · ${live_count} live` : ''}
                  {sim_count > 0 ? ` · ${sim_count} sim` : ''} ·{' '}
                  <Link href={`/m/${meeting.id}`}>recap →</Link>
                </span>
              </div>
              {kinds.length > 0 ? (
                <div className="cards" style={{ flexDirection: 'row', flexWrap: 'wrap', gap: '0.5rem' }}>
                  {kinds.map((kind) => (
                    <KindChip kind={kind} key={kind} />
                  ))}
                </div>
              ) : null}
            </section>
          ))
        )}
      </main>
      <Footer source={result.source} reason={result.reason} />
    </>
  );
}
