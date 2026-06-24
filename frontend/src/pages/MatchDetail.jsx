import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  AlertCircle, ArrowLeft, ChevronDown, Cpu, Download, Quote,
  ShieldCheck, Sparkles, Stethoscope, Telescope,
} from 'lucide-react';
import confetti from 'canvas-confetti';

import PageHeader from '@/components/ui/PageHeader.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton.jsx';
import StatusPill from '@/components/ui/StatusPill.jsx';
import ProgressRing from '@/components/ui/ProgressRing.jsx';
import Tally from '@/components/ui/Tally.jsx';
import { explainMatch, getMatchResult } from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

/**
 * Match detail — the explainability showpiece.
 *
 * Two queries:
 *   - getMatchResult: structured row for fast first render.
 *   - explainMatch:   richer report with per-criterion evidence text.
 *
 * Fires a brief confetti burst when the result is eligible — that's the
 * one moment in the app where a little celebration is warranted.
 */
export default function MatchDetail() {
  const { matchId } = useParams();
  const [confettied, setConfettied] = useState(false);

  const matchQ = useQuery({
    queryKey: ['match', matchId],
    queryFn: () => getMatchResult(matchId),
  });
  const explainQ = useQuery({
    queryKey: ['match', matchId, 'explain'],
    queryFn: () => explainMatch(matchId, 'json'),
    enabled: !!matchQ.data,
  });

  useEffect(() => {
    if (confettied) return;
    if (matchQ.data?.overall_status === 'eligible') {
      setConfettied(true);
      // Two bursts from opposite edges for a "stage curtain" feel.
      const opts = { spread: 70, ticks: 80, gravity: 1, decay: 0.92, scalar: 0.9 };
      confetti({ ...opts, particleCount: 70, origin: { x: 0.1, y: 0.3 } });
      confetti({ ...opts, particleCount: 70, origin: { x: 0.9, y: 0.3 } });
    }
  }, [matchQ.data, confettied]);

  if (matchQ.isLoading) return <SkeletonRows rows={6} />;
  if (matchQ.isError) {
    return (
      <EmptyState
        icon={AlertCircle}
        title="Couldn't load this match result"
        action={<Link to="/matching" className="btn-secondary">Back to matching</Link>}
      />
    );
  }

  const match = matchQ.data;
  const report = explainQ.data;

  async function downloadMarkdown() {
    const md = await explainMatch(matchId, 'md');
    const blob = new Blob([md], { type: 'text/markdown' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url;
    a.download = `match-${matchId.slice(0, 8)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div>
      <Link to="/matching" className="btn-ghost mb-2 -ml-2">
        <ArrowLeft className="h-4 w-4" /> Back to matching
      </Link>

      <PageHeader
        eyebrow="Match result"
        title={`${prettyOverall(match.overall_status)} match`}
        description={`Patient ${match.patient_id.slice(0, 8)}… · Trial ${match.trial_id.slice(0, 8)}…`}
        actions={
          <button onClick={downloadMarkdown} className="btn-secondary">
            <Download className="h-4 w-4" /> Export Markdown
          </button>
        }
      />

      <ScoreHeader match={match} />

      {match.missing_data_summary && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="card mt-6 border-l-4 border-l-warn-500 p-5"
        >
          <div className="mb-1.5 flex items-center gap-2 text-sm font-semibold text-warn-600">
            <Telescope className="h-4 w-4" /> Missing data summary
          </div>
          <pre className="whitespace-pre-wrap text-sm leading-relaxed text-ink-700">
            {match.missing_data_summary}
          </pre>
        </motion.div>
      )}

      <div className="mt-6">
        <div className="font-display text-lg font-semibold text-ink-900">
          Per-criterion reasoning
        </div>
        <div className="mb-4 text-sm text-ink-500">
          Click a criterion to expand its evidence + LLM reasoning.
        </div>
        {explainQ.isLoading ? (
          <SkeletonRows rows={6} />
        ) : (
          <CriterionList rows={report?.rows || []} />
        )}
      </div>
    </div>
  );
}

function prettyOverall(s) {
  return s ? s[0].toUpperCase() + s.slice(1) : '';
}

/* ───────────────────────── Score header ─────────────────────────── */

function ScoreHeader({ match }) {
  const ring = match.overall_status === 'eligible' ? 'success'
             : match.overall_status === 'ineligible' ? 'danger'
             : 'warn';
  return (
    <div className="card relative overflow-hidden p-6">
      {/* Soft gradient wash matching the verdict tone. */}
      <div
        aria-hidden
        className={cn(
          'pointer-events-none absolute -inset-x-10 -top-10 h-40 opacity-60 blur-3xl',
          ring === 'success' && 'bg-success-100',
          ring === 'warn'    && 'bg-warn-100',
          ring === 'danger'  && 'bg-danger-100',
        )}
      />
      <div className="relative flex flex-wrap items-center gap-6">
        <ProgressRing
          value={match.match_score || 0}
          tone={ring}
          label="Match score"
        />
        <ProgressRing
          value={match.confidence_score || 0}
          tone="brand"
          label="Confidence"
        />
        <div className="ml-2 flex-1">
          <StatusPill status={match.overall_status} className="mb-2" />
          <div className="mt-2 grid grid-cols-3 gap-3 text-center sm:max-w-md">
            <Tally label="Met"       value={match.criteria_met} tone="success" />
            <Tally label="Not met"   value={match.criteria_not_met} tone="danger" />
            <Tally label="Uncertain" value={match.criteria_uncertain} tone="warn" />
          </div>
        </div>
      </div>
    </div>
  );
}


/* ───────────────────────── Criterion list ───────────────────────── */

function CriterionList({ rows }) {
  const [openId, setOpenId] = useState(null);
  return (
    <ul className="space-y-2">
      {rows.map((row, i) => (
        <motion.li
          key={row.order_index}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: Math.min(i, 10) * 0.03 }}
        >
          <CriterionCard
            row={row}
            open={openId === row.order_index}
            onToggle={() => setOpenId(
              openId === row.order_index ? null : row.order_index,
            )}
          />
        </motion.li>
      ))}
    </ul>
  );
}

function CriterionCard({ row, open, onToggle }) {
  return (
    <div className="card overflow-hidden">
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-4 p-4 text-left transition-colors hover:bg-ink-50"
      >
        <StatusPill status={row.status} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="chip chip-ink text-[10px] uppercase tracking-wider">
              {row.criterion_type}
            </span>
            <span className="chip chip-brand text-[10px] uppercase tracking-wider">
              {row.category.replace(/_/g, ' ')}
            </span>
            {row.is_critical && (
              <span className="chip chip-warn text-[10px] uppercase tracking-wider">
                Critical
              </span>
            )}
            <span className="ml-auto inline-flex items-center gap-1 text-[11px] text-ink-400">
              <Cpu className="h-3 w-3" />
              {row.evaluator}
              {typeof row.confidence === 'number' && (
                <> · conf {(row.confidence * 100).toFixed(0)}%</>
              )}
            </span>
          </div>
          <div className="mt-1 truncate text-sm font-medium text-ink-800">
            {row.parsed_description || row.criterion_text}
          </div>
        </div>
        <ChevronDown
          className={cn(
            'h-4 w-4 text-ink-400 transition-transform',
            open && 'rotate-180',
          )}
        />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22 }}
            className="overflow-hidden border-t border-ink-100 bg-ink-50/60"
          >
            <div className="space-y-3 p-4 text-sm">
              <Section label="Original criterion">
                <p className="text-ink-700">{row.criterion_text}</p>
              </Section>
              <Section label="Reasoning">
                <p className="text-ink-700">{row.reasoning}</p>
              </Section>
              {row.evidence_text && (
                <Section label="Evidence">
                  <blockquote className="flex gap-2 rounded-xl border-l-4 border-l-brand-400 bg-white px-3 py-2 italic text-ink-700">
                    <Quote className="mt-0.5 h-3.5 w-3.5 shrink-0 text-brand-400" />
                    <span>{row.evidence_text}</span>
                  </blockquote>
                </Section>
              )}
              {row.evidence_source && (
                <div className="text-[11px] text-ink-400">
                  Source: <code className="font-mono">{row.evidence_source}</code>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Section({ label, children }) {
  return (
    <div>
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-ink-400">
        {label}
      </div>
      {children}
    </div>
  );
}
