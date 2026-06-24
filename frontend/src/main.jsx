import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from 'react-hot-toast';

import App from './App.jsx';
import { AuthProvider } from './auth/AuthContext.jsx';
import './index.css';

// One QueryClient for the whole app.  Reasonable retries for a UI that
// might briefly lose the dev server during reloads; no retry on auth
// errors (the interceptor short-circuits to the login screen).
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error) => {
        const status = error?.response?.status;
        if (status && status >= 400 && status < 500) return false;
        return failureCount < 2;
      },
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
});

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <App />
          <Toaster
            position="top-right"
            toastOptions={{
              className:
                '!bg-white !text-ink-800 !shadow-soft !border !border-ink-100 !rounded-xl !px-4 !py-3 !text-sm',
              duration: 3500,
              success: {
                iconTheme: { primary: '#0ea271', secondary: '#dcfae6' },
              },
              error: {
                iconTheme: { primary: '#dc2626', secondary: '#fee2e2' },
              },
            }}
          />
        </AuthProvider>
      </QueryClientProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
