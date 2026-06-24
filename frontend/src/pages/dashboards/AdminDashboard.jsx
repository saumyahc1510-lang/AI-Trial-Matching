import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  AlertTriangle, BadgeCheck, BarChart3, Brain, Clock, Cog, Globe, HeartPulse,
  RefreshCw, ScrollText, ShieldCheck, ShieldX, Sparkles, Trash2, Users, Zap,
} from 'lucide-react';
import {
  Bar, BarChart, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis,
} from 'recharts';
import { format, formatDistanceToNow, parseISO } from 'date-fns';
import toast from 'react-hot-toast';

import PageHeader from '@/components/ui/PageHeader.jsx';
import AnimatedCounter from '@/components/ui/AnimatedCounter.jsx';
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton.jsx';
import {
  adminConfig, adminCreateUser, adminHardDeletePatient, adminListPatients,
  adminListUsers, adminLLMUsage, adminPatientStats, adminPurgePatients,
  adminSyncStatus, adminTriggerSync, adminUpdateConfig, adminUpdateUser,
  adminUserStats, auditStats, queryAudit,
} from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

const ROLES = ['patient', 'coordinator', 'clinician', 'sponsor', 'admin'];

export default function AdminDashboard() {
  const qc = useQueryClient();

  // System-wide user counts for the stat cards — the user *table* below
  // does its own paginated query, so these stay accurate regardless of
  // which page is shown.
  const userStatsQ = useQuery({ queryKey: ['admin', 'users', 'stats'], queryFn: adminUserStats });
  const configQ = useQuery({ queryKey: ['admin', 'config'], queryFn: adminConfig });
  const auditQ  = useQuery({
    queryKey: ['admin', 'audit', 'recent'],
    queryFn: () => queryAudit({ limit: 20 }),
    refetchInterval: 60_000,
  });
  // True audit counters (whole table) — the stat card uses these instead
  // of the 20-row listing length, which would otherwise cap at 20.
  const auditStatsQ = useQuery({
    queryKey: ['admin', 'audit', 'stats'],
    queryFn: auditStats,
    refetchInterval: 60_000,
  });
  // Token telemetry — the LLM client persists one row per call so this
  // populates the moment any service makes its first call.  Poll at
  // 60s so dashboards stay live without hammering the DB.
  const usageQ = useQuery({
    queryKey: ['admin', 'llm-usage'],
    queryFn: adminLLMUsage,
    refetchInterval: 60_000,
  });

  // Trial sync now runs as a background job: the POST returns a job_id
  // immediately, and we poll its status instead of holding the request
  // open for the (up to 30-minute) catalog-wide pull.  ``syncJob`` holds
  // the in-flight job's id + which button started it (for the spinner).
  const [syncJob, setSyncJob] = useState(null);

  const startSync = useMutation({
    mutationFn: ({ payload }) => adminTriggerSync(payload),
    onSuccess: (data, vars) => {
      setSyncJob({ id: data.job_id, kind: vars.kind });
      toast('Sync started — running in the background…');
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Could not start sync.'),
  });

  const syncStatusQ = useQuery({
    queryKey: ['admin', 'sync-job', syncJob?.id],
    queryFn: () => adminSyncStatus(syncJob.id),
    enabled: !!syncJob?.id,
    // Poll every 2.5s until the job reaches a terminal state.
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return s === 'succeeded' || s === 'failed' ? false : 2500;
    },
  });

  // React to terminal job states once, then clear the in-flight job.
  useEffect(() => {
    const job = syncStatusQ.data;
    if (!syncJob || !job) return;
    if (job.status === 'succeeded') {
      const s = job.result?.sync;
      toast.success(`Synced ${s?.trials_created ?? 0} new + ${s?.trials_updated ?? 0} updated trial(s).`);
      qc.invalidateQueries({ queryKey: ['trials'] });
      setSyncJob(null);
    } else if (job.status === 'failed') {
      toast.error(job.error || 'Sync failed.');
      setSyncJob(null);
    }
  }, [syncStatusQ.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const syncing = !!syncJob || startSync.isPending;

  const stats = {
    users:      userStatsQ.data?.total ?? 0,
    active:     userStatsQ.data?.active ?? 0,
    admins:     userStatsQ.data?.admins ?? 0,
    audit24h:   auditStatsQ.data?.last_24h ?? 0,
  };

  return (
    <div>
      <PageHeader
        eyebrow="System administration"
        title="Operations console"
        description="User accounts, runtime configuration, scheduled sync, and the immutable audit trail."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => startSync.mutate({ payload: {}, kind: 'configured' })}
              disabled={syncing}
              className="btn-secondary"
              title="Sync the configured TRIAL_SYNC_CONDITIONS only"
            >
              <RefreshCw className={cn('h-4 w-4', syncJob?.kind === 'configured' && 'animate-spin')} />
              {syncJob?.kind === 'configured' ? 'Syncing…' : 'Sync configured'}
            </button>
            <button
              onClick={() => {
                if (window.confirm(
                  'Pull every recruiting trial from ClinicalTrials.gov (capped at 5000).  '
                  + 'This runs in the background and can take several minutes — proceed?'
                )) {
                  startSync.mutate({
                    payload: { fetch_all: true, max_trials_per_condition: 5000 },
                    kind: 'all',
                  });
                }
              }}
              disabled={syncing}
              className="btn-primary"
              title="Fetch every recruiting trial from CT.gov, no condition filter"
            >
              <Globe className={cn('h-4 w-4', syncJob?.kind === 'all' && 'animate-spin')} />
              {syncJob?.kind === 'all' ? 'Syncing all…' : 'Sync everything'}
            </button>
          </div>
        }
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={Users}      tone="brand"   label="Total users"           value={stats.users}        loading={userStatsQ.isLoading} />
        <StatCard icon={BadgeCheck} tone="success" label="Active accounts"        value={stats.active}       loading={userStatsQ.isLoading} />
        <StatCard icon={ShieldCheck}tone="warn"    label="Admins"                 value={stats.admins}       loading={userStatsQ.isLoading} />
        <StatCard icon={ScrollText} tone="accent"  label="Audit events (24h)"     value={stats.audit24h}     loading={auditStatsQ.isLoading} />
      </div>

      {/* Two-column layout below stats */}
      <div className="mt-8 grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2 space-y-6">
          <UserManagement />
          <PatientManagement />
          <AuditLogViewer rows={auditQ.data} loading={auditQ.isLoading} />
        </div>
        <div className="space-y-6">
          <SystemConfig config={configQ.data} loading={configQ.isLoading} />
          <LLMUsageCard usage={usageQ.data} loading={usageQ.isLoading} model={configQ.data?.llm_model} />
        </div>
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
          {loading ? <Skeleton className="h-9 w-20" /> : <AnimatedCounter value={value} />}
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

/* ───────────────────────── User management ─────────────────────── */

const USERS_PAGE_SIZE = 8;

function UserManagement() {
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState('');

  const usersQ = useQuery({
    queryKey: ['admin', 'users', 'list', { page, search }],
    queryFn: () => adminListUsers({
      limit: USERS_PAGE_SIZE,
      offset: page * USERS_PAGE_SIZE,
      q: search.trim() || undefined,
    }),
    // Keep the previous page visible while the next one loads — avoids a
    // skeleton flash on every page / search change.
    placeholderData: (prev) => prev,
  });

  const users = usersQ.data?.items || [];
  const total = usersQ.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / USERS_PAGE_SIZE));
  const loading = usersQ.isLoading;

  function onSearchChange(value) {
    setSearch(value);
    setPage(0);  // a new filter invalidates the current page index
  }

  const update = useMutation({
    mutationFn: ({ userId, payload }) => adminUpdateUser(userId, payload),
    onSuccess: () => {
      // Prefix invalidation refreshes both the paged list and the stat
      // cards (['admin','users','stats']).
      qc.invalidateQueries({ queryKey: ['admin', 'users'] });
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Update failed.'),
  });

  function changeRole(user, role) {
    update.mutate({ userId: user.id, payload: { role } });
  }
  function toggleActive(user) {
    update.mutate({ userId: user.id, payload: { is_active: !user.is_active } });
  }

  return (
    <div className="card">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink-100 px-5 py-4">
        <div>
          <div className="font-display text-lg font-semibold text-ink-900">Users & roles</div>
          <div className="text-xs text-ink-500">
            Change role, deactivate / reactivate, or create staff accounts.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <input
            className="input h-9 w-44 text-sm"
            placeholder="Search name or email…"
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
          />
          <button onClick={() => setCreating(true)} className="btn-secondary whitespace-nowrap">
            + New user
          </button>
        </div>
      </div>
      <div className="overflow-x-auto">
        {loading ? (
          <div className="p-5"><SkeletonRows rows={5} /></div>
        ) : total === 0 ? (
          <div className="p-6 text-center text-sm text-ink-500">
            {search.trim() ? 'No users match your search.' : 'No users yet.'}
          </div>
        ) : (
          <table className="min-w-full text-sm">
            <thead>
              <tr className="bg-ink-50/60 text-left text-[10px] font-semibold uppercase tracking-wider text-ink-500">
                <th className="px-5 py-3">User</th>
                <th className="px-5 py-3">Role</th>
                <th className="px-5 py-3">Status</th>
                <th className="px-5 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <motion.tr
                  key={u.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  className="border-t border-ink-100 last:border-b"
                >
                  <td className="px-5 py-3">
                    <div className="font-semibold text-ink-800">{u.full_name}</div>
                    <div className="text-xs text-ink-500">{u.email}</div>
                  </td>
                  <td className="px-5 py-3">
                    <select
                      className="rounded-lg border border-ink-200 bg-white px-2 py-1 text-xs font-semibold capitalize"
                      value={u.role}
                      onChange={(e) => changeRole(u, e.target.value)}
                      disabled={update.isPending}
                    >
                      {ROLES.map((r) => (
                        <option key={r} value={r}>{r}</option>
                      ))}
                    </select>
                  </td>
                  <td className="px-5 py-3">
                    <span className={cn(
                      'chip',
                      u.is_active ? 'chip-success' : 'chip-ink',
                    )}>
                      {u.is_active ? <BadgeCheck className="h-3 w-3" /> : <ShieldX className="h-3 w-3" />}
                      {u.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-right">
                    <button
                      onClick={() => toggleActive(u)}
                      disabled={update.isPending}
                      className="btn-ghost text-xs"
                    >
                      {u.is_active ? 'Deactivate' : 'Reactivate'}
                    </button>
                  </td>
                </motion.tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {total > 0 && (
        <div className="flex items-center justify-between border-t border-ink-100 px-5 py-3 text-xs text-ink-500">
          <span>
            {total} user{total === 1 ? '' : 's'} · page {page + 1} of {pageCount}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0 || usersQ.isFetching}
              className="btn-ghost text-xs disabled:opacity-40"
            >
              Prev
            </button>
            <button
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              disabled={page >= pageCount - 1 || usersQ.isFetching}
              className="btn-ghost text-xs disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>
      )}

      {creating && <CreateUserModal onClose={() => setCreating(false)} />}
    </div>
  );
}

function CreateUserModal({ onClose }) {
  const qc = useQueryClient();
  const [email, setEmail] = useState('');
  const [password, setPwd] = useState('');
  const [fullName, setName] = useState('');
  const [role, setRole] = useState('coordinator');

  const create = useMutation({
    mutationFn: () => adminCreateUser({
      email, password, full_name: fullName, role,
    }),
    onSuccess: () => {
      toast.success('User created.');
      qc.invalidateQueries({ queryKey: ['admin', 'users'] });
      onClose();
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Create failed.'),
  });

  return (
    <motion.div
      initial={{ opacity: 0 }} animate={{ opacity: 1 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-900/40 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.96, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
        className="card w-full max-w-md p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 font-display text-lg font-semibold text-ink-900">
          Create a new user
        </div>
        <div className="space-y-3">
          <Field label="Full name">
            <input className="input" value={fullName} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Email">
            <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          <Field label="Initial password">
            <input className="input" type="password" autoComplete="new-password" value={password} onChange={(e) => setPwd(e.target.value)} />
          </Field>
          <Field label="Role">
            <select className="input" value={role} onChange={(e) => setRole(e.target.value)}>
              {ROLES.filter((r) => r !== 'patient').map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </Field>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button
            onClick={() => create.mutate()}
            disabled={create.isPending || !email || !password || !fullName}
            className="btn-primary"
          >
            {create.isPending ? 'Creating…' : 'Create'}
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
}

function Field({ label, children }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-ink-500">
        {label}
      </span>
      {children}
    </label>
  );
}

/* ───────────────────────── Patient management ──────────────────── */

const PATIENTS_PAGE_SIZE = 8;
const PATIENT_STATUSES = ['active', 'inactive', 'deceased'];

function PatientManagement() {
  const qc = useQueryClient();
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [purging, setPurging] = useState(false);

  const statsQ = useQuery({ queryKey: ['admin', 'patients', 'stats'], queryFn: adminPatientStats });

  const patientsQ = useQuery({
    queryKey: ['admin', 'patients', 'list', { page, search, statusFilter }],
    queryFn: () => adminListPatients({
      limit: PATIENTS_PAGE_SIZE,
      offset: page * PATIENTS_PAGE_SIZE,
      q: search.trim() || undefined,
      status: statusFilter || undefined,
    }),
    placeholderData: (prev) => prev,
  });

  const patients = patientsQ.data?.items || [];
  const total = patientsQ.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PATIENTS_PAGE_SIZE));

  const del = useMutation({
    mutationFn: adminHardDeletePatient,
    onSuccess: () => {
      toast.success('Patient deleted.');
      qc.invalidateQueries({ queryKey: ['admin', 'patients'] });
      qc.invalidateQueries({ queryKey: ['patients'] });
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Delete failed.'),
  });

  function confirmDelete(p) {
    if (window.confirm(
      `Permanently delete ${p.first_name} ${p.last_name} and ALL of their records `
      + '(events, versions, match results)?  This cannot be undone.'
    )) {
      del.mutate(p.id);
    }
  }

  function onSearchChange(value) {
    setSearch(value);
    setPage(0);
  }
  function onStatusChange(value) {
    setStatusFilter(value);
    setPage(0);
  }

  const s = statsQ.data;

  return (
    <div className="card">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink-100 px-5 py-4">
        <div>
          <div className="flex items-center gap-2 font-display text-lg font-semibold text-ink-900">
            <HeartPulse className="h-4 w-4 text-brand-500" /> Patients
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-xs text-ink-500">
            {s ? (
              <>
                <span className="chip chip-success">{s.active} active</span>
                <span className="chip chip-ink">{s.inactive} inactive</span>
                {s.deceased > 0 && <span className="chip chip-ink">{s.deceased} deceased</span>}
                <span className="text-ink-400">· {s.total} total</span>
              </>
            ) : 'Loading…'}
          </div>
        </div>
        <button
          onClick={() => setPurging(true)}
          disabled={!s || s.total === 0}
          className="btn-ghost text-xs text-danger-600 disabled:opacity-40"
          title="Bulk hard-delete patients"
        >
          <Trash2 className="h-3.5 w-3.5" /> Purge…
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-b border-ink-100 px-5 py-3">
        <input
          className="input h-9 w-44 text-sm"
          placeholder="Search name or MRN…"
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
        />
        <select
          className="input h-9 w-32 text-sm capitalize"
          value={statusFilter}
          onChange={(e) => onStatusChange(e.target.value)}
        >
          <option value="">All statuses</option>
          {PATIENT_STATUSES.map((st) => (
            <option key={st} value={st}>{st}</option>
          ))}
        </select>
      </div>

      <div className="overflow-x-auto">
        {patientsQ.isLoading ? (
          <div className="p-5"><SkeletonRows rows={4} /></div>
        ) : total === 0 ? (
          <div className="p-6 text-center text-sm text-ink-500">
            {search.trim() || statusFilter ? 'No patients match your filter.' : 'No patients in the system.'}
          </div>
        ) : (
          <table className="min-w-full text-sm">
            <thead>
              <tr className="bg-ink-50/60 text-left text-[10px] font-semibold uppercase tracking-wider text-ink-500">
                <th className="px-5 py-3">Patient</th>
                <th className="px-5 py-3">MRN</th>
                <th className="px-5 py-3">Status</th>
                <th className="px-5 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {patients.map((p) => (
                <motion.tr
                  key={p.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  className="border-t border-ink-100 last:border-b"
                >
                  <td className="px-5 py-3">
                    <div className="font-semibold text-ink-800">{p.first_name} {p.last_name}</div>
                    <div className="text-xs text-ink-500">DOB {p.date_of_birth} · {p.sex}</div>
                  </td>
                  <td className="px-5 py-3 font-mono text-xs text-ink-500">{p.external_id || '—'}</td>
                  <td className="px-5 py-3">
                    <span className={cn('chip capitalize', p.status === 'active' ? 'chip-success' : 'chip-ink')}>
                      {p.status}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-right">
                    <button
                      onClick={() => confirmDelete(p)}
                      disabled={del.isPending}
                      className="btn-ghost text-xs text-danger-600"
                    >
                      <Trash2 className="h-3.5 w-3.5" /> Delete
                    </button>
                  </td>
                </motion.tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {total > 0 && (
        <div className="flex items-center justify-between border-t border-ink-100 px-5 py-3 text-xs text-ink-500">
          <span>page {page + 1} of {pageCount}</span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0 || patientsQ.isFetching}
              className="btn-ghost text-xs disabled:opacity-40"
            >
              Prev
            </button>
            <button
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              disabled={page >= pageCount - 1 || patientsQ.isFetching}
              className="btn-ghost text-xs disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>
      )}

      {purging && <PurgePatientsModal stats={s} onClose={() => setPurging(false)} />}
    </div>
  );
}

function PurgePatientsModal({ stats, onClose }) {
  const qc = useQueryClient();
  const [scope, setScope] = useState('');     // '' = all, or a status
  const [confirmText, setConfirmText] = useState('');

  const purge = useMutation({
    mutationFn: () => adminPurgePatients({ confirm: 'DELETE', status: scope || undefined }),
    onSuccess: (res) => {
      toast.success(`Purged ${res.deleted} patient(s).`);
      qc.invalidateQueries({ queryKey: ['admin', 'patients'] });
      qc.invalidateQueries({ queryKey: ['patients'] });
      onClose();
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Purge failed.'),
  });

  const affected = scope === 'active' ? stats?.active
    : scope === 'inactive' ? stats?.inactive
    : scope === 'deceased' ? stats?.deceased
    : stats?.total;

  return (
    <motion.div
      initial={{ opacity: 0 }} animate={{ opacity: 1 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-900/40 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.96, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
        className="card w-full max-w-md p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-2 flex items-center gap-2 font-display text-lg font-semibold text-danger-600">
          <AlertTriangle className="h-5 w-5" /> Purge patients
        </div>
        <p className="text-sm text-ink-600">
          This permanently deletes patient records and all of their medical events,
          version history, and match results. Linked login accounts are unlinked, not
          deleted. <b className="text-ink-800">This cannot be undone.</b>
        </p>

        <div className="mt-4 space-y-3">
          <Field label="Scope">
            <select className="input capitalize" value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="">All patients</option>
              {PATIENT_STATUSES.map((st) => (
                <option key={st} value={st}>{st} only</option>
              ))}
            </select>
          </Field>
          <div className="rounded-lg bg-danger-100 px-3 py-2 text-xs text-danger-600">
            {affected ?? 0} patient(s) will be deleted.
          </div>
          <Field label="Type DELETE to confirm">
            <input
              className="input"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder="DELETE"
              autoComplete="off"
            />
          </Field>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button
            onClick={() => purge.mutate()}
            disabled={purge.isPending || confirmText !== 'DELETE' || !affected}
            className="btn-primary bg-danger-600 hover:bg-danger-500"
          >
            {purge.isPending ? 'Purging…' : `Delete ${affected ?? 0}`}
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
}

/* ───────────────────────── Audit log viewer ────────────────────── */

function AuditLogViewer({ rows, loading }) {
  return (
    <div className="card">
      <div className="border-b border-ink-100 px-5 py-4">
        <div className="flex items-center gap-2 text-sm font-semibold text-ink-900">
          <ScrollText className="h-4 w-4 text-brand-500" />
          Audit log — last 20 events
        </div>
        <div className="text-xs text-ink-500">
          Append-only, enforced at the database via Postgres triggers.
        </div>
      </div>
      <div className="overflow-x-auto">
        {loading ? (
          <div className="p-5"><SkeletonRows rows={4} /></div>
        ) : !rows || rows.length === 0 ? (
          <div className="p-6 text-center text-sm text-ink-500">
            No audit events yet.  Every API call lands here.
          </div>
        ) : (
          <table className="min-w-full text-xs">
            <thead>
              <tr className="bg-ink-50/60 text-left text-[10px] font-semibold uppercase tracking-wider text-ink-500">
                <th className="px-5 py-3">When</th>
                <th className="px-5 py-3">User</th>
                <th className="px-5 py-3">Action</th>
                <th className="px-5 py-3">Resource</th>
                <th className="px-5 py-3 text-right">PHI?</th>
                <th className="px-5 py-3 text-right">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="border-t border-ink-100">
                  <td className="px-5 py-2.5 text-ink-500">
                    {formatDistanceToNow(new Date(r.timestamp), { addSuffix: true })}
                  </td>
                  <td className="px-5 py-2.5 font-mono text-[10px] text-ink-500">
                    {r.user_id ? r.user_id.slice(0, 8) + '…' : '— system —'}
                  </td>
                  <td className="px-5 py-2.5">
                    <span className="chip chip-ink text-[10px]">{r.action}</span>
                  </td>
                  <td className="px-5 py-2.5 text-ink-700">
                    {r.resource_type}
                    {r.resource_id && (
                      <span className="ml-1 font-mono text-[10px] text-ink-400">
                        {r.resource_id.slice(0, 8)}…
                      </span>
                    )}
                  </td>
                  <td className="px-5 py-2.5 text-right">
                    {r.phi_accessed ? (
                      <span className="chip chip-warn text-[10px]">PHI</span>
                    ) : <span className="text-ink-300">—</span>}
                  </td>
                  <td className={cn(
                    'px-5 py-2.5 text-right font-semibold tabular-nums',
                    r.response_status >= 500 ? 'text-danger-500'
                    : r.response_status >= 400 ? 'text-warn-600'
                    : 'text-success-600',
                  )}>
                    {r.response_status ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

/* ───────────────────────── System config ──────────────────────── */

function SystemConfig({ config, loading }) {
  const [editing, setEditing] = useState(false);
  return (
    <div className="card p-5">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
          <Cog className="h-3.5 w-3.5" /> Runtime configuration
        </div>
        {!loading && (
          <button onClick={() => setEditing(true)} className="btn-ghost text-[11px]">
            Edit parsing
          </button>
        )}
      </div>
      {loading ? (
        <SkeletonRows rows={4} />
      ) : (
        <dl className="space-y-2">
          <ConfigRow k="Provider"        v={config?.llm_provider} />
          <ConfigRow k="Model"           v={config?.llm_model} />
          <ConfigRow k="Conditions"      v={(config?.trial_sync_conditions || []).join(', ')} />
          <ConfigRow k="Parse categories" v={(config?.trial_parse_categories || []).join(', ') || 'All'} />
          <ConfigRow k="Parse cap / sync" v={config?.trial_parse_max_per_sync ? `${config.trial_parse_max_per_sync} trials` : 'No cap'} />
          <ConfigRow k="Sync interval"   v={`${config?.trial_sync_interval_hours}h`} />
          <ConfigRow k="Workers"         v={config?.use_celery ? 'Redis async' : 'Eager (dev)'} />
          <ConfigRow k="PHI scrubber"    v={config?.enable_phi_deidentification ? 'On' : 'Off'} tone={config?.enable_phi_deidentification ? 'success' : 'danger'} />
          <ConfigRow k="Audit logging"   v={config?.audit_log_enabled ? 'On' : 'Off'} tone={config?.audit_log_enabled ? 'success' : 'danger'} />
          <ConfigRow k="Diversity blend" v={`α ${config?.diversity_rank_alpha} · β ${config?.diversity_rank_beta}`} />
        </dl>
      )}
      {editing && <ParseConfigModal config={config} onClose={() => setEditing(false)} />}
    </div>
  );
}

function ParseConfigModal({ config, onClose }) {
  const qc = useQueryClient();
  const allCats = config?.available_categories || [];
  const [selected, setSelected] = useState(() => new Set(config?.trial_parse_categories || []));
  const [cap, setCap] = useState(config?.trial_parse_max_per_sync ?? 20);

  const save = useMutation({
    mutationFn: () => adminUpdateConfig({
      trial_parse_categories: Array.from(selected),
      trial_parse_max_per_sync: Number(cap),
    }),
    onSuccess: () => {
      toast.success('Parsing settings updated.');
      qc.invalidateQueries({ queryKey: ['admin', 'config'] });
      onClose();
    },
    onError: (err) => toast.error(err?.response?.data?.detail || 'Update failed.'),
  });

  function toggle(cat) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(cat) ? next.delete(cat) : next.add(cat);
      return next;
    });
  }

  return (
    <motion.div
      initial={{ opacity: 0 }} animate={{ opacity: 1 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-900/40 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.96, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
        className="card w-full max-w-lg p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1 flex items-center gap-2 font-display text-lg font-semibold text-ink-900">
          <Cog className="h-4 w-4 text-brand-500" /> Criteria-parsing settings
        </div>
        <p className="text-xs text-ink-500">
          Parsing eligibility criteria is the LLM-expensive part of a sync. Limit it to a
          few categories and cap how many trials are parsed per sync to stay within your
          token budget. Changes apply on the next sync — no restart needed.
        </p>

        <div className="mt-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wider text-ink-500">
              Categories ({selected.size === 0 ? 'all' : selected.size})
            </span>
            <button
              onClick={() => setSelected(new Set())}
              className="btn-ghost text-[11px]"
              title="Clear = parse all categories"
            >
              Select none (= all)
            </button>
          </div>
          <div className="max-h-52 overflow-y-auto rounded-xl border border-ink-100 p-2">
            <div className="grid grid-cols-2 gap-1">
              {allCats.map((cat) => (
                <label key={cat} className="flex items-center gap-2 rounded-lg px-2 py-1 text-xs hover:bg-ink-50">
                  <input
                    type="checkbox"
                    checked={selected.has(cat)}
                    onChange={() => toggle(cat)}
                  />
                  <span className="truncate text-ink-700">{cat}</span>
                </label>
              ))}
            </div>
          </div>
          {selected.size === 0 && (
            <div className="mt-2 rounded-lg bg-warn-100 px-3 py-2 text-[11px] text-warn-600">
              No categories selected — every category will be parsed. This can be expensive.
            </div>
          )}
        </div>

        <div className="mt-4">
          <Field label="Max trials parsed per sync (0 = no cap)">
            <input
              type="number" min={0} max={500}
              className="input"
              value={cap}
              onChange={(e) => setCap(e.target.value)}
            />
          </Field>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button
            onClick={() => save.mutate()}
            disabled={save.isPending}
            className="btn-primary"
          >
            {save.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
}

function ConfigRow({ k, v, tone }) {
  const cls = tone === 'success' ? 'text-success-600'
            : tone === 'danger'  ? 'text-danger-500'
            : 'text-ink-800';
  return (
    <div className="flex items-center justify-between border-b border-ink-100 py-2 last:border-b-0 text-sm">
      <dt className="text-xs uppercase tracking-wider text-ink-400">{k}</dt>
      <dd className={cn('font-medium', cls)}>{v ?? '—'}</dd>
    </div>
  );
}

/* ───────────────────────── LLM usage card ─────────────────────── */

/**
 * Reads aggregate token + latency + success-rate metrics from
 * ``/admin/usage`` and renders:
 *  - 4 headline tiles (calls, tokens last 7d, success %, avg latency)
 *  - a 7-day token-volume bar chart
 *  - per-operation breakdown so admins know which feature is eating cost
 *
 * When the usage table is empty (fresh install, no LLM calls yet) we
 * fall back to a clear "no data yet" prompt with the configured model.
 */
function LLMUsageCard({ usage, loading, model }) {
  if (loading) {
    return (
      <div className="card p-5">
        <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
          <BarChart3 className="h-3.5 w-3.5" /> LLM usage
        </div>
        <SkeletonRows rows={4} />
      </div>
    );
  }

  const hasData = usage && usage.total_calls > 0;
  if (!hasData) {
    return (
      <div className="card p-5">
        <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
          <BarChart3 className="h-3.5 w-3.5" /> LLM usage
        </div>
        <div className="rounded-xl bg-gradient-to-br from-brand-50 to-accent-50 p-4">
          <div className="text-xs text-ink-500">Current model</div>
          <div className="font-display text-base font-bold text-ink-900">{model || '—'}</div>
          <div className="mt-3 text-xs text-ink-500">
            No calls recorded yet.  Trigger a trial sync or run a match — the engine logs every
            prompt's token count + latency here.
          </div>
        </div>
      </div>
    );
  }

  // Build a stable 7-bucket window so the chart shows continuous days
  // even when some have zero activity.
  const chartData = build7DaySeries(usage.daily);
  const topOp = (usage.per_operation || [])[0];

  return (
    <div className="card p-5">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
        <BarChart3 className="h-3.5 w-3.5" /> LLM usage
      </div>

      {/* Headline tiles */}
      <div className="grid grid-cols-2 gap-2">
        <MiniTile icon={Sparkles} label="Total calls"     value={usage.total_calls.toLocaleString()} />
        <MiniTile icon={Brain}     label="Tokens (7d)"     value={shortNum(usage.tokens_last_7d)} />
        <MiniTile icon={Zap}       label="Success"         value={`${Math.round((usage.success_rate || 0) * 100)}%`}
                  tone={usage.success_rate > 0.95 ? 'success' : usage.success_rate > 0.85 ? 'warn' : 'danger'} />
        <MiniTile icon={Clock}     label="Avg latency"     value={usage.avg_latency_ms != null
                                                                  ? `${Math.round(usage.avg_latency_ms)}ms`
                                                                  : '—'} />
      </div>

      {/* 7-day bar chart */}
      <div className="mt-4 rounded-xl bg-gradient-to-br from-brand-50/60 to-accent-50/60 p-3">
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-ink-400">
          Tokens · last 7 days
        </div>
        <div className="h-28 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData} margin={{ top: 6, right: 6, left: -16, bottom: 0 }}>
              <XAxis
                dataKey="label"
                axisLine={false} tickLine={false}
                tick={{ fontSize: 10, fill: '#8a93a6' }}
              />
              <YAxis
                axisLine={false} tickLine={false}
                tick={{ fontSize: 10, fill: '#8a93a6' }}
                tickFormatter={shortNum}
                width={36}
              />
              <RTooltip
                cursor={{ fill: 'rgba(88,103,230,0.08)' }}
                contentStyle={{
                  border: 'none', borderRadius: 12, fontSize: 12,
                  boxShadow: '0 6px 28px -8px rgba(15,20,36,0.25)',
                }}
                formatter={(v, name) => [shortNum(v), name === 'tokens' ? 'tokens' : name]}
              />
              <Bar dataKey="tokens" fill="#5867e6" radius={[6, 6, 0, 0]} maxBarSize={28} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Per-operation breakdown */}
      {(usage.per_operation || []).length > 0 && (
        <div className="mt-4">
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-ink-400">
            By operation
          </div>
          <ul className="space-y-1.5">
            {usage.per_operation.slice(0, 5).map((row) => (
              <OperationBar
                key={row.label}
                label={row.label}
                tokens={row.tokens}
                calls={row.calls}
                total={topOp?.tokens || 1}
              />
            ))}
          </ul>
        </div>
      )}

      <div className="mt-3 text-[10px] text-ink-400">
        Model: <span className="font-mono">{model || '—'}</span>
      </div>
    </div>
  );
}

function MiniTile({ icon: Icon, label, value, tone }) {
  const cls = {
    success: 'text-success-600',
    warn:    'text-warn-600',
    danger:  'text-danger-500',
  }[tone] || 'text-ink-900';
  return (
    <div className="rounded-xl border border-ink-100 bg-white p-2.5">
      <div className="flex items-center gap-1.5 text-[9px] font-semibold uppercase tracking-wider text-ink-400">
        <Icon className="h-3 w-3" /> {label}
      </div>
      <div className={cn('mt-0.5 font-display text-lg font-bold tabular-nums', cls)}>
        {value}
      </div>
    </div>
  );
}

function OperationBar({ label, tokens, calls, total }) {
  const pct = Math.max(2, Math.round((tokens / total) * 100));
  return (
    <li>
      <div className="flex items-baseline justify-between text-[11px]">
        <span className="truncate font-medium text-ink-700">{label}</span>
        <span className="text-ink-400">
          <b className="text-ink-700 tabular-nums">{shortNum(tokens)}</b> tokens · {calls} calls
        </span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-ink-100">
        <motion.div
          initial={{ width: 0 }}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.6, ease: 'easeOut' }}
          className="h-full bg-gradient-to-r from-brand-400 to-brand-600"
        />
      </div>
    </li>
  );
}

/* ───────────────────────── helpers ────────────────────────────── */

function shortNum(n) {
  const num = Number(n) || 0;
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000)     return `${(num / 1_000).toFixed(num >= 10_000 ? 0 : 1)}k`;
  return num.toString();
}

function build7DaySeries(daily) {
  // ``daily`` arrives as a sparse list — fill in zero-token days so
  // the bar chart shows a continuous 7-day window.
  const byDate = new Map((daily || []).map((d) => [d.date, d]));
  const out = [];
  // Build the window entirely in UTC so the day keys match the backend's
  // UTC-bucketed ``date`` strings regardless of the viewer's timezone.
  // (Mixing local getDate() with toISOString() shifted the chart a day.)
  const now = new Date();
  const todayUTC = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  for (let i = 6; i >= 0; i--) {
    const iso = new Date(todayUTC - i * 86_400_000).toISOString().slice(0, 10);
    const point = byDate.get(iso) || { date: iso, tokens: 0, calls: 0 };
    out.push({
      // parseISO + format both run in local tz, so they cancel out and
      // just render the calendar date in the key — no shift.
      label: format(parseISO(iso), 'MMM d'),
      tokens: point.tokens,
      calls: point.calls,
    });
  }
  return out;
}
