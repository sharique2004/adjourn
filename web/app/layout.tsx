import type { Metadata, Viewport } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Adjourn — the meeting is the to-do',
  description:
    'Adjourn extracts decisions and commitments the moment a meeting ends and executes the follow-through. This is the public read surface over the mirrored graph.',
  applicationName: 'Adjourn',
  openGraph: {
    title: 'Adjourn — the meeting is the to-do',
    description:
      'Decisions and commitments become GitHub updates, Linear tickets, draft PRs, holds and sends. Fired, not suggested.',
    type: 'website',
  },
  robots: { index: true, follow: true },
};

/** The palette is forced dark; tell the browser so form controls match. */
export const viewport: Viewport = {
  themeColor: '#0b0b0c',
  colorScheme: 'dark',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
