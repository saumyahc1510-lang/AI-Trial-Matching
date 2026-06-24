import { Navigate } from 'react-router-dom';

import { useAuth } from './AuthContext.jsx';
import EmptyState from '@/components/ui/EmptyState.jsx';
import { Shield } from 'lucide-react';

/**
 * Route wrapper that allows only the named roles.
 *
 * Drops a friendly 403 surface instead of relying on the API's bare
 * 403 — bypassing the page entirely would also work, but a visible
 * explanation makes it obvious *why* a patient can't reach
 * ``/patients`` if they manually paste the URL.
 *
 * Usage:
 *
 *     <Route element={<RoleGate allow={['coordinator','clinician','admin']} />}>
 *       <Route path="/patients" element={<Patients />} />
 *     </Route>
 *
 * The component renders its ``children`` directly when ``allow``
 * includes the user's role; otherwise it shows the 403 card.
 */
export default function RoleGate({ allow, children }) {
  const { user } = useAuth();

  if (!user) return <Navigate to="/login" replace />;
  if (allow.includes(user.role)) return children;

  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <EmptyState
        icon={Shield}
        title="You don't have access to this page."
        description={
          `Your account role (${user.role}) doesn't include permission for ` +
          `this view. If you think that's a mistake, ask your administrator.`
        }
      />
    </div>
  );
}
