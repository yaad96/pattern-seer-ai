/**
 * The downloads box (2026-11-24; the owner: "can we make them async? With a progress bar? Maybe in the bottom
 * right?"). A download whose file may first have to be made (a panel's print-size PNG, an EPS/SVG/PDF export, a ZIP
 * of every panel: seconds when it was prepared, up to a few minutes for an older job, whose ready-made files the
 * clean-up deleted) is listed in a box at the bottom right with a bar: the server makes the files step by step
 * (POST / GET /api/jobs/{id}/downloads, JobManager.start_download), and at 100% the browser itself fetches the file
 * from its usual address, as any download. So the page never holds a big file in memory, and the browser shows the
 * transfer in its own list. One download is made at a time; the others wait their turn.
 * The page can't know when the browser has finished saving, so the last line says where the file went, not "saved".
 */
import { api } from './api.js';
import { h, icon, replace } from './dom.js';
import { announce, toast } from './ui.js';

const POLL_MS = 1000;
const TICK_MS = 250;
// about how long one step takes on the server, for the bar's movement inside a step (it never passes the step's end)
const STEP_S = { png: 12, detailed: 70, standard: 30, flat: 20, raster: 20 };
const PARTS = { front: 'front', back: 'back', sleeve: 'sleeve', left_sleeve: 'left sleeve', right_sleeve: 'right sleeve', collar: 'collar' };
const UNDER_WAY = new Set(['starting', 'making', 'offline']);
const READY = 'Ready. The file went to your browser’s downloads.';

const items = []; // oldest first: {jobId, what, title, url, zip, state, done, total, label, key, error, stepAt} + its row
let box = null;
let list = null;
let head = null;
let headShown = '';
let ticker = 0;

const active = () => items.find((it) => UNDER_WAY.has(it.state));
const same = (it, jobId, what, url) => it.jobId === jobId && it.url === url && JSON.stringify(it.what) === JSON.stringify(what);

/** Where the box sits: inside the top open dialog (the History drawer is one, and what is outside an open dialog
 * can't be pressed), else the page. The toasts then sit above it (--downloads-h, app.css). */
function place() {
  if (!box) return;
  const open = document.querySelectorAll('dialog[open]');
  const host = open.length ? open[open.length - 1] : document.body;
  if (box.parentElement !== host) host.appendChild(box);
  const height = box.hidden ? 0 : box.getBoundingClientRect().height + 8;
  document.documentElement.style.setProperty('--downloads-h', `${Math.round(height)}px`);
}

function ensureBox() {
  if (box) return;
  head = h('div', { class: 'downloads-head' });
  list = h('div', { class: 'downloads-list' });
  box = h('section', { class: 'downloads', 'aria-label': 'Downloads', hidden: true }, head, list);
  document.body.appendChild(box);
  document.addEventListener('jhs:dialog', place); // a dialog opened (ui.js showDialog): move into it
  document.addEventListener('close', () => window.setTimeout(place, 0), true); // one closed: back to what is under it
}

function percent(it) {
  if (it.state === 'ready') return 100;
  if (it.state !== 'making' && it.state !== 'offline') return 0;
  const total = it.total + (it.zip ? 1 : 0); // a ZIP's last step: it is put together as the browser asks for it
  if (!total) return 0;
  const expect = STEP_S[it.what.kind === 'png' || it.what.kind === 'png-zip' ? 'png' : it.what.preset || 'detailed'] || 30;
  const inStep = it.done < it.total ? Math.min(0.9, (Date.now() - it.stepAt) / (expect * 1000)) : 0;
  return Math.min(99, Math.round(((it.done + inStep) / total) * 100));
}

function statusText(it) {
  if (it.state === 'queued') return 'Waiting: it starts when the one before it is done';
  if (it.state === 'starting') return 'Starting…';
  if (it.state === 'offline') return 'Cannot reach the server. Trying again…';
  if (it.state === 'ready') return READY;
  if (it.state === 'failed') return it.error || 'The file could not be made. Please try again.';
  if (it.done >= it.total) return it.zip ? 'Putting the ZIP together…' : 'Starting…';
  if (!it.zip) return it.what.kind === 'png' ? 'Making the print-size PNG…' : it.what.preset === 'raster' ? 'Building EPS…' : 'Tracing vectors…';
  return `Making the ${PARTS[it.label] || 'image'} ${it.what.kind === 'png-zip' ? 'PNG' : 'EPS'} (${it.done + 1}/${it.total + 1})`;
}

function remove(it) {
  const at = items.indexOf(it);
  if (at >= 0) items.splice(at, 1);
  if (it.el) it.el.remove();
  render();
}

function actions(it) {
  if (it.state === 'ready') return [h('a', { class: 'btn btn-sm', href: it.url, download: '' }, 'Download again')];
  const dismiss = h('button', { type: 'button', class: 'btn btn-sm btn-ghost btn-icon', 'aria-label': 'Dismiss', onClick: () => remove(it) }, icon('x'));
  if (it.state === 'failed') return [h('button', { type: 'button', class: 'btn btn-sm', onClick: () => retry(it) }, 'Try again'), dismiss];
  return it.state === 'queued' ? [dismiss] : [];
}

/** A download's row, built once; paint() then changes only what moved, so a button keeps its focus while a bar runs. */
function build(it) {
  it.pctEl = h('span', { class: 'downloads-pct' });
  it.fill = h('div', { class: 'downloads-fill' });
  it.bar = h('div', { class: 'downloads-bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': it.title }, it.fill);
  it.mark = h('span', { class: 'downloads-mark', hidden: true }, icon('check'));
  it.statusEl = h('span', { class: 'downloads-status' });
  it.actionsEl = h('span', { class: 'downloads-actions' });
  it.el = h('div', { class: 'downloads-item' },
    h('div', { class: 'downloads-top' }, h('span', { class: 'downloads-name', title: it.title }, it.title), it.pctEl),
    it.bar, h('div', { class: 'downloads-foot' }, it.mark, it.statusEl, it.actionsEl));
  list.appendChild(it.el);
}

function paint(it) {
  if (!it.el) build(it);
  const pct = percent(it);
  if (it.shownPct !== pct) {
    it.shownPct = pct;
    it.fill.style.width = `${pct}%`;
    it.pctEl.textContent = `${pct}%`;
    it.bar.setAttribute('aria-valuenow', String(pct));
  }
  const text = statusText(it);
  if (it.shownText !== text) {
    it.shownText = text;
    it.statusEl.textContent = text;
  }
  if (it.shownState === it.state) return;
  it.shownState = it.state;
  const failed = it.state === 'failed';
  it.el.classList.toggle('is-failed', failed);
  it.el.classList.toggle('is-done', it.state === 'ready');
  it.bar.hidden = failed;
  it.pctEl.hidden = failed || it.state === 'queued';
  it.mark.hidden = it.state !== 'ready';
  replace(it.actionsEl, actions(it));
}

function render() {
  ensureBox();
  box.hidden = !items.length;
  const finished = items.some((it) => it.state === 'ready');
  const shown = `${items.length}|${finished}`;
  if (shown !== headShown) {
    headShown = shown;
    replace(head, icon('download'), h('span', { class: 'downloads-title' }, 'Downloads'),
      h('span', { class: 'badge' }, String(items.length)), h('span', { class: 'spacer' }),
      finished ? h('button', {
        type: 'button', class: 'btn btn-sm',
        onClick: () => {
          for (const it of items.filter((x) => x.state === 'ready')) {
            items.splice(items.indexOf(it), 1);
            it.el.remove();
          }
          render();
        },
      }, 'Clear the downloaded ones') : null);
  }
  for (const it of items) paint(it);
  place();
  if (active() && !ticker) ticker = window.setInterval(tick, TICK_MS);
  if (!active() && ticker) {
    window.clearInterval(ticker);
    ticker = 0;
  }
}

/** Between two answers from the server the bar keeps moving a little, inside the step under way. */
function tick() {
  const it = active();
  if (it && it.el) paint(it);
}

/** Hand the finished file to the browser: an ordinary download from the file's own address. */
function save(it) {
  const link = h('a', { href: it.url, download: '', hidden: true });
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function take(it, st) {
  const moved = st.done !== it.done || st.label !== it.label;
  it.key = st.key;
  it.done = st.done;
  it.total = st.total;
  it.label = st.label;
  if (moved || it.state !== 'making') it.stepAt = Date.now();
  if (st.error) {
    it.state = 'failed';
    it.error = st.error;
  } else if (st.ready) {
    it.state = 'ready';
    save(it);
    announce(READY);
  } else {
    it.state = 'making';
  }
}

async function run(it) {
  it.state = 'starting';
  it.error = '';
  render();
  let lost = 0;
  try {
    take(it, await api.startDownload(it.jobId, it.what));
    render();
    while (it.state === 'making' || it.state === 'offline') {
      await new Promise((resolve) => { window.setTimeout(resolve, POLL_MS); });
      if (!items.includes(it)) break;
      try {
        take(it, await api.downloadState(it.jobId, it.key));
        lost = 0;
      } catch (err) {
        if (err.status === 404 && lost < 3) { // the server restarted: ask again (what is made isn't made twice)
          lost += 1;
          take(it, await api.startDownload(it.jobId, it.what));
        } else if (err.status === 0) {
          it.state = 'offline'; // the connection dropped: the server goes on making it; keep asking
        } else {
          throw err;
        }
      }
      render();
    }
  } catch (err) {
    it.state = 'failed';
    it.error = err.message;
    announce(`Download failed: ${err.message}`);
  }
  render();
  pump();
}

function pump() {
  if (active()) return;
  const next = items.find((it) => it.state === 'queued');
  if (next) run(next);
}

function retry(it) {
  it.state = 'queued';
  it.done = 0;
  it.total = 0;
  render();
  pump();
}

/**
 * Download a file whose parts may have to be made first. jobId; what: {kind: 'png'|'export'|'eps-zip'|'png-zip',
 * variant?, preset?, size?} (as POST /api/jobs/{id}/downloads takes it); title: what the box calls it; url: where
 * the browser fetches the file once it is ready.
 */
export function startDownload({ jobId, what, title, url }) {
  ensureBox();
  const known = items.find((it) => same(it, jobId, what, url));
  if (known && (known.state === 'queued' || UNDER_WAY.has(known.state))) { // pressed twice: the one under way is it
    toast('This download is already in the box at the bottom right.', { kind: 'info' });
    return;
  }
  if (known) { // downloaded before, or failed: the same row again, at the end
    items.splice(items.indexOf(known), 1);
    known.el.remove();
  }
  items.push({ jobId, what, title, url, zip: what.kind.endsWith('-zip'), state: 'queued', done: 0, total: 0, label: '', key: '', error: '', stepAt: Date.now() });
  toast('Download started. At 100% the file is saved on your computer.', { kind: 'success' });
  render();
  pump();
}

/** For a download link (<a href download>): a click goes through the box; the link itself still works without it
 * (a right-click's "save link as"). */
export function viaBox(link, { jobId, what, title }) {
  link.addEventListener('click', (e) => {
    if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    startDownload({ jobId, what, title, url: link.getAttribute('href') });
  });
  return link;
}
