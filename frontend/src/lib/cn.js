/**
 * Tiny class-name helper — same idea as clsx but exported as `cn` so
 * the component code matches the Tailwind ecosystem's idiom.
 */
import clsx from 'clsx';

export function cn(...args) {
  return clsx(...args);
}
