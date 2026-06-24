import { Navigate, useLocation } from 'react-router-dom';
import { motion } from 'framer-motion';

import { useAuth } from './AuthContext.jsx';

/**
 * Wraps a route that requires authentication.  Hands the unauthenticated
 * user a redirect to /login that remembers where they were going so the
 * login flow can drop them back at the right place.
 *
 * Includes a brief shimmer-only "checking session" splash for the
 * bootstrap-from-localStorage path, so the UI never flashes the login
 * screen on a page refresh of an authenticated user.
 */
export default function ProtectedRoute({ children }) {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <motion.div
        className="flex h-screen items-center justify-center bg-mesh-light"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
      >
        <div className="flex items-center gap-3 rounded-2xl bg-white/70 px-6 py-4 shadow-soft backdrop-blur-xl">
          <span className="h-2.5 w-2.5 animate-breathing rounded-full bg-brand-500" />
          <span className="text-sm font-medium text-ink-600">
            Checking your session…
          </span>
        </div>
      </motion.div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return children;
}
