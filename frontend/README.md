# Trialight — Frontend

React + Vite single-page app that drives the AI Clinical Trial
Matching backend.  Light-themed, animated, opinionated about
clarity.

## Stack

| Concern | Library |
|---|---|
| Build / dev server  | Vite 5             |
| UI framework        | React 18           |
| Routing             | React Router v6    |
| Styling             | Tailwind CSS 3     |
| Animations          | Framer Motion 11   |
| Server state        | TanStack Query 5   |
| HTTP                | Axios              |
| Toasts              | react-hot-toast    |
| Icons               | lucide-react       |
| Date formatting     | date-fns           |
| Confetti            | canvas-confetti    |
| Charts (future)     | Recharts           |

## Quick start

```powershell
cd "C:\AI Trial Matching Final\frontend"
npm install
npm run dev
```

The dev server listens on <http://localhost:5173> and proxies
`/api/*` to the FastAPI backend on `http://localhost:8000` — start
the backend in a separate terminal:

```powershell
cd ..\backend
uvicorn app.main:app --reload --port 8000
```

Then open <http://localhost:5173>, log in (or self-register), and
explore.

## Light-theme palette

Tailwind extends are defined in `tailwind.config.js`.  Key tokens:

| Token | Hex | Used for |
|---|---|---|
| `brand-500`  | `#5867e6` | primary actions, accent links |
| `accent-500` | `#ff7a55` | secondary highlights, unread badges |
| `ink-500`    | `#5d667a` | body text |
| `success-500`| `#0ea271` | "eligible" / "met" verdicts |
| `warn-500`   | `#d97706` | "uncertain" verdicts |
| `danger-500` | `#dc2626` | "ineligible" / "not met" verdicts |
| `bg-mesh-light` | radial gradient | page chrome background |

## Animation budget

The app leans on a small set of repeated motion idioms — keep new
flourishes in this vocabulary so the feel stays consistent:

* **Page transitions** — 280ms `y: 14 → 0` fade via `<AnimatePresence>`
  in `AppLayout.jsx`.
* **Active sidebar item** — Framer Motion `layoutId="active-tab"`
  pill that springs between tabs.
* **Progress rings** — animate from 0 → value on mount; the inner
  number counts up in sync (`ProgressRing.jsx`).
* **Card hover** — `whileHover={{ y: -2 or -3 }}` + soft glow shadow.
* **Status pill** — scale-in spring on mount.
* **Login particles** — a 44-dot drifting canvas behind the hero
  copy; pauses on tab blur.
* **Confetti** — fired exactly once on a freshly-loaded eligible
  match (see `MatchDetail.jsx`).

## Project structure

```
frontend/
├── public/                 # static assets (favicon)
├── src/
│   ├── api/                # axios instance + endpoint wrappers
│   ├── auth/               # AuthContext + ProtectedRoute
│   ├── components/
│   │   ├── layout/         # AppLayout (sidebar + topbar)
│   │   └── ui/             # StatusPill, ProgressRing, Skeleton, …
│   ├── lib/                # cn() class-name helper
│   ├── pages/              # one module per route
│   │   ├── Login.jsx
│   │   ├── Dashboard.jsx
│   │   ├── Patients.jsx           PatientDetail.jsx
│   │   ├── Trials.jsx             TrialDetail.jsx
│   │   ├── Matching.jsx           MatchDetail.jsx
│   │   ├── Notifications.jsx
│   │   └── NotFound.jsx
│   ├── App.jsx             # route tree
│   ├── main.jsx            # bootstrap (Query, Auth, Router)
│   └── index.css           # Tailwind layers + custom components
├── index.html
├── vite.config.js
├── tailwind.config.js
├── postcss.config.js
└── package.json
```

## Common scripts

```powershell
npm run dev      # dev server with HMR
npm run build    # production bundle in dist/
npm run preview  # serve the production bundle locally
```

## API contract

Every endpoint wrapper lives in `src/api/endpoints.js`.  When the
backend adds a route, add a thin async function here and consume it
via `useQuery` / `useMutation` in the page module — components never
import `axios` directly.

The bearer token attached on every request is stored in
`localStorage` under `trialight.token`.  A response interceptor
clears the token and redirects to `/login` on any 401, so individual
pages never need to handle "session died" themselves.
