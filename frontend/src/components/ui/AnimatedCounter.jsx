import { useEffect, useState } from 'react';
import { animate, useMotionValue } from 'framer-motion';

/**
 * Counts up from 0 → ``value`` on mount, then snaps to subsequent
 * values without re-animating (so polling updates don't flicker).
 */
export default function AnimatedCounter({ value = 0, duration = 0.9, format = (n) => n, className }) {
  const motionValue = useMotionValue(0);
  const [display, setDisplay] = useState(0);
  const [primed, setPrimed] = useState(false);

  useEffect(() => {
    if (!primed) {
      const controls = animate(motionValue, value, {
        duration,
        ease: [0.22, 1, 0.36, 1],
        onUpdate: (n) => setDisplay(Math.round(n)),
        onComplete: () => setPrimed(true),
      });
      return controls.stop;
    }
    motionValue.set(value);
    setDisplay(value);
    return undefined;
  }, [value, duration, motionValue, primed]);

  return <span className={className}>{format(display)}</span>;
}
