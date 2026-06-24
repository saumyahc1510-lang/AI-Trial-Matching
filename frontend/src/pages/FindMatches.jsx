import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  ArrowLeft, ArrowRight, BadgeCheck, Brain, ChevronRight, FlaskConical,
  Heart, ListChecks, Sparkles, Wand2, AlertCircle, HelpCircle, Stethoscope,
} from 'lucide-react';
import toast from 'react-hot-toast';
import confetti from 'canvas-confetti';

import PageHeader from '@/components/ui/PageHeader.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import { SkeletonRows } from '@/components/ui/Skeleton.jsx';
import ProgressRing from '@/components/ui/ProgressRing.jsx';
import Stepper from '@/components/ui/Stepper.jsx';
import Tally from '@/components/ui/Tally.jsx';
import {
  intakeAnswers, intakeFinalize, intakeQuestions, intakeStart,
} from '@/api/endpoints.js';
import { cn } from '@/lib/cn.js';

/**
 * Patient-driven matching intake.
 *
 * Walks the patient through a 4-step flow:
 *   1. Intro      – show conditions + candidate trial pool the system
 *                   thinks they could qualify for.
 *   2. Questions  – an LLM-generated short list of clarifying questions.
 *   3. Review     – show the answers + ask the patient to confirm.
 *   4. Results    – run the matching engine, show eligible / uncertain
 *                   counts, link out to each match's explainability.
 *
 * Each step renders with the same animated stepper + card chrome so
 * the patient never feels like they've moved to a different feature.
 */
const STEPS = [
  { id: 'intro',     label: 'Get started',  icon: Sparkles    },
  { id: 'questions', label: 'A few details', icon: ListChecks },
  { id: 'review',    label: 'Review',       icon: BadgeCheck  },
  { id: 'results',   label: 'Your matches', icon: Wand2       },
];

// Generic mutation-error toast — every intake step uses the same
// shape, no point duplicating the wrapper in each ``useMutation``.
function toastDetailOr(fallback) {
  return (err) => toast.error(err?.response?.data?.detail || fallback);
}

export default function FindMatches() {
  const qc = useQueryClient();

  // ── Server data ────────────────────────────────────────────────
  const startQ = useQuery({
    queryKey: ['intake', 'start'],
    queryFn: intakeStart,
    refetchOnWindowFocus: false,
  });

  // The flow has three remote stages; only the *initial* generation of
  // questions is independent.  Once the user answers + submits, the
  // record-answers + finalize calls are always paired, so they belong
  // in a single mutation rather than three useMutation instances we
  // stitch together with await.
  const questionsM = useMutation({
    mutationFn: intakeQuestions,
    onError: toastDetailOr('Could not generate questions right now. Please try again.'),
  });

  const submitM = useMutation({
    mutationFn: async () => {
      const questions = questionsM.data?.questions || [];
      await intakeAnswers({ questions, answers });
      return intakeFinalize(questionsM.data?.candidate_trial_ids || []);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['patient'] });
      qc.invalidateQueries({ queryKey: ['notifications'] });
    },
    onError: toastDetailOr('Could not save your answers / run matching.'),
  });

  // ── Step + local state ─────────────────────────────────────────
  const [step, setStep]       = useState(0);
  const [answers, setAnswers] = useState({});  // {question_id: value}

  // Eligible-match celebration confetti.
  const eligibleCount = submitM.data?.eligible_count || 0;
  useEffect(() => {
    if (step !== 3 || eligibleCount === 0) return;
    confetti({ particleCount: 80, spread: 70, origin: { x: 0.2, y: 0.3 } });
    confetti({ particleCount: 80, spread: 70, origin: { x: 0.8, y: 0.3 } });
  }, [step, eligibleCount]);

  // ── Step transitions ───────────────────────────────────────────
  async function goToQuestions() {
    if (!questionsM.data) await questionsM.mutateAsync();
    setStep(1);
  }
  async function submitAndMatch() {
    await submitM.mutateAsync();
    setStep(3);
  }

  // ── Render ────────────────────────────────────────────────────
  if (startQ.isLoading) return <SkeletonRows rows={6} />;

  if (startQ.isError) {
    return (
      <EmptyState
        icon={AlertCircle}
        title="Couldn't start the matcher"
        description="Please check that your profile has at least one condition recorded."
        action={<Link to="/dashboard" className="btn-secondary">Back to dashboard</Link>}
      />
    );
  }

  return (
    <div>
      <Link to="/dashboard" className="btn-ghost mb-2 -ml-2">
        <ArrowLeft className="h-4 w-4" /> Back to dashboard
      </Link>

      <PageHeader
        eyebrow="Trial matcher"
        title="Find a trial that matches you"
        description="A short, guided conversation: a few questions, then a ranked list of trials you could join."
      />

      <Stepper steps={STEPS} current={step} size="md" />

      <div className="mt-6">
        <AnimatePresence mode="wait">
          <motion.div
            key={STEPS[step].id}
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
          >
            {step === 0 && (
              <IntroStep
                data={startQ.data}
                onContinue={goToQuestions}
                loading={questionsM.isPending}
              />
            )}
            {step === 1 && (
              <QuestionsStep
                questions={questionsM.data?.questions || []}
                answers={answers}
                onChange={(id, v) => setAnswers((cur) => ({ ...cur, [id]: v }))}
                onBack={() => setStep(0)}
                onContinue={() => setStep(2)}
                loading={questionsM.isPending}
              />
            )}
            {step === 2 && (
              <ReviewStep
                questions={questionsM.data?.questions || []}
                answers={answers}
                onBack={() => setStep(1)}
                onSubmit={submitAndMatch}
                loading={submitM.isPending}
              />
            )}
            {step === 3 && (
              <ResultsStep result={submitM.data} />
            )}
          </motion.div>
        </AnimatePresence>
      </div>
    </div>
  );
}

/* ───────────────────────── Step 1 · Intro ─────────────────────── */

function IntroStep({ data, onContinue, loading }) {
  const { known_conditions = [], candidates = [] } = data || {};
  return (
    <div className="card overflow-hidden">
      <div className="relative bg-gradient-to-br from-brand-500 via-brand-600 to-accent-500 p-6 text-white">
        <div
          aria-hidden
          className="pointer-events-none absolute -right-16 -top-16 h-56 w-56 rounded-full bg-white/10 blur-3xl"
        />
        <div className="relative flex items-start gap-4">
          <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-white/15 backdrop-blur">
            <Sparkles className="h-6 w-6" />
          </span>
          <div>
            <div className="text-xs font-semibold uppercase tracking-[0.18em] text-white/80">
              Hi there
            </div>
            <h2 className="mt-1 font-display text-2xl font-bold leading-tight">
              I’ll ask a few questions, then find the trials you could join.
            </h2>
            <p className="mt-2 max-w-2xl text-sm text-white/85">
              I see you’ve told us you have {prettyJoin(known_conditions)}.
              Based on that, here are {candidates.length} trials worth checking in detail.
              Each question I ask you helps unlock more of them.
            </p>
          </div>
        </div>
      </div>

      <div className="p-5">
        <div className="mb-3 text-xs font-semibold uppercase tracking-wider text-ink-400">
          Candidate trials
        </div>
        {candidates.length === 0 ? (
          <div className="rounded-xl border border-dashed border-ink-200 p-6 text-center text-sm text-ink-500">
            We didn’t find any trials in the catalog matching your conditions
            right now.  An admin can trigger a fresh sync to pull more.
          </div>
        ) : (
          <ul className="space-y-2">
            {candidates.slice(0, 5).map((c, i) => (
              <motion.li
                key={c.trial_id}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: Math.min(i, 5) * 0.04 }}
                className="flex items-center gap-3 rounded-xl border border-ink-100 bg-white p-3"
              >
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-brand-100 text-brand-600">
                  <FlaskConical className="h-4 w-4" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="line-clamp-1 text-sm font-semibold text-ink-800">
                    {c.title}
                  </div>
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-ink-500">
                    <span className="font-mono">{c.nct_id}</span>
                    {c.category && (
                      <>
                        <span>·</span>
                        <span>{c.category}</span>
                      </>
                    )}
                    {c.matched_conditions?.length > 0 && (
                      <>
                        <span>·</span>
                        <span className="inline-flex items-center gap-1 text-success-600">
                          <Heart className="h-3 w-3" />
                          matches&nbsp;{c.matched_conditions.join(', ')}
                        </span>
                      </>
                    )}
                  </div>
                </div>
                <span className="text-xs font-bold tabular-nums text-brand-600">
                  {Math.round(c.score * 100)}%
                </span>
              </motion.li>
            ))}
            {candidates.length > 5 && (
              <li className="px-1 text-[11px] text-ink-400">
                + {candidates.length - 5} more we’ll evaluate.
              </li>
            )}
          </ul>
        )}
      </div>

      <div className="flex items-center justify-between border-t border-ink-100 bg-ink-50/50 px-5 py-4">
        <div className="text-xs text-ink-500">
          We’ll never share your answers without your consent.
        </div>
        <button
          onClick={onContinue}
          disabled={loading || candidates.length === 0}
          className="btn-primary"
        >
          {loading ? (
            <>
              <Brain className="h-4 w-4 animate-breathing" />
              Thinking…
            </>
          ) : (
            <>
              Get started <ArrowRight className="h-4 w-4" />
            </>
          )}
        </button>
      </div>
    </div>
  );
}

function prettyJoin(items) {
  if (!items.length) return 'no conditions yet';
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return items.slice(0, -1).join(', ') + ', and ' + items[items.length - 1];
}

/* ───────────────────────── Step 2 · Questions ─────────────────── */

function QuestionsStep({ questions, answers, onChange, onBack, onContinue, loading }) {
  // Track index so the patient sees one question at a time — feels
  // less like a form, more like a conversation.
  const [idx, setIdx] = useState(0);
  const q = questions[idx];

  if (loading) {
    return (
      <div className="card p-8">
        <div className="flex flex-col items-center gap-3 text-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-100 text-brand-600">
            <Brain className="h-6 w-6 animate-breathing" />
          </span>
          <div className="font-display text-lg font-semibold text-ink-800">
            Picking the most useful questions…
          </div>
          <p className="max-w-md text-sm text-ink-500">
            I’m looking at every candidate trial’s eligibility criteria
            and choosing the questions that unlock the most of them.
          </p>
        </div>
      </div>
    );
  }
  if (!questions.length) {
    return (
      <EmptyState
        icon={HelpCircle}
        title="No questions to ask right now"
        description="The candidate trials don't have parsed criteria yet. An admin can trigger criteria parsing to enable this."
        action={<button onClick={onContinue} className="btn-primary">Continue anyway</button>}
      />
    );
  }
  if (!q) return null;

  const value = answers[q.id];
  const canAdvance = isAnsweredEnough(q, value);
  const isLast = idx === questions.length - 1;

  return (
    <div className="card overflow-hidden">
      {/* Progress bar */}
      <div className="h-1 bg-ink-100">
        <motion.div
          initial={{ width: 0 }}
          animate={{ width: `${((idx + 1) / questions.length) * 100}%` }}
          transition={{ duration: 0.35, ease: 'easeOut' }}
          className="h-full bg-gradient-to-r from-brand-400 to-accent-400"
        />
      </div>

      <div className="px-6 py-5">
        <div className="flex items-baseline justify-between">
          <div className="text-xs font-semibold uppercase tracking-wider text-ink-400">
            Question {idx + 1} of {questions.length}
          </div>
          {q.helps_evaluate?.length > 0 && (
            <div className="text-[11px] text-ink-400">
              helps evaluate {q.helps_evaluate.length} trial{q.helps_evaluate.length > 1 ? 's' : ''}
            </div>
          )}
        </div>
        <AnimatePresence mode="wait">
          <motion.h2
            key={q.id}
            initial={{ opacity: 0, x: 12 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -12 }}
            transition={{ duration: 0.22 }}
            className="mt-3 font-display text-2xl font-bold text-ink-900"
          >
            {q.question}
          </motion.h2>
        </AnimatePresence>
        {q.helper && (
          <p className="mt-1 text-sm text-ink-500">{q.helper}</p>
        )}

        <div className="mt-6">
          <AnswerInput q={q} value={value} onChange={(v) => onChange(q.id, v)} />
        </div>
      </div>

      <div className="flex items-center justify-between border-t border-ink-100 bg-ink-50/40 px-5 py-4">
        <button
          onClick={() => idx === 0 ? onBack() : setIdx((i) => i - 1)}
          className="btn-ghost"
        >
          <ArrowLeft className="h-4 w-4" /> {idx === 0 ? 'Back' : 'Previous'}
        </button>
        <div className="flex items-center gap-2">
          <button
            onClick={() => idx === questions.length - 1 ? null : setIdx((i) => i + 1)}
            disabled={!canAdvance && q.type !== 'text'}
            className="btn-ghost"
            title="Skip this question"
          >
            Skip
          </button>
          {isLast ? (
            <button onClick={onContinue} className="btn-primary">
              Review answers <ArrowRight className="h-4 w-4" />
            </button>
          ) : (
            <button onClick={() => setIdx((i) => i + 1)} className="btn-primary">
              Next <ArrowRight className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function isAnsweredEnough(q, value) {
  if (q.type === 'yes_no') return value === 'yes' || value === 'no';
  if (value == null) return false;
  return String(value).trim().length > 0;
}

function AnswerInput({ q, value, onChange }) {
  switch (q.type) {
    case 'yes_no':
      return (
        <div className="grid grid-cols-2 gap-3">
          {['yes', 'no'].map((opt) => (
            <button
              key={opt}
              type="button"
              onClick={() => onChange(opt)}
              className={cn(
                'rounded-xl border-2 px-4 py-3 text-base font-semibold capitalize transition-all',
                value === opt
                  ? 'border-brand-500 bg-brand-50 text-brand-700 shadow-sm'
                  : 'border-ink-200 bg-white text-ink-700 hover:border-brand-300',
              )}
            >
              {opt}
            </button>
          ))}
        </div>
      );
    case 'choice':
      return (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {(q.options || []).map((opt) => (
            <button
              key={opt}
              type="button"
              onClick={() => onChange(opt)}
              className={cn(
                'rounded-xl border-2 px-3 py-2.5 text-left text-sm font-semibold transition-all',
                value === opt
                  ? 'border-brand-500 bg-brand-50 text-brand-700 shadow-sm'
                  : 'border-ink-200 bg-white text-ink-700 hover:border-brand-300',
              )}
            >
              {opt}
            </button>
          ))}
        </div>
      );
    case 'number':
      return (
        <div className="flex items-baseline gap-3">
          <input
            type="number"
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value)}
            placeholder="—"
            className="input w-48 text-lg"
            step="any"
          />
          {q.unit && <span className="text-sm font-semibold text-ink-600">{q.unit}</span>}
        </div>
      );
    default:
      return (
        <textarea
          rows={3}
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Type your answer…"
          className="input resize-none"
        />
      );
  }
}

/* ───────────────────────── Step 3 · Review ────────────────────── */

function ReviewStep({ questions, answers, onBack, onSubmit, loading }) {
  const answeredQs = questions.filter((q) => isAnsweredEnough(q, answers[q.id]));
  return (
    <div className="card overflow-hidden">
      <div className="border-b border-ink-100 px-5 py-4">
        <div className="flex items-center gap-2 font-display text-lg font-semibold text-ink-900">
          <BadgeCheck className="h-5 w-5 text-success-500" /> Ready to match
        </div>
        <div className="text-xs text-ink-500">
          You answered {answeredQs.length} of {questions.length} questions.
          I’ll save these to your record and find your matches.
        </div>
      </div>
      <div className="space-y-3 p-5">
        {answeredQs.length === 0 ? (
          <div className="rounded-xl border border-dashed border-ink-200 p-6 text-center text-sm text-ink-500">
            No answers recorded — you can continue and we’ll match with what we have.
          </div>
        ) : (
          answeredQs.map((q) => (
            <ReviewRow key={q.id} q={q} value={answers[q.id]} />
          ))
        )}
      </div>
      <div className="flex items-center justify-between border-t border-ink-100 bg-ink-50/40 px-5 py-4">
        <button onClick={onBack} className="btn-ghost">
          <ArrowLeft className="h-4 w-4" /> Edit answers
        </button>
        <button onClick={onSubmit} disabled={loading} className="btn-primary">
          {loading ? (
            <>
              <Brain className="h-4 w-4 animate-breathing" />
              Matching trials…
            </>
          ) : (
            <>
              Find my matches <Wand2 className="h-4 w-4" />
            </>
          )}
        </button>
      </div>
    </div>
  );
}

function ReviewRow({ q, value }) {
  return (
    <div className="rounded-xl border border-ink-100 bg-white px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-xs uppercase tracking-wider text-ink-400">
            {q.event_template?.event_type || 'note'}
          </div>
          <div className="mt-0.5 text-sm font-medium text-ink-800">
            {q.question}
          </div>
        </div>
        <div className="shrink-0 rounded-lg bg-brand-50 px-3 py-1.5 text-sm font-semibold text-brand-700">
          {q.type === 'number' && q.unit ? `${value} ${q.unit}` : String(value)}
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────── Step 4 · Results ───────────────────── */

function ResultsStep({ result }) {
  const total      = result?.trials_matched   ?? 0;
  const eligible   = result?.eligible_count   ?? 0;
  const uncertain  = result?.uncertain_count  ?? 0;
  const ineligible = Math.max(0, total - eligible - uncertain);

  if (!result || total === 0) {
    return (
      <EmptyState
        icon={Stethoscope}
        title="No matches yet"
        description="We didn't find any trials matching your profile right now. New trials sync regularly — check back soon."
        action={<Link to="/trials" className="btn-secondary">Browse all trials</Link>}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="card relative overflow-hidden p-6">
        <div
          aria-hidden
          className="pointer-events-none absolute -inset-x-10 -top-10 h-44 bg-success-100/60 blur-3xl"
        />
        <div className="relative flex flex-wrap items-center gap-6">
          <ProgressRing
            value={total > 0 ? eligible / total : 0}
            tone="success"
            label="Eligible"
          />
          <div className="flex-1">
            <div className="font-display text-3xl font-bold text-ink-900">
              {eligible > 0
                ? `Great news — ${eligible} trial${eligible > 1 ? 's' : ''} you could join.`
                : 'We evaluated everything for you.'}
            </div>
            <p className="mt-1 text-sm text-ink-500">
              We checked {total} trial{total > 1 ? 's' : ''} against your updated chart.
            </p>
            <div className="mt-4 flex flex-wrap gap-3">
              <Tally label="Eligible"   value={eligible}   tone="success" />
              <Tally label="Uncertain"  value={uncertain}  tone="warn" />
              <Tally label="Not a fit"  value={ineligible} tone="ink" />
            </div>
          </div>
        </div>
      </div>

      <div className="card p-5">
        <div className="mb-3 text-xs font-semibold uppercase tracking-wider text-ink-400">
          What's next
        </div>
        <ul className="space-y-2 text-sm text-ink-700">
          <li className="flex items-start gap-2">
            <ChevronRight className="mt-0.5 h-4 w-4 text-brand-500" />
            Open the detail for any match to see exactly which criteria you meet — and which still need more information.
          </li>
          <li className="flex items-start gap-2">
            <ChevronRight className="mt-0.5 h-4 w-4 text-brand-500" />
            A coordinator can reach out about the eligible matches if you want to enroll.
          </li>
          <li className="flex items-start gap-2">
            <ChevronRight className="mt-0.5 h-4 w-4 text-brand-500" />
            Come back any time — new trials sync every few hours.
          </li>
        </ul>
        <div className="mt-4 flex flex-wrap gap-2">
          <Link to="/dashboard" className="btn-primary">
            See my dashboard <ArrowRight className="h-4 w-4" />
          </Link>
          <Link to="/trials" className="btn-secondary">
            Browse all trials
          </Link>
        </div>
      </div>
    </div>
  );
}

