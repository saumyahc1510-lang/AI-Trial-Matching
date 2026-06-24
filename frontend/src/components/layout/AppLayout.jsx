import { AnimatePresence, motion } from 'framer-motion';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import {
  Activity, BellRing, Calendar, FileSearch, FlaskConical,
  HeartPulse, LayoutDashboard, LogOut, ShieldCheck, Sparkles,
  UsersRound, Wand2,
} from 'lucide-react';
import { useQuery } from '@tanstack/react-query';

import { cn } from '@/lib/cn.js';
import { unreadCount } from '@/api/endpoints.js';
import { useAuth } from '@/auth/AuthContext.jsx';

/**
 * The whole "logged-in" shell.  Renders a left sidebar, top bar, and
 * an animated <Outlet/> for the active route.
 *
 * Animations:
 *  - The active sidebar item gets a `layoutId="active-tab"` pill that
 *    slides between items as the user navigates.  This is the Framer
 *    Motion "shared layout" trick — one of the cheapest "high-end" UI
 *    flourishes around.
 *  - Pages crossfade through <AnimatePresence/> keyed on pathname.
 */
// Sidebar entries — every role gets a tailored subset.  Patients in
// particular must never see the cohort-wide ``Patients`` link (they
// can only access their own record anyway) or the ``Matching`` console
// (they read their own matches off the dashboard).  Sponsors can't see
// PHI surfaces at all.
const NAV_ITEMS = {
  dashboard:     { to: '/dashboard',     label: 'Dashboard',     icon: LayoutDashboard },
  patients:      { to: '/patients',      label: 'Patients',      icon: UsersRound      },
  trials:        { to: '/trials',        label: 'Trials',        icon: FlaskConical    },
  matching:      { to: '/matching',      label: 'Matching',      icon: Sparkles        },
  findMatches:   { to: '/find-matches',  label: 'Find matches',  icon: Wand2           },
  notifications: { to: '/notifications', label: 'Notifications', icon: BellRing        },
  audit:         { to: '/audit',         label: 'Audit',         icon: ShieldCheck     },
};

const NAV_BY_ROLE = {
  // Patient-role users get a self-serve "Find matches" entry — same
  // icon as the staff Matching console, but routes them to the
  // guided intake flow instead of the cohort-wide picker.
  patient:     ['dashboard', 'findMatches', 'trials', 'notifications'],
  coordinator: ['dashboard', 'patients', 'trials', 'matching', 'notifications'],
  clinician:   ['dashboard', 'patients', 'trials', 'matching', 'notifications'],
  sponsor:     ['dashboard', 'trials', 'notifications'],
  admin:       ['dashboard', 'patients', 'trials', 'matching', 'notifications'],
};

const ROLE_NAV = {
  admin: ['audit'],
};

export default function AppLayout() {
  const location = useLocation();
  const { user, logout } = useAuth();

  // Unread badge — poll every 30s so the bell stays warm without being
  // chatty.  React Query already caches by key so the navigation bar
  // doesn't re-fetch on every page change.
  const { data: unread } = useQuery({
    queryKey: ['notifications', 'unread-count'],
    queryFn: unreadCount,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  });

  // Resolve the role's main NAV slice (defaults to coordinator so we
  // never crash if the backend ships a new role we don't know yet).
  const navKeys = NAV_BY_ROLE[user?.role] || NAV_BY_ROLE.coordinator;
  const mainNav = navKeys.map((k) => NAV_ITEMS[k]).filter(Boolean);
  const roleExtras = (ROLE_NAV[user?.role] || []).map((k) => NAV_ITEMS[k]).filter(Boolean);

  return (
    <div className="relative flex min-h-screen bg-ink-50 bg-mesh-light">
      {/* Decorative orb that drifts in the corner.  Pure CSS, GPU-cheap. */}
      <div
        aria-hidden
        className="pointer-events-none absolute -right-32 -top-32 h-96 w-96 rounded-full bg-gradient-to-br from-brand-200/60 to-accent-200/40 blur-3xl animate-drift"
      />
      {/* ───────────────────────── Sidebar ─────────────────────────── */}
      <aside className="relative z-10 flex w-64 shrink-0 flex-col border-r border-ink-100 bg-white/70 px-4 py-6 backdrop-blur-xl">
        <Brand />
        <nav className="mt-8 flex flex-col gap-1">
          {mainNav.map((item) => (
            <SidebarItem
              key={item.to}
              item={item}
              active={isActive(location.pathname, item.to)}
              unread={item.to === '/notifications' ? unread : null}
            />
          ))}
          {roleExtras.length > 0 && (
            <div className="my-3 border-t border-ink-100 pt-3 text-xs font-medium uppercase tracking-wide text-ink-400">
              Admin
            </div>
          )}
          {roleExtras.map((item) => (
            <SidebarItem
              key={item.to}
              item={item}
              active={isActive(location.pathname, item.to)}
            />
          ))}
        </nav>

        <div className="mt-auto">
          <UserCard user={user} onLogout={logout} />
        </div>
      </aside>

      {/* ───────────────────────── Main column ─────────────────────── */}
      <main className="relative z-10 flex min-w-0 flex-1 flex-col">
        <Topbar user={user} unread={unread} />
        <div className="flex-1 overflow-y-auto px-8 py-8">
          <AnimatePresence mode="wait">
            <motion.div
              key={location.pathname}
              initial={{ opacity: 0, y: 14 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
            >
              <Outlet />
            </motion.div>
          </AnimatePresence>
        </div>
      </main>
    </div>
  );
}

/* ─────────────────────────── Sub-components ─────────────────────── */

function Brand() {
  return (
    <NavLink to="/dashboard" className="group flex items-center gap-2.5">
      <span className="relative flex h-10 w-10 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-accent-500 shadow-glow">
        <Activity className="h-5 w-5 text-white" />
        <span className="absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full border-2 border-white bg-success-500" />
      </span>
      <span className="flex flex-col leading-none">
        <span className="font-display text-lg font-bold tracking-tight text-ink-800">
          Trialight
        </span>
        <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-ink-400">
          Clinical Trial AI
        </span>
      </span>
    </NavLink>
  );
}

function SidebarItem({ item, active, unread }) {
  const Icon = item.icon;
  return (
    <NavLink
      to={item.to}
      className={cn(
        'group relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors',
        active
          ? 'text-brand-700'
          : 'text-ink-500 hover:bg-white/80 hover:text-ink-800',
      )}
    >
      {active && (
        <motion.span
          layoutId="active-tab"
          className="absolute inset-0 -z-0 rounded-xl bg-gradient-to-r from-brand-100 to-brand-50 shadow-sm"
          transition={{ type: 'spring', stiffness: 380, damping: 32 }}
        />
      )}
      <Icon className={cn(
        'relative z-10 h-4 w-4',
        active ? 'text-brand-600' : 'text-ink-400 group-hover:text-ink-600',
      )} />
      <span className="relative z-10 flex-1">{item.label}</span>
      {!!unread && (
        <motion.span
          initial={{ scale: 0, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          className="relative z-10 inline-flex min-w-[22px] items-center justify-center rounded-full bg-accent-500 px-1.5 py-0.5 text-[10px] font-bold text-white shadow-sm"
        >
          {unread > 99 ? '99+' : unread}
        </motion.span>
      )}
    </NavLink>
  );
}

function UserCard({ user, onLogout }) {
  if (!user) return null;
  const initials = (user.full_name || user.email || '?')
    .split(/[ @]+/).map((p) => p[0]).filter(Boolean).slice(0, 2).join('').toUpperCase();
  return (
    <div className="rounded-2xl border border-ink-100 bg-white/80 p-3 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-full bg-gradient-to-br from-brand-400 to-accent-400 text-sm font-semibold text-white">
          {initials}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold text-ink-800">
            {user.full_name || user.email}
          </div>
          <div className="truncate text-xs text-ink-400">
            {prettyRole(user.role)}
          </div>
        </div>
        <button
          onClick={onLogout}
          className="rounded-lg p-1.5 text-ink-400 transition-colors hover:bg-danger-100 hover:text-danger-500"
          title="Sign out"
        >
          <LogOut className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

function Topbar({ user, unread }) {
  return (
    <div className="sticky top-0 z-20 flex items-center justify-between border-b border-ink-100/60 bg-white/50 px-8 py-4 backdrop-blur-xl">
      <div className="flex items-center gap-3">
        <Greeting user={user} />
      </div>
      <div className="flex items-center gap-3">
        <NavLink
          to="/notifications"
          className="relative flex h-10 w-10 items-center justify-center rounded-xl bg-white shadow-sm transition-shadow hover:shadow-md"
        >
          <BellRing className="h-4.5 w-4.5 text-ink-500" />
          {!!unread && (
            <span className="absolute -right-1 -top-1 flex h-5 w-5 items-center justify-center rounded-full bg-accent-500 text-[10px] font-bold text-white shadow-sm">
              {unread > 9 ? '9+' : unread}
            </span>
          )}
        </NavLink>
      </div>
    </div>
  );
}

function Greeting({ user }) {
  const hour = new Date().getHours();
  let greeting = 'Good evening';
  if (hour < 5)       greeting = 'Up late';
  else if (hour < 12) greeting = 'Good morning';
  else if (hour < 18) greeting = 'Good afternoon';
  const first = (user?.full_name || user?.email || '').split(/[ @]/)[0];
  return (
    <div>
      <div className="text-xs font-medium uppercase tracking-wider text-ink-400">
        {greeting}
      </div>
      <div className="text-base font-semibold text-ink-800">
        {first ? `Welcome back, ${first}` : 'Welcome back'}
      </div>
    </div>
  );
}

/* ─────────────────────────── Helpers ────────────────────────────── */

function isActive(pathname, to) {
  if (to === '/dashboard') return pathname === '/' || pathname.startsWith('/dashboard');
  return pathname.startsWith(to);
}

function prettyRole(role) {
  if (!role) return '';
  return role.charAt(0).toUpperCase() + role.slice(1);
}
