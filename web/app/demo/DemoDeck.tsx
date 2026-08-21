'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState, type CSSProperties } from 'react';
import { PRODUCT_URL } from '../../lib/product';

type Slide =
  | { id: string; kind: 'title'; kicker: string; title: string; lede: string }
  | { id: string; kind: 'what'; kicker: string; title: string; body: string; points: string[] }
  | {
      id: string;
      kind: 'how';
      kicker: string;
      title: string;
      steps: Array<{ n: string; name: string; body: string }>;
    }
  | {
      id: string;
      kind: 'vs';
      kicker: string;
      title: string;
      rows: Array<{ them: string; us: string }>;
    }
  | {
      id: string;
      kind: 'hold';
      kicker: string;
      title: string;
      body: string;
      waits: string[];
      fires: string[];
    }
  | { id: string; kind: 'close'; kicker: string; title: string; lede: string };

const SLIDES: Slide[] = [
  {
    id: 'title',
    kind: 'title',
    kicker: 'A short demo',
    title: 'The meeting is the to-do.',
    lede: 'When the recording stops, the work is already moving.',
  },
  {
    id: 'what',
    kind: 'what',
    kicker: 'What it is',
    title: 'A companion that executes follow-through the moment you adjourn.',
    body: 'It listens to what was decided, promised, and asked for — then files the ticket, comments the decision, holds the time, and drafts the message.',
    points: [
      'The meeting itself is the source of work. Nobody copies notes into Linear after.',
      'Receipts land on one board, grouped by the meeting that produced them.',
      'It runs on this machine. The network is used only to write the things you already agreed to write.',
    ],
  },
  {
    id: 'how',
    kind: 'how',
    kicker: 'How it works',
    title: 'A model extracts. A table decides. Nothing else gets a vote.',
    steps: [
      {
        n: 'Extract',
        name: 'The only model call',
        body: 'Transcript becomes statements: decisions, assignments, tickets, messages, holds. That is all the model is allowed to do.',
      },
      {
        n: 'Plan',
        name: 'A routing table',
        body: 'Plain rules map each statement to an action. Same statements in, same actions out. No model chooses whether to fire.',
      },
      {
        n: 'Fire',
        name: 'The executors',
        body: 'GitHub, Linear, calendar, recap, and draft PRs go out. Slack and email wait on the board until you press Send.',
      },
    ],
  },
  {
    id: 'hold',
    kind: 'hold',
    kicker: 'Ready to send',
    title: 'Irreversible mail and Slack do not leave the room without you.',
    body: 'Everything else fires the moment the meeting stops. Messages sit as drafts on Follow-through until you edit them and press Send — or Don’t send.',
    waits: ['Slack', 'Email'],
    fires: ['GitHub', 'Linear', 'Calendar', 'Recap'],
  },
  {
    id: 'vs',
    kind: 'vs',
    kicker: 'Why this, not that',
    title: 'Summaries still leave you with a to-do list. Adjourn is the list, already done.',
    rows: [
      {
        them: 'Meeting notes tools write a recap you still have to turn into tickets.',
        us: 'Adjourn files the ticket, comments the issue, and holds the time from the same sentence.',
      },
      {
        them: 'Copilots ask permission for every click, so nothing happens until you babysit them.',
        us: 'Reversible work fires. Irreversible work waits in Ready to send until you edit and send — or don\'t.',
      },
      {
        them: 'Chat bots post into Slack and mail the second the meeting ends.',
        us: 'A human still owns the send. Undo is real where the service allows it.',
      },
    ],
  },
  {
    id: 'close',
    kind: 'close',
    kicker: 'The product',
    title: 'Follow-through is already running.',
    lede: 'Open the board the meeting writes to. Or step back through the story.',
  },
];

export default function DemoDeck() {
  const [index, setIndex] = useState(0);
  const last = SLIDES.length - 1;

  const go = useCallback(
    (next: number) => {
      setIndex(Math.max(0, Math.min(last, next)));
    },
    [last],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target instanceof HTMLElement &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'BUTTON' ||
          target.tagName === 'A' ||
          target.isContentEditable);

      if (event.key === 'ArrowRight' || event.key === 'PageDown' || (event.key === ' ' && !typing)) {
        event.preventDefault();
        go(index + 1);
      } else if (event.key === 'ArrowLeft' || event.key === 'PageUp') {
        event.preventDefault();
        go(index - 1);
      } else if (event.key === 'Home') {
        event.preventDefault();
        go(0);
      } else if (event.key === 'End') {
        event.preventDefault();
        go(last);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [go, index, last]);

  const slide = SLIDES[index];
  const progress = ((index + 1) / SLIDES.length) * 100;

  return (
    <div className="deck">
      <div className="deck-vignette" aria-hidden="true" />
      <header className="deck-top">
        <Link className="deck-back" href="/">
          Adjourn
        </Link>
        <p className="deck-count" aria-live="polite">
          {String(index + 1).padStart(2, '0')} / {String(SLIDES.length).padStart(2, '0')}
        </p>
      </header>

      <div className="deck-progress" aria-hidden="true">
        <span style={{ '--deck-progress': String(progress / 100) } as CSSProperties} />
      </div>

      <main className="deck-main" key={slide.id}>
        {slide.kind === 'title' ? (
          <section className="slide slide-title">
            <p className="slide-kicker">{slide.kicker}</p>
            <h1>{slide.title}</h1>
            <p className="slide-lede">{slide.lede}</p>
          </section>
        ) : null}

        {slide.kind === 'what' ? (
          <section className="slide slide-what">
            <p className="slide-kicker">{slide.kicker}</p>
            <h1>{slide.title}</h1>
            <p className="slide-lede">{slide.body}</p>
            <ul className="slide-points">
              {slide.points.map((point) => (
                <li key={point}>{point}</li>
              ))}
            </ul>
          </section>
        ) : null}

        {slide.kind === 'how' ? (
          <section className="slide slide-how">
            <p className="slide-kicker">{slide.kicker}</p>
            <h1>{slide.title}</h1>
            <ol className="slide-steps">
              {slide.steps.map((step) => (
                <li key={step.n}>
                  <span>{step.n}</span>
                  <strong>{step.name}</strong>
                  <p>{step.body}</p>
                </li>
              ))}
            </ol>
          </section>
        ) : null}

        {slide.kind === 'hold' ? (
          <section className="slide slide-hold">
            <p className="slide-kicker">{slide.kicker}</p>
            <h1>{slide.title}</h1>
            <p className="slide-lede">{slide.body}</p>
            <div className="slide-lanes">
              <div>
                <p className="lane-label">Waits for you</p>
                <ul>
                  {slide.waits.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
              <div>
                <p className="lane-label">Fires now</p>
                <ul>
                  {slide.fires.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            </div>
          </section>
        ) : null}

        {slide.kind === 'vs' ? (
          <section className="slide slide-vs">
            <p className="slide-kicker">{slide.kicker}</p>
            <h1>{slide.title}</h1>
            <ul className="slide-vs-rows">
              {slide.rows.map((row) => (
                <li key={row.us}>
                  <p className="them">{row.them}</p>
                  <p className="us">{row.us}</p>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {slide.kind === 'close' ? (
          <section className="slide slide-close">
            <p className="slide-kicker">{slide.kicker}</p>
            <h1>{slide.title}</h1>
            <p className="slide-lede">{slide.lede}</p>
            <div className="slide-cta">
              <a className="lp-btn lp-btn-primary" href={PRODUCT_URL}>
                Open the product
              </a>
              <Link className="lp-btn lp-btn-ghost" href="/">
                Back to the start
              </Link>
            </div>
          </section>
        ) : null}
      </main>

      <nav className="deck-nav" aria-label="Slides">
        <button type="button" className="deck-arrow" onClick={() => go(index - 1)} disabled={index === 0}>
          Previous
        </button>
        <ol className="deck-dots">
          {SLIDES.map((item, i) => (
            <li key={item.id}>
              <button
                type="button"
                className={i === index ? 'is-on' : ''}
                aria-label={`Slide ${i + 1}`}
                aria-current={i === index ? 'true' : undefined}
                onClick={() => go(i)}
              />
            </li>
          ))}
        </ol>
        <button type="button" className="deck-arrow" onClick={() => go(index + 1)} disabled={index === last}>
          Next
        </button>
      </nav>
    </div>
  );
}
