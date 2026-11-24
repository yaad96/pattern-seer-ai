/**
 * The jobs box (2026-11-24; the owner: "if somebody hits Generate while a process is under way, create a large popup
 * on the bottom right (just like download queue) to show that another job is happening and once happened, it can be
 * found in history").
 * A job can leave the Recreate tab's Result area while it is still being made: another Generate replaces it there
 * (possible after a reload, when the page no longer holds the button busy; or while a failed image is generated
 * again), or "Clear" is pressed. The server goes on making it, and it is charged like any job. This box, at the
 * bottom right above the downloads box, shows each such job: what the server is doing, then that it is ready and
 * where to find it: in History, with a button that opens it there; or, on a page without a History tab, with a
 * button that shows it in the Result area again.
 * The box is remembered in the browser, per page, so it is still there after a reload (for a day; the job itself
 * stays in History). Each job is watched with the shared poller (jobs.js).
 */
import { fileUrl } from './api.js';
import { h, icon, replace, storage, fmtClock } from './dom.js';
import { isActive, watchJob } from './jobs.js';
import { announce } from './ui.js';

const KEEP_MS = 24 * 3600 * 1000;
const MAX = 6; // the newest few; an older one is in History all the same
const ENDED = {
  succeeded: 'Ready.',
  partial: 'Ready, but some images failed.',
  failed: 'It could not be made.',
};

/**
 * key: where the box is remembered (storage); history: the page has a History tab; openJob(jobId): the row's button
 * (open the job in History, or show it in the Result area again); stageText(stage): rewords the server's stage (a
 * client page leaves out the model's name); onEnded(job): a job in the box just ended.
 * Returns {track(jobId, {active}), drop(jobId)}.
 */
export function initJobsBox({ key, history = false, openJob, stageText = null, onEnded = null }) {
  const items = []; // oldest first: {jobId, at, active, ended, job, offline, unwatch} + its row
  const head = h('div', { class: 'downloads-head' });
  const list = h('div', { class: 'downloads-list' });
  // its own box (class jobsbox) with the downloads box's look: the two share the styles of their parts
  const box = h('section', { class: 'jobsbox', 'aria-label': 'Jobs in the background', hidden: true }, head, list);
  let headShown = '';
  let ticker = 0;
  document.body.appendChild(box);
  document.addEventListener('jhs:dialog', place); // a dialog opened (ui.js showDialog): move into it, as the downloads box does
  document.addEventListener('close', () => window.setTimeout(place, 0), true);

  /** Inside the top open dialog (what is outside an open dialog can't be pressed), else the page; the toasts sit
   * above it (--jobs-h, app.css). */
  function place() {
    const open = document.querySelectorAll('dialog[open]');
    const host = open.length ? open[open.length - 1] : document.body;
    if (box.parentElement !== host) host.appendChild(box);
    const height = box.hidden ? 0 : box.getBoundingClientRect().height + 8;
    document.documentElement.style.setProperty('--jobs-h', `${Math.round(height)}px`);
  }

  const save = () => storage.set(key, items.map((it) => ({ id: it.jobId, at: it.at, active: it.active, ended: it.ended })));

  function nameOf(it) {
    const labels = it.job ? it.job.variants.map((v) => v.label).filter(Boolean) : [];
    if (labels.length) return labels.join(' · ');
    return it.job && it.job.kind === 'design' ? 'Design' : 'Recreate from image';
  }

  function statusOf(it) {
    const job = it.job;
    if (!job) return it.offline ? 'Connection lost. Retrying…' : 'Starting…';
    if (isActive(job.status)) {
      if (it.offline) return 'Connection lost. Retrying…';
      const stage = job.stage || 'Queued';
      return stageText ? stageText(stage) : stage;
    }
    return ENDED[job.status] || ENDED.succeeded;
  }

  function hintOf(it) {
    const working = !it.job || isActive(it.job.status);
    if (history) return working ? 'When it is ready you will find it in History.' : 'You will find it in History.';
    return working ? 'When it is ready you can show it here again.' : '';
  }

  /** A job's row, built once; paint() then changes only what moved, so a button keeps its focus while the clock runs. */
  function build(it) {
    it.nameEl = h('span', { class: 'downloads-name' });
    it.clockEl = h('span', { class: 'downloads-pct', 'aria-hidden': 'true' });
    it.barEl = h('div', { class: 'progress-bar', 'aria-hidden': 'true' });
    it.markEl = h('span', { class: 'downloads-mark', hidden: true }, icon('check'));
    it.statusEl = h('span', { class: 'downloads-status' });
    it.hintEl = h('p', { class: 'jobsbox-hint' });
    it.thumbEl = h('img', { class: 'jobsbox-thumb', alt: '', hidden: true }); // which job: its picture, then its result
    it.el = h('div', { class: 'downloads-item jobsbox-item' },
      it.thumbEl,
      h('div', { class: 'jobsbox-main' },
        h('div', { class: 'downloads-top' }, it.nameEl, it.clockEl),
        it.barEl,
        h('div', { class: 'downloads-foot' }, it.markEl, it.statusEl),
        it.hintEl,
        h('div', { class: 'downloads-actions jobsbox-actions' },
          h('button', { type: 'button', class: 'btn btn-sm', onClick: () => openJob(it.jobId) }, history ? 'Open in History' : 'Show the result'),
          h('button', { type: 'button', class: 'btn btn-sm btn-ghost btn-icon', 'aria-label': 'Dismiss', onClick: () => remove(it) }, icon('x')))));
    list.appendChild(it.el);
  }

  function paint(it) {
    if (!it.el) build(it);
    const job = it.job;
    const working = !job || isActive(job.status);
    const name = nameOf(it);
    if (it.nameEl.textContent !== name) {
      it.nameEl.textContent = name;
      it.nameEl.title = name;
    }
    const created = job ? Date.parse(job.created_at) : NaN;
    const clock = working && !Number.isNaN(created) ? fmtClock(Date.now() - created) : '';
    if (it.clockEl.textContent !== clock) it.clockEl.textContent = clock;
    const status = statusOf(it);
    if (it.statusEl.textContent !== status) it.statusEl.textContent = status;
    const hint = hintOf(it);
    if (it.hintEl.textContent !== hint) it.hintEl.textContent = hint;
    it.hintEl.hidden = !hint;
    const made = job ? job.variants.find((v) => v.status === 'succeeded' && (v.preview || v.png)) : null;
    const picture = made ? made.preview || made.png : job && job.inputs && job.inputs[0];
    const src = picture ? fileUrl(job.id, picture) : '';
    if (it.thumbSrc !== src) {
      it.thumbSrc = src;
      if (src) it.thumbEl.src = src;
      it.thumbEl.hidden = !src;
    }
    const shown = working ? 'working' : job.status;
    if (it.shown === shown) return;
    it.shown = shown;
    it.barEl.hidden = !working;
    it.markEl.hidden = working || job.status === 'failed';
    it.el.classList.toggle('is-done', !working && job.status !== 'failed');
    it.el.classList.toggle('is-failed', !working && job.status === 'failed');
  }

  function render() {
    box.hidden = !items.length;
    const working = items.filter((it) => !it.job || isActive(it.job.status)).length;
    const shown = `${items.length}|${working}`;
    if (shown !== headShown) {
      headShown = shown;
      replace(head, working ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : icon('check'),
        h('span', { class: 'downloads-title' }, 'Jobs in the background'),
        h('span', { class: 'badge' }, String(items.length)));
    }
    for (const it of items) paint(it);
    place();
    if (working && !ticker) ticker = window.setInterval(() => items.forEach((it) => it.el && paint(it)), 1000);
    if (!working && ticker) {
      window.clearInterval(ticker);
      ticker = 0;
    }
  }

  function remove(it) {
    const at = items.indexOf(it);
    if (at < 0) return;
    items.splice(at, 1);
    if (it.unwatch) it.unwatch();
    if (it.el) it.el.remove();
    save();
    render();
  }

  function onUpdate(it, job, meta) {
    if (!items.includes(it)) return;
    if (!job) {
      if (meta.gone) remove(it); // deleted meanwhile
      else if (meta.offline) {
        it.offline = true;
        render();
      }
      return;
    }
    it.offline = !!meta.offline;
    it.job = job;
    if (isActive(job.status)) {
      if (!it.active) {
        it.active = true;
        save();
      }
    } else if (!it.active) {
      remove(it); // it had already ended when it left the Result area: nothing to tell
      return;
    } else if (!it.ended) {
      it.ended = true;
      save();
      announce(`${nameOf(it)}: ${statusOf(it)} ${hintOf(it)}`.trim());
      if (onEnded) onEnded(job);
    }
    render();
  }

  function add(jobId, at, active, ended) {
    const it = { jobId, at, active, ended, job: null, offline: false, unwatch: null, el: null, shown: '', thumbSrc: '' };
    items.push(it);
    while (items.length > MAX) remove(items[0]);
    it.unwatch = watchJob(jobId, (job, meta) => onUpdate(it, job, meta));
    return it;
  }

  // what the box held before a reload (not older than a day)
  const kept = storage.get(key, []);
  if (Array.isArray(kept)) {
    for (const k of kept) {
      if (k && typeof k.id === 'string' && /^\d{8}-\d{6}-[0-9a-f]{6}$/.test(k.id) && Date.now() - Number(k.at) < KEEP_MS
        && !items.some((it) => it.jobId === k.id)) add(k.id, Number(k.at), !!k.active, !!k.ended);
    }
  }
  save();
  render();

  return {
    /** A job that left the Result area while it may still be being made. active: it is known to be (else the box
     * finds out, and drops a job that had already ended). */
    track(jobId, { active = false } = {}) {
      if (items.some((it) => it.jobId === jobId)) return;
      add(jobId, Date.now(), active, false);
      save();
      render();
    },
    /** The job is shown in the Result area (again): it is not a background job. */
    drop(jobId) {
      const it = items.find((x) => x.jobId === jobId);
      if (it) remove(it);
    },
  };
}
