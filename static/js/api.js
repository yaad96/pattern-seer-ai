/**
 * HTTP client. Paths, form fields and JSON fields mirror app/main.py and app/schemas.py exactly.
 */

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

const seg = (value) => encodeURIComponent(String(value));

/** A random id (32 hex) for one press of Generate, Send or Add tokens. Sent again after a lost answer, the server
 * answers with what that press already did instead of doing it, and charging it, twice. (crypto.randomUUID needs a
 * secure context; plain http on the factory network isn't one.) */
export function newRequestId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

const PRESS_TTL_MS = 10 * 60 * 1000; // the server keeps a press for 15 minutes

/** The id of this press of Generate / Send, kept under `key` in `store` (dom.js storage) until it is answered
 * (pressDone): pressed again with the same pictures, words and settings (`fingerprint`) within 10 minutes, e.g. after a
 * lost answer, it is the same press, and the server gives back the job it already made. Anything else: a new id. */
export function pressId(store, key, fingerprint) {
  const kept = store.get(key, null);
  if (kept && kept.fp === fingerprint && typeof kept.at === 'number' && Date.now() - kept.at < PRESS_TTL_MS) return kept.id;
  const id = newRequestId();
  store.set(key, { id, fp: fingerprint, at: Date.now() });
  return id;
}

/** A file, as a press's fingerprint sees it. */
export function fileSig(file) {
  return file ? [file.name, file.size, file.lastModified] : null;
}

let config = null;

/** The GET /api/config payload (null until loadConfig() resolves). */
export function getConfig() {
  return config;
}

export async function loadConfig() {
  config = await request('GET', '/api/config');
  return config;
}

const JOBS_API = '/api/jobs';
const FILES = '/files';

/** URL of a job file: inline by default, as an attachment with download=true. */
export function fileUrl(jobId, filename, download) {
  return `${FILES}/${seg(jobId)}/${seg(filename)}${download ? '?download=1' : ''}`;
}

/** URL of a panel job's ZIP, which the browser downloads itself once the downloads box has its files ready
 * (downloads.js): every panel's EPS at one preset (kind 'eps': GET .../panels-eps.zip?preset=), or every panel's PNG
 * at the print or the original size (kind 'png': GET .../panels-png.zip?size=). */
export function zipUrl(jobId, kind, value) {
  return `/api/jobs/${seg(jobId)}/panels-${kind}.zip?${kind === 'eps' ? 'preset' : 'size'}=${seg(value)}`;
}

function describeDetail(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.slice(0, 3).map((item) => {
      if (!item || typeof item !== 'object') return String(item);
      const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body').join(' › ') : '';
      const msg = typeof item.msg === 'string' ? item.msg : 'invalid value';
      return loc ? `${loc}: ${msg}` : msg;
    }).join('; ');
  }
  return '';
}

function errorMessage(status, data) {
  const detail = data && typeof data === 'object' ? describeDetail(data.detail) : '';
  if (status === 401) return 'Your session has ended. Please sign in again.';
  if (status === 413) return detail || 'The upload is too large.';
  if (status === 429) return detail || 'Too many requests. Please wait a little and try again.';
  if (detail) return detail;
  if (status >= 500) return `The server had a problem (error ${status}). Please try again.`;
  return `Request failed (error ${status}).`;
}

/** fetch() that turns a network failure into an ApiError and sends a 401 to the sign-in page. */
async function send(path, init) {
  let res;
  try {
    res = await fetch(path, init);
  } catch (_) {
    throw new ApiError('Cannot reach the server. Check that it is running and that you are on the factory network.', 0);
  }
  if (res.status === 401) {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.hash)}`;
  }
  return res;
}

async function readJson(res) {
  try {
    const text = await res.text();
    return text ? JSON.parse(text) : null;
  } catch (_) {
    return null;
  }
}

async function request(method, path, options) {
  const { json, form } = options || {};
  const init = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin' };
  if (json !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(json);
  } else if (form) {
    init.body = form;
  }
  const res = await send(path, init);
  if (res.status === 204) return null;
  const data = await readJson(res);
  // The site was closed on the dashboard (app/site_switch.py): the server answers like a site that isn't there. An
  // open client demo page reloads, so it shows the browser's own 404 screen too. Only there: the studio is never
  // closed off, and the demo page calls no route that could be missing for any other reason.
  if (res.status === 404 && data && data.detail === 'Not Found' && document.documentElement.dataset.app === 'customer') {
    window.location.reload();
  }
  if (!res.ok) throw new ApiError(errorMessage(res.status, data), res.status, data ? data.detail : undefined);
  if (data === null) throw new ApiError('Unexpected response from the server.', res.status);
  return data;
}

export const api = {
  /** POST /api/recreate (multipart: image, options=JSON RecreateOptions, back_image and sleeve_image for panels)
   * -> {job_id}. */
  recreate(file, options, backFile, sleeveFile, requestId) {
    const form = new FormData();
    form.append('image', file, file.name || 'reference.png');
    form.append('options', JSON.stringify(options));
    if (backFile) form.append('back_image', backFile, backFile.name || 'back.png');
    if (sleeveFile) form.append('sleeve_image', sleeveFile, sleeveFile.name || 'sleeve.png');
    if (requestId) form.append('request_id', requestId);
    return request('POST', '/api/recreate', { form });
  },

  /** POST /api/design (multipart) -> {job_id, session_id}. parent = {jobId, variant} | null. */
  design({ message, options, sessionId, parent, attachments, requestId }) {
    const form = new FormData();
    form.append('message', message);
    if (requestId) form.append('request_id', requestId);
    form.append('options', JSON.stringify(options));
    if (sessionId) form.append('session_id', sessionId);
    if (parent) {
      form.append('parent_job_id', parent.jobId);
      form.append('parent_variant', String(parent.variant));
    }
    (attachments || []).forEach((file, i) => {
      form.append('attachments', file, file.name || `reference-${i + 1}.png`);
    });
    return request('POST', '/api/design', { form });
  },

  /** POST /api/prompt-preview -> {system, user, provider, model, aspect_ratio, resolution, warnings}. */
  promptPreview({ kind, options, message, inputRatio }) {
    return request('POST', '/api/prompt-preview', {
      json: { kind, options, message: message || '', input_ratio: inputRatio || null },
    });
  },

  /** GET /api/admin/site (studio login) -> {closed, since}: the dashboard's site switch. */
  siteState() {
    return request('GET', '/api/admin/site');
  },

  /** POST /api/admin/site {closed} -> {closed, since}: close the site (a plain 404 for everyone but the studio
   * login) or open it again. */
  setSiteClosed(closed) {
    return request('POST', '/api/admin/site', { json: { closed: Boolean(closed) } });
  },

  /** GET /api/admin/usage?days= (studio login) -> {jobs: [...with their paid calls and our tokens], tokens, sync} for
   * the dashboard; 0 = all. */
  adminUsage(days) {
    return request('GET', `/api/admin/usage?days=${encodeURIComponent(String(days))}`);
  },

  /** POST /api/admin/sync -> the sync status: send this machine's Dashboard to the server now (app/sync.py). */
  syncNow() {
    return request('POST', '/api/admin/sync');
  },

  /** GET /api/tokens/balance?client= -> {enabled, client, balance, available, prices, charged}: a client page's
   * purse (the client login's own; the studio login names the page's client, and its own jobs aren't charged). */
  tokenBalance(client) {
    return request('GET', `/api/tokens/balance${client ? `?client=${encodeURIComponent(client)}` : ''}`);
  },

  /** GET /api/tokens/mine?client= -> a client page's own purse, its jobs' tokens and its payments ("Customer dashboard"). */
  myTokens(client) {
    return request('GET', `/api/tokens/mine${client ? `?client=${encodeURIComponent(client)}` : ''}`);
  },

  /** GET /api/tokens/clients?days= (head admin, partner) -> {enabled, can_add, can_take_back, role, waiting, prices,
   * clients: [...]}. */
  tokenClients(days) {
    return request('GET', `/api/tokens/clients?days=${encodeURIComponent(String(days))}`);
  },

  /** GET /api/tokens/clients/{slug}?days= (studio, partner) -> {client, jobs: [...with their token items], topups}. */
  tokenClient(slug, days) {
    return request('GET', `/api/tokens/clients/${seg(slug)}?days=${encodeURIComponent(String(days))}`);
  },

  /** POST /api/tokens/clients/{slug}/topup {tokens, note, taka, request_id} (studio login) -> {entry, balance,
   * repeated?}. taka: what the client paid, whole taka, or null. requestId: the form's own id (newRequestId): the same
   * id again records nothing new (repeated: true). */
  addTokens(slug, tokens, note, taka = null, requestId = null) {
    return request('POST', `/api/tokens/clients/${seg(slug)}/topup`,
      { json: { tokens, note, taka, request_id: requestId } });
  },

  /** GET /api/tokens/jobs?days=&client= (head admin, partner) -> {jobs: [...every client's jobs with their tokens,
   * and the demo login's (no tokens)]}; client "_demo": only the demo login's. */
  tokenJobs(days, client) {
    const q = new URLSearchParams({ days: String(days) });
    if (client) q.set('client', client);
    return request('GET', `/api/tokens/jobs?${q}`);
  },

  /** GET /api/tokens/topups?days= (head admin, partner) -> {topups: [...tokens added or taken back by hand]}. */
  tokenTopups(days) {
    return request('GET', `/api/tokens/topups?days=${encodeURIComponent(String(days))}`);
  },

  /** GET /api/pay/requests (head admin, partner) -> {payments: [...bKash payments, newest first], waiting, role, …}. */
  payRequests() {
    return request('GET', '/api/pay/requests');
  },

  /** POST /api/pay/requests/{id}/approve -> {payment, balance}: the money was seen in bKash; the tokens are added. */
  payApprove(id) {
    return request('POST', `/api/pay/requests/${seg(id)}/approve`);
  },

  /** POST /api/pay/requests/{id}/reject {reason, text} -> {payment}. reason: not_found | amount | used | other. */
  payReject(id, reason, text = '') {
    return request('POST', `/api/pay/requests/${seg(id)}/reject`, { json: { reason, text } });
  },

  /** GET /api/pay/settings (head admin) -> {number, packages, enabled, …}: where clients pay and what they can buy. */
  paySettings() {
    return request('GET', '/api/pay/settings');
  },

  /** POST /api/pay/settings {number, packages: [{tokens, taka}]} (head admin) -> the saved settings. */
  savePaySettings(number, packages) {
    return request('POST', '/api/pay/settings', { json: { number, packages } });
  },

  /** POST /api/pay/requests {tokens, trx, sender, request_id} (a client's own login) -> {payment, repeated}: the
   * client paid for the package of `tokens` tokens with bKash; the payment waits to be checked. */
  buyTokens(tokens, trx, sender, requestId) {
    return request('POST', '/api/pay/requests', { json: { tokens, trx, sender, request_id: requestId } });
  },

  /** POST /api/clients/preview (multipart: name, slug, colors=JSON, logo) -> {suggested, slug_problem, welcome, and
   * with a logo: logo, mark, colors, light, dark}: the Customer EntryPoint Builder's preview; nothing is kept. */
  customerPreview(form) {
    return request('POST', '/api/clients/preview', { form });
  },

  /** POST /api/clients (multipart: name, phone, slug, colors=JSON, logo) -> {client: {slug, name, phone, path,
   * username, password}, tokens, balance}: a customer's entry point, working at once (head admin and partner). */
  customerCreate(form) {
    return request('POST', '/api/clients', { form });
  },

  /** POST /api/clients/{slug} {name?, phone?, password?} -> {entry, name, changed}: change a client's name, phone or
   * password (head admin and partner); only what is sent is changed. */
  customerEdit(slug, changes) {
    return request('POST', `/api/clients/${seg(slug)}`, { json: changes });
  },

  /** GET /api/admin/sheet-script -> {script, version}: the Apps Script for the Google Sheet, with this machine's
   * secret in it (the head admin; the Supreme dashboard's Settings copies it for pasting into the sheet). */
  sheetScript() {
    return request('GET', '/api/admin/sheet-script');
  },

  /** POST /api/admin/sheet-check -> the sheet log's status after asking the sheet's script which version it is. */
  sheetCheck() {
    return request('POST', '/api/admin/sheet-check');
  },

  /** POST /api/jobs/{id}/downloads {kind, variant, preset, size} -> {key, done, total, label, ready, error}: make a
   * download's files ready (the downloads box). kind: png | export | eps-zip | png-zip. */
  startDownload(jobId, what) {
    return request('POST', `${JOBS_API}/${seg(jobId)}/downloads`, { json: what });
  },

  /** GET /api/jobs/{id}/downloads?key= -> the same, as it stands now (404: the server restarted; start it again). */
  downloadState(jobId, key) {
    return request('GET', `${JOBS_API}/${seg(jobId)}/downloads?key=${encodeURIComponent(key)}`);
  },

  /** GET /api/jobs -> JobSummary[] (newest first). */
  listJobs({ limit, kind, sessionId, clients, client, demo } = {}) {
    const q = new URLSearchParams();
    if (limit) q.set('limit', String(limit));
    if (kind) q.set('kind', kind);
    if (sessionId) q.set('session_id', sessionId);
    if (clients) q.set('clients', '1'); // only the jobs the Partner dashboard's History lists: the clients' and the demo login's
    if (client) q.set('client', client); // only that client's (a client page's History)
    if (demo) q.set('demo', '1'); // only the demo login's (the demo page's History, as the head admin sees it there)
    const qs = q.toString();
    return request('GET', `${JOBS_API}${qs ? `?${qs}` : ''}`);
  },

  job(jobId) {
    return request('GET', `${JOBS_API}/${seg(jobId)}`);
  },

  deleteJob(jobId) {
    return request('DELETE', `${JOBS_API}/${seg(jobId)}`);
  },

  /** POST .../variants/{index}/vectorize {preset} -> VectorAsset. */
  vectorize(jobId, index, preset) {
    return request('POST', `${JOBS_API}/${seg(jobId)}/variants/${seg(index)}/vectorize`, { json: { preset } });
  },

  /** GET .../{job_id}/calls -> the job's paid API calls, oldest first (prompt, images, settings, reply, cost). */
  jobCalls(jobId) {
    return request('GET', `${JOBS_API}/${seg(jobId)}/calls`);
  },

  /** POST .../variants/{index}/retry -> {job_id}: generate one failed Recreate image again (a new image call). */
  retryVariant(jobId, index) {
    return request('POST', `${JOBS_API}/${seg(jobId)}/variants/${seg(index)}/retry`);
  },

};
