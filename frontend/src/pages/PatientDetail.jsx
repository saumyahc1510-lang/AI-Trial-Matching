import { Link, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  ActivitySquare, ArrowLeft, ChevronRight, Pill, Stethoscope,
  TestTube2, Syringe, ScanLine, Sparkles, AlertTriangle, BedSingle,
} from 'lucide-react';
import { format, parseISO } from 'date-fns';
import toast from 'react-hot-toast';

import PageHeader from '@/components/ui/PageHeader.jsx';
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import StatusPill from '@/components/ui/StatusPill.jsx';
import { cn } from '@/lib/cn.js';
import {
  getPatient, patientMatches, triggerMatch,
} from '@/api/endpoints.js';

/**
 * Patient detail — demographic summary + animated vertical timeline +
 * latest matches.
 */
export default function PatientDetail() {
  const { patientId } = useParams();
  const qc = useQueryClient();

  const patientQ = useQuery({
    queryKey: ['patient', patientId],
    queryFn: () => getPatient(patientId),
  });

  const matchesQ = useQuery({
    queryKey: ['patient', patientId, 'matches'],
    queryFn: () => patientMatches(patientId, { limit: 6 }),
  });

  const runMatch = useMutation({
    mutationFn: () => triggerMatch(patientId, 'manual'),
    onSuccess: (data) => {
      toast.success(`Match queued — ${data.trials_queued ?? 0} trial(s) evaluated.`);
      qc.invalidateQueries({ queryKey: ['patient', patientId, 'matches'] });
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Match trigger failed.'),
  });

  if (patientQ.isLoading) return <SkeletonRows rows={6} />;
  if (patientQ.isError) {
    return (
      <EmptyState
        icon={AlertTriangle}
        title="Couldn't load this patient"
        description="The record may have been removed or your session may have expired."
        action={<Link to="/patients" className="btn-secondary">Back to patients</Link>}
      />
    );
  }

  const patient = patientQ.data;
  const events = (patient.medical_events || []).slice().sort(
    (a, b) => new Date(a.event_date) - new Date(b.event_date),
  );

  return (
    <div>
      <Link to="/patients" className="btn-ghost mb-2 -ml-2">
        <ArrowLeft className="h-4 w-4" /> Back to patients
      </Link>

      <PageHeader
        eyebrow="Patient record"
        title={`${patient.first_name} ${patient.last_name}`}
        description={`External ID: ${patient.external_id || 'n/a'} · Version ${patient.current_version}`}
        actions={
          <button
            onClick={() => runMatch.mutate()}
            disabled={runMatch.isPending}
            className="btn-primary"
          >
            <Sparkles className={cn('h-4 w-4', runMatch.isPending && 'animate-spin')} />
            {runMatch.isPending ? 'Matching…' : 'Run matching'}
          </button>
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Left column — demographic facts + matches */}
        <div className="space-y-6 lg:col-span-1">
          <DemographicCard patient={patient} />
          <MatchesCard matches={matchesQ.data?.matches} loading={matchesQ.isLoading} />
        </div>

        {/* Right column — animated medical timeline */}
        <div className="lg:col-span-2">
          <TimelineCard events={events} />
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────── Demographic card ─────────────────────── */

function DemographicCard({ patient }) {
  return (
    <div className="card p-5">
      <div className="mb-4 text-xs font-semibold uppercase tracking-wider text-ink-400">
        Demographics
      </div>
      <Row label="Sex"        value={patient.sex} />
      <Row label="Birthdate"  value={patient.date_of_birth} />
      <Row label="Race"       value={patient.race || '—'} />
      <Row label="Ethnicity"  value={patient.ethnicity || '—'} />
      <Row label="Language"   value={(patient.preferred_language || 'en').toUpperCase()} />
      <Row label="Status"     value={patient.status} />
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between border-b border-ink-100 py-2.5 last:border-b-0">
      <span className="text-xs uppercase tracking-wider text-ink-400">{label}</span>
      <span className="text-sm font-medium text-ink-800">{value}</span>
    </div>
  );
}

/* ───────────────────────── Matches card ─────────────────────────── */

function MatchesCard({ matches, loading }) {
  return (
    <div className="card p-5">
      <div className="mb-3 flex items-center justify-between">
        <div className="text-xs font-semibold uppercase tracking-wider text-ink-400">
          Latest matches
        </div>
        <Link to="/matching" className="btn-ghost text-xs">View all →</Link>
      </div>
      {loading ? (
        <SkeletonRows rows={3} />
      ) : !matches || matches.length === 0 ? (
        <div className="rounded-xl border border-dashed border-ink-200 p-5 text-center text-xs text-ink-500">
          No matches yet — run matching to populate this list.
        </div>
      ) : (
        <ul className="space-y-2">
          {matches.map((m) => (
            <li key={m.id}>
              <Link
                to={`/matching/${m.id}`}
                className="flex items-center justify-between rounded-xl border border-ink-100 px-3 py-2.5 transition-colors hover:border-brand-200 hover:bg-brand-50/40"
              >
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold text-ink-800">
                    {m.trial_id.slice(0, 8)}…
                  </div>
                  <div className="text-xs text-ink-400">
                    Match {Math.round((m.match_score || 0) * 100)}% · Confidence{' '}
                    {Math.round((m.confidence_score || 0) * 100)}%
                  </div>
                </div>
                <StatusPill status={m.overall_status} size="sm" />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ───────────────────────── Timeline ─────────────────────────────── */

function TimelineCard({ events }) {
  return (
    <div className="card relative overflow-hidden">
      <div className="border-b border-ink-100 px-5 py-4">
        <div className="font-display text-lg font-semibold text-ink-900">
          Medical timeline
        </div>
        <div className="text-xs text-ink-500">
          {events.length} event{events.length === 1 ? '' : 's'} — sorted oldest → newest.
        </div>
      </div>

      {events.length === 0 ? (
        <div className="p-8 text-center text-sm text-ink-500">
          No medical events have been ingested for this patient yet.
        </div>
      ) : (
        <div className="relative px-6 py-6">
          {/* Vertical guide line that draws itself in from the top. */}
          <motion.div
            initial={{ scaleY: 0 }}
            animate={{ scaleY: 1 }}
            transition={{ duration: 0.8, ease: 'easeOut' }}
            style={{ originY: 0 }}
            aria-hidden
            className="absolute left-[34px] top-6 bottom-6 w-px bg-gradient-to-b from-brand-200 via-brand-200 to-transparent"
          />
          <ul className="space-y-5">
            {events.map((evt, i) => (
              <TimelineRow key={evt.id} evt={evt} index={i} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

const TYPE_ICON = {
  diagnosis:       { Icon: Stethoscope, tone: 'brand'   },
  medication:      { Icon: Pill,        tone: 'accent'  },
  lab_result:      { Icon: TestTube2,   tone: 'success' },
  procedure:       { Icon: Syringe,     tone: 'warn'    },
  vital_sign:      { Icon: ActivitySquare, tone: 'success' },
  imaging:         { Icon: ScanLine,    tone: 'brand'   },
  hospitalization: { Icon: BedSingle,   tone: 'warn'    },
  allergy:         { Icon: AlertTriangle, tone: 'danger' },
  note:            { Icon: ChevronRight, tone: 'ink'    },
};

const TONE_BG = {
  brand:   'bg-brand-100 text-brand-600',
  accent:  'bg-accent-100 text-accent-600',
  success: 'bg-success-100 text-success-600',
  warn:    'bg-warn-100 text-warn-600',
  danger:  'bg-danger-100 text-danger-600',
  ink:     'bg-ink-100 text-ink-500',
};

function TimelineRow({ evt, index }) {
  const meta = TYPE_ICON[evt.event_type] || TYPE_ICON.note;
  const { Icon, tone } = meta;
  return (
    <motion.li
      initial={{ opacity: 0, x: -16 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: Math.min(index, 12) * 0.05, type: 'spring', stiffness: 240, damping: 22 }}
      className="relative flex gap-4"
    >
      <span className={cn(
        'relative z-10 mt-0.5 flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl ring-4 ring-white',
        TONE_BG[tone],
      )}>
        <Icon className="h-5 w-5" />
      </span>
      <div className="flex-1 rounded-xl border border-ink-100 bg-white/80 px-4 py-3 shadow-sm">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div className="font-display text-sm font-semibold text-ink-900">
            {evt.display_name}
          </div>
          <div className="text-[11px] font-medium text-ink-400">
            {formatDate(evt.event_date)}
          </div>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-500">
          <span className="chip chip-ink uppercase tracking-wide">
            {evt.event_type.replace(/_/g, ' ')}
          </span>
          {evt.code && (
            <span className="text-[11px] font-mono text-ink-500">
              {evt.code_system}: {evt.code}
            </span>
          )}
          {evt.value && (
            <span className="text-[11px] font-medium text-ink-700">
              {evt.value}{evt.unit ? ` ${evt.unit}` : ''}
            </span>
          )}
        </div>
      </div>
    </motion.li>
  );
}

function formatDate(iso) {
  try {
    return format(parseISO(iso), 'MMM d, yyyy');
  } catch {
    return iso;
  }
}
