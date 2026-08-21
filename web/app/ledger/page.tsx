import type { Metadata } from 'next';
import { Footer, StatementRow, TopBar } from '../components/ui';
import { getLedger } from '../../lib/read';
import { COMMITMENT_KINDS } from '../../lib/types';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

export const metadata: Metadata = {
  title: 'Commitment ledger — Adjourn',
  description:
    'Every commitment anyone made, across meetings: messages, emails, assignments, deadlines.',
};

export default async function LedgerPage() {
  const ledger = await getLedger();
  const { people, meetings } = ledger.data;

  const total = people.reduce((sum, p) => sum + p.entries.length, 0);
  const open = people.reduce((sum, p) => sum + p.open_count, 0);
  const meetingsById = new Map(meetings.map((m) => [m.id, m]));

  return (
    <>
      <TopBar current="/ledger" />
      <main className="shell">
        <div className="page-head">
          <div>
            <h1>Commitment ledger</h1>
            <p className="sub">
              {COMMITMENT_KINDS.join(' · ')} — grouped by who said it, across every meeting
            </p>
          </div>
        </div>

        <div className="stat-row">
          <span>
            people <b>{people.length}</b>
          </span>
          <span>
            commitments <b>{total}</b>
          </span>
          <span>
            open <b>{open}</b>
          </span>
          <span>
            meetings <b>{meetings.length}</b>
          </span>
        </div>

        {people.length === 0 ? (
          <p className="empty">No commitments in the graph yet.</p>
        ) : (
          people.map((person) => (
            <section className="person-block" key={person.person}>
              <div className="person-head">
                <h2>{person.person}</h2>
                <span className="counts">
                  <span className="open">{person.open_count} open</span>
                  {person.superseded_count > 0 ? (
                    <span>{person.superseded_count} superseded</span>
                  ) : null}
                </span>
              </div>
              <div className="timeline">
                {person.entries.map((entry) => (
                  <StatementRow
                    key={entry.statement.id}
                    statement={entry.statement}
                    showMeeting={
                      entry.meeting ?? meetingsById.get(entry.statement.meeting_id) ?? null
                    }
                  />
                ))}
              </div>
            </section>
          ))
        )}

        <p className="empty" style={{ borderBottom: 'none' }}>
          A commitment leaves this ledger when a later statement supersedes it. Nothing is deleted.
        </p>
      </main>
      <Footer source={ledger.source} reason={ledger.reason} />
    </>
  );
}
