import Link from 'next/link';
import { TopBar } from './components/ui';

export default function NotFound() {
  return (
    <>
      <TopBar current="" />
      <main className="shell">
        <div className="page-head">
          <div>
            <h1>404</h1>
            <p className="sub">No such meeting in the mirrored graph.</p>
          </div>
        </div>
        <p className="empty" style={{ borderBottom: 'none' }}>
          <Link href="/board">← follow-through board</Link>
        </p>
      </main>
    </>
  );
}
