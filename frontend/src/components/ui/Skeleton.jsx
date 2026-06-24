import { cn } from '@/lib/cn.js';

/**
 * Shimmer skeleton placeholder.  Used in lieu of spinners — the shimmer
 * communicates "data is on its way" without the harsh stop-motion of a
 * loading spinner.
 */
export function Skeleton({ className, ...props }) {
  return <div className={cn('skeleton', className)} {...props} />;
}

export function SkeletonRows({ rows = 3, className }) {
  return (
    <div className={cn('flex flex-col gap-2.5', className)}>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className={cn('h-12', i % 2 ? 'w-[88%]' : 'w-full')} />
      ))}
    </div>
  );
}
