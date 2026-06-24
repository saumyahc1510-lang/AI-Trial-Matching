import { motion } from 'framer-motion';

import { cn } from '@/lib/cn.js';

/**
 * Page header with an animated underline accent.  Used as the very
 * first child of every page so the visual rhythm stays consistent.
 */
export default function PageHeader({ eyebrow, title, description, actions, className }) {
  return (
    <header className={cn('mb-8 flex flex-wrap items-end justify-between gap-4', className)}>
      <div>
        {eyebrow && (
          <motion.div
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            className="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-brand-500"
          >
            {eyebrow}
          </motion.div>
        )}
        <motion.h1
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.04 }}
          className="font-display text-3xl font-bold tracking-tight text-ink-900"
        >
          {title}
          {/* Animated underline that draws in from the left. */}
          <motion.span
            initial={{ scaleX: 0, originX: 0 }}
            animate={{ scaleX: 1 }}
            transition={{ delay: 0.18, duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
            className="ml-1 inline-block h-2 w-12 rounded-full bg-gradient-to-r from-brand-400 to-accent-400"
          />
        </motion.h1>
        {description && (
          <motion.p
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.18 }}
            className="mt-2 max-w-2xl text-sm text-ink-500"
          >
            {description}
          </motion.p>
        )}
      </div>
      {actions && (
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2 }}
          className="flex items-center gap-2"
        >
          {actions}
        </motion.div>
      )}
    </header>
  );
}
