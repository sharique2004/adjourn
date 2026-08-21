import type { Metadata } from 'next';
import { notFound } from 'next/navigation';
import { ExecutionCard, Footer, StatementChip, StatementRow, TopBar } from '../../components/ui';
import { getMeeting } from '../../../lib/read';
import { shortDate } from '../../../lib/format';
import { COMMITMENT_KINDS } from '../../../lib/types';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

type Params = { params: Promise<{ meetingId: string }> };

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { meetingId } = await params;
  const { data } = await getMeeting(decodeURIComponent(meetingId));
  const title = data.meeting?.title ?? 'Meeting';
  return {
    title: `${title} — Adjourn`,
    description: `Recap: ${data.executions.length} executions, ${data.statements.length} statements.`,
  };
}

export default async function MeetingPage({ params }: Params) {
  const { meetingId } = await params;
  const recap = await getMeeting(decodeURIComponent(meetingId));
  const { meeting, executions, statements, commitments } = recap.data;

  // A meeting with no trace at all in the current source is a 404, not an
  // empty page pretending the meeting existed.
  if (!meeting && executions.length === 0 && statements.length === 0) notFound();

  const header = meeting ?? { id: decodeURIComponent(meetingId), title: decodeURIComponent(meetingId), date: '' };
  const decisions = statements.filter((s) => s.kind === 'decision').length;
  const questions = statements.filter((s) => s.kind === 'question').length;
  const commitmentCount = statements.filter((s) =>
    (COMMITMENT_KINDS as string[]).includes(s.kind),
  ).length;
  const superseded = statements.filter((s) => s.superseded_by.length > 0).length;
  const live = executions.filter((e) => e.mode === 'live').length;

  return (
    <>
      <TopBar current="/board" />
      <main className="shell">
        <div className="page-head">
          <div>
            <h1>{header.title}</h1>
            <p className="sub">
              {shortDate(header.date)} · {header.id}
            </p>
          </div>
        </div>

        <div className="stat-row">
          <span>
            executions <b>{executions.length}</b> ({live} live)
          </span>
          <span>
            decisions <b>{decisions}</b>
          </span>
          <span>
            commitments <b>{commitmentCount}</b>
          </span>
          <span>
            unanswered <b>{questions}</b>
          </span>
          <span>
            superseded <b>{superseded}</b>
          </span>
        </div>

        <section className="section">
          <div className="section-head">
            <h2>Executed</h2>
            <span className="note">newest first</span>
          </div>
          {executions.length === 0 ? (
            <p className="empty">Nothing fired for this meeting.</p>
          ) : (
            <div className="cards">
              {executions.map((execution) => (
                <ExecutionCard
                  key={`${execution.kind}:${execution.external_id}:${execution.fired_at}`}
                  execution={execution}
                />
              ))}
            </div>
          )}
        </section>

        <section className="section">
          <div className="section-head">
            <h2>Statements</h2>
            <span className="note">in order</span>
          </div>
          {statements.length === 0 ? (
            <p className="empty">No statements mirrored for this meeting.</p>
          ) : (
            <div className="timeline">
              {statements.map((statement) => (
                <StatementRow key={statement.id} statement={statement} />
              ))}
            </div>
          )}
        </section>

        <section className="section">
          <div className="section-head">
            <h2>Commitments</h2>
            <span className="note">by person, this meeting</span>
          </div>
          {commitments.length === 0 ? (
            <p className="empty">Nobody promised anything here.</p>
          ) : (
            commitments.map((person) => (
              <div className="person-block" key={person.person}>
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
                    <div className="stmt" key={entry.statement.id}>
                      <div className="stmt-rail">
                        <StatementChip kind={entry.statement.kind} />
                      </div>
                      <div>
                        <p className="stmt-text">{entry.statement.text}</p>
                        <div className="stmt-meta">
                          <span>{entry.statement.id}</span>
                          {entry.statement.issue ? <span>{entry.statement.issue}</span> : null}
                          {entry.statement.superseded_by.length > 0 ? (
                            <span className="note-superseded">superseded</span>
                          ) : null}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))
          )}
        </section>
      </main>
      <Footer source={recap.source} reason={recap.reason} />
    </>
  );
}
