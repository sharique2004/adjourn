import type { Metadata } from 'next';
import BoardLive from './BoardLive';
import { Footer, TopBar } from '../components/ui';
import { getBoard } from '../../lib/read';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

export const metadata: Metadata = {
  title: 'Follow-through — Adjourn',
  description: 'Execution receipts mirrored from the Mac, grouped by meeting, newest first.',
};

export default async function BoardPage() {
  const board = await getBoard();
  return (
    <>
      <TopBar current="/board" />
      <main className="shell">
        <BoardLive initial={board} />
      </main>
      <Footer source={board.source} reason={board.reason} />
    </>
  );
}
