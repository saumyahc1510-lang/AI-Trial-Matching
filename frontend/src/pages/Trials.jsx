import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  ArrowUpRight, Check, ChevronDown, FlaskConical, LayoutGrid, Search,
} from 'lucide-react';

import PageHeader from '@/components/ui/PageHeader.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import { Skeleton } from '@/components/ui/Skeleton.jsx';
import { listTrials, listTrialCategories } from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

const STATUS_FILTERS = [
  { value: '',                    label: 'All'              },
  { value: 'RECRUITING',          label: 'Recruiting'       },
  { value: 'NOT_YET_RECRUITING',  label: 'Not yet open'     },
  { value: 'ACTIVE_NOT_RECRUITING', label: 'Active (closed enrollment)' },
  { value: 'COMPLETED',           label: 'Completed'        },
];

export default function Trials() {
  const [statusFilter, setStatusFilter]   = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [query, setQuery] = useState('');

  // Trial list — filtered server-side by status + category, free-text
  // search applied client-side on top.
  const { data: trials = [], isLoading } = useQuery({
    queryKey: ['trials', 'all', statusFilter, categoryFilter],
    queryFn: () => listTrials({
      limit: 200,
      overall_status: statusFilter || undefined,
      category: categoryFilter || undefined,
    }),
  });

  // Canonical category list — driven by the backend so the dropdown
  // stays in sync if we add a new specialty without redeploying the
  // frontend.
  const { data: categories = [], isLoading: catsLoading } = useQuery({
    queryKey: ['trials', 'categories'],
    queryFn: listTrialCategories,
    staleTime: 5 * 60_000,
  });

  const filtered = useMemo(() => {
    if (!query) return trials;
    const q = query.toLowerCase();
    return trials.filter((t) => (
      (t.nct_id || '').toLowerCase().includes(q) ||
      (t.title  || '').toLowerCase().includes(q) ||
      (t.sponsor || '').toLowerCase().includes(q) ||
      (t.conditions || []).join(' ').toLowerCase().includes(q)
    ));
  }, [trials, query]);

  return (
    <div>
      <PageHeader
        eyebrow="Catalog"
        title="Trials"
        description="Synced from ClinicalTrials.gov v2.  Click into one to see parsed criteria + a plain-language summary."
      />

      {/* Filters */}
      <div className="card mb-5 p-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex flex-1 min-w-[220px] items-center gap-2 px-2">
            <Search className="h-4 w-4 text-ink-400" />
            <input
              className="w-full bg-transparent text-sm placeholder-ink-400 focus:outline-none"
              placeholder="Search by NCT, title, sponsor, or condition…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <CategoryDropdown
            value={categoryFilter}
            onChange={setCategoryFilter}
            options={categories}
            loading={catsLoading}
          />
          <div className="flex flex-wrap gap-1.5">
            {STATUS_FILTERS.map((f) => (
              <button
                key={f.value || 'all'}
                onClick={() => setStatusFilter(f.value)}
                className={cn(
                  'rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors',
                  statusFilter === f.value
                    ? 'bg-brand-500 text-white shadow-sm'
                    : 'bg-ink-100 text-ink-600 hover:bg-ink-200',
                )}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Grid */}
      {isLoading ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-32" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={FlaskConical}
          title="No trials match your filters"
          description={
            query
              ? `Nothing matches "${query}" with the current status filter.`
              : 'Ask an admin to trigger a CT.gov sync to populate the catalog.'
          }
        />
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {filtered.map((t, i) => (
            <motion.div
              key={t.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: Math.min(i, 8) * 0.03 }}
            >
              <TrialCard trial={t} />
            </motion.div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ───────────────────────── Trial card ───────────────────────────── */

function TrialCard({ trial }) {
  const statusUpper = (trial.overall_status || '').toUpperCase();
  return (
    <Link
      to={`/trials/${trial.id}`}
      className="card group relative block overflow-hidden p-5 transition-shadow hover:shadow-glow"
    >
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-md bg-ink-100 px-2 py-0.5 font-mono text-xs font-semibold text-ink-600">
            {trial.nct_id}
          </span>
          <StatusBadge status={statusUpper} />
          {trial.category && (
            <span className="chip bg-accent-100 text-accent-700">
              <LayoutGrid className="h-3 w-3" /> {trial.category}
            </span>
          )}
          {trial.phase && (
            <span className="chip chip-brand">{trial.phase}</span>
          )}
        </div>
        <ArrowUpRight className="h-4 w-4 -translate-x-1 text-ink-300 opacity-0 transition-all group-hover:translate-x-0 group-hover:opacity-100 group-hover:text-brand-500" />
      </div>
      <div className="font-display text-base font-semibold leading-snug text-ink-900 line-clamp-2">
        {trial.title}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-ink-500">
        {trial.sponsor && <span>{trial.sponsor}</span>}
        {trial.enrollment_count && (
          <span className="text-ink-400">· enrollment {trial.enrollment_count}</span>
        )}
      </div>
      {trial.conditions && trial.conditions.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {trial.conditions.slice(0, 4).map((c) => (
            <span key={c} className="chip chip-ink">{c}</span>
          ))}
          {trial.conditions.length > 4 && (
            <span className="chip chip-ink">+{trial.conditions.length - 4}</span>
          )}
        </div>
      )}
    </Link>
  );
}

function StatusBadge({ status }) {
  const colour = {
    RECRUITING:           'bg-success-100 text-success-600',
    NOT_YET_RECRUITING:   'bg-brand-100   text-brand-600',
    ACTIVE_NOT_RECRUITING:'bg-warn-100    text-warn-600',
    COMPLETED:            'bg-ink-100     text-ink-600',
    SUSPENDED:            'bg-warn-100    text-warn-600',
    TERMINATED:           'bg-danger-100  text-danger-600',
    WITHDRAWN:            'bg-danger-100  text-danger-600',
  }[status] || 'bg-ink-100 text-ink-600';
  return (
    <span className={cn('chip', colour)}>
      {status.replace(/_/g, ' ').toLowerCase()}
    </span>
  );
}

/* ───────────────────────── Category dropdown ────────────────────── */

/**
 * Popover dropdown for the trial-category filter.
 *
 * - Closes on outside click and on Esc.
 * - Categories with ``trial_count === 0`` render greyed out so users
 *   see the full taxonomy without being able to pick a dead category.
 * - Lives in this file (rather than ``components/ui``) because it's
 *   tightly coupled to the trials-catalog UX; if we need it elsewhere
 *   we can promote it later.
 */
function CategoryDropdown({ value, onChange, options, loading }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  // Close on outside click / Esc.
  useEffect(() => {
    if (!open) return;
    function onClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    function onKey(e) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const label = value || 'All categories';
  const totalCount = (options || []).reduce((sum, o) => sum + (o.trial_count || 0), 0);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={cn(
          'inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors',
          value
            ? 'bg-accent-500 text-white shadow-sm hover:bg-accent-600'
            : 'bg-ink-100 text-ink-600 hover:bg-ink-200',
        )}
      >
        <LayoutGrid className="h-3.5 w-3.5" />
        <span className="max-w-[180px] truncate">{label}</span>
        <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', open && 'rotate-180')} />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.98 }}
            transition={{ duration: 0.15 }}
            className="absolute right-0 z-30 mt-2 w-72 origin-top-right rounded-xl border border-ink-100 bg-white p-1 shadow-soft"
          >
            {/* "All" option always at top */}
            <DropdownItem
              active={value === ''}
              label="All categories"
              count={totalCount}
              onClick={() => { onChange(''); setOpen(false); }}
            />
            <div className="my-1 border-t border-ink-100" />

            <div className="max-h-72 overflow-y-auto">
              {loading ? (
                <div className="px-3 py-2 text-xs text-ink-400">Loading categories…</div>
              ) : (
                (options || []).map((opt) => (
                  <DropdownItem
                    key={opt.name}
                    active={value === opt.name}
                    label={opt.name}
                    count={opt.trial_count}
                    disabled={opt.trial_count === 0}
                    onClick={() => {
                      if (opt.trial_count === 0) return;
                      onChange(opt.name);
                      setOpen(false);
                    }}
                  />
                ))
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function DropdownItem({ active, label, count, disabled, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-xs font-medium transition-colors',
        active
          ? 'bg-accent-50 text-accent-700'
          : disabled
            ? 'cursor-not-allowed text-ink-300'
            : 'text-ink-700 hover:bg-ink-100',
      )}
    >
      <span className="flex items-center gap-2 truncate">
        {active && <Check className="h-3.5 w-3.5 text-accent-600" />}
        <span className={cn('truncate', !active && 'pl-5')}>{label}</span>
      </span>
      <span className={cn(
        'ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-bold tabular-nums',
        active ? 'bg-accent-100 text-accent-700' : 'bg-ink-100 text-ink-500',
      )}>
        {count}
      </span>
    </button>
  );
}
