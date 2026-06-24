import { Navigate, Route, Routes } from 'react-router-dom';

import AppLayout from '@/components/layout/AppLayout.jsx';
import ProtectedRoute from '@/auth/ProtectedRoute.jsx';
import RoleGate from '@/auth/RoleGate.jsx';

import Login from '@/pages/Login.jsx';
import Dashboard from '@/pages/Dashboard.jsx';
import Patients from '@/pages/Patients.jsx';
import PatientDetail from '@/pages/PatientDetail.jsx';
import Trials from '@/pages/Trials.jsx';
import TrialDetail from '@/pages/TrialDetail.jsx';
import Matching from '@/pages/Matching.jsx';
import MatchDetail from '@/pages/MatchDetail.jsx';
import Notifications from '@/pages/Notifications.jsx';
import AuditPage from '@/pages/AuditPage.jsx';
import FindMatches from '@/pages/FindMatches.jsx';
import NotFound from '@/pages/NotFound.jsx';

/**
 * Top-level route tree.
 *
 * Public routes (login) sit outside the layout; everything else is
 * wrapped by <ProtectedRoute/> + the <AppLayout/> chrome.
 */
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      <Route
        element={
          <ProtectedRoute>
            <AppLayout />
          </ProtectedRoute>
        }
      >
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard"           element={<Dashboard />} />

        {/* Cohort-wide patient surfaces are coordinator+ only.  The
            backend enforces the same boundary via ensure_can_access_patient
            — the gate just gives URL-pasters a friendly 403. */}
        <Route path="/patients" element={
          <RoleGate allow={['coordinator', 'clinician', 'admin']}>
            <Patients />
          </RoleGate>
        } />
        <Route path="/patients/:patientId" element={
          <RoleGate allow={['coordinator', 'clinician', 'admin']}>
            <PatientDetail />
          </RoleGate>
        } />

        <Route path="/trials"              element={<Trials />} />
        <Route path="/trials/:trialId"     element={<TrialDetail />} />

        {/* The matching console is a coordinator/clinician tool; the
            patient role reads their own matches from the dashboard. */}
        <Route path="/matching" element={
          <RoleGate allow={['coordinator', 'clinician', 'admin']}>
            <Matching />
          </RoleGate>
        } />
        <Route path="/matching/:matchId"   element={<MatchDetail />} />

        <Route path="/notifications"       element={<Notifications />} />

        {/* Patient-driven matching intake — patient-role only.
            Coordinators/clinicians/admins use /matching for the same job. */}
        <Route path="/find-matches" element={
          <RoleGate allow={['patient']}>
            <FindMatches />
          </RoleGate>
        } />

        <Route path="/audit" element={
          <RoleGate allow={['admin']}>
            <AuditPage />
          </RoleGate>
        } />
      </Route>

      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
