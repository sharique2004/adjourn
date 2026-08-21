import { NextResponse } from 'next/server';
import { cloudConfig } from '../../../lib/graph';
import { getBoard } from '../../../lib/read';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const revalidate = 0;

/**
 * Deploy check: is this instance reading the cloud graph or the snapshot?
 * Reports the host it is pointed at but never the username or password.
 */
export async function GET() {
  const config = cloudConfig();
  const board = await getBoard();

  return NextResponse.json(
    {
      ok: true,
      source: board.source,
      reason: board.reason ?? null,
      configured: config !== null,
      host: config ? `${config.host}:${config.port}` : null,
      graph: config?.graph ?? null,
      meetings: board.data.groups.length,
      executions: board.data.execution_count,
      checked_at: board.generated_at,
    },
    { headers: { 'Cache-Control': 'no-store, max-age=0' } },
  );
}
