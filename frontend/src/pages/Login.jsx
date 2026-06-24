import { useEffect, useRef, useState } from 'react';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { AnimatePresence, motion } from 'framer-motion';
import {
  Activity, Eye, EyeOff, LogIn, ShieldCheck, Sparkles, UserPlus,
} from 'lucide-react';
import toast from 'react-hot-toast';

import { useAuth } from '@/auth/AuthContext.jsx';
import { register as registerApi } from '@/api/endpoints.js';
import PatientOnboarding from '@/components/onboarding/PatientOnboarding.jsx';

/**
 * Login + register split-screen.
 *
 * Left: rich illustration of the value prop with floating particles.
 * Right: form card.  Wrong credentials → animated shake on the card.
 * Register lives behind a tiny crossfade rather than a separate page —
 * lets new users self-sign-up without losing the marketing context.
 */
export default function Login() {
  const { isAuthenticated, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = location.state?.from?.pathname || '/dashboard';

  // 'login' | 'register' | 'onboarding'
  // The register form collects email/password/name; on submit we advance
  // to 'onboarding' which collects optional demographics + conditions
  // and then calls /auth/register with the full payload.
  const [mode, setMode] = useState('login');
  const [email, setEmail]     = useState('');
  const [password, setPwd]    = useState('');
  const [fullName, setName]   = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [busy, setBusy]       = useState(false);
  const [shake, setShake]     = useState(0);     // bump to retrigger

  if (isAuthenticated) return <Navigate to={from} replace />;

  // Onboarding renders full-page (the multi-step form needs the
  // real estate), so dispatch early.
  if (mode === 'onboarding') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-mesh-light p-6">
        <PatientOnboarding
          busy={busy}
          title={`Welcome, ${fullName.split(' ')[0] || 'there'} — let’s set up your profile`}
          description="These details power your trial matches.  All steps are optional except date of birth + sex."
          onCancel={() => setMode('register')}
          onSubmit={async (demographics) => {
            setBusy(true);
            try {
              await registerApi({
                email, password, full_name: fullName,
                ...demographics,
              });
              toast.success('Profile created — signing you in…');
              await login({ email, password });
              navigate(from, { replace: true });
            } catch (err) {
              const detail = err?.response?.data?.detail || err?.message || 'Registration failed.';
              toast.error(typeof detail === 'string' ? detail : 'Registration failed.');
              setShake((n) => n + 1);
              setMode('register');
            } finally {
              setBusy(false);
            }
          }}
        />
      </div>
    );
  }

  async function onSubmit(e) {
    e.preventDefault();
    if (mode === 'register') {
      // Validate credentials locally, then jump to onboarding — we
      // don't hit the backend until the whole patient payload is ready.
      if (password.length < 8) {
        setShake((n) => n + 1);
        toast.error('Password must be at least 8 characters long.');
        return;
      }
      if (!fullName.trim()) {
        setShake((n) => n + 1);
        toast.error('Please tell us your name.');
        return;
      }
      setMode('onboarding');
      return;
    }
    setBusy(true);
    try {
      await login({ email, password });
      navigate(from, { replace: true });
    } catch (err) {
      setShake((n) => n + 1);
      const detail =
        err?.response?.data?.detail ||
        err?.message ||
        'Sign-in failed.  Please try again.';
      toast.error(typeof detail === 'string' ? detail : 'Sign-in failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen grid-cols-1 bg-mesh-light lg:grid-cols-2">
      <HeroPanel />
      <div className="relative flex items-center justify-center p-6">
        <motion.div
          key={shake}
          initial={{ opacity: 0, y: 12 }}
          animate={{
            opacity: 1,
            y: 0,
            x: shake ? [-1, 2, -4, 4, -4, 4, 0] : 0,
          }}
          transition={{
            opacity: { duration: 0.4 },
            x: { duration: 0.5, ease: [0.36, 0.07, 0.19, 0.97] },
          }}
          className="card-glass relative w-full max-w-md p-8"
        >
          <div className="mb-6 flex items-center gap-3">
            <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-accent-500 shadow-glow">
              <Activity className="h-5 w-5 text-white" />
            </span>
            <div>
              <div className="font-display text-xl font-bold tracking-tight text-ink-900">
                Trialight
              </div>
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-400">
                Clinical Trial AI
              </div>
            </div>
          </div>

          <AnimatePresence mode="wait">
            <motion.div
              key={mode}
              initial={{ opacity: 0, x: 8 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -8 }}
              transition={{ duration: 0.22 }}
            >
              <h2 className="font-display text-2xl font-bold text-ink-900">
                {mode === 'login' ? 'Welcome back.' : 'Create your account.'}
              </h2>
              <p className="mt-1.5 text-sm text-ink-500">
                {mode === 'login'
                  ? 'Sign in to keep matching patients with the trials they need.'
                  : 'Self-service registration creates a patient-role account.  Ask an admin for elevated access.'}
              </p>
            </motion.div>
          </AnimatePresence>

          <form onSubmit={onSubmit} className="mt-6 flex flex-col gap-4">
            <AnimatePresence>
              {mode === 'register' && (
                <motion.div
                  key="name"
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.2 }}
                  className="overflow-hidden"
                >
                  <Field label="Full name">
                    <input
                      className="input"
                      type="text"
                      autoComplete="name"
                      required={mode === 'register'}
                      value={fullName}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="Jane Reed"
                    />
                  </Field>
                </motion.div>
              )}
            </AnimatePresence>

            <Field label="Email">
              <input
                className="input"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@hospital.org"
              />
            </Field>

            <Field label="Password">
              <div className="relative">
                <input
                  className="input pr-11"
                  type={showPwd ? 'text' : 'password'}
                  autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                  required
                  minLength={mode === 'register' ? 8 : undefined}
                  value={password}
                  onChange={(e) => setPwd(e.target.value)}
                  placeholder={mode === 'register' ? 'Min. 8 characters' : '••••••••'}
                />
                <button
                  type="button"
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-lg p-2 text-ink-400 hover:bg-ink-100 hover:text-ink-700"
                  onClick={() => setShowPwd((b) => !b)}
                  tabIndex={-1}
                >
                  {showPwd ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </Field>

            <button type="submit" className="btn-primary mt-2 group" disabled={busy}>
              {busy ? (
                <span className="flex items-center gap-2">
                  <span className="h-2 w-2 animate-breathing rounded-full bg-white" />
                  Signing in…
                </span>
              ) : (
                <>
                  {mode === 'login' ? (
                    <LogIn className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
                  ) : (
                    <UserPlus className="h-4 w-4" />
                  )}
                  {mode === 'login' ? 'Sign in' : 'Continue to profile setup'}
                </>
              )}
            </button>
          </form>

          <div className="mt-5 text-center text-sm text-ink-500">
            {mode === 'login' ? (
              <>New here?{' '}
                <button
                  className="font-semibold text-brand-600 hover:underline"
                  onClick={() => setMode('register')}
                >
                  Create an account
                </button>
              </>
            ) : (
              <>Already have one?{' '}
                <button
                  className="font-semibold text-brand-600 hover:underline"
                  onClick={() => setMode('login')}
                >
                  Sign in
                </button>
              </>
            )}
          </div>
        </motion.div>
      </div>
    </div>
  );
}

/* ─────────────────────────── Hero panel ──────────────────────────── */

function HeroPanel() {
  return (
    <div className="relative hidden overflow-hidden bg-gradient-to-br from-brand-500 via-brand-600 to-accent-500 lg:flex lg:flex-col lg:items-start lg:justify-center lg:p-14 lg:text-white">
      <Particles />
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6 }}
        className="relative z-10"
      >
        <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-white/30 bg-white/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] backdrop-blur">
          <Sparkles className="h-3.5 w-3.5" />
          AI-powered eligibility, with receipts
        </div>
        <h1 className="font-display text-5xl font-bold leading-tight tracking-tight">
          The trial<br />finds the patient,<br />not the other way around.
        </h1>
        <p className="mt-5 max-w-md text-base/relaxed text-white/85">
          Deep temporal reasoning, three-state matching, and per-criterion
          explainability — every match shows its work.
        </p>
        <div className="mt-10 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Feature icon={<Sparkles className="h-4 w-4" />}
                   title="LLM reasoning with audit trail"
                   body="Every verdict cites the EHR event that drove it." />
          <Feature icon={<ShieldCheck className="h-4 w-4" />}
                   title="HIPAA-aware by design"
                   body="PHI is scrubbed before any prompt leaves the box." />
        </div>
      </motion.div>
    </div>
  );
}

function Feature({ icon, title, body }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.4 }}
      className="rounded-2xl border border-white/20 bg-white/10 p-4 backdrop-blur"
    >
      <div className="flex items-center gap-2 text-white">
        <span className="rounded-lg bg-white/20 p-1.5">{icon}</span>
        <div className="text-sm font-semibold">{title}</div>
      </div>
      <p className="mt-1.5 text-xs text-white/75">{body}</p>
    </motion.div>
  );
}

/* ─────────────────────────── Particles ───────────────────────────── */

/**
 * Tiny canvas particle field that drifts behind the hero copy.  No
 * external deps — just a requestAnimationFrame loop drawing 40-ish
 * floating dots.  Pauses on tab blur to be CPU-friendly.
 */
function Particles() {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let raf = 0;
    let running = true;
    let dots = [];

    function resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const { width, height } = canvas.getBoundingClientRect();
      canvas.width  = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // Re-seed when the canvas resizes — keeps density consistent.
      dots = Array.from({ length: 44 }).map(() => ({
        x: Math.random() * width,
        y: Math.random() * height,
        r: 1 + Math.random() * 2.4,
        vx: (Math.random() - 0.5) * 0.18,
        vy: (Math.random() - 0.5) * 0.18,
        a: 0.18 + Math.random() * 0.4,
      }));
    }

    function tick() {
      if (!running) return;
      const { width, height } = canvas.getBoundingClientRect();
      ctx.clearRect(0, 0, width, height);
      for (const d of dots) {
        d.x += d.vx; d.y += d.vy;
        if (d.x < -10) d.x = width  + 10;
        if (d.x > width  + 10) d.x = -10;
        if (d.y < -10) d.y = height + 10;
        if (d.y > height + 10) d.y = -10;
        ctx.beginPath();
        ctx.fillStyle = `rgba(255,255,255,${d.a})`;
        ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
        ctx.fill();
      }
      raf = requestAnimationFrame(tick);
    }

    function onVisibility() {
      running = !document.hidden;
      if (running) tick();
    }

    resize();
    window.addEventListener('resize', resize);
    document.addEventListener('visibilitychange', onVisibility);
    tick();
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', resize);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none absolute inset-0 h-full w-full"
      aria-hidden
    />
  );
}

/* ─────────────────────────── Field wrapper ──────────────────────── */

function Field({ label, children }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-ink-500">
        {label}
      </span>
      {children}
    </label>
  );
}
