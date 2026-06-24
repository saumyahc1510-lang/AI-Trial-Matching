/**
 * Thin endpoint wrappers — one async function per backend route.
 *
 * Keeping the URL strings + payload shapes in one file means React
 * Query hooks (`useQuery`, `useMutation`) can stay focused on caching /
 * UI plumbing rather than HTTP details, and the call shape is easy to
 * mock in tests.
 */
import { api } from './client.js';

/* ── Auth ──────────────────────────────────────────────────────────── */

export async function login({ email, password }) {
  // OAuth2 password flow → form-encoded body.
  const form = new URLSearchParams();
  form.append('username', email);
  form.append('password', password);
  const { data } = await api.post('/auth/login', form, {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  });
  return data;
}

export async function register(payload) {
  // payload may include optional demographic fields (date_of_birth, sex,
  // race, ethnicity, preferred_language, conditions[]) that the backend
  // uses to create + link a Patient row when present.
  const { data } = await api.post('/auth/register', payload);
  return data;
}

export async function me() {
  const { data } = await api.get('/auth/me');
  return data;
}

export async function refreshToken() {
  const { data } = await api.post('/auth/token/refresh');
  return data;
}

/* ── Patients ──────────────────────────────────────────────────────── */

export async function listPatients({ limit = 50, offset = 0, status } = {}) {
  const { data } = await api.get('/patients/', { params: { limit, offset, status } });
  return data;
}

export async function getPatient(patientId) {
  const { data } = await api.get(`/patients/${patientId}`);
  return data;
}

export async function createPatient(payload) {
  const { data } = await api.post('/patients/', payload);
  return data;
}

export async function ingestFhirBootstrap(bundle) {
  const { data } = await api.post('/patients/fhir', { bundle });
  return data;
}

export async function ingestFhirForExisting(patientId, bundle) {
  const { data } = await api.post(`/patients/${patientId}/fhir`, { bundle });
  return data;
}

export async function patientTimeline(patientId, { limit = 500 } = {}) {
  const { data } = await api.get(`/patients/${patientId}/timeline`, {
    params: { limit },
  });
  return data;
}

/* ── Trials ────────────────────────────────────────────────────────── */

export async function listTrials({
  limit = 50, offset = 0, overall_status, condition, category,
} = {}) {
  const { data } = await api.get('/trials/', {
    params: { limit, offset, overall_status, condition, category },
  });
  return data;
}

export async function listTrialCategories() {
  // Returns [{ name, trial_count }] — used by the Trials-page dropdown.
  const { data } = await api.get('/trials/categories');
  return data;
}

export async function getTrial(trialId) {
  const { data } = await api.get(`/trials/${trialId}`);
  return data;
}

export async function trialSummary(trialId, language) {
  const { data } = await api.get(`/trials/${trialId}/summary`, {
    params: { language },
  });
  return data;
}

/* ── Matching ──────────────────────────────────────────────────────── */

export async function triggerMatch(patientId, triggered_by = 'manual') {
  const { data } = await api.post('/matching/trigger', {
    patient_id: patientId,
    triggered_by,
  });
  return data;
}

export async function patientMatches(patientId, { limit = 50, overall_status } = {}) {
  const { data } = await api.get(`/matching/patients/${patientId}`, {
    params: { limit, overall_status },
  });
  return data;
}

export async function getMatchResult(matchResultId) {
  const { data } = await api.get(`/matching/results/${matchResultId}`);
  return data;
}

export async function explainMatch(matchResultId, format = 'json') {
  const url = `/matching/results/${matchResultId}/explain`;
  const { data } = await api.get(url, {
    params: { format },
    // Markdown comes back as text/markdown — Axios will hand us a string.
    responseType: format === 'json' ? 'json' : 'text',
  });
  return data;
}

export async function reviewMatch(matchResultId, payload) {
  const { data } = await api.post(`/matching/results/${matchResultId}/review`, payload);
  return data;
}

/* ── Patient-driven intake ─────────────────────────────────────────── */

export async function intakeStart() {
  const { data } = await api.get('/intake/start');
  return data;
}

export async function intakeQuestions() {
  const { data } = await api.post('/intake/questions');
  return data;
}

export async function intakeAnswers({ questions, answers }) {
  const { data } = await api.post('/intake/answers', { questions, answers });
  return data;
}

export async function intakeFinalize(candidateTrialIds = []) {
  const { data } = await api.post('/intake/finalize', {
    candidate_trial_ids: candidateTrialIds,
  });
  return data;
}

/* ── Notifications ─────────────────────────────────────────────────── */

export async function listNotifications({ unread_only = false, limit = 50 } = {}) {
  const { data } = await api.get('/notifications/', {
    params: { unread_only, limit },
  });
  return data;
}

export async function unreadCount() {
  const { data } = await api.get('/notifications/unread/count');
  return data;
}

export async function markRead(notificationId) {
  await api.post(`/notifications/${notificationId}/read`);
}

export async function markAllRead() {
  const { data } = await api.post('/notifications/read-all');
  return data;
}

/* ── Feedback ──────────────────────────────────────────────────────── */

export async function submitFeedback(payload) {
  const { data } = await api.post('/feedback/', payload);
  return data;
}

export async function feedbackStats() {
  const { data } = await api.get('/feedback/stats');
  return data;
}

/* ── Admin (admin role only) ───────────────────────────────────────── */

export async function adminListUsers(params = {}) {
  // Paginated + filterable: returns { items, total, limit, offset }.
  const { data } = await api.get('/admin/users', { params });
  return data;
}

export async function adminUserStats() {
  // System-wide counts (total / active / admins) for the stat cards,
  // independent of the current page or filter.
  const { data } = await api.get('/admin/users/stats');
  return data;
}

export async function adminUpdateUser(userId, payload) {
  const { data } = await api.patch(`/admin/users/${userId}`, payload);
  return data;
}

export async function adminCreateUser(payload) {
  const { data } = await api.post('/admin/users', payload);
  return data;
}

export async function adminTriggerSync(payload = {}) {
  // Returns { job_id, status } — the sync runs in the background.
  const { data } = await api.post('/admin/trial-sync', payload);
  return data;
}

export async function adminSyncStatus(jobId) {
  // Poll a background sync job: { status, result, error, ... }.
  const { data } = await api.get(`/admin/trial-sync/${jobId}`);
  return data;
}

export async function adminConfig() {
  const { data } = await api.get('/admin/config');
  return data;
}

export async function adminUpdateConfig(payload) {
  // Runtime-editable settings (parse categories / cap). Returns full config.
  const { data } = await api.patch('/admin/config', payload);
  return data;
}

export async function adminLLMUsage() {
  // Aggregate LLM telemetry — backs the admin dashboard's usage card.
  const { data } = await api.get('/admin/usage');
  return data;
}

/* ── Admin patient lifecycle management ────────────────────────────── */

export async function adminListPatients(params = {}) {
  // Paginated + filterable: returns { items, total, limit, offset }.
  const { data } = await api.get('/admin/patients', { params });
  return data;
}

export async function adminPatientStats() {
  // Counts by lifecycle status: { total, active, inactive, deceased }.
  const { data } = await api.get('/admin/patients/stats');
  return data;
}

export async function adminHardDeletePatient(patientId) {
  // Hard delete (cascades all records) — not the soft-delete on /patients.
  await api.delete(`/admin/patients/${patientId}`);
}

export async function adminPurgePatients(payload) {
  // Bulk hard-delete. payload: { confirm: 'DELETE', status?: '…' }.
  const { data } = await api.post('/admin/patients/purge', payload);
  return data; // { deleted }
}

/* ── Audit (admin only) ────────────────────────────────────────────── */

export async function queryAudit(params = {}) {
  const { data } = await api.get('/audit/', { params });
  return data;
}

export async function auditStats() {
  // True counts over the whole audit table (total + last 24h), so the
  // dashboard stat card isn't capped at the listing's page size.
  const { data } = await api.get('/audit/stats');
  return data;
}
