/**
 * Skeleton shimmer components for initial page-load states.
 * All variants use Tailwind's `animate-pulse` for a consistent shimmer feel.
 */

/** Single shimmer line — configurable width/height */
export function SkeletonLine({ className = '' }: { className?: string }) {
  return (
    <div className={`bg-dark-600 rounded animate-pulse ${className}`} />
  )
}

/** Stat summary card: icon placeholder + two text lines */
export function SkeletonStat() {
  return (
    <div className="bg-dark-800 border border-dark-600 rounded-xl p-4 flex items-center gap-3">
      <div className="w-9 h-9 rounded-lg bg-dark-600 animate-pulse shrink-0" />
      <div className="flex-1 space-y-2">
        <SkeletonLine className="h-3 w-24" />
        <SkeletonLine className="h-5 w-16" />
      </div>
    </div>
  )
}

/** Signal / trade list row */
export function SkeletonRow() {
  return (
    <div className="bg-dark-800 border border-dark-600 rounded-xl p-4 flex items-center gap-4">
      <div className="w-14 h-5 rounded bg-dark-600 animate-pulse shrink-0" />
      <div className="flex-1 space-y-2">
        <SkeletonLine className="h-4 w-1/3" />
        <SkeletonLine className="h-3 w-1/2" />
      </div>
      <SkeletonLine className="h-4 w-20" />
    </div>
  )
}

/** Generic card with three content lines */
export function SkeletonCard({ lines = 3 }: { lines?: number }) {
  return (
    <div className="bg-dark-800 border border-dark-600 rounded-xl p-4 space-y-3">
      {Array.from({ length: lines }).map((_, i) => (
        <SkeletonLine
          key={i}
          className={`h-4 ${i === 0 ? 'w-1/2' : i === lines - 1 ? 'w-2/5' : 'w-3/4'}`}
        />
      ))}
    </div>
  )
}

/** Table / list of N skeleton rows */
export function SkeletonList({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-3">
      {Array.from({ length: rows }).map((_, i) => (
        <SkeletonRow key={i} />
      ))}
    </div>
  )
}
