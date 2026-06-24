# AI Clinical Trial Matching — Backend

A full-stack backend that performs **deep reasoning** about patient
eligibility for clinical trials.  Reconstructs patient medical
timelines, evaluates time-dependent criteria, produces three-state
matching (eligible / ineligible / uncertain), and provides per-
criterion explainability — powered by LLM reasoning + structured
clinical NER.

## Architecture at a glance

```
ClinicalTrials.gov ─┐
                    ├─► trial_sync ─► criteria_parser (LLM) ─► TrialCriterion[]
HAPI FHIR / JSON ───┤
                    └─► ehr_parser ─► Patient + MedicalEvent[]
                                           │
                                           ▼
                                  temporal_engine
                                           │
                                           ▼
                              eligibility_reasoner (LLM)
                                           │
                                           ▼
                                  matching_engine ─► MatchResult
                                           │            + CriterionEvaluation
                                           │            + UncertaintyFlag
                              ┌────────────┼────────────┐
                              ▼            ▼            ▼
                       diversity     explainability  uncertainty
                        ranker         renderer       engine
                              ▼            ▼            ▼
                              └─►  FastAPI /api/v1  ◄───┘
                                           │
                                           ▼
                                     Audit + RBAC
```

Six layers:

| Layer | Modules | Responsibility |
|-------|---------|----------------|
| Foundation | `models/`, `schemas/`, `database.py`, `config.py` | ORM, validation, settings |
| Ingestion | `services/ehr_parser.py`, `services/trial_sync.py`, `services/criteria_parser.py`, `services/ner_engine.py` | Bring chart + trial data in |
| Intelligence | `services/temporal_engine.py`, `services/eligibility_reasoner.py` | Reason over time + criteria |
| Matching | `services/matching_engine.py`, `services/uncertainty_engine.py`, `services/explainability.py` | Score + explain |
| Differentiators | `services/diversity_ranker.py`, `services/plain_language.py`, `services/wearable_service.py`, `services/notification_service.py` | Equity, multilingual, IoT |
| Compliance + API | `middleware/`, `auth.py`, `api/` | HIPAA, JWT/RBAC, REST surface |

## Prerequisites

- **Python 3.11+** (tested on 3.13)
- **PostgreSQL 14+** with the `trial_matching` database created
- **Groq API key** (free tier — 30 RPM, 14,400 RPD; sign up at <https://console.groq.com>)
- **Redis** — only required when `USE_CELERY=true`.  Skip for dev.
- *(Optional)* `transformers` + `torch` for unstructured-note NER

## Quick start

```powershell
# 1. Clone + create venv
cd "C:\AI Trial Matching Final\backend"
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Install deps
pip install -r requirements.txt

# 3. Create the database
psql -U postgres -c "CREATE DATABASE trial_matching;"

# 4. Copy + edit env vars
copy .env.example .env
# → set DATABASE_URL password + GROQ_API_KEY

# 5. Apply migrations
alembic upgrade head

# 6. Run the API
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000/docs> for the interactive Swagger UI.

## Environment variables

Set in `backend/.env` — see `.env.example` for the full template.

| Variable | Required | Default | Notes |
|----------|:--------:|---------|-------|
| `DATABASE_URL` | ✅ | – | `postgresql://user:pass@host:port/dbname` |
| `GROQ_API_KEY` | ✅ | – | From <https://console.groq.com> |
| `LLM_MODEL` |   | `llama-3.3-70b-versatile` | Any Groq model id |
| `LLM_PROVIDER` |   | `groq` | `groq` / `openai` / `local` (stubs) |
| `JWT_SECRET_KEY` | ✅ | dev placeholder | **Rotate before production** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` |   | `480` | 8h default |
| `USE_CELERY` |   | `false` | `true` switches to Redis broker |
| `REDIS_URL` |   | `redis://localhost:6379/0` | Only used when `USE_CELERY=true` |
| `TRIAL_SYNC_CONDITIONS` |   | `cancer,diabetes,cardiovascular disease` | Comma-separated |
| `TRIAL_SYNC_INTERVAL_HOURS` |   | `6` | Beat schedule |
| `NER_MODEL_NAME` |   | `d4data/biomedical-ner-all` | Used only if `transformers` is installed |
| `DIVERSITY_RANK_ALPHA` |   | `0.85` | Weight of `match × confidence` in final rank |
| `DIVERSITY_RANK_BETA` |   | `0.15` | Weight of diversity boost |
| `ENABLE_PHI_DEIDENTIFICATION` |   | `true` | Strip PHI before LLM calls |
| `AUDIT_LOG_ENABLED` |   | `true` | One row per request |
| `SQL_ECHO` |   | `false` | Verbose ORM logging |
| `CORS_ORIGINS` |   | `http://localhost:3000,http://localhost:8000` | Comma-separated |

## Common operations

### Run the API server

```powershell
uvicorn app.main:app --reload --port 8000
```

### Run Celery workers

```powershell
# Eager / dev mode — no broker needed; ignore the worker.
# .env: USE_CELERY=false

# Async / prod mode — start Redis first, then:
celery -A app.workers.celery_app worker --loglevel=info
celery -A app.workers.celery_app beat   --loglevel=info
```

### Apply database migrations

```powershell
alembic upgrade head                # apply latest
alembic revision --autogenerate -m "describe change"
alembic downgrade -1                # roll back one
alembic history                     # show all
```

Current migration chain:

1. `daa879540492` — initial schema (14 tables)
2. `0f6f7433c1fd` — convert business-time columns to `timestamptz`
3. `141f665cac92` — add `clinical_trials.summary_cache` JSONB
4. `588f16c0d71d` — add `notifications` table
5. `6e86b2ebed78` — enforce `audit_logs` immutability via DB triggers

### Run the test suite

```powershell
python -m pytest -v                 # all tests
python -m pytest -k temporal        # subset
python -m pytest --cov=app          # with coverage
```

Tests run against your live `trial_matching` database but every test is
wrapped in a transaction that's rolled back on teardown — your data is
safe.  The LLM is faked via `FakeLLMClient` so tests never hit Groq.

### Trigger a trial sync manually

```powershell
# Either via the admin API…
curl -X POST http://localhost:8000/api/v1/admin/trial-sync \
     -H "Authorization: Bearer <admin-token>" \
     -H "Content-Type: application/json" \
     -d '{"conditions": ["breast cancer"], "max_trials_per_condition": 25}'

# …or directly from Python in eager mode:
python -c "from app.workers.trial_sync_worker import run_manual_sync_task; \
           print(run_manual_sync_task.delay(conditions=['breast cancer'], \
                                            max_trials_per_condition=5).get())"
```

## Project structure

```
backend/
├── alembic/                          # database migrations
│   ├── env.py
│   └── versions/
│       ├── 2026_05_26_2022-daa879540492_initial_schema.py
│       ├── 2026_05_27_1020-0f6f7433c1fd_convert_business_time_columns_to_.py
│       ├── 2026_05_27_1224-141f665cac92_add_trial_summary_cache_column.py
│       ├── 2026_05_27_1238-588f16c0d71d_add_notifications_table.py
│       └── 2026_05_27_1540-6e86b2ebed78_enforce_audit_logs_immutability_via_db_.py
├── app/
│   ├── api/                          # 9 sub-routers under /api/v1
│   │   ├── auth.py, patients.py, trials.py, matching.py
│   │   ├── feedback.py, wearables.py, notifications.py
│   │   ├── admin.py, audit.py, router.py
│   ├── middleware/                   # audit, HIPAA, CORS
│   ├── models/                       # 9 ORM modules, 15 tables
│   ├── schemas/                      # Pydantic request/response shapes
│   ├── services/                     # business logic (14 modules)
│   ├── workers/                      # Celery tasks (4 workers + app config)
│   ├── auth.py                       # JWT + RBAC dependencies
│   ├── config.py                     # pydantic-settings + env loader
│   ├── database.py                   # sync + async engines
│   └── main.py                       # FastAPI factory
├── tests/                            # pytest suite (44 tests)
├── alembic.ini
├── pytest.ini
├── requirements.txt
├── .env.example
└── README.md
```

## API surface — `/api/v1`

| Group | Endpoints | RBAC |
|-------|-----------|------|
| `/auth` | login, register, me, token/refresh | open / self |
| `/patients` | CRUD, FHIR import, timeline, versions | coordinator+ |
| `/trials` | list, get, create, criteria, sites, summary | any authed |
| `/matching` | trigger, results, explain (json+md), review | coordinator+ |
| `/feedback` | submit, stats | clinician+ |
| `/wearables` | devices, readings, aggregate, resolve | patient owner+ |
| `/notifications` | list, unread/count, read, read-all | self |
| `/admin` | users, trial-sync, config | admin only |
| `/audit` | query | admin only |

48 endpoints total — full list at `/docs`.

## HIPAA / compliance posture

| Control | How it's enforced |
|---------|-------------------|
| Authentication | bcrypt password hashing + JWT (HS256, 8h default) |
| Authorization | Role-based via FastAPI `Depends(require_role(...))` |
| PHI redaction | Outbound LLM prompts pass through `PHIScrubber` (names, MRN, SSN, phone, email + relative-date redaction) |
| Audit logging | One immutable row per request via `AuditMiddleware` |
| Audit immutability | Postgres triggers reject UPDATE / DELETE / TRUNCATE on `audit_logs` |
| Patient versioning | Every demographic mutation snapshots to `PatientVersion` |

> **Production deployments** must additionally: provision a BAA with the
> chosen LLM vendor (or self-host via Ollama), enable TLS, rotate
> `JWT_SECRET_KEY`, set up Redis for Celery, and review the `CORS_ORIGINS`
> list.

## Cost analysis

| Component | Cost |
|-----------|------|
| ClinicalTrials.gov API v2 | Free, no auth |
| Groq API (free tier) | 30 RPM / 14,400 RPD — sufficient for dev |
| PostgreSQL, Redis | Open source, self-hosted |
| Python deps | All MIT/Apache/BSD |

Total dev/prototype cost: **$0**.  Production cost scales with Groq
paid-tier rates (~$0.05 – $0.79 per 1M tokens) or self-hosted-LLM
compute.

## License

(insert your project license here)
