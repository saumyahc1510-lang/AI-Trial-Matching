import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  Activity, ArrowUpRight, ChevronRight, Pill, ScanLine, Stethoscope,
  Syringe, Sparkles, TestTube2, AlertCircle,
} from 'lucide-react';
import { formatDistanceToNow, parseISO, format } from 'date-fns';
import toast from 'react-hot-toast';

import PageHeader from '@/components/ui/PageHeader.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import StatusPill from '@/components/ui/StatusPill.jsx';
import ProgressRing from '@/components/ui/ProgressRing.jsx';
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton.jsx';
import PatientOnboarding from '@/components/onboarding/PatientOnboarding.jsx';
import {
  getPatient, listNotifications, listTrials, patientMatches,
  register as registerApi, trialSummary,
} from '@/api/endpoints.js';
import { useAuth } from '@/auth/AuthContext.jsx';
import { cn } from '@/lib/cn.js';

/**
 * Patient-role landing page.
 *
 * Hard-locks the view to the patient's *own* record — the API enforces
 * the same boundary at the dependency layer (``ensure_can_access_patient``)
 * so even a tampered token wouldn't unlock another patient.
 *
 * When the user signed up without demographics there's no
 * associated_patient_id; in that case we surface the onboarding form
 * inline so they can finish setting up without going through /login.
 */
export default function PatientDashboard({ user }) {
  // No linked patient yet → onboarding card takes the page over.
  if (!user.associated_patient_id) {
    return <PatientOnboardingCard user={user} />;
  }
  return <PatientHome user={user} />;
}

/* ───────────────────────── Onboarding fallback ────────────────── */

function PatientOnboardingCard({ user }) {
  // Re-register with full demographics — backend's onboarding path is
  // idempotent against the email so it would 409.  In practice this
  // should never fire because Login pushes new patients through
  // onboarding first; this is a safety net for accounts created via
  // the bare /admin/users path.
  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <div className="card max-w-xl p-6 text-center">
        <AlertCircle className="mx-auto mb-3 h-10 w-10 text-warn-500" />
        <div className="font-display text-xl font-bold text-ink-900">
          Your profile isn’t linked to a patient record yet.
        </div>
        <p className="mt-2 text-sm text-ink-500">
          Ask a coordinator to link your account, or sign up again with a
          fresh email to walk through the onboarding flow.
        </p>
      </div>
    </div>
  );
}

/* ───────────────────────── Home view ──────────────────────────── */

function PatientHome({ user }) {
  const navigate = useNavigate();
  const patientId = user.associated_patient_id;

  const patientQ = useQuery({
    queryKey: ['patient', patientId],
    queryFn: () => getPatient(patientId),
  });
  const matchesQ = useQuery({
    queryKey: ['patient', patientId, 'matches'],
    queryFn: () => patientMatches(patientId, { limit: 10 }),
  });
  const notifsQ = useQuery({
    queryKey: ['notifications', 'recent', user.id],
    queryFn: () => listNotifications({ limit: 5 }),
  });

  const matches = matchesQ.data?.matches || [];
  const stats = {
    eligible:   matches.filter((m) => m.overall_status === 'eligible').length,
    uncertain:  matches.filter((m) => m.overall_status === 'uncertain').length,
    ineligible: matches.filter((m) => m.overall_status === 'ineligible').length,
  };

  return (
    <div>
      <PageHeader
        eyebrow={`Welcome, ${(user.full_name || '').split(' ')[0] || 'there'}`}
        title="Your trial dashboard"
        description="A clear, plain-language view of the trials you could join — and what would unlock the rest."
      />

      {/* Quick stats */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <PatientStatCard tone="success" label="Trials you qualify for" value={stats.eligible} />
        <PatientStatCard tone="warn"    label="Needs more information"  value={stats.uncertain} />
        <PatientStatCard tone="ink"     label="Not a match"               value={stats.ineligible} />
      </div>

      {/* Top match — big featured card */}
      <div className="mt-6">
        {matchesQ.isLoading ? (
          <Skeleton className="h-44" />
        ) : matches.length === 0 ? (
          <EmptyState
            icon={Sparkles}
            title="No matches yet"
            description={
              patientQ.data?.medical_events?.length === 0
                ? 'Add a diagnosis to your record so the matcher has something to work with.'
                : "Let's find your trials — we'll ask a few short questions, then show you what you could join."
            }
            action={
              <div className="flex flex-wrap items-center justify-center gap-2">
                <Link to="/find-matches" className="btn-primary">
                  <Sparkles className="h-4 w-4" />
                  Find a trial that matches you
                </Link>
                <Link to="/trials" className="btn-ghost">
                  Browse all trials
                </Link>
              </div>
            }
          />
        ) : (
          <FeaturedMatchCard match={matches[0]} />
        )}
      </div>

      {/* Two-column body */}
      <div className="mt-8 grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <MatchListCard matches={matches.slice(1)} loading={matchesQ.isLoading} />
          <div className="mt-6">
            <TimelineCard
              loading={patientQ.isLoading}
              events={patientQ.data?.medical_events || []}
              language={patientQ.data?.preferred_language || 'en'}
            />
          </div>
        </div>
        <div className="space-y-6">
          <ProfileCard patient={patientQ.data} loading={patientQ.isLoading} />
          <NotificationsPeek notifications={notifsQ.data || []} />
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────── Stat card ───────────────────────────── */

function PatientStatCard({ tone, label, value }) {
  const cls = {
    success: 'bg-success-100 text-success-600',
    warn:    'bg-warn-100    text-warn-600',
    ink:     'bg-ink-100     text-ink-500',
  }[tone];
  return (
    <motion.div
      whileHover={{ y: -3 }}
      transition={{ type: 'spring', stiffness: 320, damping: 20 }}
      className="card p-5"
    >
      <div className={cn('mb-3 inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-[10px] font-semibold uppercase tracking-wider', cls)}>
        {label}
      </div>
      <div className="font-display text-4xl font-bold tabular-nums text-ink-900">
        {value}
      </div>
    </motion.div>
  );
}

/* ───────────────────────── Featured match ──────────────────────── */

function FeaturedMatchCard({ match }) {
  const ring = ringTone(match.overall_status);
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="card relative overflow-hidden p-6"
    >
      <div
        aria-hidden
        className={cn(
          'pointer-events-none absolute -inset-x-10 -top-10 h-40 opacity-50 blur-3xl',
          ring === 'success' && 'bg-success-100',
          ring === 'warn'    && 'bg-warn-100',
          ring === 'danger'  && 'bg-danger-100',
        )}
      />
      <div className="relative flex flex-wrap items-center gap-6">
        <ProgressRing
          value={match.match_score || 0}
          tone={ring}
          label="Your fit"
        />
        <div className="flex-1">
          <div className="mb-2 inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-brand-500">
            <Sparkles className="h-3.5 w-3.5" /> Top match
          </div>
          <div className="font-display text-2xl font-bold text-ink-900">
            Trial {match.trial_id.slice(0, 8)}…
          </div>
          <p className="mt-1 text-sm text-ink-500">
            {match.missing_data_summary || 'You meet every criterion the system could evaluate. A coordinator will reach out to confirm the next steps.'}
          </p>
          <div className="mt-3 flex items-center gap-3">
            <StatusPill status={match.overall_status} />
            <Link to={`/matching/${match.id}`} className="btn-primary">
              Why this match? <ChevronRight className="h-4 w-4" />
            </Link>
          </div>
        </div>
      </div>
    </motion.div>
  );
}

function MatchListCard({ matches, loading }) {
  return (
    <div className="card">
      <div className="border-b border-ink-100 px-5 py-4">
        <div className="font-display text-lg font-semibold text-ink-900">
          Other matches
        </div>
        <div className="text-xs text-ink-500">
          Ranked by clinical fit, with diversity-aware ordering applied on top.
        </div>
      </div>
      <div className="p-3">
        {loading ? (
          <SkeletonRows rows={3} />
        ) : matches.length === 0 ? (
          <div className="rounded-xl border border-dashed border-ink-200 p-6 text-center text-sm text-ink-500">
            No other matches right now.
          </div>
        ) : (
          <ul className="space-y-2">
            {matches.map((m, i) => (
              <motion.li
                key={m.id}
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: Math.min(i, 8) * 0.03 }}
              >
                <Link
                  to={`/matching/${m.id}`}
                  className="flex items-center gap-4 rounded-xl border border-ink-100 bg-white px-3 py-3 transition-colors hover:border-brand-200 hover:bg-brand-50/40"
                >
                  <ProgressRing
                    value={m.match_score || 0}
                    tone={ringTone(m.overall_status)}
                    size={56} stroke={6}
                    showPercent
                  />
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-display text-sm font-semibold text-ink-800">
                      Trial {m.trial_id.slice(0, 8)}…
                    </div>
                    <div className="mt-0.5 line-clamp-1 text-xs text-ink-500">
                      {m.criteria_met} met · {m.criteria_uncertain} uncertain · {m.criteria_not_met} not met
                    </div>
                  </div>
                  <StatusPill status={m.overall_status} size="sm" />
                  <ArrowUpRight className="h-4 w-4 text-ink-300" />
                </Link>
              </motion.li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/* ───────────────────────── Timeline (mini) ─────────────────────── */

const TYPE_META = {
  diagnosis:       { Icon: Stethoscope, tone: 'brand'   },
  medication:      { Icon: Pill,        tone: 'accent'  },
  lab_result:      { Icon: TestTube2,   tone: 'success' },
  procedure:       { Icon: Syringe,     tone: 'warn'    },
  vital_sign:      { Icon: Activity,    tone: 'success' },
  imaging:         { Icon: ScanLine,    tone: 'brand'   },
  allergy:         { Icon: AlertCircle, tone: 'danger'  },
};

function TimelineCard({ events, loading }) {
  const recent = (events || []).slice().sort(
    (a, b) => new Date(b.event_date) - new Date(a.event_date),
  ).slice(0, 6);
  return (
    <div className="card">
      <div className="border-b border-ink-100 px-5 py-4">
        <div className="font-display text-lg font-semibold text-ink-900">
          Your medical timeline
        </div>
        <div className="text-xs text-ink-500">
          The most-recent events parsed from your health record.
        </div>
      </div>
      <div className="p-5">
        {loading ? (
          <SkeletonRows rows={4} />
        ) : recent.length === 0 ? (
          <div className="rounded-xl border border-dashed border-ink-200 p-6 text-center text-sm text-ink-500">
            Nothing recorded yet. Add conditions in your profile to populate this.
          </div>
        ) : (
          <ul className="space-y-3">
            {recent.map((evt, i) => {
              const meta = TYPE_META[evt.event_type] || TYPE_META.diagnosis;
              const Icon = meta.Icon;
              return (
                <motion.li
                  key={evt.id}
                  initial={{ opacity: 0, x: -8 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: Math.min(i, 6) * 0.04 }}
                  className="flex items-start gap-3"
                >
                  <span className={cn(
                    'flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl',
                    {
                      brand:   'bg-brand-100 text-brand-600',
                      accent:  'bg-accent-100 text-accent-600',
                      success: 'bg-success-100 text-success-600',
                      warn:    'bg-warn-100 text-warn-600',
                      danger:  'bg-danger-100 text-danger-600',
                    }[meta.tone],
                  )}>
                    <Icon className="h-5 w-5" />
                  </span>
                  <div className="flex-1">
                    <div className="text-sm font-semibold text-ink-800">
                      {evt.display_name}
                    </div>
                    <div className="text-xs text-ink-500">
                      {format(parseISO(evt.event_date), 'MMM d, yyyy')}
                      {evt.value && (
                        <> · <span className="font-medium text-ink-700">{evt.value}{evt.unit ? ` ${evt.unit}` : ''}</span></>
                      )}
                    </div>
                  </div>
                </motion.li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

/* ───────────────────────── Profile card ────────────────────────── */

function ProfileCard({ patient, loading }) {
  if (loading) return <Skeleton className="h-44" />;
  if (!patient) return null;
  return (
    <div className="card p-5">
      <div className="mb-4 flex items-center gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-400 to-accent-400 text-sm font-semibold text-white">
          {patient.first_name?.[0]}{patient.last_name?.[0]}
        </span>
        <div>
          <div className="font-display text-base font-semibold text-ink-900">
            {patient.first_name} {patient.last_name}
          </div>
          <div className="text-xs text-ink-500">
            {patient.external_id || 'No record ID'}
          </div>
        </div>
      </div>
      <Row label="Sex"        value={patient.sex} />
      <Row label="DOB"        value={patient.date_of_birth} />
      <Row label="Language"   value={(patient.preferred_language || 'en').toUpperCase()} />
      <Row label="Race"       value={patient.race || '—'} />
      <Row label="Ethnicity"  value={patient.ethnicity || '—'} />
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between border-b border-ink-100 py-2 last:border-b-0 text-sm">
      <span className="text-xs uppercase tracking-wider text-ink-400">{label}</span>
      <span className="font-medium text-ink-800">{value}</span>
    </div>
  );
}

/* ───────────────────────── Notifications peek ──────────────────── */

function NotificationsPeek({ notifications }) {
  const items = notifications.slice(0, 3);
  return (
    <div className="card p-5">
      <div className="mb-3 flex items-center justify-between">
        <div className="text-xs font-semibold uppercase tracking-wider text-ink-400">
          Recent activity
        </div>
        <Link to="/notifications" className="btn-ghost text-xs">View all →</Link>
      </div>
      {items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-ink-200 p-4 text-center text-xs text-ink-500">
          You’ll see new-match alerts here.
        </div>
      ) : (
        <ul className="space-y-2">
          {items.map((n) => (
            <li key={n.id} className="rounded-lg border border-ink-100 p-2 text-xs">
              <div className="flex items-center gap-2">
                {!n.read && <span className="h-1.5 w-1.5 rounded-full bg-accent-500" />}
                <span className="font-medium text-ink-800">{n.title}</span>
                <span className="ml-auto text-ink-400">
                  {formatDistanceToNow(new Date(n.created_at), { addSuffix: true })}
                </span>
              </div>
              <div className="mt-0.5 line-clamp-2 text-ink-500">{n.message}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ───────────────────────── Helpers ─────────────────────────────── */

function ringTone(status) {
  return ({
    eligible:   'success',
    ineligible: 'danger',
    uncertain:  'warn',
  })[status] || 'brand';
}
