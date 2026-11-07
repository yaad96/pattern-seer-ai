/**
 * Background-job polling (one shared poller per job id) plus the status badge and progress UI.
 */
import { api } from './api.js';
import { h, fmtClock } from './dom.js';

const POLL_MS = 1500;
const HIDDEN_POLL_MS = 5000;
const TERMINAL = ['succeeded', 'partial', 'failed'];

export const isTerminal = (status) => TERMINAL.includes(status);
export const isActive = (status) => status === 'queued' || status === 'running';

const watchers = new Map();

/**
 * Poll GET /api/jobs/{id} while the job is queued/running.
 * listener(job, meta): meta.done on the terminal update, meta.offline while the server is
 * unreachable (job = last known state, may be null), meta.gone with job = null after a 404.
 * Returns an unsubscribe function.
 */
export function watchJob(jobId, listener) {
  let w = watchers.get(jobId);
  if (!w) {
    w = { listeners: new Set(), job: null, timer: 0, failures: 0, stopped: false };
    watchers.set(jobId, w);
    w.timer = setTimeout(() => tick(jobId, w), 0);
  } else if (w.job) {
    const snapshot = w.job;
    setTimeout(() => { if (w.listeners.has(listener)) listener(snapshot, {}); }, 0);
  }
  w.listeners.add(listener);
  return () => {
    w.listeners.delete(listener);
    if (!w.listeners.size) stop(jobId, w);
  };
}

function stop(jobId, w) {
  w.stopped = true;
  clearTimeout(w.timer);
  if (watchers.get(jobId) === w) watchers.delete(jobId);
}

function emit(w, job, meta) {
  for (const fn of Array.from(w.listeners)) fn(job, meta);
}

async function tick(jobId, w) {
  if (w.stopped) return;
  let job;
  try {
    job = await api.job(jobId);
  } catch (err) {
    if (w.stopped) return;
    if (err.status === 404) {
      stop(jobId, w);
      emit(w, null, { gone: true });
      return;
    }
    w.failures += 1;
    emit(w, w.job, { offline: true, message: err.message });
    w.timer = setTimeout(() => tick(jobId, w), Math.min(15000, POLL_MS * 2 ** Math.min(w.failures, 4)));
    return;
  }
  if (w.stopped) return;
  w.failures = 0;
  w.job = job;
  const done = isTerminal(job.status);
  if (done) stop(jobId, w);
  emit(w, job, { done });
  if (!done) w.timer = setTimeout(() => tick(jobId, w), document.hidden ? HIDDEN_POLL_MS : POLL_MS);
}

const STATUS = {
  queued: ['Queued', 'badge'],
  running: ['Working', 'badge-info'],
  succeeded: ['Done', 'badge-success'],
  partial: ['Partly done', 'badge-warning'],
  failed: ['Failed', 'badge-danger'],
};

export function statusBadge(status) {
  const [label, tone] = STATUS[status] || [String(status || 'Unknown'), 'badge'];
  return h('span', { class: ['badge', 'badge-dot', tone] }, label);
}

/** Progress panel: spinner, server stage (aria-live), elapsed time and an indeterminate bar. The clock counts
 * from the job's creation, or from `since` (ms) when given: a failed image generated again starts at 0:00.
 * stageText(stage) may reword the server's stage (the customer page leaves out the model's name). */
export function createProgress({ since, stageText } = {}) {
  const stage = h('span', { class: 'job-progress-stage', 'aria-live': 'polite' }, 'Starting…');
  const time = h('span', { class: 'job-progress-time', 'aria-hidden': 'true' }, '0:00');
  const note = h('p', { class: 'job-progress-note' }, 'Image models usually take 20–90 seconds per image.');
  const el = h('div', { class: 'job-progress' },
    h('div', { class: 'job-progress-row' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), stage, time),
    h('div', { class: 'progress-bar', 'aria-hidden': 'true' }),
    note);
  let start = since || Date.now();
  let timer = 0;
  let attached = false;
  const tickClock = () => {
    if (el.isConnected) attached = true;
    else if (attached) {
      clearInterval(timer);
      timer = 0;
      return;
    }
    time.textContent = fmtClock(Date.now() - start);
  };
  return {
    el,
    update(job, meta) {
      if (job && !since) {
        const created = Date.parse(job.created_at);
        const age = Date.now() - created;
        if (!Number.isNaN(created) && age >= 0 && age < 6 * 3600 * 1000) start = created;
      }
      let text = job && job.stage ? job.stage : 'Queued';
      if (stageText) text = stageText(text);
      if (meta && meta.offline) text = 'Connection lost. Retrying…';
      if (stage.textContent !== text) stage.textContent = text;
      if (job && job.variants && job.variants.length > 1) {
        const done = job.variants.filter((v) => v.status !== 'pending').length;
        note.textContent = `${done} of ${job.variants.length} variants finished. Image models usually take 20–90 seconds per image.`;
      }
      tickClock();
      if (!timer) timer = setInterval(tickClock, 1000);
    },
    stop() {
      clearInterval(timer);
      timer = 0;
    },
  };
}
