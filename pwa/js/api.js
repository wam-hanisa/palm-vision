/**
 * Thin fetch() wrapper: JWT attach/refresh for reads, and the
 * write-or-queue decision for offline-writable resources.
 *
 * IMPORTANT per docs/05-offline-sync.md § Conflict handling: only *creates*
 * on offline-writable resources (Tree, HarvestTree, FertilizerTree,
 * ActualHarvest, ActualFertilizer) go through the queue — see
 * queueableWrite() below. Editing an existing record (e.g. app.js's tree
 * edit form) and the approve action are online-only — see
 * onlineOnlyWrite() below. Two differently-named functions, not one
 * flag-driven function, so which behavior a given call site gets is
 * obvious from the call site itself, not from an easy-to-miss argument.
 */

const API_BASE = ""; // same-origin — Django serves both the API and this PWA locally

async function getTokens() {
  const access = await window.PalmVisionDB.getMeta("access_token");
  const refresh = await window.PalmVisionDB.getMeta("refresh_token");
  return { access, refresh };
}

async function setTokens({ access, refresh }) {
  if (access) await window.PalmVisionDB.setMeta("access_token", access);
  if (refresh) await window.PalmVisionDB.setMeta("refresh_token", refresh);
}

async function login(username, password) {
  const response = await fetch(`${API_BASE}/api/token/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    throw new Error("Login failed — check your username and password.");
  }
  const data = await response.json();
  await setTokens({ access: data.access, refresh: data.refresh });
  await window.PalmVisionDB.setMeta("username", username);
  return data;
}

async function logout() {
  await setTokens({ access: "", refresh: "" });
}

async function refreshAccessToken() {
  const { refresh } = await getTokens();
  if (!refresh) throw new Error("No refresh token available — please log in again.");
  const response = await fetch(`${API_BASE}/api/token/refresh/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh }),
  });
  if (!response.ok) throw new Error("Session expired — please log in again.");
  const data = await response.json();
  await setTokens({ access: data.access });
  return data.access;
}

/** Authenticated fetch with one automatic retry-after-refresh on 401.
 * Throws a distinguishable NetworkError (via .isNetworkError) when the
 * request never reached the server at all — that's the signal callers use
 * to decide "queue this for later" vs "show a real validation error."
 */
async function apiFetch(path, options = {}) {
  const { access } = await getTokens();
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (access) headers.Authorization = `Bearer ${access}`;

  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  } catch (err) {
    const networkError = new Error("Network request failed — device is likely offline.");
    networkError.isNetworkError = true;
    throw networkError;
  }

  if (response.status === 401 && access) {
    try {
      const newAccess = await refreshAccessToken();
      const retryHeaders = { ...headers, Authorization: `Bearer ${newAccess}` };
      response = await fetch(`${API_BASE}${path}`, { ...options, headers: retryHeaders });
    } catch (err) {
      // Refresh itself failed (or the retry did) — surface as a normal
      // response so the caller's status-code handling still applies,
      // rather than throwing a network error for what's really an auth
      // problem the user needs to resolve by logging in again.
    }
  }

  return response;
}

/** The single gate for "should this write happen now, or go in the
 * outbox." Used for offline-writable creates only (see module docstring).
 * `resource` is used purely for outbox bookkeeping/display, not sent to
 * the server.
 */
async function queueableWrite(resource, path, method, body) {
  try {
    const response = await apiFetch(path, { method, body: JSON.stringify(body) });
    if (response.ok) {
      return { queued: false, response: await response.json() };
    }
    if (response.status >= 500) {
      // Server-side failure, not a validation problem with this payload —
      // still worth queuing for retry rather than losing the field worker's
      // data entry.
      const outboxId = await window.PalmVisionDB.enqueueOutbox({ resource, path, method, body });
      return { queued: true, outboxId };
    }
    // 4xx: a real validation/permission error. Don't queue something the
    // server has already told us is invalid — surface it now while the
    // field worker can still correct it.
    const errorBody = await response.json().catch(() => ({}));
    const error = new Error("Request rejected by the server.");
    error.status = response.status;
    error.body = errorBody;
    throw error;
  } catch (err) {
    if (err.isNetworkError) {
      const outboxId = await window.PalmVisionDB.enqueueOutbox({ resource, path, method, body });
      return { queued: true, outboxId };
    }
    throw err;
  }
}

/** Edits go through here, never queueableWrite() — docs/05-offline-sync.md
 * § Conflict handling: "Editing an existing record is an online-only
 * action, not part of the offline queue — this deliberately avoids the
 * harder class of conflict-resolution problems rather than half-solving
 * them." A network failure here is never silently queued for later replay
 * (unlike queueableWrite()'s 5xx/offline handling) — it propagates as
 * apiFetch()'s usual distinguishable NetworkError (`.isNetworkError`) so
 * the caller can tell the field worker plainly "try again once you're
 * back online," instead of the edit vanishing into a queue that won't
 * apply it until later (by which point the record may have changed under
 * it — exactly the conflict this project chose not to try to resolve).
 */
async function onlineOnlyWrite(path, method, body) {
  const response = await apiFetch(path, { method, body: JSON.stringify(body) });
  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}));
    const error = new Error("Request rejected by the server.");
    error.status = response.status;
    error.body = errorBody;
    throw error;
  }
  return response.json();
}

window.PalmVisionAPI = {
  login,
  logout,
  apiFetch,
  queueableWrite,
  onlineOnlyWrite,
  getTokens,
};
