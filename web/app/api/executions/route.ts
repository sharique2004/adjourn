import { NextResponse } from 'next/server';
import { getBoard } from '../../../lib/read';

/** TCP + TLS to FalkorDB Cloud — this cannot run on the edge runtime. */
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const revalidate = 0;

/**
 * The board polls this every 5 seconds. It always answers 200: a cloud failure
 * is a `source: "demo"` payload, not an error the client has to reason about.
 */
export async function GET() {
  const board = await getBoard();
  return NextResponse.json(board, {
    headers: {
      // Poll results must never be cached by Vercel's edge or the browser.
      'Cache-Control': 'no-store, max-age=0',
    },
  });
}
