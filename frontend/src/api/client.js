/**
 * Axios instance with JWT auto-attach + 401 auto-redirect.
 *
 * Components shouldn't import axios directly — go through `api` so:
 *   - the baseURL is consistent (`/api/v1`, proxied to the FastAPI app
 *     in dev via vite.config.js);
 *   - every request carries the bearer token when one is present;
 *   - a 401 anywhere boots the user to /login without each call having
 *     to handle that case.
 */
import axios from 'axios';

const TOKEN_KEY = 'trialight.token';

export function getStoredToken() {
  return localStorage.getItem(TOKEN_KEY) || null;
}

export function setStoredToken(token) {
  if (!token) {
    localStorage.removeItem(TOKEN_KEY);
  } else {
    localStorage.setItem(TOKEN_KEY, token);
  }
}

// Same base path as the FastAPI app's settings.API_V1_PREFIX.  Dev uses
// the Vite proxy → `/api`; prod can override via env later.
export const api = axios.create({
  baseURL: '/api/v1',
  // The login endpoint sends form-encoded data (OAuth2 password flow),
  // every other endpoint uses JSON.  Defaults set per-request.
});

// Attach the bearer token on every outgoing request.
api.interceptors.request.use((config) => {
  const token = getStoredToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Handle global auth failures — if the token is rejected anywhere, drop
// it and hop to /login.  Components see a rejected promise so they can
// still render an error toast if they want.
let onUnauthorized = () => {};

export function setUnauthorizedHandler(handler) {
  onUnauthorized = typeof handler === 'function' ? handler : () => {};
}

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status;
    // 401 → token expired / invalid.  403 stays — that's a real
    // authorization decision the UI should surface, not a session reset.
    if (status === 401 && getStoredToken()) {
      setStoredToken(null);
      onUnauthorized();
    }
    return Promise.reject(error);
  },
);
