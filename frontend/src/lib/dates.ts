/**
 * Date utilities.
 *
 * The backend stores all timestamps as naive UTC ISO strings without a 'Z'
 * suffix (e.g. "2026-03-11T10:02:26.000000"). JavaScript's Date constructor
 * treats timezone-naive strings as LOCAL time, causing an offset equal to the
 * user's UTC offset (e.g. UTC+8 → every timestamp appears 8 hours in the past).
 *
 * Always use parseUtc() instead of new Date() when parsing backend timestamps.
 */

/**
 * Parse a backend UTC timestamp string into a Date, correctly treating it as UTC
 * even if the 'Z' suffix is absent.
 */
export function parseUtc(ts: string | null | undefined): Date | null {
  if (!ts) return null
  const utc = ts.endsWith('Z') || ts.includes('+') ? ts : ts + 'Z'
  return new Date(utc)
}
