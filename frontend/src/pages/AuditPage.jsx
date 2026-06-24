import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  AlertCircle, ChevronLeft, ChevronRight, FileSearch, RefreshCw,
  ScrollText, Shield, ShieldAlert,
} from 'lucide-react';
import { format, formatDistanceToNow, parseISO } from 'date-fns';

import PageHeader from '@/components/ui/PageHeader.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton.jsx';
import { queryAudit } from '@/api/endpoints.js';
import { useAuth } from '@/auth/AuthContext.jsx';
import { cn } from '@/lib/cn.js';

const ACTION_OPTIONS = [
  { value: '',                  label: 'All actions' },
  { value: 'login',             label: 'Login' },
  { value: 'logout',            label: 'Logout' },
  { value: 'create',            label: 'Create' },
  { value: 'read',              label: 'Read' },
  { value: 'update',            label: 'Update' },
  { value: 'delete',            label: 'Delete' },
  { value: 'match_triggered',   label: 'Match triggered' },
  { value: 'match_completed',   label: 'Match completed' },
  { value: 'feedback_submitted',label: 'Feedback submitted' },
  { value: 'export',            label: 'Export' },
  { value: 'api_call',          label: 'API call' },
];

const RESOURCE_OPTIONS = [
  { value: '', label: 'All resources' },
  { value: 'auth',          label: 'Auth' },
  { value: 'patients',      label: 'Patients' },
  { value: 'trials',        label: 'Trials' },
  { value: 'matching',      label: 'Matching' },
  { value: 'feedback',      label: 'Feedback' },
  { value: 'wearables',     label: 'Wearables' },
  { value: 'notifications', label: 'Notifications' },
  { value: 'admin',         label: 'Admin' },
  { value: 'audit',         label: 'Audit' },
  { value: 'system',        label: 'System' },
];

const PAGE_SIZE = 25;

/**
 * Full-screen audit-log viewer for admins.
 *
 * Sits behind the sidebar link the AdminDashboard's inline table can't
 * cover — the inline table is a "what just happened" preview; this page
 * is the "compliance officer wants to investigate" surface.
 *
 * Filters mirror the backend's ``GET /audit/`` query params + a client-
 * side free-text filter on the visible page (so admins can grep
 * resource_ids without flooding the API).
 */
export default function AuditPage() {
  const { user } = useAuth();

  const [action, setAction]     = useState('');
  const [resource, setResource] = useState('');
  const [phiOnly, setPhiOnly]   = useState(false);
  const [search, setSearch]     = useState('');
  const [page, setPage]         = useState(0);

  // Reset to page 0 whenever a server-side filter changes.
  const filterKey = `${action}|${resource}|${phiOnly}`;
  const [, setPriorKey] = useState(filterKey);
  useMemo(() => {
    setPriorKey((prev) => {
      if (prev !== filterKey) setPage(0);
      return filterKey;
    });
  }, [filterKey]);

  const auditQ = useQuery({
    queryKey: ['audit', 'list', action, resource, phiOnly, page],
    queryFn: () => queryAudit({
      action:        action   || undefined,
      resource_type: resource || undefined,
      phi_only:      phiOnly  || undefined,
      limit:         PAGE_SIZE,
      offset:        page * PAGE_SIZE,
    }),
    keepPreviousData: true,
  });

  const rows = auditQ.data || [];
  const filtered = useMemo(() => {
    if (!search) return rows;
    const q = search.toLowerCase();
    return rows.filter((r) => (
      (r.user_id    || '').toLowerCase().includes(q) ||
      (r.resource_id|| '').toLowerCase().includes(q) ||
      (r.request_path || '').toLowerCase().includes(q) ||
      (r.ip_address || '').toLowerCase().includes(q)
    ));
  }, [rows, search]);

  if (user?.role !== 'admin') {
    return (
      <EmptyState
        icon={Shield}
        title="Admin-only"
        description="The audit log is restricted to system administrators."
      />
    );
  }

  return (
    <div>
      <PageHeader
        eyebrow="Compliance"
        title="Audit log"
        description="Every API call recorded.  Immutable at the database layer via Postgres triggers — even an admin cannot mutate or delete a row here."
        actions={
          <button onClick={() => auditQ.refetch()} className="btn-secondary" disabled={auditQ.isFetching}>
            <RefreshCw className={cn('h-4 w-4', auditQ.isFetching && 'animate-spin')} />
            Refresh
          </button>
        }
      />

      {/* Filter row */}
      <div className="card mb-5 p-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-5">
          <SelectField label="Action" value={action} onChange={setAction} options={ACTION_OPTIONS} />
          <SelectField label="Resource" value={resource} onChange={setResource} options={RESOURCE_OPTIONS} />
          <TextField   label="Search (user / resource id / path / ip)" value={search} onChange={setSearch} />
          <ToggleField label="PHI access only" value={phiOnly} onChange={setPhiOnly} />
          <CountField label="On this page" count={filtered.length} of={rows.length} loading={auditQ.isLoading} />
        </div>
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        {auditQ.isLoading && !auditQ.data ? (
          <div className="p-5"><SkeletonRows rows={8} /></div>
        ) : auditQ.isError ? (
          <EmptyState icon={AlertCircle} title="Audit query failed" description={auditQ.error?.message || ''} />
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={FileSearch}
            title="No audit entries match"
            description="Try clearing one of the filters above."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-xs">
              <thead>
                <tr className="bg-ink-50/60 text-left text-[10px] font-semibold uppercase tracking-wider text-ink-500">
                  <th className="px-4 py-3">When</th>
                  <th className="px-4 py-3">User</th>
                  <th className="px-4 py-3">Action</th>
                  <th className="px-4 py-3">Resource</th>
                  <th className="px-4 py-3">Path</th>
                  <th className="px-4 py-3">IP</th>
                  <th className="px-4 py-3 text-right">PHI</th>
                  <th className="px-4 py-3 text-right">Status</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r, i) => (
                  <motion.tr
                    key={r.id}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ delay: Math.min(i, 10) * 0.015 }}
                    className="border-t border-ink-100 align-top hover:bg-ink-50"
                  >
                    <td className="px-4 py-2.5">
                      <div className="text-ink-700">
                        {format(parseISO(r.timestamp), 'MMM d, HH:mm:ss')}
                      </div>
                      <div className="text-[10px] text-ink-400">
                        {formatDistanceToNow(parseISO(r.timestamp), { addSuffix: true })}
                      </div>
                    </td>
                    <td className="px-4 py-2.5">
                      {r.user_id ? (
                        <span className="font-mono text-[10px] text-ink-600" title={r.user_id}>
                          {r.user_id.slice(0, 12)}…
                        </span>
                      ) : <span className="text-[10px] uppercase tracking-wider text-ink-400">system</span>}
                    </td>
                    <td className="px-4 py-2.5">
                      <span className="chip chip-ink text-[10px]">{r.action}</span>
                    </td>
                    <td className="px-4 py-2.5 text-ink-700">
                      <div className="font-medium">{r.resource_type}</div>
                      {r.resource_id && (
                        <div className="font-mono text-[10px] text-ink-400">
                          {r.resource_id.length > 18 ? `${r.resource_id.slice(0, 12)}…` : r.resource_id}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="font-mono text-[10px] text-ink-500" title={r.request_path}>
                        {r.request_method && (
                          <span className={cn(
                            'mr-1 rounded px-1 font-semibold',
                            r.request_method === 'GET'    && 'bg-brand-100 text-brand-700',
                            r.request_method === 'POST'   && 'bg-success-100 text-success-600',
                            r.request_method === 'PATCH'  && 'bg-warn-100 text-warn-600',
                            r.request_method === 'PUT'    && 'bg-warn-100 text-warn-600',
                            r.request_method === 'DELETE' && 'bg-danger-100 text-danger-500',
                          )}>
                            {r.request_method}
                          </span>
                        )}
                        {(r.request_path || '').length > 36
                          ? `${(r.request_path || '').slice(0, 33)}…`
                          : r.request_path}
                      </div>
                    </td>
                    <td className="px-4 py-2.5 font-mono text-[10px] text-ink-500">
                      {r.ip_address || '—'}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      {r.phi_accessed ? (
                        <span className="inline-flex items-center gap-1 rounded-full bg-warn-100 px-2 py-0.5 text-[10px] font-bold text-warn-600">
                          <ShieldAlert className="h-3 w-3" /> PHI
                        </span>
                      ) : <span className="text-ink-300">—</span>}
                    </td>
                    <td className={cn(
                      'px-4 py-2.5 text-right font-mono font-semibold tabular-nums',
                      r.response_status >= 500 ? 'text-danger-500'
                      : r.response_status >= 400 ? 'text-warn-600'
                      : 'text-success-600',
                    )}>
                      {r.response_status ?? '—'}
                    </td>
                  </motion.tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination */}
        <div className="flex items-center justify-between border-t border-ink-100 bg-ink-50/40 px-4 py-3 text-xs text-ink-500">
          <span className="inline-flex items-center gap-2">
            <ScrollText className="h-3.5 w-3.5" />
            Showing entries {page * PAGE_SIZE + 1}–{page * PAGE_SIZE + (rows.length || 0)}
          </span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0 || auditQ.isFetching}
              className="btn-ghost"
            >
              <ChevronLeft className="h-3.5 w-3.5" /> Newer
            </button>
            <span className="px-2 font-semibold text-ink-700">Page {page + 1}</span>
            <button
              onClick={() => setPage((p) => p + 1)}
              disabled={rows.length < PAGE_SIZE || auditQ.isFetching}
              className="btn-ghost"
            >
              Older <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────── Filter widgets ───────────────────────── */

function SelectField({ label, value, onChange, options }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-ink-500">
        {label}
      </span>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </label>
  );
}

function TextField({ label, value, onChange }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-ink-500">
        {label}
      </span>
      <input
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Filters the current page client-side"
      />
    </label>
  );
}

function ToggleField({ label, value, onChange }) {
  return (
    <label className="flex h-full cursor-pointer flex-col">
      <span className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-ink-500">
        {label}
      </span>
      <button
        type="button"
        onClick={() => onChange(!value)}
        className={cn(
          'flex flex-1 items-center justify-between rounded-xl border px-3 py-2 text-sm font-semibold transition-colors',
          value
            ? 'border-warn-400 bg-warn-100/40 text-warn-700'
            : 'border-ink-200 bg-white text-ink-500 hover:border-warn-300',
        )}
      >
        <span className="flex items-center gap-2">
          <ShieldAlert className="h-4 w-4" />
          {value ? 'PHI events only' : 'Show all'}
        </span>
        <span className={cn(
          'h-5 w-9 rounded-full p-0.5 transition-colors',
          value ? 'bg-warn-500' : 'bg-ink-200',
        )}>
          <span className={cn(
            'block h-4 w-4 rounded-full bg-white transition-transform',
            value && 'translate-x-4',
          )} />
        </span>
      </button>
    </label>
  );
}

function CountField({ count, of, loading }) {
  return (
    <div className="flex h-full flex-col">
      <span className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-ink-500">
        Visible
      </span>
      <div className="flex flex-1 items-center justify-center rounded-xl bg-brand-50 px-3 py-2 text-sm font-semibold text-brand-700">
        {loading
          ? <Skeleton className="h-5 w-16" />
          : <span><b className="tabular-nums">{count}</b> / {of}</span>}
      </div>
    </div>
  );
}
