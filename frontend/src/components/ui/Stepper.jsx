import { cn } from '@/lib/cn.js';

/**
 * Horizontal stepper used by every multi-step flow.
 *
 * Props:
 *   - steps:   [{ id, label, icon }]
 *   - current: index of the in-progress step (0-based)
 *   - size:    'sm' (default — onboarding scale) | 'md' (intake scale)
 *
 * Renders a CSS-grid of `steps.length` cells.  Each cell is in one of
 * three visual states: ``done`` (completed), ``current`` (in progress),
 * or ``todo`` (upcoming).  The component is presentational — owners
 * drive ``current`` from their own state machine.
 */
export default function Stepper({ steps, current, size = 'sm', className }) {
  const cell    = size === 'md' ? 'px-3 py-2.5' : 'px-2 py-1.5';
  const gap     = size === 'md' ? 'gap-2'       : 'gap-1.5';
  const iconCls = size === 'md' ? 'h-4 w-4'     : 'h-3.5 w-3.5';

  return (
    <ol
      className={cn(
        `grid ${gap}`,
        // Tailwind needs a literal class name — generate the right
        // grid-cols class without string interpolation gymnastics.
        STEP_COL_CLASS[steps.length] || `grid-cols-${steps.length}`,
        className,
      )}
      style={!STEP_COL_CLASS[steps.length]
        ? { gridTemplateColumns: `repeat(${steps.length}, minmax(0, 1fr))` }
        : undefined}
    >
      {steps.map((s, i) => {
        const Icon = s.icon;
        const state = i < current ? 'done' : i === current ? 'current' : 'todo';
        return (
          <li key={s.id} className="relative">
            <div
              className={cn(
                'flex items-center gap-1.5 rounded-lg text-xs font-semibold',
                cell,
                STATE_CLASS[state],
              )}
            >
              {Icon && <Icon className={iconCls} />}
              <span className="truncate">{s.label}</span>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

const STATE_CLASS = {
  done:    'bg-brand-100 text-brand-700',
  current: 'bg-brand-500 text-white shadow-sm',
  todo:    'bg-ink-100 text-ink-500',
};

// Pre-bind the common arities so Tailwind's static class scanner can
// see them; fall back to inline grid-template-columns for anything else.
const STEP_COL_CLASS = {
  2: 'grid-cols-2',
  3: 'grid-cols-3',
  4: 'grid-cols-4',
  5: 'grid-cols-5',
  6: 'grid-cols-6',
};
