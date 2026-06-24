import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  AlertCircle, ArrowLeft, CheckCircle2, Globe2, Languages,
  MapPin, Stethoscope, XCircle, BookOpen,
} from 'lucide-react';

import PageHeader from '@/components/ui/PageHeader.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton.jsx';
import { getTrial, trialSummary } from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

const LANGS = [
  { code: 'en', label: 'English'  },
  { code: 'es', label: 'Español'  },
  { code: 'zh', label: '中文'      },
  { code: 'hi', label: 'हिन्दी'    },
  { code: 'ar', label: 'العربية'   },
  { code: 'fr', label: 'Français' },
  { code: 'pt', label: 'Português'},
  { code: 'ko', label: '한국어'   },
];

export default function TrialDetail() {
  const { trialId } = useParams();
  const [lang, setLang] = useState('en');

  const trialQ = useQuery({
    queryKey: ['trial', trialId],
    queryFn: () => getTrial(trialId),
  });

  // The summary is its own query so the language switcher can
  // crossfade without flickering the rest of the page.
  const summaryQ = useQuery({
    queryKey: ['trial', trialId, 'summary', lang],
    queryFn: () => trialSummary(trialId, lang),
    enabled: !!trialQ.data,
  });

  if (trialQ.isLoading) return <SkeletonRows rows={6} />;
  if (trialQ.isError) {
    return (
      <EmptyState
        icon={AlertCircle}
        title="Couldn't load this trial"
        action={<Link to="/trials" className="btn-secondary">Back to trials</Link>}
      />
    );
  }
  const trial = trialQ.data;

  return (
    <div>
      <Link to="/trials" className="btn-ghost mb-2 -ml-2">
        <ArrowLeft className="h-4 w-4" /> Back to trials
      </Link>

      <PageHeader
        eyebrow={trial.nct_id}
        title={trial.title}
        description={trial.sponsor || undefined}
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <SummaryCard
            trial={trial}
            summary={summaryQ.data}
            loading={summaryQ.isLoading}
            lang={lang}
            onLangChange={setLang}
          />
          <CriteriaCard criteria={trial.criteria || []} />
        </div>

        <aside className="space-y-5">
          <FactsCard trial={trial} />
          <SitesCard sites={trial.sites || []} />
        </aside>
      </div>
    </div>
  );
}

/* ───────────────────────── Plain-language summary ──────────────── */

function SummaryCard({ summary, loading, lang, onLangChange }) {
  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink-100 px-5 py-4">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-brand-500">
            <BookOpen className="h-3.5 w-3.5" /> Plain-language summary
          </div>
          <div className="mt-0.5 text-xs text-ink-400">
            {summary?.from_cache ? 'Served from cache.' : 'Freshly generated.'}
          </div>
        </div>
        <LanguageSwitcher value={lang} onChange={onLangChange} />
      </div>

      <div className="relative min-h-[160px] p-5">
        {loading ? (
          <SkeletonRows rows={4} />
        ) : (
          <AnimatePresence mode="wait">
            <motion.p
              key={summary?.language || lang}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.25 }}
              className="whitespace-pre-line text-sm leading-relaxed text-ink-700"
            >
              {summary?.text || 'No summary available yet.'}
            </motion.p>
          </AnimatePresence>
        )}
      </div>
    </div>
  );
}

function LanguageSwitcher({ value, onChange }) {
  return (
    <div className="flex flex-wrap items-center gap-1 rounded-xl border border-ink-100 bg-white/70 p-1">
      <Languages className="ml-1.5 h-3.5 w-3.5 text-ink-400" />
      {LANGS.map((l) => (
        <button
          key={l.code}
          onClick={() => onChange(l.code)}
          className={cn(
            'relative rounded-lg px-2.5 py-1 text-xs font-semibold transition-colors',
            value === l.code ? 'text-white' : 'text-ink-500 hover:text-ink-800',
          )}
        >
          {value === l.code && (
            <motion.span
              layoutId="lang-pill"
              className="absolute inset-0 -z-10 rounded-lg bg-brand-500 shadow-sm"
              transition={{ type: 'spring', stiffness: 380, damping: 30 }}
            />
          )}
          {l.label}
        </button>
      ))}
    </div>
  );
}

/* ───────────────────────── Criteria ─────────────────────────────── */

function CriteriaCard({ criteria }) {
  const inclusion = criteria.filter((c) => c.criterion_type === 'inclusion');
  const exclusion = criteria.filter((c) => c.criterion_type === 'exclusion');
  return (
    <div className="card overflow-hidden">
      <div className="border-b border-ink-100 px-5 py-4">
        <div className="font-display text-lg font-semibold text-ink-900">
          Eligibility criteria
        </div>
        <div className="text-xs text-ink-500">
          {criteria.length === 0
            ? 'Criteria not yet parsed for this trial.'
            : `${inclusion.length} inclusion + ${exclusion.length} exclusion.`}
        </div>
      </div>
      <div className="grid grid-cols-1 gap-0 md:grid-cols-2">
        <CriteriaColumn
          icon={CheckCircle2}
          accent="success"
          title="Inclusion"
          items={inclusion}
          empty="No inclusion criteria parsed."
        />
        <CriteriaColumn
          icon={XCircle}
          accent="danger"
          title="Exclusion"
          items={exclusion}
          empty="No exclusion criteria parsed."
          rightBorder
        />
      </div>
    </div>
  );
}

function CriteriaColumn({ icon: Icon, accent, title, items, empty, rightBorder }) {
  return (
    <div className={cn(
      'p-5',
      !rightBorder && 'md:border-r md:border-ink-100',
    )}>
      <div className={cn(
        'mb-3 inline-flex items-center gap-2 text-sm font-semibold',
        accent === 'success' ? 'text-success-600' : 'text-danger-500',
      )}>
        <Icon className="h-4 w-4" /> {title}
      </div>
      {items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-ink-200 px-3 py-4 text-center text-xs text-ink-500">
          {empty}
        </div>
      ) : (
        <ul className="space-y-2.5">
          {items.map((c, i) => (
            <motion.li
              key={c.id}
              initial={{ opacity: 0, x: -6 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: Math.min(i, 10) * 0.025 }}
              className="rounded-xl border border-ink-100 bg-white p-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="chip chip-ink text-[10px] uppercase tracking-wide">
                  {c.category.replace(/_/g, ' ')}
                </span>
                {!c.is_critical && (
                  <span className="text-[10px] font-medium text-ink-400">advisory</span>
                )}
              </div>
              <div className="mt-1.5 text-sm font-medium text-ink-800">
                {c.parsed_description || c.original_text}
              </div>
              {(c.temporal_constraint || c.value_constraint) && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {c.value_constraint && (
                    <span className="rounded-md bg-brand-50 px-1.5 py-0.5 font-mono text-[11px] text-brand-700">
                      {c.value_constraint.metric}{' '}
                      {c.value_constraint.operator}{' '}
                      {fmtValue(c.value_constraint)}
                      {c.value_constraint.unit ? ` ${c.value_constraint.unit}` : ''}
                    </span>
                  )}
                  {c.temporal_constraint && (
                    <span className="rounded-md bg-accent-50 px-1.5 py-0.5 font-mono text-[11px] text-accent-700">
                      {c.temporal_constraint.type}{' '}
                      {c.temporal_constraint.duration_value}{' '}
                      {c.temporal_constraint.duration_unit}
                    </span>
                  )}
                </div>
              )}
            </motion.li>
          ))}
        </ul>
      )}
    </div>
  );
}

function fmtValue(constraint) {
  const v = constraint.value;
  if (Array.isArray(v)) return v.join(' – ');
  return v;
}

/* ───────────────────────── Aside cards ──────────────────────────── */

function FactsCard({ trial }) {
  return (
    <div className="card p-5">
      <div className="mb-4 text-xs font-semibold uppercase tracking-wider text-ink-400">
        At a glance
      </div>
      <Fact label="Status"           value={trial.overall_status} />
      <Fact label="Phase"            value={trial.phase || '—'} />
      <Fact label="Study type"       value={trial.study_type || '—'} />
      <Fact label="Enrollment"       value={trial.enrollment_count ?? '—'} />
      <Fact label="Start date"       value={trial.start_date || '—'} />
      <Fact label="Completion date"  value={trial.completion_date || '—'} />
      {trial.source_url && (
        <a
          href={trial.source_url}
          target="_blank"
          rel="noreferrer"
          className="btn-secondary mt-4 w-full justify-center"
        >
          <Globe2 className="h-4 w-4" /> View on ClinicalTrials.gov
        </a>
      )}
    </div>
  );
}

function Fact({ label, value }) {
  return (
    <div className="flex items-center justify-between border-b border-ink-100 py-2 last:border-b-0">
      <span className="text-xs uppercase tracking-wider text-ink-400">{label}</span>
      <span className="text-sm font-medium text-ink-800">{value}</span>
    </div>
  );
}

function SitesCard({ sites }) {
  return (
    <div className="card p-5">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
        <MapPin className="h-3.5 w-3.5" /> Sites
      </div>
      {sites.length === 0 ? (
        <div className="rounded-lg border border-dashed border-ink-200 px-3 py-4 text-center text-xs text-ink-500">
          No sites listed for this trial.
        </div>
      ) : (
        <ul className="space-y-2">
          {sites.slice(0, 6).map((s) => (
            <li key={s.id} className="rounded-lg border border-ink-100 p-3 text-sm">
              <div className="font-medium text-ink-800">{s.facility_name}</div>
              <div className="text-xs text-ink-500">
                {[s.city, s.state, s.country].filter(Boolean).join(', ')}
              </div>
              <div className="mt-1">
                <span className={cn(
                  'chip text-[10px]',
                  s.site_status === 'recruiting' ? 'chip-success' : 'chip-ink',
                )}>
                  {s.site_status}
                </span>
              </div>
            </li>
          ))}
          {sites.length > 6 && (
            <li className="text-xs text-ink-400">+{sites.length - 6} more…</li>
          )}
        </ul>
      )}
    </div>
  );
}
