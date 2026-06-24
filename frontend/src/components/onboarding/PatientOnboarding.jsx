import { useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import {
  ArrowLeft, ArrowRight, Check, Heart, HeartPulse,
  Languages, ScrollText, Sparkles, UserRound,
} from 'lucide-react';

import { cn } from '@/lib/cn.js';
import Stepper from '@/components/ui/Stepper.jsx';

/**
 * Multi-step patient demographic + condition collection.
 *
 * Reused by:
 *   - The /login register sub-flow (calls onComplete with the full
 *     payload so the parent can hit /auth/register).
 *   - A future "complete your profile" prompt for patient-role users
 *     who registered without demographics.
 *
 * Steps:
 *   1. Identity        – DOB + sex
 *   2. Background      – race + ethnicity + preferred language
 *   3. Conditions      – primary diagnosis (free text + optional SNOMED)
 *   4. Review          – summary + submit
 */
const STEPS = [
  { id: 'identity',   label: 'Identity',    icon: UserRound  },
  { id: 'background', label: 'Background',  icon: Languages  },
  { id: 'conditions', label: 'Conditions',  icon: ScrollText },
  { id: 'review',     label: 'Review',      icon: Sparkles   },
];

const SEXES = [
  { value: 'female', label: 'Female' },
  { value: 'male',   label: 'Male'   },
  { value: 'other',  label: 'Other'  },
  { value: 'unknown',label: 'Prefer not to say' },
];

const COMMON_RACES = [
  'White',
  'Black or African American',
  'Asian',
  'American Indian or Alaska Native',
  'Native Hawaiian or Other Pacific Islander',
  'Two or More Races',
  'Other',
];

const ETHNICITIES = [
  'Hispanic or Latino',
  'Not Hispanic or Latino',
];

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

// Light scaffolding of common conditions — chips users can tap instead
// of typing.  Stays small + deliberate; free-text input still wins.
const SUGGESTED_CONDITIONS = [
  { name: 'Type 2 diabetes',            code: '44054006'  },
  { name: 'Hypertension',               code: '38341003'  },
  { name: 'Asthma',                     code: '195967001' },
  { name: 'Breast cancer',              code: '254837009' },
  { name: 'Lung cancer',                code: '254637007' },
  { name: 'Major depressive disorder',  code: '370143000' },
  { name: 'Rheumatoid arthritis',       code: '69896004'  },
];

export default function PatientOnboarding({
  busy = false,
  initial = {},
  onSubmit,
  onCancel,
  title = 'Tell us about yourself',
  description = 'A few quick details so the AI can match you to the right trials.  Nothing here is shared without your consent.',
}) {
  const [step, setStep]     = useState(0);
  const [dob, setDob]       = useState(initial.date_of_birth || '');
  const [sex, setSex]       = useState(initial.sex || '');
  const [race, setRace]     = useState(initial.race || '');
  const [ethnicity, setEth] = useState(initial.ethnicity || '');
  const [lang, setLang]     = useState(initial.preferred_language || 'en');
  const [conditions, setConditions] = useState(initial.conditions || []);
  const [newCond, setNewCond] = useState('');

  const canAdvance = (() => {
    if (step === 0) return !!dob && !!sex;
    if (step === 1) return true;
    if (step === 2) return conditions.length >= 1;
    return true;
  })();

  function addCondition(name, code = null) {
    const trimmed = (name || '').trim();
    if (!trimmed) return;
    if (conditions.some((c) => c.display_name.toLowerCase() === trimmed.toLowerCase())) return;
    setConditions((cur) => [...cur, { display_name: trimmed, code, code_system: code ? 'SNOMED-CT' : null }]);
    setNewCond('');
  }

  function removeCondition(name) {
    setConditions((cur) => cur.filter((c) => c.display_name !== name));
  }

  function submit() {
    onSubmit({
      date_of_birth: dob,
      sex,
      race: race || null,
      ethnicity: ethnicity || null,
      preferred_language: lang,
      conditions,
    });
  }

  return (
    <div className="card-glass w-full max-w-2xl overflow-hidden p-0">
      {/* Top header + stepper */}
      <div className="border-b border-white/40 bg-white/40 px-6 py-5">
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-accent-500 shadow-glow">
            <HeartPulse className="h-5 w-5 text-white" />
          </span>
          <div>
            <div className="font-display text-lg font-bold text-ink-900">{title}</div>
            <div className="text-xs text-ink-500">{description}</div>
          </div>
        </div>
        <Stepper steps={STEPS} current={step} className="mt-4" />
      </div>

      {/* Body */}
      <div className="min-h-[260px] px-6 py-6">
        <AnimatePresence mode="wait">
          <motion.div
            key={step}
            initial={{ opacity: 0, x: 16 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -16 }}
            transition={{ duration: 0.2 }}
          >
            {step === 0 && (
              <IdentityStep
                dob={dob} setDob={setDob}
                sex={sex} setSex={setSex}
              />
            )}
            {step === 1 && (
              <BackgroundStep
                race={race} setRace={setRace}
                ethnicity={ethnicity} setEth={setEth}
                lang={lang} setLang={setLang}
              />
            )}
            {step === 2 && (
              <ConditionsStep
                conditions={conditions}
                newCond={newCond}
                setNewCond={setNewCond}
                onAdd={addCondition}
                onRemove={removeCondition}
              />
            )}
            {step === 3 && (
              <ReviewStep
                dob={dob} sex={sex} race={race}
                ethnicity={ethnicity} lang={lang}
                conditions={conditions}
              />
            )}
          </motion.div>
        </AnimatePresence>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between border-t border-white/40 bg-white/40 px-6 py-4">
        <div>
          {step > 0 ? (
            <button onClick={() => setStep((s) => s - 1)} className="btn-ghost">
              <ArrowLeft className="h-4 w-4" /> Back
            </button>
          ) : (onCancel ? (
            <button onClick={onCancel} className="btn-ghost">Cancel</button>
          ) : <span />)}
        </div>
        <div>
          {step < STEPS.length - 1 ? (
            <button
              disabled={!canAdvance}
              onClick={() => setStep((s) => s + 1)}
              className="btn-primary"
            >
              Continue <ArrowRight className="h-4 w-4" />
            </button>
          ) : (
            <button
              disabled={busy || !canAdvance}
              onClick={submit}
              className="btn-primary"
            >
              {busy ? 'Creating profile…' : 'Create profile'} <Check className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────── Stepper ─────────────────────────────── */

/* ───────────────────────── Step bodies ─────────────────────────── */

function IdentityStep({ dob, setDob, sex, setSex }) {
  return (
    <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
      <Field label="Date of birth">
        <input
          className="input"
          type="date"
          required
          value={dob}
          onChange={(e) => setDob(e.target.value)}
          max={new Date().toISOString().slice(0, 10)}
        />
        <Hint>Used for age-based eligibility checks.  Stays inside the system.</Hint>
      </Field>
      <Field label="Sex assigned at birth">
        <div className="grid grid-cols-2 gap-2">
          {SEXES.map((s) => (
            <button
              key={s.value}
              type="button"
              onClick={() => setSex(s.value)}
              className={cn(
                'rounded-xl border-2 px-3 py-2.5 text-sm font-semibold transition-all',
                sex === s.value
                  ? 'border-brand-500 bg-brand-50 text-brand-700 shadow-sm'
                  : 'border-ink-200 bg-white text-ink-700 hover:border-brand-300',
              )}
            >
              {s.label}
            </button>
          ))}
        </div>
      </Field>
    </div>
  );
}

function BackgroundStep({ race, setRace, ethnicity, setEth, lang, setLang }) {
  return (
    <div className="flex flex-col gap-5">
      <Field label="Race">
        <div className="flex flex-wrap gap-2">
          {COMMON_RACES.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => setRace(r === race ? '' : r)}
              className={cn(
                'chip cursor-pointer transition-colors',
                race === r ? 'chip-brand' : 'chip-ink hover:bg-ink-200',
              )}
            >
              {r}
            </button>
          ))}
        </div>
        <Hint>Optional. Used only for diversity-aware ranking — never for matching itself.</Hint>
      </Field>
      <Field label="Ethnicity">
        <div className="flex flex-wrap gap-2">
          {ETHNICITIES.map((e) => (
            <button
              key={e}
              type="button"
              onClick={() => setEth(e === ethnicity ? '' : e)}
              className={cn(
                'chip cursor-pointer transition-colors',
                ethnicity === e ? 'chip-brand' : 'chip-ink hover:bg-ink-200',
              )}
            >
              {e}
            </button>
          ))}
        </div>
      </Field>
      <Field label="Preferred language">
        <div className="flex flex-wrap gap-1.5 rounded-xl border border-ink-100 bg-white/70 p-1">
          {LANGS.map((l) => (
            <button
              key={l.code}
              type="button"
              onClick={() => setLang(l.code)}
              className={cn(
                'rounded-lg px-3 py-1.5 text-sm font-semibold transition-colors',
                lang === l.code ? 'bg-brand-500 text-white shadow-sm' : 'text-ink-600 hover:bg-ink-100',
              )}
            >
              {l.label}
            </button>
          ))}
        </div>
        <Hint>Trial summaries auto-translate to your preferred language.</Hint>
      </Field>
    </div>
  );
}

function ConditionsStep({ conditions, newCond, setNewCond, onAdd, onRemove }) {
  return (
    <div className="flex flex-col gap-4">
      <Field label="Add a diagnosis or condition">
        <div className="flex gap-2">
          <input
            className="input flex-1"
            placeholder="e.g. Type 2 diabetes, breast cancer, hypertension…"
            value={newCond}
            onChange={(e) => setNewCond(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                onAdd(newCond);
              }
            }}
          />
          <button
            type="button"
            onClick={() => onAdd(newCond)}
            disabled={!newCond.trim()}
            className="btn-primary"
          >
            Add
          </button>
        </div>
        <Hint>Press Enter or click Add.  These become diagnosis events on your medical timeline.</Hint>
      </Field>

      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-500">
          Or pick from common conditions
        </div>
        <div className="flex flex-wrap gap-1.5">
          {SUGGESTED_CONDITIONS.map((s) => {
            const active = conditions.some((c) => c.display_name === s.name);
            return (
              <button
                key={s.name}
                type="button"
                onClick={() => active ? onRemove(s.name) : onAdd(s.name, s.code)}
                className={cn(
                  'chip cursor-pointer',
                  active ? 'chip-brand' : 'chip-ink hover:bg-ink-200',
                )}
              >
                {active && <Check className="h-3 w-3" />}
                {s.name}
              </button>
            );
          })}
        </div>
      </div>

      {conditions.length > 0 && (
        <div>
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-500">
            On your record ({conditions.length})
          </div>
          <ul className="flex flex-wrap gap-1.5">
            {conditions.map((c) => (
              <motion.li
                key={c.display_name}
                initial={{ scale: 0.9, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                className="inline-flex items-center gap-2 rounded-full bg-brand-100 px-3 py-1 text-xs font-semibold text-brand-700"
              >
                <Heart className="h-3 w-3" />
                {c.display_name}
                <button
                  type="button"
                  onClick={() => onRemove(c.display_name)}
                  className="ml-1 rounded-full px-1 text-brand-600 hover:bg-brand-200"
                  aria-label={`Remove ${c.display_name}`}
                >
                  ×
                </button>
              </motion.li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ReviewStep({ dob, sex, race, ethnicity, lang, conditions }) {
  return (
    <div>
      <div className="mb-4 rounded-xl bg-brand-50 p-4 text-sm text-brand-700">
        <span className="font-semibold">Almost done.</span> Review the
        information below.  You can update everything later from your
        dashboard.
      </div>
      <dl className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <ReviewRow label="Date of birth" value={dob} />
        <ReviewRow label="Sex"           value={sex} />
        <ReviewRow label="Race"          value={race || '—'} />
        <ReviewRow label="Ethnicity"     value={ethnicity || '—'} />
        <ReviewRow label="Language"      value={lang.toUpperCase()} />
        <ReviewRow
          label="Conditions"
          value={conditions.length > 0 ? `${conditions.length} listed` : 'none'}
        />
      </dl>
      {conditions.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-1.5">
          {conditions.map((c) => (
            <span key={c.display_name} className="chip chip-brand">
              {c.display_name}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function ReviewRow({ label, value }) {
  return (
    <div className="rounded-xl border border-ink-100 bg-white px-4 py-3">
      <dt className="text-[10px] font-semibold uppercase tracking-wider text-ink-400">{label}</dt>
      <dd className="mt-0.5 text-sm font-semibold text-ink-800">{value}</dd>
    </div>
  );
}

/* ───────────────────────── Bits ────────────────────────────────── */

function Field({ label, children }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-ink-500">
        {label}
      </span>
      {children}
    </label>
  );
}

function Hint({ children }) {
  return <p className="mt-1.5 text-xs text-ink-400">{children}</p>;
}
