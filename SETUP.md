# Setup guide

How to run this project on a fresh machine after cloning from GitHub.

There are two parts: a **Python/FastAPI backend** (port 8000) and a
**React/Vite frontend** (port 5173). The frontend proxies API calls to the
backend, so start the backend first.

---

## 0. Prerequisites — install these first

| Tool | Version | Notes |
|------|---------|-------|
| **Python** | 3.11 – 3.13 | needed for the backend |
| **Node.js** | 18 LTS or newer | needed for the frontend (includes `npm`) |
| **PostgreSQL** | 14 or newer | the database |
| **Git** | any | to clone the repo |

- Python: https://www.python.org/downloads/ (on Windows, tick **"Add Python to PATH"**)
- Node: https://nodejs.org/ (LTS)
- PostgreSQL: https://www.postgresql.org/download/ — during install, set a
  password for the **`postgres`** user and **remember it**. Keep the default
  port **5432**.

You also need a **free Groq API key** (the app uses Groq for its LLM calls):
sign up at https://console.groq.com → API Keys → create one. It looks like
`gsk_...`. Each person should use **their own** key.

---

## 1. Clone the repo

```bash
git clone <your-repo-url>
cd "AI Trial Matching Final"
```

---

## 2. Create the PostgreSQL database

Open a terminal and create an empty database called `trial_matching`.

**Using `psql`** (works everywhere):
```bash
psql -U postgres
# enter your postgres password, then at the prompt:
CREATE DATABASE trial_matching;
\q
```

> On Windows, if `psql` isn't found, open **"SQL Shell (psql)"** from the
> Start menu (installed with PostgreSQL) and run `CREATE DATABASE trial_matching;`
> there. Or use the **pgAdmin** GUI → right-click Databases → Create.

You don't need to create any tables by hand — step 4 does that.

---

## 3. Backend setup

```bash
cd backend

# create + activate a virtual environment
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

# install dependencies (this is a large download — torch/transformers are ~2 GB)
pip install -r requirements.txt
```

### Configure environment

Copy the example env file and edit it:
```bash
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux
```

Open `backend/.env` and set:
- **`DATABASE_URL`** — put your postgres password in. If your password has
  special characters (`@ : / ?`), URL-encode them (e.g. `@` → `%40`):
  ```
  DATABASE_URL=postgresql://postgres:YOURPASSWORD@localhost:5432/trial_matching
  ```
- **`GROQ_API_KEY`** — paste your own `gsk_...` key.
- **`JWT_SECRET_KEY`** — set to any long random string.

Leave `USE_CELERY=false` (no Redis needed for local dev).

### Create the database tables

From the `backend/` folder, with the venv active:
```bash
alembic upgrade head
```
This builds every table **and** the HIPAA audit-log immutability trigger and
foreign-key cascades. (Alembic reads `DATABASE_URL` from your `.env`.)

### Create the first admin user

Self-registration only creates *patient* accounts, so bootstrap one admin
directly. From `backend/` with the venv active:

```bash
python -c "from app.database import _get_session_factory; from app.auth import hash_password; from app.models.user import User, UserRole; db=_get_session_factory()(); db.add(User(email='admin@example.com', hashed_password=hash_password('ChangeMe123!'), full_name='Admin', role=UserRole.ADMIN.value, is_active=True)); db.commit(); print('admin created')"
```

Change the email/password before running. This is the login you'll use for
the admin dashboard.

### Run the backend

```bash
uvicorn app.main:app --reload --port 8000
```
Leave this terminal running. API docs are at http://localhost:8000/docs.

---

## 4. Frontend setup

Open a **second terminal**:

```bash
cd frontend
npm install
npm run dev
```

Then open **http://localhost:5173** in the browser and log in with the admin
account you created. (The frontend proxies `/api` calls to the backend on
:8000 automatically — no extra config.)

---

## 5. Load some trial data

A fresh database has no clinical trials yet. Log in as admin →
**Operations console** → **"Sync configured"**. This pulls trials from
ClinicalTrials.gov and parses eligibility criteria for a few categories
(controlled by `TRIAL_PARSE_CATEGORIES` / `TRIAL_PARSE_MAX_PER_SYNC`, editable
live via **Edit parsing** on the System config card).

> **Heads-up on the Groq free tier:** there's a daily token limit (~100k/day).
> Criteria parsing and patient matching both use it. The per-sync parse cap
> keeps a single sync within budget — run sync a few times across the day to
> parse more, rather than all at once.

---

## Daily run (after first-time setup)

Two terminals:
```bash
# terminal 1
cd backend && .venv\Scripts\Activate.ps1 && uvicorn app.main:app --reload --port 8000
# terminal 2
cd frontend && npm run dev
```

---

## Troubleshooting

| Symptom | Fix |
|--------|-----|
| `alembic` can't connect / auth fails | Check the password and that it's URL-encoded in `DATABASE_URL`; confirm Postgres is running on 5432. |
| `database "trial_matching" does not exist` | Re-do step 2. |
| Backend starts but frontend shows network errors | Make sure the backend is running on :8000 **before** using the UI. |
| Login fails | Re-run the admin-bootstrap command (step 3) and use that exact email/password. |
| `pip install` fails on torch | Use Python 3.11/3.12; make sure you're in a 64-bit Python. |
| Sync / matching returns rate-limit errors | Groq daily token quota is used up — wait for the daily reset or lower the parse cap. |
