/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Brand palette — soft blue-violet primary, coral accent, warm
        // neutrals.  All chosen to stay light + airy while still
        // carrying enough contrast for medical-grade legibility.
        brand: {
          50:  '#f3f6ff',
          100: '#e2e9ff',
          200: '#c6d2ff',
          300: '#9eb1ff',
          400: '#7689f5',
          500: '#5867e6', // primary
          600: '#4451d4',
          700: '#3940b0',
          800: '#2f368e',
          900: '#262d72',
        },
        accent: {
          50:  '#fff4f1',
          100: '#ffe1d9',
          300: '#ffac95',
          500: '#ff7a55', // coral
          600: '#e85b34',
          700: '#bf4727',
        },
        ink: {
          50:  '#f8fafc',
          100: '#eef1f6',
          200: '#dde2ec',
          300: '#bcc3d1',
          400: '#8a93a6',
          500: '#5d667a', // body text
          600: '#3f485a',
          700: '#2b3445',
          800: '#1d2535',
          900: '#0f1424',
        },
        success: { 500: '#0ea271', 600: '#0a7d57', 100: '#dcfae6' },
        warn:    { 500: '#d97706', 600: '#b45309', 100: '#fef3c7' },
        danger:  { 500: '#dc2626', 600: '#b91c1c', 100: '#fee2e2' },
      },
      fontFamily: {
        sans:    ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        display: ['"Space Grotesk"', 'Inter', 'sans-serif'],
        mono:    ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      boxShadow: {
        // Soft, layered shadows for the airy "card floating on cloud" look.
        soft:  '0 1px 2px rgba(15, 20, 36, 0.04), 0 4px 16px rgba(15, 20, 36, 0.06)',
        glow:  '0 6px 28px -8px rgba(88, 103, 230, 0.45)',
        ring:  '0 0 0 4px rgba(88, 103, 230, 0.12)',
      },
      backgroundImage: {
        'mesh-light':
          'radial-gradient(at 5% 8%, rgba(198, 210, 255, 0.55) 0px, transparent 55%), ' +
          'radial-gradient(at 92% 12%, rgba(255, 172, 149, 0.35) 0px, transparent 55%), ' +
          'radial-gradient(at 60% 92%, rgba(158, 177, 255, 0.45) 0px, transparent 55%)',
      },
      keyframes: {
        // Cross-fade for the language switcher, page transitions, etc.
        fadeIn:    { '0%': { opacity: 0 }, '100%': { opacity: 1 } },
        // Pulse used by the "AI thinking" indicator.
        breathing: {
          '0%, 100%': { transform: 'scale(1)', opacity: 0.85 },
          '50%':      { transform: 'scale(1.08)', opacity: 1 },
        },
        // Subtle background mesh drift for hero areas.
        drift: {
          '0%, 100%': { transform: 'translate3d(0,0,0)' },
          '50%':      { transform: 'translate3d(-12px, 8px, 0)' },
        },
        // Shake for invalid form input.
        shake: {
          '10%, 90%':   { transform: 'translate3d(-1px, 0, 0)' },
          '20%, 80%':   { transform: 'translate3d(2px, 0, 0)' },
          '30%, 50%, 70%': { transform: 'translate3d(-4px, 0, 0)' },
          '40%, 60%':   { transform: 'translate3d(4px, 0, 0)' },
        },
        shimmer: {
          '0%':   { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
      },
      animation: {
        'fade-in':   'fadeIn 0.4s ease-out',
        'breathing': 'breathing 2.4s ease-in-out infinite',
        'drift':     'drift 14s ease-in-out infinite',
        'shake':     'shake 0.6s cubic-bezier(.36,.07,.19,.97) both',
        'shimmer':   'shimmer 1.8s linear infinite',
      },
    },
  },
  plugins: [],
};
