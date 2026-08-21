import { Gloock, Schibsted_Grotesk } from 'next/font/google';
import type { Metadata } from 'next';
import DemoDeck from './DemoDeck';
import '../landing.css';
import './demo.css';

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

export const metadata: Metadata = {
  title: 'Demo — Adjourn',
  description: 'What Adjourn is, how it works, and why follow-through is not another summary.',
};

export default function DemoPage() {
  return (
    <div className={`${gloock.variable} ${grotesk.variable}`}>
      <DemoDeck />
    </div>
  );
}
