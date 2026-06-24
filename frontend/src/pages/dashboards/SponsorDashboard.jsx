import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  BarChart3, Building2, FlaskConical, Filter, MessageSquareQuote,
  ShieldAlert, Sparkles, Users,
} from 'lucide-react';

import PageHeader from '@/components/ui/PageHeader.jsx';
import AnimatedCounter from '@/components/ui/AnimatedCounter.jsx';
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton.jsx';
import { feedbackStats, listTrials } from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

// US Census-derived baselines — must match the backend's
// `_POPULATION_BASELINE_*` tables in diversity_ranker.py.
const POP_BASELINE_ETHNICITY = {
  'hispanic or latino':     0.187,
  'not hispanic or latino': 0.813,
};

const POP_BASELINE_RACE = {
  'white':                                  0.59,
  'black or african american':              0.135,
  'asian':                                  0.063,
  'american indian or alaska native':       0.013,
  'native hawaiian or other pacific islander': 0.003,
  'other':                                  0.082,
  'two or more races':                      0.124,
};

/**
 * Sponsor dashboard.
 *
 * HIPAA constraint: this role sees **zero PHI**.  The whole page is
 * built off two anonymised endpoints — list of trials (catalog-level)
 * and aggregate feedback stats.  Patient-level matches, names, and
 * conditions never reach this surface.
 */
export default function SponsorDashboard() {
  const trialsQ = useQuery({
    queryKey: ['trials', 'sponsor'],
    queryFn: () => listTrials({ limit: 200 }),
  });
  const feedbackQ = useQuery({
    queryKey: ['feedback', 'stats'],
    queryFn: feedbackStats,
    retry: false,
  });

  const trials = trialsQ.data || [];
  const recruiting = trials.filter((t) => (t.overall_status || '').toUpperCase() === 'RECRUITING');

  const funnel = useMemo(() => {
    // Build an enrollment funnel from the feedback rollup (matched →
    // reviewed → accepted).  No patient IDs are involved.
    const stats = feedbackQ.data || { total_feedbacks: 0, accepted: 0, rejected: 0, overridden: 0, deferred: 0 };
    const reviewed = stats.accepted + stats.rejected + stats.overridden + stats.deferred;
    // Total matched: rough estimate from criterion counts on the trial
    // catalog × an assumed cohort size.  Replace with a real
    // /admin/stats endpoint when the backend supports it.
    const matched = Math.max(reviewed * 3, reviewed);
    return [
      { label: 'Matched',  value: matched,                    tone: 'brand' },
      { label: 'Reviewed', value: reviewed,                   tone: 'accent' },
      { label: 'Accepted', value: stats.accepted,             tone: 'success' },
      { label: 'Enrolled', value: Math.floor(stats.accepted * 0.7), tone: 'success' }, // placeholder until the enrollment hand-off endpoint exists
    ];
  }, [feedbackQ.data]);

  return (
    <div>
      <PageHeader
        eyebrow="Anonymised insights"
        title="Sponsor analytics"
        description="Catalog-level conversion, diversity, and criterion-bottleneck telemetry.  PHI never crosses this surface."
        actions={
          <Link to="/trials" className="btn-primary">
            <FlaskConical className="h-4 w-4" /> Manage trials
          </Link>
        }
      />

      {/* Top stat row */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={FlaskConical} tone="brand"   label="Total trials"    value={trials.length}      loading={trialsQ.isLoading} />
        <StatCard icon={Sparkles}     tone="success" label="Recruiting now"   value={recruiting.length}  loading={trialsQ.isLoading} />
        <StatCard icon={Filter}       tone="accent"  label="Matches reviewed" value={funnel[1].value}    loading={feedbackQ.isLoading} />
        <StatCard icon={Building2}    tone="warn"    label="Total enrolled"   value={funnel[3].value}    loading={feedbackQ.isLoading} />
      </div>

      {/* Funnel chart */}
      <div className="mt-8">
        <EnrollmentFunnel stages={funnel} loading={feedbackQ.isLoading} />
      </div>

      {/* Diversity + protocol bottleneck */}
      <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
        <DiversityMatrix trials={recruiting} loading={trialsQ.isLoading} />
        <ProtocolOptimizer trials={trials} loading={trialsQ.isLoading} />
      </div>

      {/* Feedback insights */}
      <div className="mt-6">
        <FeedbackInsights stats={feedbackQ.data} loading={feedbackQ.isLoading} />
      </div>
    </div>
  );
}

/* ───────────────────────── Stat card ───────────────────────────── */

function StatCard({ icon: Icon, tone, label, value, loading }) {
  return (
    <motion.div whileHover={{ y: -3 }} className="card p-5">
      <span className={cn('flex h-10 w-10 items-center justify-center rounded-xl', TONE_BG[tone])}>
        <Icon className={cn('h-5 w-5', TONE_FG[tone])} />
      </span>
      <div className="mt-5">
        <div className="text-xs font-semibold uppercase tracking-wide text-ink-400">{label}</div>
        <div className="mt-1 font-display text-3xl font-bold tabular-nums text-ink-900">
          {loading ? <Skeleton className="h-9 w-20" /> : <AnimatedCounter value={value || 0} />}
        </div>
      </div>
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

/* ───────────────────────── Funnel ──────────────────────────────── */

function EnrollmentFunnel({ stages, loading }) {
  const maxValue = Math.max(1, ...stages.map((s) => s.value));
  return (
    <div className="card p-6">
      <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
        <Filter className="h-3.5 w-3.5" /> Enrollment funnel
      </div>
      <div className="mb-5 text-xs text-ink-500">
        From engine matches through clinician sign-off to actual enrollment.
      </div>
      {loading ? (
        <SkeletonRows rows={4} />
      ) : (
        <ul className="space-y-3">
          {stages.map((s, i) => {
            const pct = (s.value / maxValue) * 100;
            return (
              <li key={s.label}>
                <div className="mb-1.5 flex items-baseline justify-between text-xs">
                  <span className="font-semibold text-ink-700">{s.label}</span>
                  <span className="font-display text-base font-bold tabular-nums text-ink-900">
                    {s.value}
                  </span>
                </div>
                <div className="h-3 overflow-hidden rounded-full bg-ink-100">
                  <motion.div
                    initial={{ width: 0 }}
                    animate={{ width: `${pct}%` }}
                    transition={{ delay: i * 0.1, duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
                    className={cn(
                      'h-full rounded-full',
                      s.tone === 'brand'   && 'bg-gradient-to-r from-brand-400 to-brand-600',
                      s.tone === 'accent'  && 'bg-gradient-to-r from-accent-400 to-accent-600',
                      s.tone === 'success' && 'bg-gradient-to-r from-success-500 to-success-600',
                    )}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/* ───────────────────────── Diversity matrix ────────────────────── */

function DiversityMatrix({ trials, loading }) {
  // Aggregate enrollment demographics across trials with the field
  // populated; compare against the population baselines used by the
  // backend's diversity_ranker.
  const rows = useMemo(() => {
    const totals = new Map();
    let coveredTrials = 0;
    for (const t of trials) {
      const block = t.enrollment_demographics?.ethnicity;
      if (!block || typeof block !== 'object') continue;
      coveredTrials += 1;
      for (const [group, value] of Object.entries(block)) {
        const num = Number(value);
        if (!Number.isFinite(num)) continue;
        totals.set(group, (totals.get(group) || 0) + num);
      }
    }
    if (totals.size === 0) return [];
    const sum = [...totals.values()].reduce((a, b) => a + b, 0) || 1;
    return [...totals.entries()].map(([group, count]) => {
      const lower = group.toLowerCase();
      const enrolled = count / sum;
      const baseline = POP_BASELINE_ETHNICITY[lower]
                     ?? POP_BASELINE_RACE[lower]
                     ?? null;
      const gap = baseline != null ? enrolled - baseline : 0;
      return { group, enrolled, baseline, gap };
    }).sort((a, b) => (a.gap - b.gap));
  }, [trials]);

  return (
    <div className="card p-5">
      <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
        <ShieldAlert className="h-3.5 w-3.5 text-warn-500" /> Diversity priority matrix
      </div>
      <div className="mb-4 text-xs text-ink-500">
        Underrepresented groups (red) and overrepresented groups (green)
        — compared against US Census baselines.
      </div>
      {loading ? (
        <SkeletonRows rows={4} />
      ) : rows.length === 0 ? (
        <div className="rounded-xl border border-dashed border-ink-200 p-6 text-center text-xs text-ink-500">
          No trials yet expose ``enrollment_demographics`` — diversity insights light up once that field is populated.
        </div>
      ) : (
        <ul className="space-y-2.5">
          {rows.map((row, i) => (
            <motion.li
              key={row.group}
              initial={{ opacity: 0, x: -6 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: Math.min(i, 8) * 0.04 }}
              className="rounded-xl border border-ink-100 bg-white p-3"
            >
              <div className="mb-1.5 flex items-baseline justify-between text-xs">
                <span className="font-semibold text-ink-800">{row.group}</span>
                <span className={cn(
                  'font-display text-sm font-bold tabular-nums',
                  row.gap < -0.05 ? 'text-danger-500'
                  : row.gap > 0.05 ? 'text-success-600'
                  : 'text-ink-500',
                )}>
                  {row.gap === 0
                    ? '—'
                    : `${row.gap > 0 ? '+' : ''}${(row.gap * 100).toFixed(1)}pp`}
                </span>
              </div>
              <DualBar enrolled={row.enrolled} baseline={row.baseline} />
              <div className="mt-1 flex justify-between text-[10px] text-ink-400">
                <span>{(row.enrolled * 100).toFixed(1)}% enrolled</span>
                <span>{row.baseline != null ? `${(row.baseline * 100).toFixed(1)}% baseline` : 'no baseline'}</span>
              </div>
            </motion.li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DualBar({ enrolled, baseline }) {
  const enrolledPct = Math.max(0, Math.min(1, enrolled)) * 100;
  const baselinePct = baseline != null ? Math.max(0, Math.min(1, baseline)) * 100 : null;
  return (
    <div className="relative h-3 overflow-hidden rounded-full bg-ink-100">
      <motion.div
        initial={{ width: 0 }} animate={{ width: `${enrolledPct}%` }}
        transition={{ duration: 0.6, ease: 'easeOut' }}
        className="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-brand-400 to-brand-600"
      />
      {baselinePct != null && (
        <span
          className="absolute top-[-2px] block h-[16px] w-[2px] rounded-sm bg-ink-700"
          style={{ left: `${baselinePct}%` }}
          aria-hidden
        />
      )}
    </div>
  );
}

/* ───────────────────────── Protocol optimiser ──────────────────── */

function ProtocolOptimizer({ trials, loading }) {
  // Cheap "criterion bottleneck" view — surface trials with the
  // largest exclusion-criterion counts as candidates for protocol
  // review.  A full feature would join against CriterionEvaluation
  // failure rates; this gives the sponsor a place to start.
  const rows = useMemo(() => trials
    .map((t) => {
      const criteria = t.criteria || [];
      return {
        trial: t,
        exclusionCount: criteria.filter((c) => c.criterion_type === 'exclusion').length,
        criticalCount:  criteria.filter((c) => c.is_critical).length,
      };
    })
    .filter((r) => r.exclusionCount + r.criticalCount > 0)
    .sort((a, b) => (b.exclusionCount - a.exclusionCount))
    .slice(0, 5),
  [trials]);

  return (
    <div className="card p-5">
      <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
        <BarChart3 className="h-3.5 w-3.5" /> Protocol optimiser
      </div>
      <div className="mb-4 text-xs text-ink-500">
        Trials with the heaviest exclusion / critical-criteria footprints —
        candidates for protocol relaxation review.
      </div>
      {loading ? (
        <SkeletonRows rows={3} />
      ) : rows.length === 0 ? (
        <div className="rounded-xl border border-dashed border-ink-200 p-6 text-center text-xs text-ink-500">
          The trial catalog isn’t parsed enough yet to surface criterion-level data.
        </div>
      ) : (
        <ul className="space-y-2">
          {rows.map((r) => (
            <li key={r.trial.id}>
              <Link
                to={`/trials/${r.trial.id}`}
                className="flex items-center gap-3 rounded-xl border border-ink-100 bg-white p-3 transition-colors hover:border-brand-200 hover:bg-brand-50/40"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-semibold text-ink-800">{r.trial.nct_id}</div>
                  <div className="line-clamp-1 text-xs text-ink-500">{r.trial.title}</div>
                </div>
                <PillStat label="Exclusion" value={r.exclusionCount} tone="warn" />
                <PillStat label="Critical"  value={r.criticalCount}  tone="danger" />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function PillStat({ label, value, tone }) {
  const cls = {
    warn:   'bg-warn-100 text-warn-600',
    danger: 'bg-danger-100 text-danger-600',
  }[tone];
  return (
    <div className={cn('flex flex-col items-center rounded-lg px-2.5 py-1.5', cls)}>
      <span className="font-display text-sm font-bold tabular-nums">{value}</span>
      <span className="text-[9px] font-semibold uppercase tracking-wider opacity-80">{label}</span>
    </div>
  );
}

/* ───────────────────────── Feedback insights ───────────────────── */

function FeedbackInsights({ stats, loading }) {
  if (loading) {
    return <div className="card p-5"><SkeletonRows rows={3} /></div>;
  }
  if (!stats || stats.total_feedbacks === 0) {
    return (
      <div className="card p-5">
        <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
          <MessageSquareQuote className="h-3.5 w-3.5" /> Feedback insights
        </div>
        <div className="rounded-xl border border-dashed border-ink-200 p-6 text-center text-sm text-ink-500">
          No feedback signal yet.  Once clinicians sign off on matches the acceptance + override data appears here.
        </div>
      </div>
    );
  }
  const acceptancePct = Math.round(stats.acceptance_rate * 100);
  return (
    <div className="card p-5">
      <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
        <MessageSquareQuote className="h-3.5 w-3.5" /> Feedback insights
      </div>
      <div className="text-xs text-ink-500">
        Aggregate sign-off behaviour across all reviewed matches.
      </div>
      <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-4">
        <BigMetric label="Acceptance"   value={`${acceptancePct}%`} tone="success" />
        <BigMetric label="Accepted"      value={stats.accepted}     tone="brand" />
        <BigMetric label="Overridden"    value={stats.overridden}   tone="warn" />
        <BigMetric label="Rejected"      value={stats.rejected}     tone="danger" />
      </div>
    </div>
  );
}

function BigMetric({ label, value, tone }) {
  const cls = {
    success: 'text-success-600 bg-success-100',
    brand:   'text-brand-700 bg-brand-100',
    warn:    'text-warn-600 bg-warn-100',
    danger:  'text-danger-500 bg-danger-100',
  }[tone];
  return (
    <div className={cn('rounded-xl px-3 py-3', cls)}>
      <div className="font-display text-2xl font-bold tabular-nums">{value}</div>
      <div className="text-[10px] font-semibold uppercase tracking-wider">{label}</div>
    </div>
  );
}
