import { Navigate } from 'react-router-dom';

import { useAuth } from '@/auth/AuthContext.jsx';

import PatientDashboard from './dashboards/PatientDashboard.jsx';
import CoordinatorDashboard from './CoordinatorDashboard.jsx';
import ClinicianDashboard from './dashboards/ClinicianDashboard.jsx';
import SponsorDashboard from './dashboards/SponsorDashboard.jsx';
import AdminDashboard from './dashboards/AdminDashboard.jsx';

/**
 * Role-aware dashboard router.
 *
 * Each of the 5 roles defined in the backend gets its own surface —
 * the layout/sidebar stays identical so users learn the chrome once,
 * but the *content* of the home page is tuned to what they actually
 * need to do.
 *
 *   PATIENT     → matched trials feed, own timeline, wearable hub
 *   COORDINATOR → roster + uncertainty queue + rematch alerts
 *   CLINICIAN   → review queue + sign-off console
 *   SPONSOR     → anonymised aggregate analytics
 *   ADMIN       → user mgmt + audit log + sync + config
 */
export default function Dashboard() {
  const { user } = useAuth();

  switch (user?.role) {
    case 'patient':     return <PatientDashboard user={user} />;
    case 'coordinator': return <CoordinatorDashboard />;
    case 'clinician':   return <ClinicianDashboard />;
    case 'sponsor':     return <SponsorDashboard />;
    case 'admin':       return <AdminDashboard />;
    default:
      // Unknown / not-yet-loaded role — kick to login so the auth
      // context can re-bootstrap.
      return <Navigate to="/login" replace />;
  }
}
