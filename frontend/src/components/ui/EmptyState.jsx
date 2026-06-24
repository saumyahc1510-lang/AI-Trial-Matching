import { motion } from 'framer-motion';
import { cn } from '@/lib/cn.js';

/**
 * Empty / zero-data state.  Centred icon + headline + optional action.
 */
export default function EmptyState({ icon: Icon, title, description, action, className }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        'flex flex-col items-center justify-center rounded-2xl border border-dashed border-ink-200 bg-white/60 px-8 py-14 text-center',
        className,
      )}
    >
      {Icon && (
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-100 to-accent-100 text-brand-500">
          <Icon className="h-7 w-7" />
        </div>
      )}
      <h3 className="font-display text-lg font-semibold text-ink-800">
        {title}
      </h3>
      {description && (
        <p className="mt-2 max-w-md text-sm text-ink-500">{description}</p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </motion.div>
  );
}
