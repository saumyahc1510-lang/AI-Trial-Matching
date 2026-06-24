import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  ArrowUpRight, FileSearch, Plus, Search, Upload, UsersRound,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { format } from 'date-fns';

import PageHeader from '@/components/ui/PageHeader.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import { Skeleton } from '@/components/ui/Skeleton.jsx';
import { listPatients, ingestFhirBootstrap } from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

export default function Patients() {
  const qc = useQueryClient();
  const [query, setQuery] = useState('');

  const { data: patients = [], isLoading } = useQuery({
    queryKey: ['patients', 'all'],
    queryFn: () => listPatients({ limit: 200 }),
  });

  const ingest = useMutation({
    mutationFn: ingestFhirBootstrap,
    onSuccess: (res) => {
      toast.success(
        `Ingested patient (${res.events_created} events created, ${res.events_skipped} skipped).`,
      );
      qc.invalidateQueries({ queryKey: ['patients'] });
    },
    onError: (err) => {
      const detail = err?.response?.data?.detail || err.message;
      toast.error(typeof detail === 'string' ? detail : 'Ingestion failed.');
    },
  });

  const filtered = filterPatients(patients, query);

  return (
    <div>
      <PageHeader
        eyebrow="Catalog"
        title="Patients"
        description="The cohort the matching engine reasons over. Drop a FHIR R4 Bundle to add one."
        actions={
          <FhirUploadButton onSubmit={(bundle) => ingest.mutate(bundle)} busy={ingest.isPending} />
        }
      />

      <div className="card mb-5">
        <div className="flex items-center gap-3 p-3">
          <span className="ml-2 text-ink-400">
            <Search className="h-4 w-4" />
          </span>
          <input
            className="flex-1 bg-transparent text-sm placeholder-ink-400 focus:outline-none"
            placeholder="Search by name, external ID, sex, status…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="rounded-full bg-ink-100 px-2 py-0.5 text-xs font-semibold text-ink-500">
            {filtered.length} / {patients.length}
          </span>
        </div>
      </div>

      {isLoading ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={UsersRound}
          title="No patients yet"
          description={
            query
              ? `Nothing matches "${query}".`
              : 'Drop a FHIR R4 Bundle to import a patient and start matching them against the trial catalog.'
          }
        />
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((p, i) => (
            <motion.div
              key={p.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: Math.min(i, 8) * 0.03 }}
            >
              <PatientCard patient={p} />
            </motion.div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────── Card ───────────────────────────────── */

function PatientCard({ patient }) {
  const initials = `${patient.first_name?.[0] || '?'}${patient.last_name?.[0] || ''}`.toUpperCase();
  return (
    <Link
      to={`/patients/${patient.id}`}
      className="card group block p-4 transition-shadow hover:shadow-glow"
    >
      <div className="flex items-start gap-3">
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-400 to-accent-400 text-sm font-semibold text-white">
          {initials}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate font-display text-base font-semibold text-ink-900">
              {patient.first_name} {patient.last_name}
            </span>
            <span
              className={cn(
                'chip',
                patient.status === 'active'   && 'chip-success',
                patient.status === 'inactive' && 'chip-ink',
                patient.status === 'deceased' && 'chip-danger',
              )}
            >
              {patient.status}
            </span>
          </div>
          <div className="mt-0.5 truncate text-xs text-ink-400">
            {patient.external_id || 'No external ID'}
          </div>
          <div className="mt-3 flex flex-wrap gap-1.5 text-[11px]">
            <Meta>{patient.sex}</Meta>
            {patient.date_of_birth && (
              <Meta>
                DOB&nbsp;{patient.date_of_birth}
              </Meta>
            )}
            {patient.race      && <Meta>{patient.race}</Meta>}
            {patient.ethnicity && <Meta>{patient.ethnicity}</Meta>}
            {patient.preferred_language && (
              <Meta>{patient.preferred_language.toUpperCase()}</Meta>
            )}
          </div>
        </div>
        <ArrowUpRight className="h-4 w-4 -translate-x-1 text-ink-300 opacity-0 transition-all group-hover:translate-x-0 group-hover:opacity-100 group-hover:text-brand-500" />
      </div>
    </Link>
  );
}

function Meta({ children }) {
  return (
    <span className="rounded-full bg-ink-100/80 px-2 py-0.5 text-ink-600">
      {children}
    </span>
  );
}

/* ───────────────────── FHIR upload dialog ───────────────────────── */

function FhirUploadButton({ onSubmit, busy }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className="btn-primary" onClick={() => setOpen(true)}>
        <Upload className="h-4 w-4" />
        Import FHIR
      </button>
      {open && (
        <FhirUploadModal
          onClose={() => setOpen(false)}
          onSubmit={(bundle) => {
            onSubmit(bundle);
            setOpen(false);
          }}
          busy={busy}
        />
      )}
    </>
  );
}

function FhirUploadModal({ onClose, onSubmit, busy }) {
  const [text, setText] = useState(SAMPLE_BUNDLE);
  const [dropHot, setDropHot] = useState(false);

  function readFile(file) {
    const reader = new FileReader();
    reader.onload = () => setText(String(reader.result || ''));
    reader.readAsText(file);
  }

  function handleSubmit() {
    let bundle;
    try {
      bundle = JSON.parse(text);
    } catch (e) {
      toast.error('That doesn’t look like valid JSON.');
      return;
    }
    onSubmit(bundle);
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-900/40 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.96, opacity: 0, y: 8 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        className="card relative w-full max-w-2xl overflow-hidden p-0"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-ink-100 px-5 py-4">
          <div>
            <div className="font-display text-lg font-semibold text-ink-900">
              Import FHIR Bundle
            </div>
            <div className="text-xs text-ink-500">
              Paste a FHIR R4 Bundle JSON, or drop a .json file below.  Demo
              bundle is pre-filled so you can try it instantly.
            </div>
          </div>
          <button onClick={onClose} className="btn-ghost">Close</button>
        </div>
        <div
          className={cn(
            'm-5 rounded-xl border-2 border-dashed p-4 transition-colors',
            dropHot
              ? 'border-brand-400 bg-brand-50'
              : 'border-ink-200 bg-ink-50',
          )}
          onDragOver={(e) => { e.preventDefault(); setDropHot(true); }}
          onDragLeave={() => setDropHot(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDropHot(false);
            const f = e.dataTransfer.files?.[0];
            if (f) readFile(f);
          }}
        >
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
            rows={14}
            className="h-72 w-full resize-none rounded-lg border border-ink-200 bg-white p-3 font-mono text-xs text-ink-700 focus:outline-none"
          />
          <div className="mt-2 flex items-center justify-between text-xs text-ink-400">
            <span>{dropHot ? 'Release to load file' : 'Tip: drag-and-drop a .json file anywhere on this box.'}</span>
            <button
              onClick={() => setText(SAMPLE_BUNDLE)}
              className="font-semibold text-brand-600 hover:underline"
            >
              Reset to demo
            </button>
          </div>
        </div>
        <div className="flex justify-end gap-2 border-t border-ink-100 px-5 py-3">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button
            onClick={handleSubmit}
            disabled={busy}
            className="btn-primary"
          >
            <Plus className="h-4 w-4" />
            {busy ? 'Ingesting…' : 'Ingest bundle'}
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
}

/* ────────────────────────── Helpers ─────────────────────────────── */

function filterPatients(patients, q) {
  if (!q) return patients;
  const lower = q.toLowerCase();
  return patients.filter((p) => {
    const haystack = [
      p.first_name, p.last_name, p.external_id,
      p.race, p.ethnicity, p.status, p.sex,
    ].filter(Boolean).join(' ').toLowerCase();
    return haystack.includes(lower);
  });
}

const SAMPLE_BUNDLE = JSON.stringify(
  {
    resourceType: 'Bundle',
    type: 'collection',
    entry: [
      {
        resource: {
          resourceType: 'Patient',
          id: 'demo-1',
          identifier: [{ value: 'DEMO-001' }],
          name: [{ family: 'Reed', given: ['Jane'] }],
          gender: 'female',
          birthDate: '1972-08-04',
        },
      },
      {
        resource: {
          resourceType: 'Condition',
          id: 'c1',
          code: {
            coding: [{
              system: 'http://snomed.info/sct',
              code: '254837009',
              display: 'Invasive ductal breast carcinoma',
            }],
          },
          recordedDate: '2024-11-01',
        },
      },
      {
        resource: {
          resourceType: 'MedicationStatement',
          id: 'm1',
          status: 'active',
          medicationCodeableConcept: {
            coding: [{
              system: 'http://www.nlm.nih.gov/research/umls/rxnorm',
              code: '40048',
              display: 'Cisplatin',
            }],
          },
          effectivePeriod: { start: '2024-12-15' },
        },
      },
      {
        resource: {
          resourceType: 'Observation',
          id: 'o1',
          status: 'final',
          category: [{ coding: [{ code: 'laboratory' }] }],
          code: {
            coding: [{
              system: 'http://loinc.org',
              code: '4548-4',
              display: 'HbA1c',
            }],
          },
          effectiveDateTime: '2025-03-12T08:30:00Z',
          valueQuantity: { value: 5.7, unit: '%' },
        },
      },
    ],
  },
  null,
  2,
);
