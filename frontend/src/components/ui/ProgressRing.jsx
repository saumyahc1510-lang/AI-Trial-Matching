import { motion, useMotionValue, useTransform, animate } from 'framer-motion';
import { useEffect, useRef, useState } from 'react';

import { cn } from '@/lib/cn.js';

/**
 * SVG ring that animates from 0 → ``value`` (0..1) on mount.
 *
 * The central number counts up in sync with the arc — gives the
 * matching dashboard a satisfying "AI brain ticking through" feel.
 */
export default function ProgressRing({
  value,                  // 0..1
  size = 140,
  stroke = 12,
  label,
  sublabel,
  tone = 'brand',         // 'brand' | 'success' | 'warn' | 'danger'
  showPercent = true,
  className,
}) {
  const v = Math.max(0, Math.min(1, value ?? 0));
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;

  const motionVal = useMotionValue(0);
  const offset = useTransform(motionVal, (m) => circumference * (1 - m));
  const [percent, setPercent] = useState(0);

  // Track the latest value in a ref so the animate() callback always
  // unsubscribes cleanly when the parent component re-renders.
  const ref = useRef();

  useEffect(() => {
    const controls = animate(motionVal, v, {
      duration: 0.9,
      ease: [0.22, 1, 0.36, 1],
      onUpdate: (latest) => setPercent(Math.round(latest * 100)),
    });
    return controls.stop;
  }, [v, motionVal]);

  const stops = STROKE_BY_TONE[tone] || STROKE_BY_TONE.brand;

  return (
    <div
      ref={ref}
      className={cn('relative inline-flex flex-col items-center justify-center', className)}
      style={{ width: size, height: size }}
    >
      <svg width={size} height={size} className="-rotate-90 overflow-visible">
        <defs>
          <linearGradient id={`pr-${tone}`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor={stops[0]} />
            <stop offset="1" stopColor={stops[1]} />
          </linearGradient>
        </defs>
        <circle
          cx={size / 2} cy={size / 2} r={radius}
          stroke="#eef1f6" strokeWidth={stroke} fill="none"
        />
        <motion.circle
          cx={size / 2} cy={size / 2} r={radius}
          stroke={`url(#pr-${tone})`}
          strokeWidth={stroke}
          strokeLinecap="round"
          fill="none"
          strokeDasharray={circumference}
          style={{ strokeDashoffset: offset }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        {showPercent && (
          <span className="font-display text-3xl font-bold tracking-tight text-ink-900 tabular-nums">
            {percent}
            <span className="ml-0.5 text-base font-semibold text-ink-400">%</span>
          </span>
        )}
        {label && (
          <span className="mt-1 text-[11px] font-semibold uppercase tracking-wide text-ink-500">
            {label}
          </span>
        )}
        {sublabel && (
          <span className="text-[10px] text-ink-400">{sublabel}</span>
        )}
      </div>
    </div>
  );
}

const STROKE_BY_TONE = {
  brand:   ['#5867e6', '#7689f5'],
  success: ['#0ea271', '#34c990'],
  warn:    ['#d97706', '#f59e0b'],
  danger:  ['#dc2626', '#f87171'],
};
