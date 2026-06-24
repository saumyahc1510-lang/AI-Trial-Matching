import { cn } from '@/lib/cn.js';

/**
 * Single-metric tile — big number + tiny uppercase label, tone-coded.
 *
 * Used by the match-detail score header and the find-matches results
 * step.  The tone palette covers the standard three-state matching
 * verdicts (success / warn / danger) plus a neutral ``ink`` tone for
 * "not a fit" buckets where danger reads too strong.
 */
export default function Tally({ label, value, tone = 'ink', className }) {
  return (
    <div className={cn('rounded-xl px-3 py-2 text-center', TONE_CLASS[tone] || TONE_CLASS.ink, className)}>
      <div className="font-display text-2xl font-bold tabular-nums">{value}</div>
      <div className="text-[10px] font-semibold uppercase tracking-wider">{label}</div>
    </div>
  );
}

const TONE_CLASS = {
  success: 'bg-success-100 text-success-600',
  warn:    'bg-warn-100 text-warn-600',
  danger:  'bg-danger-100 text-danger-500',
  ink:     'bg-ink-100 text-ink-600',
  brand:   'bg-brand-100 text-brand-700',
};
