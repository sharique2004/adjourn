{/*
  THESIS: the meeting itself becomes the work. Refuses a metric hero.
  OWN-WORLD: warm black, brass, Gloock for speech, Schibsted Grotesk for acts.
  STORY: stop talking; follow-through is already on the table.
  FIRST VIEWPORT: thesis left, quote peeling into receipts right, two buttons.
  FORM: split first viewport with one authored motion.
*/}
import Link from 'next/link';
import { Gloock, Schibsted_Grotesk } from 'next/font/google';
import { PRODUCT_URL } from '../lib/product';
import './landing.css';

const gloock = Gloock({
  subsets: ['latin'],
  weight: '400',
  variable: '--font-gloock',
  display: 'swap',
});

const grotesk = Schibsted_Grotesk({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-grotesk',
  display: 'swap',
});

export const dynamic = 'force-static';

const RECEIPTS = [
  { chip: 'Slack', text: 'Channel summary, waiting in Ready to send.', flag: 'Draft', live: false },
  { chip: 'Linear', text: 'SHA-16 filed in Backlog from the room.', flag: 'Live', live: true },
  { chip: 'GitHub', text: 'What changed, commented on issue #2.', flag: 'Live', live: true },
];

export default function LandingPage() {
  return (
    <div className={`lp ${gloock.variable} ${grotesk.variable}`}>
      <div className="lp-vignette" aria-hidden="true" />
      <div className="lp-frame">
        <header className="lp-mark">
          <b>Adjourn</b>
          <span>The meeting is the to-do</span>
        </header>

        <div className="lp-stage">
          <div className="lp-copy">
            <p className="lp-kicker">When the recording stops</p>
            <h1 className="lp-title">
              The meeting
              <br />
              is the <em>to-do</em>.
            </h1>
            <p className="lp-lede">
              Decisions, promises, and tickets leave the room as work — not as a
              summary you still have to turn into one.
            </p>
            <div className="lp-cta">
              <a className="lp-btn lp-btn-primary" href={PRODUCT_URL}>
                Open the product
              </a>
              <Link className="lp-btn lp-btn-ghost" href="/demo">
                See the demo
              </Link>
            </div>
          </div>

          <div className="lp-proof">
            <div className="lp-stopped" aria-hidden="true">
              {[18, 28, 12, 32, 20, 36, 14, 24, 10, 30, 16, 22, 8, 26, 12].map((h, i) => (
                <span key={i} style={{ height: h, animationDelay: `${i * 40}ms` }} />
              ))}
            </div>
            <blockquote className="lp-quote">
              “Alright. I&apos;ll Slack the channel the summary once we&apos;re done here.”
              <cite>Spoken, then followed through</cite>
            </blockquote>
            <ul className="lp-receipts">
              {RECEIPTS.map((row) => (
                <li className="lp-receipt" key={row.chip}>
                  <span className="lp-chip">{row.chip}</span>
                  <p>{row.text}</p>
                  <span className={row.live ? 'lp-flag lp-flag-live' : 'lp-flag'}>{row.flag}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>

        <footer className="lp-foot">
          <span>Local-first. A model extracts. A table decides.</span>
          <span>Slack and email wait for you.</span>
        </footer>
      </div>
    </div>
  );
}
