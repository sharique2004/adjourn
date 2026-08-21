import Link from 'next/link';
import { Footer, KindChip, TopBar } from './components/ui';
import { getBoard } from '../lib/read';
import { EXECUTOR_KINDS } from '../lib/types';

export const dynamic = 'force-dynamic';

/** Three factual lines. No adjectives that cannot be checked. */
const LINES = [
  'The recording stops. Extraction runs on the Mac and the first action fires in about four seconds.',
  'Decisions and commitments become GitHub comments, Linear tickets, draft PRs, calendar holds and recap pages — fired, not suggested.',
  'Slack and email sends wait 60 seconds behind a visible countdown. Every fire writes a receipt, wifi or no wifi.',
];

const SPEC: Array<[(typeof EXECUTOR_KINDS)[number], string]> = [
  ['github_update', 'Comments what changed on the issue the decision was about.'],
  ['linear_create', 'Files the ticket someone asked for, assigned to whoever asked.'],
  ['linear_move', 'Moves an existing ticket to the state the update implies.'],
  ['pull_request_stub', 'Opens a draft PR carrying the decision and the quote behind it.'],
  ['slack_send', 'Posts the message someone promised, after the 60s regret window.'],
  ['email_send', 'Sends the email someone promised, after the same window.'],
  ['calendar_hold', 'Holds the time the meeting agreed to, with the right people on it.'],
  ['recap_page', 'Writes the recap: decisions, commitments, unanswered questions.'],
];

export default async function LandingPage() {
  const board = await getBoard();
  const meetings = board.data.groups.length;
  const executions = board.data.execution_count;

  return (
    <>
      <TopBar current="/" />
      <main className="shell">
        <section className="hero">
          <h1>Adjourn</h1>
          <p className="tagline">The meeting is the to-do.</p>
          <ul className="hero-lines">
            {LINES.map((line, i) => (
              <li key={line}>
                <span className="idx">{String(i + 1).padStart(2, '0')}</span>
                <span>{line}</span>
              </li>
            ))}
          </ul>
          <div className="hero-cta">
            <Link className="btn" href="/board">
              Follow-through board →
            </Link>
            <Link className="btn btn-quiet" href="/ledger">
              Commitment ledger
            </Link>
          </div>
        </section>

        <section className="section">
          <div className="section-head">
            <h2>Executors</h2>
            <span className="note">8 kinds</span>
          </div>
          <div className="spec-strip">
            {SPEC.map(([kind, desc]) => (
              <div className="spec-row" key={kind}>
                <KindChip kind={kind} />
                <span className="desc">{desc}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="section">
          <div className="section-head">
            <h2>Topology</h2>
            <span className="note">read-only surface</span>
          </div>
          <div className="stat-row">
            <span>
              mirrored meetings <b>{meetings}</b>
            </span>
            <span>
              execution receipts <b>{executions}</b>
            </span>
            <span>
              source <b>{board.source === 'cloud' ? 'falkordb cloud' : 'bundled snapshot'}</b>
            </span>
          </div>
          <p className="prose" style={{ marginTop: 22 }}>
            The Mac is authoritative. Extraction, execution and undo all run locally and keep
            running when the network does not. Each result is mirrored to a FalkorDB Cloud graph as
            an <code>(:Execution)-[:FROM_MEETING]-&gt;(:Meeting)</code> receipt alongside the{' '}
            <code>Person / Statement / Issue</code> graph the meeting produced.
          </p>
          <p className="prose" style={{ marginTop: 14 }}>
            <strong>This site reads that mirror and nothing else.</strong> It cannot fire an action
            and it cannot undo one — undo lives on the Mac board, next to the thing that fired.
          </p>
        </section>
      </main>
      <Footer source={board.source} reason={board.reason} />
    </>
  );
}
