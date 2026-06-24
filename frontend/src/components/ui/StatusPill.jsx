import { motion } from 'framer-motion';
import { Check, HelpCircle, X } from 'lucide-react';

import { cn } from '@/lib/cn.js';

/**
 * Three-state status pill — used everywhere we render a match /
 * criterion / overall verdict.
 *
 * `status` accepts both the API's strings (`eligible`/`ineligible`/
 * `uncertain` for matches, `met`/`not_met`/`uncertain` for criteria)
 * via a single normalisation lookup.
 */
const VARIANTS = {
  eligible:   { label: 'Eligible',   tone: 'success', Icon: Check },
  met:        { label: 'Met',        tone: 'success', Icon: Check },
  ineligible: { label: 'Ineligible', tone: 'danger',  Icon: X },
  not_met:    { label: 'Not met',    tone: 'danger',  Icon: X },
  uncertain:  { label: 'Uncertain',  tone: 'warn',    Icon: HelpCircle },
};

const TONE_CLASS = {
  success: 'bg-success-100 text-success-600 border-success-500/20',
  warn:    'bg-warn-100    text-warn-600    border-warn-500/20',
  danger:  'bg-danger-100  text-danger-600  border-danger-500/20',
};

export default function StatusPill({ status, size = 'md', className }) {
  const variant = VARIANTS[status] || VARIANTS.uncertain;
  const Icon = variant.Icon;
  return (
    <motion.span
      initial={{ scale: 0.9, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      transition={{ type: 'spring', stiffness: 320, damping: 22 }}
      className={cn(
        'inline-flex items-center gap-1 rounded-full border font-semibold',
        size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-1 text-xs',
        TONE_CLASS[variant.tone],
        className,
      )}
    >
      <Icon className={size === 'sm' ? 'h-3 w-3' : 'h-3.5 w-3.5'} />
      {variant.label}
    </motion.span>
  );
}
