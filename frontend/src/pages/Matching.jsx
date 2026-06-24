import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  ArrowUpRight, Brain, ChevronRight, Sparkles, Wand2, UsersRound,
} from 'lucide-react';
import toast from 'react-hot-toast';

import PageHeader from '@/components/ui/PageHeader.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import StatusPill from '@/components/ui/StatusPill.jsx';
import ProgressRing from '@/components/ui/ProgressRing.jsx';
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton.jsx';
import {
  listPatients, patientMatches, triggerMatch,
} from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

/**
 * Matching landing page.
 *
 * Left rail: patient picker (with a small live search).  Right pane:
 * latest matches for the selected patient, with a big "Run a new match"
 * CTA that animates while the LLM thinks.
 */
export default function Matching() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState(null);
  const [query, setQuery] = useState('');

  const patientsQ = useQuery({
    queryKey: ['patients', 'matching'],
    queryFn: () => listPatients({ limit: 200 }),
  });

  // Auto-pick the first patient once data arrives.
  const patients = patientsQ.data || [];
  const selectedId = selected || patients[0]?.id;

  const matchesQ = useQuery({
    queryKey: ['patient', selectedId, 'matches', 'page'],
    queryFn: () => patientMatches(selectedId, { limit: 50 }),
    enabled: !!selectedId,
  });

  const runMatch = useMutation({
    mutationFn: () => triggerMatch(selectedId, 'manual'),
    onSuccess: (data) => {
      toast.success(`Match queued — ${data.trials_queued ?? 0} trial(s) evaluated.`);
      qc.invalidateQueries({ queryKey: ['patient', selectedId, 'matches'] });
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Match trigger failed.'),
  });

  const filteredPatients = useMemo(() => {
    if (!query) return patients;
    const q = query.toLowerCase();
    return patients.filter((p) => (
      `${p.first_name} ${p.last_name} ${p.external_id || ''}`.toLowerCase().includes(q)
    ));
  }, [patients, query]);

  const selectedPatient = patients.find((p) => p.id === selectedId);

  return (
    <div>
      <PageHeader
        eyebrow="Matching"
        title="Find every trial your patient could join"
        description="Pick a patient, run the matching engine, and explore the per-criterion reasoning behind each verdict."
        actions={
          selectedPatient ? (
            <button
              className="btn-primary"
              onClick={() => runMatch.mutate()}
              disabled={runMatch.isPending}
            >
              {runMatch.isPending ? (
                <ThinkingIndicator label="AI is matching…" />
              ) : (
                <>
                  <Wand2 className="h-4 w-4" /> Run matching
                </>
              )}
            </button>
          ) : null
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Patient picker */}
        <div className="card lg:col-span-1">
          <div className="border-b border-ink-100 p-4">
            <div className="text-xs font-semibold uppercase tracking-wider text-ink-400">
              Patient
            </div>
            <input
              className="input mt-2"
              placeholder="Search patients…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <div className="max-h-[560px] overflow-y-auto p-2">
            {patientsQ.isLoading ? (
              <SkeletonRows rows={5} />
            ) : filteredPatients.length === 0 ? (
              <div className="rounded-lg border border-dashed border-ink-200 m-2 p-4 text-center text-xs text-ink-500">
                No patients.
              </div>
            ) : (
              <ul className="space-y-1">
                {filteredPatients.map((p) => (
                  <PatientPickerItem
                    key={p.id}
                    patient={p}
                    active={p.id === selectedId}
                    onSelect={() => setSelected(p.id)}
                  />
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* Match results */}
        <div className="lg:col-span-2">
          {!selectedId ? (
            <EmptyState
              icon={UsersRound}
              title="No patient selected"
              description="Choose a patient from the left to see their match results."
            />
          ) : matchesQ.isLoading ? (
            <SkeletonRows rows={6} />
          ) : (matchesQ.data?.matches || []).length === 0 ? (
            <EmptyState
              icon={Sparkles}
              title="No matches yet for this patient"
              description="Click ‘Run matching’ to evaluate them against every recruiting trial in the catalog."
            />
          ) : (
            <ResultsList matches={matchesQ.data.matches} thinking={runMatch.isPending} />
          )}
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────── Patient picker row ───────────────────── */

function PatientPickerItem({ patient, active, onSelect }) {
  const initials = `${patient.first_name?.[0] || '?'}${patient.last_name?.[0] || ''}`.toUpperCase();
  return (
    <li>
      <button
        onClick={onSelect}
        className={cn(
          'relative flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-colors',
          active ? 'text-brand-700' : 'text-ink-700 hover:bg-ink-50',
        )}
      >
        {active && (
          <motion.span
            layoutId="patient-active"
            className="absolute inset-0 -z-0 rounded-xl bg-brand-50 shadow-sm"
          />
        )}
        <span className="relative z-10 flex h-9 w-9 items-center justify-center rounded-full bg-gradient-to-br from-brand-300 to-accent-300 text-xs font-semibold text-white">
          {initials}
        </span>
        <span className="relative z-10 min-w-0 flex-1">
          <div className="truncate text-sm font-semibold">
            {patient.first_name} {patient.last_name}
          </div>
          <div className="truncate text-[11px] text-ink-400">
            {patient.external_id || patient.sex}
          </div>
        </span>
        <ChevronRight className="relative z-10 h-4 w-4 text-ink-300" />
      </button>
    </li>
  );
}

/* ───────────────────────── Thinking indicator ──────────────────── */

function ThinkingIndicator({ label = 'Thinking…' }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span className="relative flex h-4 w-4">
        <Brain className="h-4 w-4" />
        <span className="absolute inset-0 animate-breathing rounded-full bg-white/30" />
      </span>
      {label}
    </span>
  );
}

/* ───────────────────────── Results list ─────────────────────────── */

function ResultsList({ matches, thinking }) {
  return (
    <div className="space-y-3">
      <AnimatePresence initial={false}>
        {thinking && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className="card flex items-center gap-3 border-l-4 border-l-brand-400 px-4 py-3"
          >
            <Brain className="h-4 w-4 animate-breathing text-brand-500" />
            <div className="text-sm font-medium text-ink-700">
              Reasoning over every criterion of every recruiting trial…
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {matches.map((m, i) => (
        <motion.div
          key={m.id}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: Math.min(i, 10) * 0.04 }}
        >
          <MatchRow match={m} />
        </motion.div>
      ))}
    </div>
  );
}

function MatchRow({ match }) {
  const matchPct      = Math.round((match.match_score      || 0) * 100);
  const confidencePct = Math.round((match.confidence_score || 0) * 100);
  return (
    <Link
      to={`/matching/${match.id}`}
      className="card group flex items-center gap-5 p-5 transition-shadow hover:shadow-glow"
    >
      <ProgressRing
        size={92}
        stroke={9}
        value={(match.match_score || 0)}
        tone={ringTone(match.overall_status)}
        label="Match"
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill status={match.overall_status} />
          <span className="text-[11px] font-semibold uppercase tracking-wider text-ink-400">
            Trial {match.trial_id.slice(0, 8)}…
          </span>
        </div>
        <div className="mt-2 flex flex-wrap items-end gap-x-4 gap-y-1 text-sm">
          <Stat label="Match"         value={`${matchPct}%`} />
          <Stat label="Confidence"    value={`${confidencePct}%`} />
          <Stat label="Met"           value={match.criteria_met} tone="success" />
          <Stat label="Not met"       value={match.criteria_not_met} tone="danger" />
          <Stat label="Uncertain"     value={match.criteria_uncertain} tone="warn" />
        </div>
        {match.missing_data_summary && (
          <p className="mt-2 line-clamp-2 text-xs text-ink-500">
            {match.missing_data_summary}
          </p>
        )}
      </div>
      <ArrowUpRight className="h-4 w-4 -translate-x-1 text-ink-300 opacity-0 transition-all group-hover:translate-x-0 group-hover:opacity-100 group-hover:text-brand-500" />
    </Link>
  );
}

function Stat({ label, value, tone }) {
  const cls = {
    success: 'text-success-600',
    warn:    'text-warn-600',
    danger:  'text-danger-500',
  }[tone] || 'text-ink-800';
  return (
    <div>
      <div className="text-[10px] font-medium uppercase tracking-wider text-ink-400">
        {label}
      </div>
      <div className={cn('font-display text-lg font-bold tabular-nums', cls)}>
        {value}
      </div>
    </div>
  );
}

function ringTone(status) {
  return ({
    eligible:   'success',
    ineligible: 'danger',
    uncertain:  'warn',
  })[status] || 'brand';
}
