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
    throw new ApiError('Cannot reach the studio server. Check that it is running and that you are on the factory network.', 0);
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
  if (!res.ok) throw new ApiError(errorMessage(res.status, data), res.status, data ? data.detail : undefined);
  if (data === null) throw new ApiError('Unexpected response from the server.', res.status);
  return data;
}

export const api = {
  /** POST /api/recreate (multipart: image, options=JSON RecreateOptions, back_image and sleeve_image for panels)
   * -> {job_id}. */
  recreate(file, options, backFile, sleeveFile) {
    const form = new FormData();
    form.append('image', file, file.name || 'reference.png');
    form.append('options', JSON.stringify(options));
    if (backFile) form.append('back_image', backFile, backFile.name || 'back.png');
    if (sleeveFile) form.append('sleeve_image', sleeveFile, sleeveFile.name || 'sleeve.png');
    return request('POST', '/api/recreate', { form });
  },

  /** POST /api/design (multipart) -> {job_id, session_id}. parent = {jobId, variant} | null. */
  design({ message, options, sessionId, parent, attachments }) {
    const form = new FormData();
    form.append('message', message);
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

  /** GET /api/admin/usage?days= (studio login) -> {jobs: [...with their paid calls]} for the dashboard; 0 = all. */
  adminUsage(days) {
    return request('GET', `/api/admin/usage?days=${encodeURIComponent(String(days))}`);
  },

  /** GET /api/jobs -> JobSummary[] (newest first). */
  listJobs({ limit, kind, sessionId } = {}) {
    const q = new URLSearchParams();
    if (limit) q.set('limit', String(limit));
    if (kind) q.set('kind', kind);
    if (sessionId) q.set('session_id', sessionId);
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

  /** GET .../{job_id}/panels-eps.zip?preset= -> {blob, filename}: every panel's EPS at one preset, in one ZIP. */
  panelsZip(jobId, preset) {
    return zipFile(`${JOBS_API}/${seg(jobId)}/panels-eps.zip?preset=${seg(preset)}`, `jersey-${jobId}-panels-eps-${preset}.zip`);
  },

  /** GET .../{job_id}/panels-png.zip?size=print|original -> {blob, filename}: every panel's PNG in one ZIP. */
  panelsPngZip(jobId, size) {
    return zipFile(`${JOBS_API}/${seg(jobId)}/panels-png.zip?size=${seg(size)}`, `jersey-${jobId}-panels-png-${size}.zip`);
  },
};

async function zipFile(url, fallbackName) {
  const res = await send(url, { credentials: 'same-origin' });
  if (!res.ok) {
    const data = await readJson(res);
    throw new ApiError(errorMessage(res.status, data), res.status, data ? data.detail : undefined);
  }
  const match = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '');
  return { blob: await res.blob(), filename: match ? match[1] : fallbackName };
}
