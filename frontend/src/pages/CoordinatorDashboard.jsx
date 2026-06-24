import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { useQueries, useQuery } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  ArrowRight, AlertTriangle, BellRing, FlaskConical, Sparkles,
  UsersRound, ShieldCheck,
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';

import PageHeader from '@/components/ui/PageHeader.jsx';
import AnimatedCounter from '@/components/ui/AnimatedCounter.jsx';
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton.jsx';
import {
  listNotifications, listPatients, listTrials, patientMatches,
} from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

/**
 * Coordinator dashboard.
 *
 * Built around the coordinator's daily loop:
 *   1. Scan the patient roster for anyone with new uncertainties.
 *   2. Resolve those (chase labs, talk to clinician).
 *   3. Sweep the rematch-alerts inbox for newly-eligible patients to
 *      reach out to.
 *   4. Keep an eye on whether wearables are actually streaming.
 */
export default function CoordinatorDashboard() {
  const patientsQ      = useQuery({ queryKey: ['patients', 'top'],   queryFn: () => listPatients({ limit: 50 }) });
  const trialsQ        = useQuery({ queryKey: ['trials',   'top'],   queryFn: () => listTrials({ limit: 50 }) });
  const notificationsQ = useQuery({ queryKey: ['notifications', 'recent'], queryFn: () => listNotifications({ limit: 8 }) });

  // Fetch latest matches for the first ~6 patients in parallel so we
  // can build the per-patient roster table without N round-trips.
  const rosterPatients = (patientsQ.data || []).slice(0, 6);
  const rosterMatchQs = useQueries({
    queries: rosterPatients.map((p) => ({
      queryKey: ['patient', p.id, 'matches', 'roster'],
      queryFn: () => patientMatches(p.id, { limit: 30 }),
      enabled: !!p.id,
    })),
  });

  const roster = useMemo(() => rosterPatients.map((p, i) => {
    const matches = rosterMatchQs[i]?.data?.matches || [];
    return {
      patient: p,
      eligible:   matches.filter((m) => m.overall_status === 'eligible').length,
      uncertain:  matches.filter((m) => m.overall_status === 'uncertain').length,
      ineligible: matches.filter((m) => m.overall_status === 'ineligible').length,
      uncertainTotal: matches.reduce((sum, m) => sum + (m.criteria_uncertain || 0), 0),
      hasUnresolved: matches.some((m) => (m.missing_data_summary || '').trim().length > 0),
      missing: matches.find((m) => (m.missing_data_summary || '').trim())?.missing_data_summary,
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [JSON.stringify(rosterPatients.map((p) => p.id)), rosterMatchQs.map((q) => q.dataUpdatedAt).join(',')]);

  const totalUncertainFlags = roster.reduce((s, r) => s + r.uncertainTotal, 0);

  return (
    <div>
      <PageHeader
        eyebrow="Overview"
        title="Your coordination cockpit"
        description="Patients with new matches, open uncertainty flags, and trials ready for outreach."
        actions={
          <Link to="/matching" className="btn-primary">
            <Sparkles className="h-4 w-4" /> Run a match
          </Link>
        }
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={UsersRound}    tone="brand"   label="Active patients"        value={patientsQ.data?.length}  loading={patientsQ.isLoading}  to="/patients" />
        <StatCard icon={FlaskConical}  tone="accent"  label="Trials in catalog"      value={trialsQ.data?.length}    loading={trialsQ.isLoading}    to="/trials" />
        <StatCard icon={AlertTriangle} tone="warn"    label="Open uncertainty flags" value={totalUncertainFlags}     loading={rosterMatchQs.some((q) => q.isLoading)} to="/matching" />
        <StatCard icon={BellRing}      tone="success" label="Unread alerts"           value={(notificationsQ.data || []).filter((n) => !n.read).length} loading={notificationsQ.isLoading} to="/notifications" />
      </div>

      <div className="mt-8 grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2 space-y-6">
          <PatientRoster roster={roster} loading={patientsQ.isLoading || rosterMatchQs.some((q) => q.isLoading)} />
          <UncertaintyQueue roster={roster} loading={rosterMatchQs.some((q) => q.isLoading)} />
        </div>
        <div className="space-y-6">
          <RematchAlerts notifications={notificationsQ.data} loading={notificationsQ.isLoading} />
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────── Stat card ───────────────────────────── */

function StatCard({ icon: Icon, tone, label, value, loading, to }) {
  return (
    <motion.div whileHover={{ y: -3 }} transition={{ type: 'spring', stiffness: 320, damping: 20 }}>
      <Link to={to} className="card group relative block overflow-hidden p-5 transition-shadow hover:shadow-glow">
        <div className="flex items-start justify-between">
          <span className={cn('flex h-10 w-10 items-center justify-center rounded-xl', TONE_BG[tone])}>
            <Icon className={cn('h-5 w-5', TONE_FG[tone])} />
          </span>
          <ArrowRight className="h-4 w-4 -translate-x-1 text-ink-300 opacity-0 transition-all group-hover:translate-x-0 group-hover:opacity-100" />
        </div>
        <div className="mt-5">
          <div className="text-xs font-semibold uppercase tracking-wide text-ink-400">{label}</div>
          <div className="mt-1 font-display text-3xl font-bold tracking-tight text-ink-900 tabular-nums">
            {loading ? <Skeleton className="h-9 w-20" /> : <AnimatedCounter value={value ?? 0} />}
          </div>
        </div>
      </Link>
    </motion.div>
  );
}

const TONE_BG = {
  brand:   'bg-brand-100',
  accent:  'bg-accent-100',
  warn:    'bg-warn-100',
  success: 'bg-success-100',
};
const TONE_FG = {
  brand:   'text-brand-600',
  accent:  'text-accent-600',
  warn:    'text-warn-600',
  success: 'text-success-600',
};

/* ───────────────────────── Patient roster ──────────────────────── */

function PatientRoster({ roster, loading }) {
  return (
    <div className="card">
      <div className="flex items-center justify-between border-b border-ink-100 px-5 py-4">
        <div>
          <div className="font-display text-lg font-semibold text-ink-900">Patient roster</div>
          <div className="text-xs text-ink-500">
            Top 6 patients in the catalog — eligibility split for each.
          </div>
        </div>
        <Link to="/patients" className="btn-ghost text-xs">All patients →</Link>
      </div>
      <div className="p-3">
        {loading ? (
          <SkeletonRows rows={4} />
        ) : roster.length === 0 ? (
          <div className="rounded-xl border border-dashed border-ink-200 p-6 text-center text-sm text-ink-500">
            No patients yet — import a FHIR bundle on the Patients page.
          </div>
        ) : (
          <ul className="space-y-2">
            {roster.map((r, i) => (
              <motion.li
                key={r.patient.id}
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: Math.min(i, 8) * 0.03 }}
              >
                <Link
                  to={`/patients/${r.patient.id}`}
                  className="flex items-center gap-3 rounded-xl border border-ink-100 bg-white px-3 py-3 transition-colors hover:border-brand-200 hover:bg-brand-50/40"
                >
                  <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-brand-300 to-accent-300 text-xs font-semibold text-white">
                    {(r.patient.first_name?.[0] || '?')}{(r.patient.last_name?.[0] || '')}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-semibold text-ink-800">
                      {r.patient.first_name} {r.patient.last_name}
                    </div>
                    <div className="text-[11px] text-ink-400">
                      {r.patient.external_id || r.patient.sex}
                    </div>
                  </div>
                  <RosterPill tone="success" label="Eligible"  value={r.eligible} />
                  <RosterPill tone="warn"    label="Uncertain" value={r.uncertain} />
                  <RosterPill tone="ink"     label="Ineligible"value={r.ineligible} />
                </Link>
              </motion.li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function RosterPill({ tone, label, value }) {
  const cls = {
    success: 'bg-success-100 text-success-600',
    warn:    'bg-warn-100 text-warn-600',
    ink:     'bg-ink-100 text-ink-600',
  }[tone];
  return (
    <div className={cn('flex flex-col items-center rounded-lg px-2.5 py-1.5', cls)}>
      <span className="font-display text-base font-bold tabular-nums">{value}</span>
      <span className="text-[9px] font-semibold uppercase tracking-wider opacity-80">{label}</span>
    </div>
  );
}

/* ───────────────────────── Uncertainty queue ───────────────────── */

function UncertaintyQueue({ roster, loading }) {
  const items = roster.filter((r) => r.hasUnresolved && r.missing).slice(0, 5);
  return (
    <div className="card">
      <div className="border-b border-ink-100 px-5 py-4">
        <div className="flex items-center gap-2 text-sm font-semibold text-warn-600">
          <AlertTriangle className="h-4 w-4" /> Uncertainty resolution queue
        </div>
        <div className="text-xs text-ink-500">
          Actionable prompts from the matching engine — chase these to unblock enrollment.
        </div>
      </div>
      <div className="p-5">
        {loading ? (
          <SkeletonRows rows={3} />
        ) : items.length === 0 ? (
          <div className="rounded-xl border border-dashed border-ink-200 p-6 text-center text-sm text-ink-500">
            No open uncertainty flags right now.  Nice work.
          </div>
        ) : (
          <ul className="space-y-3">
            {items.map((r) => (
              <li key={r.patient.id} className="rounded-xl border border-ink-100 p-3">
                <div className="mb-1 flex items-center gap-2">
                  <ShieldCheck className="h-3.5 w-3.5 text-warn-500" />
                  <Link to={`/patients/${r.patient.id}`} className="text-sm font-semibold text-ink-800 hover:text-brand-600 hover:underline">
                    {r.patient.first_name} {r.patient.last_name}
                  </Link>
                </div>
                <pre className="whitespace-pre-wrap text-xs text-ink-600 font-sans">
                  {r.missing}
                </pre>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/* ───────────────────────── Rematch alerts ──────────────────────── */

function RematchAlerts({ notifications, loading }) {
  const items = (notifications || []).slice(0, 5);
  return (
    <div className="card">
      <div className="border-b border-ink-100 px-5 py-4">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
          <BellRing className="h-3.5 w-3.5" /> Rematch alerts
        </div>
      </div>
      <div className="p-3">
        {loading ? (
          <SkeletonRows rows={3} />
        ) : items.length === 0 ? (
          <div className="rounded-xl border border-dashed border-ink-200 p-4 text-center text-xs text-ink-500">
            Nothing new yet.
          </div>
        ) : (
          <ul className="space-y-2">
            <AnimatePresence initial={false}>
              {items.map((n, i) => (
                <motion.li key={n.id} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0 }} transition={{ delay: Math.min(i, 6) * 0.03 }}>
                  <div className="rounded-lg border border-ink-100 p-2 text-xs">
                    <div className="flex items-center gap-2">
                      {!n.read && <span className="h-1.5 w-1.5 rounded-full bg-accent-500" />}
                      <span className="font-medium text-ink-800">{n.title}</span>
                      <span className="ml-auto text-ink-400">
                        {formatDistanceToNow(new Date(n.created_at), { addSuffix: true })}
                      </span>
                    </div>
                    <div className="mt-0.5 line-clamp-2 text-ink-500">{n.message}</div>
                  </div>
                </motion.li>
              ))}
            </AnimatePresence>
          </ul>
        )}
      </div>
    </div>
  );
}

