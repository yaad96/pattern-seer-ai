/**
 * Dashboard tab (studio page, since 2026-11-08): what was made each day and what it cost, from GET /api/admin/usage
 * (app/usage.py): every job and every paid API call with its tokens and cost, jobs deleted in History included (the
 * usage ledger). Days are UTC by default, like OpenAI's bill, or this computer's time. A job retried on a later day
 * appears on both days, each row with that day's calls, so a day's total is exactly what was spent that day.
 */
import { api, fileUrl, getConfig } from './api.js';
import { h, icon, replace, fmtDateTime, fmtInt, fmtSeconds, fmtUsd, storage } from './dom.js';
import { statusBadge } from './jobs.js';
import { modelLabel } from './results.js';
import { callout, confirmDialog, loadingRow, segmented, setBusy, toast } from './ui.js';

const PREFS_KEY = 'dashboard.prefs';
const DAY_MS = 86400000;
const PANEL_NAMES = { front: 'Front', back: 'Back', sleeve: 'Sleeve' };
const BACK_METHODS = { mirror: 'Mirror of the front', copy: 'Copy of the front', image: 'Back picture' };
const PURPOSES = { image: 'Image', brief: 'Design brief', reference_check: 'Reference check' };

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

/** The dashboard's name for a job's output type, and its filter key. */
function typeOf(job) {
  if (job.kind === 'design') return { key: 'design', label: 'Design from description' };
  const modes = (getConfig().catalog.recreate_modes || []);
  const mode = modes.find((m) => m.value === job.mode);
  return { key: job.mode || 'recreate', label: mode ? mode.label : (job.mode || 'Recreate from image') };
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
  const saved = storage.get(PREFS_KEY, {}) || {};
  const prefs = { range: '30', zone: 'utc', who: 'all', type: 'all', ...saved };
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
  const who = control('Login', 'who', [['all', 'All'], ['studio', 'Studio login'], ['demo', 'Demo login']], false);
  const typeSel = h('select', { class: 'select', 'aria-label': 'Output type' });
  typeSel.addEventListener('change', () => { prefs.type = typeSel.value; savePrefs(); render(); });
  const refreshBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('refresh'), 'Refresh');
  refreshBtn.addEventListener('click', () => load());
  const csvBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('download'), 'Download CSV');
  csvBtn.addEventListener('click', () => downloadCsv());
  const note = h('p', { class: 'hint' });
  const body = h('div', { class: 'dash-body' });
  const siteBox = h('section', { class: 'card dash-site' }, h('div', { class: 'card-body' }, loadingRow('Loading…')));

  replace(root,
    h('div', { class: 'history-toolbar' }, h('h2', null, 'Dashboard'), h('span', { class: 'spacer' }), refreshBtn, csvBtn),
    siteBox,
    h('div', { class: 'dash-filters' }, range.el, zone.el, who.el, h('label', { class: 'dash-type' }, h('span', { class: 'field-label' }, 'Output type'), typeSel)),
    note, body);

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
          ? 'Only your studio login can use the site. Everyone else, the demo link included, gets a “page not found” (404) error, as if the site weren’t there.'
          : 'Everyone can use the site, the demo link included. Closing it shows them a “page not found” (404) error until you open it again here.'),
        h('p', { class: 'hint' }, 'This switch controls this server only.')),
      btn));
  }

  async function switchSite(close, btn) {
    if (siteBusy) return;
    if (close && !(await confirmDialog({
      title: 'Close the site?',
      message: 'The demo link and everyone not signed in as studio will get a “page not found” (404) error until you open the site again here. Images being made right now still finish.',
      confirmLabel: 'Close the site',
      danger: true,
    }))) return;
    siteBusy = true;
    setBusy(btn, true, close ? 'Closing…' : 'Opening…');
    try {
      renderSite(await api.setSiteClosed(close));
      toast(close ? 'The site is closed.' : 'The site is open again.', { kind: close ? 'warning' : 'success' });
    } catch (err) {
      setBusy(btn, false);
      toast(err.message, { kind: 'error' });
    } finally {
      siteBusy = false;
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
      if (prefs.who === 'studio' && job.demo) continue;
      if (prefs.type !== 'all' && typeOf(job).key !== prefs.type) continue;
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
    const total = { jobs: 0, failedJobs: 0, demoJobs: 0, unrecorded: 0 };
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
        if (!r.job.cost_recorded) total.unrecorded += 1;
      }
      byDay.set(r.day, dayT);
      const t = typeOf(r.job);
      const typeT = byType.get(t.key) || { label: t.label, jobs: 0, costedJobs: 0, ai: 0, cost: 0 };
      typeT.jobs += r.made ? 1 : 0;
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
        plural(total.demoJobs, 'by the demo login', 'by the demo login'))),
      tile('Tokens', h('span', null, fmtInt(t.input || 0), h('span', { class: 'dash-tile-unit' }, ' / '), fmtInt(t.output || 0)),
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
        h('thead', null, h('tr', null, ['Output type', 'Jobs', 'AI images', 'Cost', 'Average per job', 'Average per image']
          .map((t, i) => h('th', { class: i ? 'num' : null }, t)))),
        h('tbody', null, rows.map((r) => h('tr', null,
          h('td', null, r.label),
          h('td', { class: 'num' }, fmtInt(r.jobs)),
          h('td', { class: 'num' }, fmtInt(r.ai)),
          h('td', { class: 'num' }, fmtUsd(r.cost) || '$0'),
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
            fmtUsd(t.cost || 0) || '$0', ' · ', h('span', null, 'Tokens'), ` ${fmtInt(t.input || 0)} / ${fmtInt(t.output || 0)}`)),
        h('div', { class: 'card-body dash-scroll' }, h('table', { class: 'dash-table' },
          h('thead', null, h('tr', null,
            ['Time', 'Job', 'Output type', 'Status', 'Model', 'AI images', 'Input tokens', 'Output tokens', 'Cost', '']
              .map((label, i) => h('th', { class: i >= 5 && i <= 8 ? 'num' : null }, label)))),
          list.map((r) => jobRows(r)))));
    }));
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
      job.deleted ? h('span', { class: 'badge badge-danger' }, 'Deleted') : null,
      !r.made ? h('span', { class: 'badge badge-accent' }, 'Generated again') : null,
    ].filter(Boolean);
    const thumb = job.thumb && !job.deleted
      ? h('img', { class: 'dash-thumb', src: fileUrl(job.id, job.thumb), alt: '', loading: 'lazy' })
      : h('span', { class: 'dash-thumb is-empty', 'aria-hidden': 'true' });
    const callsBtn = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', 'aria-expanded': String(open) },
      icon('chevron', open ? 'chev is-open' : 'chev'), `Calls (${r.calls.length})`);
    callsBtn.disabled = !r.calls.length;
    const openBtn = !job.deleted && onOpenJob
      ? h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onClick: () => onOpenJob(job.id) }, 'Details') : null;
    let cost;
    if (!job.cost_recorded && r.made) cost = h('span', { class: 'muted' }, 'Not recorded');
    else cost = h('span', null, fmtUsd(s.cost) || '$0', s.uncosted ? h('span', { class: 'muted' }, ` + ${plural(s.uncosted, 'unknown', 'unknown')}`) : null);
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
      h('td', { class: 'nowrap' }, callsBtn, openBtn));
    const detail = h('tr', { class: 'dash-calls', hidden: !open }, h('td', { colspan: '10' }, open ? callsTable(r.calls) : null));
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

  /* ---------- CSV: one row per paid call in the period and filters shown ---------- */
  function downloadCsv() {
    if (!report) return;
    const { rows } = rowsFor();
    const head = ['day', 'time_utc', 'job_id', 'output_type', 'panels', 'back', 'demo_login', 'deleted', 'purpose', 'panel',
      'provider', 'model', 'size', 'status', 'attempt', 'trigger', 'latency_s', 'input_tokens', 'input_text_tokens',
      'input_image_tokens', 'output_tokens', 'cost_usd'];
    const cell = (v) => {
      const s = v === null || v === undefined ? '' : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const lines = [head.join(',')];
    for (const r of rows) {
      for (const c of r.calls) {
        const k = c.tokens || {};
        lines.push([r.day, callTime(c), r.job.id, r.job.kind === 'design' ? 'design' : r.job.mode, (r.job.panels || []).join('+'),
          r.job.mode === 'panels' ? r.job.back : '', r.job.demo ? 'yes' : 'no', r.job.deleted ? 'yes' : 'no', c.purpose,
          c.panel, c.provider, c.model, c.size, c.status, c.attempt, c.trigger, c.latency_s,
          (k.input || 0) + (k.cache_read || 0) + (k.cache_write || 0), k.input_text, k.input_image, k.output, c.cost_usd]
          .map(cell).join(','));
      }
    }
    if (lines.length === 1) {
      toast('There are no paid calls in this period.', { kind: 'warning' });
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
