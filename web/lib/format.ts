/**
 * Deterministic formatting. Everything renders in UTC and is labelled UTC:
 * the server and the browser are in different timezones, and a board that
 * flickers on hydration is worse than one that makes you do the arithmetic.
 */

const MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** "20 Aug 17:02" — falls back to the raw string if it is not a date. */
export function shortTime(iso: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${pad(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ${pad(d.getUTCHours())}:${pad(
    d.getUTCMinutes(),
  )}`;
}

/** "20 Aug 2026" for meeting dates, which carry no time. */
export function shortDate(value: string): string {
  if (!value) return '—';
  const d = new Date(value.length === 10 ? `${value}T00:00:00Z` : value);
  if (Number.isNaN(d.getTime())) return value;
  return `${pad(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** Chip text: kinds are snake_case in the contract and stay that way on screen. */
export function kindLabel(kind: string): string {
  return kind || 'unknown';
}

/** Strip the scheme so a long URL reads as a destination, not a string. */
export function linkLabel(url: string): string {
  if (!url) return '';
  if (url.startsWith('/')) return url;
  return url.replace(/^https?:\/\//, '').replace(/\/$/, '');
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
