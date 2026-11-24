/**
 * Dashboard tab (studio page, since 2026-11-08): what was made each day and what it cost, from GET /api/admin/usage
 * (app/usage.py): every job and every paid API call with its tokens and cost, jobs deleted in History included (the
 * usage ledger). Days are UTC by default, like OpenAI's bill, or this computer's time. A job retried on a later day
 * appears on both days, each row with that day's calls, so a day's total is exactly what was spent that day.
 * Since 2026-11-23 also our own tokens (app/tokens.py: what each client job cost its client's purse) beside the real
 * costs, a "By client" table, and the other machines' jobs (the Mac's, sent with app/sync.py) with a machine filter.
 * The Dashboard is in English (the owner's choice, 2026-11-23: an internal page): its panel, toasts and dialogs are
 * marked untranslated, while the rest of the studio page stays Bangla.
 */
import { api, fileUrl } from './api.js';
import { h, icon, replace, fmtDateTime, fmtInt, fmtSeconds, fmtUsd, storage } from './dom.js';
import { statusBadge } from './jobs.js';
import { modelLabel } from './results.js';
import { TOKEN_ITEMS } from './tokens.js';
import { callout, confirmDialog, loadingRow, segmented, setBusy, toast } from './ui.js';

const PREFS_KEY = 'dashboard.prefs';
const DAY_MS = 86400000;
const PANEL_NAMES = { front: 'Front', back: 'Back', sleeve: 'Sleeve' };
const BACK_METHODS = { mirror: 'Mirror of the front', copy: 'Copy of the front', image: 'Back picture' };
const PURPOSES = { image: 'Image', brief: 'Design brief', reference_check: 'Reference check' };
const MACHINES = { server: 'Server', mac: 'Mac' };
const machineName = (m) => (Object.prototype.hasOwnProperty.call(MACHINES, m) ? MACHINES[m] : m || '');

/** Money for tiles and the chart (exact amounts stay in the tables). */
const money = (v) => `$${v >= 1 ? v.toFixed(2) : v.toFixed(3)}`;
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const callTime = (c) => c.finished_at || c.started_at || '';

function dayKey(iso, zone) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  if (zone === 'utc') return d.toISOString().slice(0, 10);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** "Sat, 7 Nov 2026" for a day key (shown as that calendar day, whatever the zone). */
function dayLabel(key, opts = { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }) {
  const [y, m, d] = key.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, { ...opts, timeZone: 'UTC' });
}

function clockTime(iso, zone) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', ...(zone === 'utc' ? { timeZone: 'UTC' } : {}) });
}

/** The output types in English (the catalog's labels are the Bangla shown on the pages). */
const MODES = { pattern: 'Pattern only', flat_jersey: 'Flat jersey artwork', replica: 'HD replica', panels: 'Production panels' };

/** The dashboard's name for a job's output type, and its filter key. */
function typeOf(job) {
  if (job.kind === 'design') return { key: 'design', label: 'Design from description' };
  return { key: job.mode || 'recreate', label: MODES[job.mode] || job.mode || 'Recreate from image' };
}

/** Tokens, images and money of a set of calls. */
function sum(calls) {
  const t = { cost: 0, uncosted: 0, ai: 0, failed: 0, image: 0, designer: 0, designerCalls: 0, input: 0, output: 0 };
  for (const c of calls) {
    const cost = typeof c.cost_usd === 'number' ? c.cost_usd : null;
    if (cost === null) t.uncosted += 1;
    else t.cost += cost;
    if (c.purpose === 'image') {
      if (c.status === 'ok') t.ai += 1;
      else t.failed += 1;
      t.image += cost || 0;
    } else {
      t.designerCalls += 1;
      t.designer += cost || 0;
    }
    const k = c.tokens || {};
    t.input += (k.input || 0) + (k.cache_read || 0) + (k.cache_write || 0);
    t.output += k.output || 0;
  }
  return t;
}

function addInto(total, part) {
  for (const key of Object.keys(part)) total[key] = (total[key] || 0) + part[key];
  return total;
}

export function initDashboard(root, { onOpenJob, onSiteChange } = {}) {
  root.setAttribute('data-no-translate', ''); // English (see above)
  const say = (message, kind) => toast(message, { kind, raw: true });
  const saved = storage.get(PREFS_KEY, {}) || {};
  const prefs = { range: '30', zone: 'utc', who: 'all', type: 'all', machine: 'all', ...saved };
  let report = null;
  let loading = false;
  let seq = 0;
  const expanded = new Set();

  const savePrefs = () => storage.set(PREFS_KEY, prefs);
  const control = (legend, key, options, refetch) => {
    const s = segmented({
      legend, value: prefs[key], options: options.map(([value, label]) => ({ value, label })),
      onChange: (v) => { prefs[key] = v; savePrefs(); if (refetch) load(); else render(); },
    });
    return s;
  };
  const range = control('Period', 'range', [['7', 'Last 7 days'], ['30', 'Last 30 days'], ['90', 'Last 90 days'], ['0', 'All']], true);
  const zone = control('Days by', 'zone', [['utc', 'UTC (like the OpenAI bill)'], ['local', 'This computer’s time']], false);
  const who = control('Login', 'who', [['all', 'All'], ['studio', 'Head admin login'], ['demo', 'Demo login'],
    ['client', 'Client logins']], false); // a client page's own login (app/clients.py): job.client
  const typeSel = h('select', { class: 'select', 'aria-label': 'Output type' });
  typeSel.addEventListener('change', () => { prefs.type = typeSel.value; savePrefs(); render(); });
  const refreshBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('refresh'), 'Refresh');
  refreshBtn.addEventListener('click', () => load());
  const csvBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('download'), 'Download CSV');
  csvBtn.addEventListener('click', () => downloadCsv());
  // the Mac: send its Dashboard to the online server now (it also does so by itself every few minutes)
  const syncBtn = h('button', { type: 'button', class: 'btn btn-sm', hidden: true }, icon('refresh'), 'Send to the server');
  syncBtn.addEventListener('click', () => syncNow());
  const machineBox = h('div', { class: 'dash-machine' }); // the machine filter, when there are jobs from more than one
  const syncNote = h('p', { class: 'hint', hidden: true });
  const note = h('p', { class: 'hint' });
  const body = h('div', { class: 'dash-body' });
  const siteBox = h('section', { class: 'card dash-site' }, h('div', { class: 'card-body' }, loadingRow('Loading…')));

  replace(root,
    h('div', { class: 'history-toolbar' }, h('h2', null, 'Dashboard'), h('span', { class: 'spacer' }), syncBtn, refreshBtn, csvBtn),
    siteBox,
    h('div', { class: 'dash-filters' }, range.el, zone.el, who.el, machineBox,
      h('label', { class: 'dash-type' }, h('span', { class: 'field-label' }, 'Output type'), typeSel)),
    syncNote, note, body);

  /* ---------- the site switch (app/site_switch.py, 2026-11-11): close the site for everyone but the studio login,
     who then get a plain "page not found" (404), as if the site weren't there; open it again here ---------- */
  let siteBusy = false;

  async function loadSite() {
    try {
      renderSite(await api.siteState());
    } catch (err) {
      replace(siteBox, h('div', { class: 'card-body' }, callout('error', err.message)));
    }
  }

  function renderSite(state) {
    const closed = Boolean(state.closed);
    if (onSiteChange) onSiteChange(closed);
    const btn = h('button', { type: 'button', class: ['btn', closed ? 'btn-primary' : 'btn-danger-solid'] },
      icon(closed ? 'check' : 'x'), closed ? 'Open the site' : 'Close the site');
    btn.addEventListener('click', () => switchSite(!closed, btn));
    replace(siteBox, h('div', { class: 'card-body dash-site-body' },
      h('div', { class: 'dash-site-text' },
        h('div', { class: 'dash-site-head' },
          h('h3', { class: 'card-title' }, 'Site'),
          h('span', { class: ['badge', 'badge-dot', closed ? 'badge-danger' : 'badge-success'] }, closed ? 'Closed' : 'Open'),
          state.since ? h('span', { class: 'muted' }, `Since ${fmtDateTime(state.since)}`) : null),
        h('p', { class: 'hint' }, closed
          ? 'Only your head admin login can use the site. Everyone else, the demo link included, gets a “page not found” (404) error, as if the site weren’t there.'
          : 'Everyone can use the site, the demo link included. Closing it shows them a “page not found” (404) error until you open it again here.'),
        h('p', { class: 'hint' }, 'This switch controls this server only.')),
      btn));
  }

  async function switchSite(close, btn) {
    if (siteBusy) return;
    if (close && !(await confirmDialog({
      title: 'Close the site?',
      message: 'The demo link and everyone not signed in as the head admin will get a “page not found” (404) error until you open the site again here. Images being made right now still finish.',
      confirmLabel: 'Close the site',
      danger: true,
      raw: true,
    }))) return;
    siteBusy = true;
    setBusy(btn, true, close ? 'Closing…' : 'Opening…');
    try {
      renderSite(await api.setSiteClosed(close));
      say(close ? 'The site is closed.' : 'The site is open again.', close ? 'warning' : 'success');
    } catch (err) {
      setBusy(btn, false);
      say(err.message, 'error');
    } finally {
      siteBusy = false;
    }
  }

  /* ---------- the Mac's Dashboard on the server (app/sync.py) ---------- */
  function renderSync() {
    const st = report.sync || {};
    syncBtn.hidden = !st.enabled;
    const lines = [];
    if (st.enabled) {
      if (st.last_ok_at) lines.push(`Sent to the server ${fmtDateTime(st.last_ok_at)}`);
      if (st.last_error) lines.push(`Last try failed: ${st.last_error}`);
      if (!st.last_ok_at && !st.last_error) lines.push('Sends this Dashboard to the server every few minutes.');
    }
    for (const r of report.remote || []) {
      lines.push(`${machineName(r.machine)}: received ${fmtDateTime(r.received_at)} (${plural(r.jobs, 'job', 'jobs')})`);
    }
    const sh = report.sheet; // the Google Sheet log (app/sheets.py)
    if (sh) {
      if (!sh.enabled) lines.push(`Google Sheet: not set up (${plural(sh.pending, 'row', 'rows')} kept on this computer)`);
      else if (sh.last_error) lines.push(`Google Sheet: ${plural(sh.pending, 'row', 'rows')} waiting (${sh.last_error})`);
      else lines.push(sh.pending ? `Google Sheet: ${plural(sh.pending, 'row', 'rows')} waiting` : 'Google Sheet: up to date');
    }
    replace(syncNote, lines.map((line, i) => [i ? ' · ' : null, h('span', null, line)]));
    syncNote.hidden = !lines.length;
  }

  async function syncNow() {
    setBusy(syncBtn, true, 'Sending…');
    try {
      const st = await api.syncNow();
      if (report) report.sync = st;
      if (st.last_error && st.last_attempt_at !== st.last_ok_at) say(st.last_error, 'error');
      else say('Sent to the server.', 'success');
    } catch (err) {
      say(err.message, 'error');
    } finally {
      setBusy(syncBtn, false);
      if (report) renderSync();
    }
  }

  /* ---------- data ---------- */
  async function load() {
    const mine = ++seq;
    loading = true;
    if (!report) replace(body, loadingRow('Loading…'));
    try {
      const days = Number(prefs.range);
      const data = await api.adminUsage(days ? days + 1 : 0); // a day more, so a whole local day is covered
      if (mine !== seq) return;
      report = data;
      render();
    } catch (err) {
      if (mine === seq) replace(body, callout('error', err.message));
    } finally {
      if (mine === seq) loading = false;
    }
  }

  /** Rows: one per job and day it had activity (made that day, or calls that day), newest first. */
  function rowsFor() {
    const days = Number(prefs.range);
    const rangeCut = days ? dayKey(new Date(Date.now() - (days - 1) * DAY_MS).toISOString(), prefs.zone) : '';
    // the server's start date (Settings.dashboard_since; the Mac starts on 2026-11-08): nothing earlier is shown
    const floorCut = report.floor ? dayKey(report.floor, prefs.zone) : '';
    const cutoff = floorCut > rangeCut ? floorCut : rangeCut;
    const rows = [];
    for (const job of report.jobs) {
      if (prefs.who === 'demo' && !job.demo) continue;
      if (prefs.who === 'client' && !job.client) continue;
      if (prefs.who === 'studio' && (job.demo || job.client)) continue;
      if (prefs.type !== 'all' && typeOf(job).key !== prefs.type) continue;
      if (prefs.machine !== 'all' && (job.machine || report.machine) !== prefs.machine) continue;
      const created = dayKey(job.created_at, prefs.zone);
      const byDay = new Map();
      for (const c of job.calls || []) {
        const key = dayKey(callTime(c), prefs.zone) || created;
        if (!byDay.has(key)) byDay.set(key, []);
        byDay.get(key).push(c);
      }
      if (!byDay.has(created)) byDay.set(created, []);
      for (const [day, calls] of byDay) {
        if (cutoff && day < cutoff) continue;
        const first = calls.length ? calls.map(callTime).sort()[0] : job.created_at;
        rows.push({ job, day, calls, made: day === created, at: day === created ? job.created_at : first });
      }
    }
    rows.sort((a, b) => (b.day.localeCompare(a.day) || b.at.localeCompare(a.at)));
    return { rows, cutoff };
  }

  /* ---------- render ---------- */
  function render() {
    if (!report) return;
    const types = new Map();
    for (const job of report.jobs) {
      const t = typeOf(job);
      types.set(t.key, t.label);
    }
    replace(typeSel, h('option', { value: 'all' }, 'All'),
      [...types].map(([key, label]) => h('option', { value: key }, label)));
    if (prefs.type !== 'all' && !types.has(prefs.type)) prefs.type = 'all';
    typeSel.value = prefs.type;
    const machines = [...new Set(report.jobs.map((j) => j.machine || report.machine))].sort();
    if (prefs.machine !== 'all' && !machines.includes(prefs.machine)) prefs.machine = 'all';
    replace(machineBox, machines.length > 1 ? segmented({
      legend: 'Machine', value: prefs.machine,
      options: [{ value: 'all', label: 'All' }, ...machines.map((m) => ({ value: m, label: machineName(m) }))],
      onChange: (v) => {
        prefs.machine = v;
        savePrefs();
        render(); // rebuilds this control: put the keyboard focus back on the chosen machine
        const chosen = machineBox.querySelector('input:checked');
        if (chosen) chosen.focus();
      },
    }).el : null);
    renderSync();

    const { rows, cutoff } = rowsFor();
    note.textContent = report.floor ? `Shows jobs from ${dayLabel(dayKey(report.floor, prefs.zone))} on.` : '';
    if (!rows.length) {
      replace(body, h('div', { class: 'empty' },
        h('div', { class: 'empty-icon' }, icon('chart')),
        h('p', { class: 'empty-title' }, 'No jobs in this period'),
        h('p', { class: 'empty-text' }, 'Jobs made on this server appear here with their tokens and costs.')));
      return;
    }

    // totals for the whole period
    const total = { jobs: 0, failedJobs: 0, demoJobs: 0, clientJobs: 0, unrecorded: 0, ourTokens: 0 };
    const callTotals = {};
    const byType = new Map();
    const byDay = new Map();
    for (const r of rows) {
      const s = sum(r.calls);
      addInto(callTotals, s);
      const dayT = byDay.get(r.day) || { jobs: 0, ai: 0, cost: 0 };
      dayT.ai += s.ai;
      dayT.cost += s.cost;
      if (r.made) {
        total.jobs += 1;
        dayT.jobs += 1;
        if (r.job.status === 'failed') total.failedJobs += 1;
        if (r.job.demo) total.demoJobs += 1;
        if (r.job.client) total.clientJobs += 1;
        if (!r.job.cost_recorded) total.unrecorded += 1;
        if (local(r)) total.ourTokens += r.job.tokens || 0; // on the day it was made; this server's purses only
      }
      byDay.set(r.day, dayT);
      const t = typeOf(r.job);
      const typeT = byType.get(t.key) || { label: t.label, jobs: 0, costedJobs: 0, ai: 0, cost: 0, ourTokens: 0 };
      typeT.jobs += r.made ? 1 : 0;
      typeT.ourTokens += r.made && local(r) ? r.job.tokens || 0 : 0;
      typeT.costedJobs += r.made && r.job.cost_recorded ? 1 : 0; // jobs from before 2026-11-03 have no cost
      typeT.ai += s.ai;
      typeT.cost += s.cost;
      byType.set(t.key, typeT);
    }
    if (total.unrecorded && !report.floor) {
      note.textContent = `${plural(total.unrecorded, 'job', 'jobs')} from before costs were recorded (2026-11-03) are listed without a cost.`;
    }

    replace(body,
      tiles(total, callTotals),
      chart(byDay, cutoff),
      tokensOn() ? clientTable(rows) : null,
      typeTable(byType),
      days(rows));
  }

  function tile(label, value, sub) {
    return h('div', { class: 'dash-tile' },
      h('div', { class: 'dash-tile-label' }, label),
      h('div', { class: 'dash-tile-value' }, value),
      sub ? h('div', { class: 'dash-tile-sub' }, sub) : null);
  }

  function tiles(total, t) {
    const perImage = t.ai ? t.image / t.ai : null;
    return h('div', { class: 'dash-tiles' },
      tile('Total cost', money(t.cost || 0),
        h('span', null, h('span', null, 'Image AI'), ` ${money(t.image || 0)} · `, h('span', null, 'AI designer'), ` ${money(t.designer || 0)}`)),
      tile('AI images', fmtInt(t.ai || 0), perImage !== null
        ? h('span', null, h('span', null, 'Average per image'), ` ${money(perImage)}`) : null),
      tile('Jobs', fmtInt(total.jobs), h('span', null, plural(total.failedJobs, 'failed', 'failed'), ' · ',
        plural(total.demoJobs, 'by the demo login', 'by the demo login'),
        total.clientJobs ? [' · ', plural(total.clientJobs, 'by a client login', 'by client logins')] : null)),
      tokensOn() ? tile('Our tokens', fmtInt(total.ourTokens), h('span', null, 'Charged to clients’ purses')) : null,
      tile('API tokens', h('span', null, fmtInt(t.input || 0), h('span', { class: 'dash-tile-unit' }, ' / '), fmtInt(t.output || 0)),
        h('span', null, h('span', null, 'Input'), ' / ', h('span', null, 'Output'))),
      tile('Failed attempts', fmtInt(t.failed || 0), t.uncosted
        ? plural(t.uncosted, 'call of unknown cost', 'calls of unknown cost') : h('span', null, 'No charge recorded')));
  }

  /* One series: cost per day, as bars (no legend: the title names it). Hover or focus a day for its numbers. */
  function chart(byDay, cutoff) {
    const keys = [...byDay.keys()].sort();
    const start = cutoff || keys[0];
    const end = dayKey(new Date().toISOString(), prefs.zone);
    const all = [];
    for (let t = Date.parse(`${start}T00:00:00Z`); t <= Date.parse(`${end > keys[keys.length - 1] ? end : keys[keys.length - 1]}T00:00:00Z`); t += DAY_MS) {
      all.push(new Date(t).toISOString().slice(0, 10));
    }
    const max = Math.max(...all.map((k) => (byDay.get(k) || {}).cost || 0), 0);
    const tip = h('div', { class: 'dash-tip', hidden: true, role: 'status' });
    const bars = h('div', { class: 'dash-bars', style: { '--n': all.length } }, all.map((k) => {
      const d = byDay.get(k) || { jobs: 0, ai: 0, cost: 0 };
      const pct = max ? Math.max(d.cost ? 1.5 : 0, (d.cost / max) * 100) : 0;
      const text = [dayLabel(k), money(d.cost), plural(d.ai, 'AI image', 'AI images'), plural(d.jobs, 'job', 'jobs')];
      const col = h('div', { class: 'dash-bar-col', tabindex: '0', 'aria-label': text.join(' · ') }, // parts translate
        h('div', { class: 'dash-bar', style: { height: `${pct}%` } }));
      const show = () => {
        replace(tip, h('strong', null, text[0]), h('span', null, text[1]), h('span', null, text[2]), h('span', null, text[3]));
        tip.hidden = false;
        const box = bars.getBoundingClientRect();
        const c = col.getBoundingClientRect();
        tip.style.left = `${Math.min(Math.max(c.left - box.left + c.width / 2, 70), box.width - 70)}px`;
      };
      col.addEventListener('mouseenter', show);
      col.addEventListener('focus', show);
      col.addEventListener('mouseleave', () => { tip.hidden = true; });
      col.addEventListener('blur', () => { tip.hidden = true; });
      return col;
    }));
    const tick = (k) => h('span', null, dayLabel(k, { day: 'numeric', month: 'short' }));
    return h('section', { class: 'card dash-chart' },
      h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'Cost per day'),
        h('span', { class: 'muted' }, max ? h('span', null, h('span', null, 'Highest'), ` ${money(max)}`) : null)),
      h('div', { class: 'card-body' },
        h('div', { class: 'dash-plot' }, bars, tip),
        h('div', { class: 'dash-axis' }, tick(all[0]), all.length > 2 ? tick(all[Math.floor(all.length / 2)]) : null, all.length > 1 ? tick(all[all.length - 1]) : null)));
  }

  function typeTable(byType) {
    const rows = [...byType.values()].sort((a, b) => b.cost - a.cost);
    return h('section', { class: 'card' },
      h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'By output type')),
      h('div', { class: 'card-body dash-scroll' }, h('table', { class: 'dash-table' },
        h('thead', null, h('tr', null, ['Output type', 'Jobs', 'AI images', 'Cost', tokensOn() ? 'Our tokens' : null,
          'Average per job', 'Average per image'].filter((t) => t !== null)
          .map((t, i) => h('th', { class: i ? 'num' : null }, t)))),
        h('tbody', null, rows.map((r) => h('tr', null,
          h('td', null, r.label),
          h('td', { class: 'num' }, fmtInt(r.jobs)),
          h('td', { class: 'num' }, fmtInt(r.ai)),
          h('td', { class: 'num' }, fmtUsd(r.cost) || '$0'),
          tokensOn() ? h('td', { class: 'num' }, r.ourTokens ? fmtInt(r.ourTokens) : '–') : null,
          h('td', { class: 'num' }, r.costedJobs ? money(r.cost / r.costedJobs) : '–'),
          h('td', { class: 'num' }, r.ai ? money(r.cost / r.ai) : '–')))))));
  }

  function days(rows) {
    const groups = new Map();
    for (const r of rows) {
      if (!groups.has(r.day)) groups.set(r.day, []);
      groups.get(r.day).push(r);
    }
    return h('div', { class: 'dash-days' }, [...groups].map(([day, list]) => {
      const t = list.reduce((acc, r) => addInto(acc, sum(r.calls)), {});
      const made = list.filter((r) => r.made).length;
      return h('section', { class: 'card dash-day' },
        h('div', { class: 'card-header' },
          h('h3', { class: 'card-title' }, dayLabel(day)),
          h('span', { class: 'muted' }, plural(made, 'job', 'jobs'), ' · ', plural(t.ai || 0, 'AI image', 'AI images'), ' · ',
            fmtUsd(t.cost || 0) || '$0', ' · ', h('span', null, 'API tokens'), ` ${fmtInt(t.input || 0)} / ${fmtInt(t.output || 0)}`,
            ourOf(list) ? [' · ', h('span', null, 'Our tokens'), ` ${fmtInt(ourOf(list))}`] : null)),
        h('div', { class: 'card-body dash-scroll' }, h('table', { class: 'dash-table' },
          h('thead', null, h('tr', null,
            ['Time', 'Job', 'Output type', 'Status', 'Model', 'AI images', 'Input tokens', 'Output tokens', 'Cost', tokensOn() ? 'Our tokens' : null, '']
              .filter((label) => label !== null)
              .map((label, i, all) => h('th', { class: i >= 5 && i < all.length - 1 ? 'num' : null }, label)))),
          byChat(list).map((g) => (isChat(g[0]) ? chatRows(g) : jobRows(g[0]))))));
    }));
  }

  /* ---------- Design chats (2026-11-23): a chat's messages on one day nest under one row ---------- */
  const isChat = (r) => r.job.kind === 'design' && Boolean(r.job.chat);

  /** A day's rows (newest first) with each Design chat's messages together, where its latest message is. */
  function byChat(list) {
    const out = [];
    const chats = new Map();
    for (const r of list) {
      if (!isChat(r)) out.push([r]);
      else if (chats.has(r.job.chat)) chats.get(r.job.chat).push(r);
      else {
        const group = [r];
        chats.set(r.job.chat, group);
        out.push(group);
      }
    }
    return out;
  }

  /** A chat's status from its messages': working while one is, else done / partly done / failed. */
  function chatStatus(statuses) {
    if (statuses.some((s) => s === 'queued' || s === 'running')) return 'running';
    const ok = statuses.filter((s) => s === 'succeeded').length;
    return ok === statuses.length ? 'succeeded' : ok || statuses.includes('partial') ? 'partial' : 'failed';
  }

  /** The cost cell of `s` (sum of calls): exact dollars, and the calls of unknown cost. */
  function costCell(s, unrecorded) {
    if (unrecorded) return h('span', { class: 'muted' }, 'Not recorded');
    return h('span', null, fmtUsd(s.cost || 0) || '$0',
      s.uncosted ? h('span', { class: 'muted' }, ` + ${plural(s.uncosted, 'unknown', 'unknown')}`) : null);
  }

  /** A Design chat on one day: one row with its totals (the AI designer's and the images' cost apart) that opens
   * its messages, oldest first: the client's words, the designer's reply, each message's cost and our tokens. */
  function chatRows(group) {
    const msgs = [...group].sort((a, b) => a.job.id.localeCompare(b.job.id)); // ids sort by creation, to the ms
    const first = msgs[0];
    const last = msgs[msgs.length - 1];
    const key = `chat:${first.job.chat}|${first.day}`;
    const open = expanded.has(key);
    const s = msgs.reduce((acc, r) => addInto(acc, sum(r.calls)), {});
    const shown = [...msgs].reverse().find((r) => r.job.thumb && !r.job.deleted);
    const named = msgs.find((r) => r.job.title);
    const asks = msgs.filter((r) => r.job.refine).length;
    const charged = msgs.filter((r) => r.made && r.job.client && typeof r.job.tokens === 'number');
    const client = (msgs.find((r) => r.job.client) || first).job.client;
    const deleted = msgs.filter((r) => r.job.deleted).length;
    const badges = [
      msgs.some((r) => r.job.demo) ? h('span', { class: 'badge' }, 'Demo login') : null,
      client ? h('span', { class: 'badge' }, `Client: ${client}`) : null,
      deleted ? h('span', { class: 'badge badge-danger' }, `${deleted} deleted`) : null,
      (first.job.machine || report.machine) !== report.machine ? h('span', { class: 'badge' }, machineName(first.job.machine)) : null,
    ].filter(Boolean);
    const thumb = shown
      ? h('img', { class: 'dash-thumb', src: fileUrl(shown.job.id, shown.job.thumb), alt: '', loading: 'lazy' })
      : h('span', { class: 'dash-thumb is-empty', 'aria-hidden': 'true' });
    const toggle = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', 'aria-expanded': String(open) },
      icon('chevron', open ? 'chev is-open' : 'chev'), `Messages (${msgs.length})`);
    const main = h('tr', { class: ['dash-job', 'dash-chat', open && 'is-open'] },
      h('td', { class: 'nowrap' }, clockTime(first.at, prefs.zone),
        clockTime(last.at, prefs.zone) !== clockTime(first.at, prefs.zone)
          ? h('div', { class: 'dash-sub' }, `to ${clockTime(last.at, prefs.zone)}`) : null),
      h('td', null, h('div', { class: 'dash-jobcell' }, thumb, h('div', null,
        h('div', { class: 'dash-chat-title', 'data-no-translate': '' }, named ? named.job.title : 'Design chat'),
        h('div', { class: 'dash-sub' }, plural(msgs.length, 'message', 'messages'),
          asks ? ` · ${plural(asks, 'change request', 'change requests')}` : ''),
        badges.length ? h('div', { class: 'dash-badges' }, badges) : null))),
      h('td', null, h('div', null, 'Design from description'), h('div', { class: 'dash-sub' }, 'Chat')),
      h('td', null, statusBadge(chatStatus(msgs.map((r) => r.job.status)))),
      h('td', null, last.job.model ? modelLabel(last.job.provider, last.job.model) : '',
        last.job.resolution ? h('div', { class: 'dash-sub' }, last.job.resolution) : null),
      h('td', { class: 'num' }, fmtInt(s.ai || 0), s.failed ? h('div', { class: 'dash-sub' }, plural(s.failed, 'failed', 'failed')) : null),
      h('td', { class: 'num' }, s.input ? fmtInt(s.input) : '–'),
      h('td', { class: 'num' }, s.output ? fmtInt(s.output) : '–'),
      h('td', { class: 'num' }, costCell(s, false),
        h('div', { class: 'dash-sub' }, `AI designer ${fmtUsd(s.designer || 0) || '$0'} · Images ${fmtUsd(s.image || 0) || '$0'}`)),
      tokensOn() ? h('td', { class: 'num' }, charged.length ? fmtInt(charged.filter(local).reduce((acc, r) => acc + r.job.tokens, 0)) : '–') : null,
      h('td', { class: 'nowrap' }, toggle));
    const detail = h('tr', { class: 'dash-calls', hidden: !open }, h('td', { colspan: tokensOn() ? '11' : '10' }, open ? messagesTable(msgs) : null));
    toggle.addEventListener('click', () => {
      const now = !expanded.has(key);
      if (now) expanded.add(key);
      else expanded.delete(key);
      toggle.setAttribute('aria-expanded', String(now));
      toggle.querySelector('.chev').classList.toggle('is-open', now);
      main.classList.toggle('is-open', now);
      detail.hidden = !now;
      replace(detail.firstChild, now ? messagesTable(msgs) : null);
    });
    return h('tbody', null, main, detail);
  }

  function messagesTable(msgs) {
    return h('table', { class: 'dash-table dash-calltable dash-msgtable' },
      h('thead', null, h('tr', null, ['Time', 'Design', 'Message', 'AI designer', 'Image', 'Cost', tokensOn() ? 'Our tokens' : null, 'Status', '']
        .filter((label) => label !== null)
        .map((label, i) => h('th', { class: i >= 3 && i <= (tokensOn() ? 6 : 5) ? 'num' : null }, label)))),
      msgs.map((r) => messageRows(r)));
  }

  /** One message of a chat: its words, what its AI designer and its image cost, our tokens, and its calls. */
  function messageRows(r) {
    const { job } = r;
    const s = sum(r.calls);
    const key = `${job.id}|${r.day}`;
    const open = expanded.has(key);
    const remote = (job.machine || report.machine) !== report.machine;
    let words;
    if (job.deleted) words = h('span', { class: 'muted' }, 'Deleted in History');
    else if (remote) words = h('span', { class: 'muted' }, `The words stay on the ${machineName(job.machine)}`);
    else {
      words = [
        job.message ? h('div', { class: 'dash-msg', 'data-no-translate': '' }, job.message) : null,
        job.reply ? h('div', { class: 'dash-reply' }, h('span', null, 'Designer: '), h('span', { 'data-no-translate': '' }, job.reply)) : null,
      ];
    }
    const thumb = job.thumb && !job.deleted
      ? h('img', { class: 'dash-thumb', src: fileUrl(job.id, job.thumb), alt: '', loading: 'lazy' })
      : h('span', { class: 'dash-thumb is-empty', 'aria-hidden': 'true' });
    const callsBtn = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', 'aria-expanded': String(open) },
      icon('chevron', open ? 'chev is-open' : 'chev'), `Calls (${r.calls.length})`);
    callsBtn.disabled = !r.calls.length;
    const openBtn = !job.deleted && !remote && onOpenJob
      ? h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onClick: () => onOpenJob(job.id) }, 'Details') : null;
    const main = h('tr', { class: ['dash-msgrow', open && 'is-open'] },
      h('td', { class: 'nowrap' }, clockTime(r.at, prefs.zone)),
      h('td', null, thumb),
      h('td', { class: 'dash-msgcell' },
        h('div', { class: 'dash-badges' }, h('span', { class: 'badge' }, job.refine ? 'Change request' : 'New design'),
          !r.made ? h('span', { class: 'badge badge-accent' }, 'Continued from the day before') : null),
        words),
      h('td', { class: 'num' }, s.designerCalls ? fmtUsd(s.designer) || '$0' : '–'),
      h('td', { class: 'num' }, s.ai || s.failed ? fmtUsd(s.image) || '$0' : '–',
        s.failed ? h('div', { class: 'dash-sub' }, plural(s.failed, 'failed attempt', 'failed attempts')) : null),
      h('td', { class: 'num' }, costCell(s, !job.cost_recorded && r.made)),
      tokensOn() ? h('td', { class: 'num' }, ourCell(job, r.made)) : null,
      h('td', null, job.status ? statusBadge(job.status) : null),
      h('td', { class: 'nowrap' }, callsBtn, openBtn));
    const detail = h('tr', { class: 'dash-calls', hidden: !open }, h('td', { colspan: tokensOn() ? '9' : '8' }, open ? callsTable(r.calls) : null));
    callsBtn.addEventListener('click', () => {
      const now = !expanded.has(key);
      if (now) expanded.add(key);
      else expanded.delete(key);
      callsBtn.setAttribute('aria-expanded', String(now));
      callsBtn.querySelector('.chev').classList.toggle('is-open', now);
      main.classList.toggle('is-open', now);
      detail.hidden = !now;
      replace(detail.firstChild, now ? callsTable(r.calls) : null);
    });
    return h('tbody', null, main, detail);
  }

  function typeCell(job) {
    const t = typeOf(job);
    const parts = [h('div', null, t.label)];
    if (job.mode === 'panels' && Array.isArray(job.panels)) {
      const items = [];
      job.panels.forEach((p, i) => {
        if (i) items.push(' · ');
        items.push(h('span', null, PANEL_NAMES[p] || p));
        if (p === 'back' && job.panels.includes('front') && BACK_METHODS[job.back]) {
          items.push(' (', h('span', null, BACK_METHODS[job.back]), ')');
        }
      });
      parts.push(h('div', { class: 'dash-sub' }, items));
    } else if (job.kind === 'design' && job.refine) {
      parts.push(h('div', { class: 'dash-sub' }, 'Refinement'));
    }
    return h('td', null, parts);
  }

  function jobRows(r) {
    const { job } = r;
    const s = sum(r.calls);
    const key = `${job.id}|${r.day}`;
    const open = expanded.has(key);
    const badges = [
      job.demo ? h('span', { class: 'badge' }, 'Demo login') : null,
      job.client ? h('span', { class: 'badge' }, `Client: ${job.client}`) : null, // short: the column stays narrow
      job.deleted ? h('span', { class: 'badge badge-danger' }, 'Deleted') : null,
      !r.made ? h('span', { class: 'badge badge-accent' }, 'Generated again') : null,
      (job.machine || report.machine) !== report.machine ? h('span', { class: 'badge' }, machineName(job.machine)) : null,
    ].filter(Boolean);
    const thumb = job.thumb && !job.deleted
      ? h('img', { class: 'dash-thumb', src: fileUrl(job.id, job.thumb), alt: '', loading: 'lazy' })
      : h('span', { class: 'dash-thumb is-empty', 'aria-hidden': 'true' });
    const callsBtn = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', 'aria-expanded': String(open) },
      icon('chevron', open ? 'chev is-open' : 'chev'), `Calls (${r.calls.length})`);
    callsBtn.disabled = !r.calls.length;
    const openBtn = !job.deleted && (job.machine || report.machine) === report.machine && onOpenJob
      ? h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onClick: () => onOpenJob(job.id) }, 'Details') : null;
    const cost = costCell(s, !job.cost_recorded && r.made);
    const main = h('tr', { class: ['dash-job', open && 'is-open'] },
      h('td', { class: 'nowrap' }, clockTime(r.at, prefs.zone)),
      h('td', null, h('div', { class: 'dash-jobcell' }, thumb, h('div', null,
        h('code', { class: 'dash-id', 'data-no-translate': '' }, job.id), badges.length ? h('div', { class: 'dash-badges' }, badges) : null))),
      typeCell(job),
      h('td', null, job.status ? statusBadge(job.status) : null),
      h('td', null, job.model ? modelLabel(job.provider, job.model) : '', job.resolution ? h('div', { class: 'dash-sub' }, job.resolution) : null),
      h('td', { class: 'num' }, fmtInt(s.ai), s.failed ? h('div', { class: 'dash-sub' }, plural(s.failed, 'failed', 'failed')) : null),
      h('td', { class: 'num' }, s.input ? fmtInt(s.input) : '–'),
      h('td', { class: 'num' }, s.output ? fmtInt(s.output) : '–'),
      h('td', { class: 'num' }, cost),
      tokensOn() ? h('td', { class: 'num' }, ourCell(job, r.made)) : null,
      h('td', { class: 'nowrap' }, callsBtn, openBtn));
    const detail = h('tr', { class: 'dash-calls', hidden: !open }, h('td', { colspan: tokensOn() ? '11' : '10' }, open ? callsTable(r.calls) : null));
    callsBtn.addEventListener('click', () => {
      const now = !expanded.has(key);
      if (now) expanded.add(key);
      else expanded.delete(key);
      callsBtn.setAttribute('aria-expanded', String(now));
      callsBtn.querySelector('.chev').classList.toggle('is-open', now);
      main.classList.toggle('is-open', now);
      detail.hidden = !now;
      replace(detail.firstChild, now ? callsTable(r.calls) : null);
    });
    return h('tbody', null, main, detail);
  }

  function callsTable(calls) {
    return h('table', { class: 'dash-table dash-calltable' },
      h('thead', null, h('tr', null, ['Time', 'Call', 'Model', 'Size', 'Result', 'Took', 'Input tokens', 'Output tokens', 'Cost']
        .map((label, i) => h('th', { class: i >= 5 ? 'num' : null }, label)))),
      h('tbody', null, calls.map((c) => {
        const k = c.tokens || {};
        const input = (k.input || 0) + (k.cache_read || 0) + (k.cache_write || 0);
        const split = k.input_text !== undefined || k.input_image !== undefined
          ? h('div', { class: 'dash-sub' }, h('span', null, 'Text'), ` ${fmtInt(k.input_text || 0)} · `, h('span', null, 'Pictures'), ` ${fmtInt(k.input_image || 0)}`)
          : null;
        const what = [h('span', null, PURPOSES[c.purpose] || c.purpose || '')];
        if (c.label) what.push(': ', h('span', null, c.label));
        const result = [c.status === 'ok' ? h('span', { class: 'badge badge-success' }, 'OK') : h('span', { class: 'badge badge-danger' }, 'Failed')];
        if (c.attempt > 1) result.push(h('div', { class: 'dash-sub' }, `Attempt ${c.attempt}`));
        if (c.trigger === 'retry') result.push(h('div', { class: 'dash-sub' }, 'Generated again'));
        if (c.status !== 'ok' && c.error) result.push(h('div', { class: 'dash-sub dash-error' }, c.error));
        return h('tr', null,
          h('td', { class: 'nowrap' }, clockTime(callTime(c), prefs.zone)),
          h('td', null, what),
          h('td', null, c.purpose === 'image' ? modelLabel(c.provider, c.model) : h('code', { 'data-no-translate': '' }, c.model || '')),
          h('td', { class: 'nowrap' }, c.size || ''),
          h('td', null, result),
          h('td', { class: 'num nowrap' }, fmtSeconds(c.latency_s)),
          h('td', { class: 'num' }, input ? fmtInt(input) : '–', split),
          h('td', { class: 'num' }, k.output ? fmtInt(k.output) : '–'),
          h('td', { class: 'num' }, typeof c.cost_usd === 'number' ? (fmtUsd(c.cost_usd) || '$0') : h('span', { class: 'muted' }, 'Unknown')));
      })));
  }

  /* ---------- our tokens (app/tokens.py) ---------- */
  function tokensOn() {
    return Boolean(report && report.tokens && report.tokens.enabled);
  }

  /** A row of this machine's own job (each machine has its own purses: another's tokens aren't this server's). */
  function local(r) {
    return (r.job.machine || report.machine) === report.machine;
  }

  /** The tokens charged for the jobs made in `list` (rows), to this machine's purses. */
  function ourOf(list) {
    return list.reduce((acc, r) => acc + (r.made && local(r) ? r.job.tokens || 0 : 0), 0);
  }

  /** A job's tokens with what they were for in the tooltip ("Front N · Sleeve N"); on the row of the day it was made. */
  function ourCell(job, made) {
    if (!made || !job.tokens && job.tokens !== 0 || !job.client) return '–';
    const parts = (job.token_items || []).map((i) => `${TOKEN_ITEMS[i.item] || i.item} ${i.tokens}`);
    return h('span', { title: parts.join(' · ') }, fmtInt(job.tokens)); // " · " parts translate one by one
  }

  /** Each client in the period: jobs, our tokens and the real cost behind them, and its purse now. */
  function clientTable(rows) {
    const by = new Map((report.tokens.clients || []).map((c) => [c.slug, { ...c, jobs: 0, tokens: 0, cost: 0 }]));
    for (const r of rows) {
      if (!r.job.client || !local(r)) continue; // the balances are this server's: so are the tokens beside them
      const row = by.get(r.job.client) || { slug: r.job.client, name: r.job.client, balance: null, jobs: 0, tokens: 0, cost: 0 };
      row.cost += sum(r.calls).cost;
      if (r.made) {
        row.jobs += 1;
        row.tokens += r.job.tokens || 0;
      }
      by.set(r.job.client, row);
    }
    const list = [...by.values()].sort((a, b) => b.tokens - a.tokens || a.name.localeCompare(b.name));
    if (!list.length) return null;
    return h('section', { class: 'card' },
      h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'By client'),
        h('a', { class: 'btn btn-sm btn-ghost', href: '/client-dashboard' }, 'Client dashboard')),
      h('div', { class: 'card-body dash-scroll' }, h('table', { class: 'dash-table' },
        h('thead', null, h('tr', null, ['Client', 'Jobs', 'Our tokens', 'Taka received', 'Real cost', 'Real cost per 100 tokens', 'Balance now']
          .map((t, i) => h('th', { class: i ? 'num' : null }, t)))),
        h('tbody', null, list.map((c) => h('tr', null,
          h('td', { 'data-no-translate': '' }, c.slug ? h('a', { href: `/client-dashboard#client=${c.slug}` }, c.name) : c.name),
          h('td', { class: 'num' }, fmtInt(c.jobs)),
          h('td', { class: 'num' }, fmtInt(c.tokens)),
          h('td', { class: 'num' }, c.taka ? `৳${fmtInt(c.taka)}` : '–'), // what the client paid us in the period
          h('td', { class: 'num' }, fmtUsd(c.cost) || '$0'),
          h('td', { class: 'num' }, c.tokens ? money((c.cost / c.tokens) * 100) : '–'),
          h('td', { class: 'num' }, c.balance === null ? '–' : fmtInt(c.balance))))))));
  }

  /* ---------- CSV: one row per paid call in the period and filters shown ---------- */
  function downloadCsv() {
    if (!report) return;
    const { rows } = rowsFor();
    const head = ['day', 'time_utc', 'machine', 'job_id', 'chat_id', 'output_type', 'panels', 'back', 'demo_login', 'client_login',
      'job_our_tokens', 'deleted', 'purpose', 'panel',
      'provider', 'model', 'size', 'status', 'attempt', 'trigger', 'latency_s', 'input_tokens', 'input_text_tokens',
      'input_image_tokens', 'output_tokens', 'cost_usd'];
    const cell = (v) => {
      const s = v === null || v === undefined ? '' : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const lines = [head.join(',')];
    for (const r of rows) {
      let tokensHere = r.made; // the job's tokens on one row only, so the column sums right
      for (const c of r.calls) {
        const k = c.tokens || {};
        lines.push([r.day, callTime(c), r.job.machine || report.machine, r.job.id, r.job.chat || '',
          r.job.kind === 'design' ? 'design' : r.job.mode,
          (r.job.panels || []).join('+'), r.job.mode === 'panels' ? r.job.back : '', r.job.demo ? 'yes' : 'no', r.job.client || '',
          tokensHere ? r.job.tokens ?? '' : '', r.job.deleted ? 'yes' : 'no', c.purpose,
          c.panel, c.provider, c.model, c.size, c.status, c.attempt, c.trigger, c.latency_s,
          (k.input || 0) + (k.cache_read || 0) + (k.cache_write || 0), k.input_text, k.input_image, k.output, c.cost_usd]
          .map(cell).join(','));
        tokensHere = false;
      }
    }
    if (lines.length === 1) {
      say('There are no paid calls in this period.', 'warning');
      return;
    }
    const days = rows.map((r) => r.day).sort();
    const url = URL.createObjectURL(new Blob([`${lines.join('\n')}\n`], { type: 'text/csv' }));
    const a = h('a', { href: url, download: `jersey-usage-${days[0]}-to-${days[days.length - 1]}.csv`, hidden: true });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return {
    activate() {
      loadSite();
      if (!loading) load();
    },
  };
}
