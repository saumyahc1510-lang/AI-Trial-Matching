/**
 * Auth context — the only place that *holds* the current user + token.
 *
 * Pages consume via `useAuth()` which exposes `{ user, login, logout,
 * isLoading, isAuthenticated }`.  The provider:
 *
 *   1. Bootstraps from localStorage on mount and validates the token
 *      via `GET /auth/me`.  An invalid / expired token is cleared
 *      silently so the user just sees the login screen.
 *   2. Installs a 401 handler on the axios interceptor so any rejected
 *      request anywhere boots the user back to /login without each
 *      page having to handle that case.
 */
import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from 'react';
import { useNavigate } from 'react-router-dom';

import {
  getStoredToken, setStoredToken, setUnauthorizedHandler,
} from '@/api/client.js';
import { login as loginApi, me as fetchMe } from '@/api/endpoints.js';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser]       = useState(null);
  const [isLoading, setIsLoading] = useState(true);  // bootstrap-in-flight
  const navigate = useNavigate();

  /* ── Global 401 handler ─────────────────────────────────────────── */
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null);
      navigate('/login', { replace: true });
    });
  }, [navigate]);

  /* ── Bootstrap from localStorage ────────────────────────────────── */
  useEffect(() => {
    const token = getStoredToken();
    if (!token) {
      setIsLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const profile = await fetchMe();
        if (!cancelled) setUser(profile);
      } catch {
        // Token invalid / expired — the response interceptor will have
        // cleared it already; just leave us logged out.
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  /* ── Public API ─────────────────────────────────────────────────── */
  const login = useCallback(async ({ email, password }) => {
    const { access_token } = await loginApi({ email, password });
    setStoredToken(access_token);
    // Re-fetch the profile so the navbar etc. shows the right name + role.
    const profile = await fetchMe();
    setUser(profile);
    return profile;
  }, []);

  const logout = useCallback(() => {
    setStoredToken(null);
    setUser(null);
    navigate('/login', { replace: true });
  }, [navigate]);

  const value = useMemo(() => ({
    user,
    isLoading,
    isAuthenticated: !!user,
    login,
    logout,
  }), [user, isLoading, login, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth() must be used inside an <AuthProvider>.');
  }
  return ctx;
}
