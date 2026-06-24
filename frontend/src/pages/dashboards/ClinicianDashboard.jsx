import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  ArrowUpRight, BadgeCheck, Brain, CheckCircle2, ClipboardList,
  ChevronDown, FileSignature, Gauge, Quote, Stethoscope, XCircle,
} from 'lucide-react';
import toast from 'react-hot-toast';

import PageHeader from '@/components/ui/PageHeader.jsx';
import AnimatedCounter from '@/components/ui/AnimatedCounter.jsx';
import StatusPill from '@/components/ui/StatusPill.jsx';
import ProgressRing from '@/components/ui/ProgressRing.jsx';
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton.jsx';
import {
  explainMatch, listPatients, patientMatches, reviewMatch, submitFeedback,
} from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

/**
 * Clinician dashboard.
 *
 * The clinician's work is *review and sign-off* — not data wrangling.
 * The page surfaces:
 *   - A counter row showing total Pending / Accepted / Rejected
 *     review states across all the patients they can see.
 *   - A queue of pending matches with a one-click expand to view the
 *     per-criterion evaluation table inline + a sign-off bar at the
 *     bottom of each card (Accept / Reject / Defer).
 *
 * Every decision threads through the existing /matching/results/:id/review
 * endpoint, so the audit log + match_result.coordinator_status row stay
 * coherent with what the coordinator dashboard shows.
 */
export default function ClinicianDashboard() {
  const patientsQ = useQuery({
    queryKey: ['patients', 'clinician'],
    queryFn: () => listPatients({ limit: 50 }),
  });

  // Pull matches for every visible patient (capped at first 6) so we
  // can compute the review queue + counter row without a dedicated
  // backend endpoint.
  const patientsForQueue = (patientsQ.data || []).slice(0, 6);
  const matchQs = useQueries({
    queries: patientsForQueue.map((p) => ({
      queryKey: ['patient', p.id, 'matches', 'clinician'],
      queryFn: () => patientMatches(p.id, { limit: 30 }),
      enabled: !!p.id,
    })),
  });

  // Flatten + decorate matches with their patient so we can render a
  // single ranked queue across the whole roster.
  const queue = useMemo(() => {
    const out = [];
    matchQs.forEach((q, i) => {
      const matches = q.data?.matches || [];
      const patient = patientsForQueue[i];
      matches.forEach((m) => out.push({ patient, match: m }));
    });
    return out.sort((a, b) => (b.match.final_rank_score || 0) - (a.match.final_rank_score || 0));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(patientsForQueue.map((p) => p.id)), matchQs.map((q) => q.dataUpdatedAt).join(',')]);

  const stats = {
    pending:  queue.filter((row) => row.match.coordinator_status === 'pending_review').length,
    accepted: queue.filter((row) => row.match.coordinator_status === 'accepted').length,
    rejected: queue.filter((row) => row.match.coordinator_status === 'rejected').length,
    deferred: queue.filter((row) => row.match.coordinator_status === 'deferred').length,
  };

  const loading = patientsQ.isLoading || matchQs.some((q) => q.isLoading);
  const pendingQueue = queue.filter((row) => row.match.coordinator_status === 'pending_review');

  return (
    <div>
      <PageHeader
        eyebrow="Sign-off"
        title="Clinical review queue"
        description="LLM verdicts, per-criterion evidence, and a one-click sign-off bar.  Every override lands in the audit log with your justification."
      />

      {/* Counter row */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={ClipboardList} tone="warn"    label="Pending review" value={stats.pending}  loading={loading} />
        <StatCard icon={CheckCircle2}  tone="success" label="Accepted"        value={stats.accepted} loading={loading} />
        <StatCard icon={XCircle}       tone="danger"  label="Rejected"        value={stats.rejected} loading={loading} />
        <StatCard icon={Gauge}         tone="brand"   label="Deferred"        value={stats.deferred} loading={loading} />
      </div>

      <div className="mt-8">
        <div className="mb-3 flex items-baseline justify-between">
          <div>
            <div className="font-display text-lg font-semibold text-ink-900">
              Pending review queue
            </div>
            <div className="text-xs text-ink-500">
              Ranked by clinical fit × confidence × diversity priority.
            </div>
          </div>
          <Link to="/patients" className="btn-ghost text-xs">All patients →</Link>
        </div>

        {loading ? (
          <SkeletonRows rows={4} />
        ) : pendingQueue.length === 0 ? (
          <div className="card p-8 text-center text-sm text-ink-500">
            <BadgeCheck className="mx-auto mb-3 h-10 w-10 text-success-500" />
            Nothing pending.  When the matching engine writes a new
            result it will land here for your sign-off.
          </div>
        ) : (
          <ul className="space-y-3">
            {pendingQueue.map((row, i) => (
              <motion.li
                key={row.match.id}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: Math.min(i, 8) * 0.03 }}
              >
                <ReviewCard patient={row.patient} match={row.match} />
              </motion.li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/* ───────────────────────── Counter card ────────────────────────── */

function StatCard({ icon: Icon, tone, label, value, loading }) {
  return (
    <motion.div whileHover={{ y: -3 }} className="card p-5">
      <div className="flex items-start justify-between">
        <span className={cn('flex h-10 w-10 items-center justify-center rounded-xl', TONE_BG[tone])}>
          <Icon className={cn('h-5 w-5', TONE_FG[tone])} />
        </span>
      </div>
      <div className="mt-5">
        <div className="text-xs font-semibold uppercase tracking-wide text-ink-400">{label}</div>
        <div className="mt-1 font-display text-3xl font-bold tracking-tight text-ink-900 tabular-nums">
          {loading ? <Skeleton className="h-9 w-20" /> : <AnimatedCounter value={value} />}
        </div>
      </div>
    </motion.div>
  );
}

const TONE_BG = {
  brand:   'bg-brand-100',
  warn:    'bg-warn-100',
  success: 'bg-success-100',
  danger:  'bg-danger-100',
};
const TONE_FG = {
  brand:   'text-brand-600',
  warn:    'text-warn-600',
  success: 'text-success-600',
  danger:  'text-danger-500',
};

/* ───────────────────────── Review card ─────────────────────────── */

function ReviewCard({ patient, match }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [notes, setNotes] = useState('');

  const explainQ = useQuery({
    queryKey: ['match', match.id, 'explain'],
    queryFn: () => explainMatch(match.id, 'json'),
    enabled: open,
  });

  const review = useMutation({
    mutationFn: (coordinator_status) => reviewMatch(match.id, {
      coordinator_status,
      coordinator_notes: notes || null,
    }),
    onSuccess: (data) => {
      toast.success(`Marked ${data.coordinator_status.replace('_', ' ')}.`);
      qc.invalidateQueries({ queryKey: ['patient', patient.id, 'matches', 'clinician'] });
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Review failed.'),
  });

  const ring = ringTone(match.overall_status);

  return (
    <div className="card overflow-hidden">
      <div className="flex items-center gap-5 p-5">
        <ProgressRing
          value={match.match_score || 0}
          tone={ring}
          size={92} stroke={9}
          label="Match"
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Link to={`/patients/${patient.id}`} className="font-display text-base font-semibold text-ink-900 hover:text-brand-600 hover:underline">
              {patient.first_name} {patient.last_name}
            </Link>
            <span className="text-[11px] text-ink-400">·</span>
            <span className="text-[11px] font-mono text-ink-500">
              Trial {match.trial_id.slice(0, 8)}…
            </span>
            <StatusPill status={match.overall_status} size="sm" className="ml-1" />
          </div>
          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs">
            <Counter label="Met"        value={match.criteria_met} tone="success" />
            <Counter label="Not met"    value={match.criteria_not_met} tone="danger" />
            <Counter label="Uncertain"  value={match.criteria_uncertain} tone="warn" />
            <Counter label="Confidence" value={`${Math.round((match.confidence_score || 0) * 100)}%`} />
          </div>
          {match.missing_data_summary && (
            <div className="mt-2 line-clamp-1 rounded-lg bg-warn-100/50 px-3 py-1.5 text-[11px] text-warn-700">
              <span className="font-semibold">Missing:</span> {match.missing_data_summary}
            </div>
          )}
        </div>
        <Link to={`/matching/${match.id}`} className="btn-secondary shrink-0">
          Full record <ArrowUpRight className="h-4 w-4" />
        </Link>
      </div>

      {/* Expand toggle */}
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between border-t border-ink-100 bg-ink-50/50 px-5 py-2.5 text-xs font-semibold text-ink-500 hover:bg-ink-100"
      >
        <span className="inline-flex items-center gap-1.5">
          <Brain className="h-3.5 w-3.5" />
          {open ? 'Hide' : 'Show'} per-criterion evidence
        </span>
        <ChevronDown className={cn('h-4 w-4 transition-transform', open && 'rotate-180')} />
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden border-t border-ink-100 bg-white"
          >
            <div className="p-5">
              {explainQ.isLoading ? (
                <SkeletonRows rows={3} />
              ) : (
                <CriterionTable rows={(explainQ.data?.rows || []).slice(0, 6)} />
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Sign-off bar */}
      <div className="flex flex-wrap items-center gap-2 border-t border-ink-100 bg-ink-50/40 px-5 py-3">
        <input
          className="input flex-1"
          placeholder="Add an optional clinical justification…"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
        <button
          onClick={() => review.mutate('accepted')}
          disabled={review.isPending}
          className="btn-primary"
        >
          <CheckCircle2 className="h-4 w-4" /> Accept
        </button>
        <button
          onClick={() => review.mutate('rejected')}
          disabled={review.isPending}
          className="btn-secondary border-danger-200 bg-danger-100/40 text-danger-600 hover:bg-danger-100 hover:text-danger-600"
        >
          <XCircle className="h-4 w-4" /> Reject
        </button>
        <button
          onClick={() => review.mutate('deferred')}
          disabled={review.isPending}
          className="btn-ghost"
        >
          Defer
        </button>
      </div>
    </div>
  );
}

function Counter({ label, value, tone }) {
  const cls = {
    success: 'text-success-600',
    warn:    'text-warn-600',
    danger:  'text-danger-500',
  }[tone] || 'text-ink-800';
  return (
    <div>
      <div className="text-[9px] font-semibold uppercase tracking-wider text-ink-400">{label}</div>
      <div className={cn('font-display text-base font-bold tabular-nums', cls)}>{value}</div>
    </div>
  );
}

/* ───────────────────────── Inline criterion table ──────────────── */

function CriterionTable({ rows }) {
  if (rows.length === 0) {
    return <div className="text-sm text-ink-400">No criterion rows available.</div>;
  }
  return (
    <ul className="space-y-2">
      {rows.map((row) => (
        <li key={row.order_index} className="rounded-xl border border-ink-100 bg-white px-3 py-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill status={row.status} size="sm" />
            <span className="chip chip-ink text-[10px]">{row.criterion_type}</span>
            <span className="ml-auto text-[10px] text-ink-400">
              via {row.evaluator} · conf {(row.confidence * 100).toFixed(0)}%
            </span>
          </div>
          <div className="mt-1 text-sm font-medium text-ink-800">
            {row.parsed_description || row.criterion_text}
          </div>
          <div className="mt-1 text-xs text-ink-500">{row.reasoning}</div>
          {row.evidence_text && (
            <blockquote className="mt-1.5 flex gap-2 rounded-md border-l-2 border-l-brand-300 bg-brand-50 px-2 py-1 text-xs italic text-ink-700">
              <Quote className="h-3 w-3 shrink-0 text-brand-400" />
              {row.evidence_text}
            </blockquote>
          )}
        </li>
      ))}
    </ul>
  );
}

function ringTone(status) {
  return ({
    eligible:   'success',
    ineligible: 'danger',
    uncertain:  'warn',
  })[status] || 'brand';
}
