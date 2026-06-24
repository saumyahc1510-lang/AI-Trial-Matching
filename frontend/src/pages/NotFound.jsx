import { Link } from 'react-router-dom';
import { motion } from 'framer-motion';
import { Compass, Home } from 'lucide-react';

export default function NotFound() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-mesh-light p-6">
      <motion.div
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
        className="card-glass max-w-md p-10 text-center"
      >
        <span className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-accent-500 text-white shadow-glow">
          <Compass className="h-7 w-7" />
        </span>
        <div className="font-display text-2xl font-bold text-ink-900">
          Off the trail
        </div>
        <p className="mt-2 text-sm text-ink-500">
          We couldn’t find the page you were looking for.  No critical
          criterion was harmed in the making of this 404.
        </p>
        <Link to="/dashboard" className="btn-primary mt-6">
          <Home className="h-4 w-4" /> Back to dashboard
        </Link>
      </motion.div>
    </div>
  );
}
