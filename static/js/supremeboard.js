/**
 * The Supreme dashboard (/supreme-dashboard, 2026-11-24; the owner: "only i get to see it. It has everything"): the
 * head admin's own page. The Partner dashboard's four tabs (boards.js: To do, Clients, Payments, Jobs), here with
 * what only the head admin sees (the real cost per client, taking tokens back, every login's jobs with their API
 * calls), plus two of its own:
 *   History     every job with everything about it (history.js, scope "studio"): pictures, downloads, exports, the
 *               prompts, the API calls, the tokens it cost its client, Delete. It was the main page's third tab until
 *               2026-11-25 (the owner: "in supreme dashboard, move the history inside it"); /#history and
 *               /?job=<id>#history lead here. A click on a job's row in Jobs or on a client's page opens that job's
 *               view here too (the owner: "when I click on it, the expanded history component should be seen")
 *   LLM Costs   what OpenAI and Claude charged: the tiles, cost per day, by output type (dashboard.js)
 *   Settings    the site switch, the bKash number and packages clients buy, the Google Sheet and its script, the
 *               token prices, the disk
 * and "Open the Google Sheet" at the top. In English, without the Bangla layer.
 */
import { api, loadConfig } from './api.js';
import { h, icon, replace, fmtDateTime, fmtInt, storage } from './dom.js';
import { card, startBoard, taka } from './boards.js';
import { initDashboard, sum } from './dashboard.js';
import { initHistory } from './history.js';
import { pricesParts } from './tokenviews.js';
import { callout, copyText, field, inputEl, loadingRow, modal, setBusy, toast } from './ui.js';

const GB = 1e9;
const DAY_MS = 86400000;
const JOB_ID_RE = /^\d{8}-\d{6}-[0-9a-f]{6}$/;
const config = loadConfig(); // the model names in the Jobs tab (ids until it has loaded), and what History needs
config.catch(() => {});

let board = null;
let report = null; // GET /api/admin/usage, as dashboard.js last read it
let reportAt = 0; // when (ms)

/** Read the report again when it is older than `ms`: a tab shows how things stand now, not when the page was opened. */
function freshen(ms) {
  if (Date.now() - reportAt > ms) dash.reload();
}

/* ---------- History: every job, with everything about it (history.js; the main page's tab until 2026-11-25) ---------- */
const historyEl = h('div', null, loadingRow('Loading…'));
let historyView = null;

/** A job deleted here: the image tools' pages forget it too (they would only say it no longer exists), and the
 * tables that list it are read again. */
function onDeleted(jobId) {
  for (const key of ['recreate.lastJob', 'panels.lastJob']) {
    if (storage.get(key, null) === jobId) storage.remove(key);
  }
  dash.reload();
  if (board) board.reload();
}

async function ensureHistory() {
  if (!historyView) {
    await config; // the model names and the export choices
    if (!historyView) {
      historyView = initHistory(historyEl, {
        scope: 'studio',
        // a design goes on in the image tools' Design tab, on the main page
        onContinue: (job) => { window.location.href = `/?continue=${encodeURIComponent(job.id)}#design`; },
        onDeleted,
      });
    }
  }
  return historyView;
}

async function showHistory() {
  try {
    (await ensureHistory()).activate();
  } catch (err) {
    replace(historyEl, callout('error', err.message));
  }
}

/** A job's whole view (the History drawer), from any row that names a job. */
async function openJob(jobId) {
  try {
    (await ensureHistory()).open(jobId);
  } catch (err) {
    toast(err.message, { kind: 'error' });
  }
}

/* ---------- LLM Costs and Jobs (dashboard.js), with one filter bar moved to whichever is open ---------- */
const dash = initDashboard({
  onOpenJob: openJob, // "Details", and a click on the job's row
  onSiteChange: () => renderHealth(),
  onReport: (next) => {
    report = next;
    reportAt = Date.now();
    renderSheetLink();
    renderHealth();
    renderSheet();
    renderStorage();
  },
});
const costsFilters = h('div');
const jobsFilters = h('div');
const costsTab = h('div', { class: 'dash-body' }, costsFilters, dash.costs);
const jobsTab = h('div', { class: 'dash-body' },
  h('div', { class: 'history-toolbar board-subbar' },
    h('p', { class: 'hint' }, 'Every job by every login, day by day, with what it cost at OpenAI and what the client paid in tokens.'),
    h('span', { class: 'spacer' }), dash.csvBtn),
  jobsFilters, dash.jobs);

/* ---------- "Open the Google Sheet" (the sheet's own address, which its script tells the app) ---------- */
const sheetLink = h('a', { class: 'btn btn-sm btn-sheet', target: '_blank', rel: 'noopener', hidden: true },
  icon('sheet'), 'Open the Google Sheet', icon('external'));

function renderSheetLink() {
  const url = report && report.sheet && report.sheet.sheet_url;
  sheetLink.hidden = !url;
  if (url) sheetLink.href = url;
}

/* ---------- "Is everything running?" (the To do tab) ---------- */
const healthBox = h('div');

function light(tone, name, text) {
  return h('div', { class: ['health', `is-${tone}`] }, h('span', { class: 'health-dot', 'aria-hidden': 'true' }),
    h('span', null, h('strong', null, name), ' ', text));
}

function renderHealth() {
  if (!report) {
    replace(healthBox, card('Is everything running?', loadingRow('Loading…')));
    return;
  }
  const items = [];
  const site = report.site || {};
  items.push(light(site.closed ? 'bad' : 'ok', 'Site', site.closed ? 'closed (open it under Settings)' : 'open'));
  const sh = report.sheet;
  if (sh) {
    if (!sh.enabled) items.push(light('warn', 'Google Sheet', `not set up (${fmtInt(sh.pending)} rows kept here)`));
    else if (sh.last_error) items.push(light('bad', 'Google Sheet', `${fmtInt(sh.pending)} rows waiting`));
    else if (sh.script !== sh.script_wanted) items.push(light('warn', 'Google Sheet', 'needs its new script (see Settings)'));
    else items.push(light(sh.pending ? 'warn' : 'ok', 'Google Sheet', sh.pending ? `${fmtInt(sh.pending)} rows waiting` : 'up to date'));
  }
  for (const r of report.remote || []) { // the Mac, when it sends its Dashboard here
    const fresh = Date.now() - Date.parse(r.received_at) < 30 * 60 * 1000;
    items.push(light(fresh ? 'ok' : 'idle', r.machine === 'mac' ? 'Mac' : r.machine, `sent ${fmtDateTime(r.received_at)}`));
  }
  const sync = report.sync || {};
  if (sync.enabled) { // this machine sends its Dashboard to the server
    items.push(light(sync.last_error ? 'bad' : 'ok', 'Sending to the server',
      sync.last_error ? 'failed last time' : sync.last_ok_at ? `sent ${fmtDateTime(sync.last_ok_at)}` : 'every few minutes'));
  }
  if (report.disk) {
    const free = report.disk.free / GB;
    items.push(light(free < 2 ? 'bad' : free < 5 ? 'warn' : 'ok', 'Disk', `${free.toFixed(1)} GB free`));
  }
  const dayAgo = Date.now() - DAY_MS;
  const failed = (report.jobs || []).filter((j) => (j.status === 'failed' || j.status === 'partial')
    && Date.parse(j.created_at) >= dayAgo && (j.machine || report.machine) === report.machine).length;
  items.push(light(failed ? 'warn' : 'ok', 'Failed jobs', failed ? `${fmtInt(failed)} in the last 24 hours (see Jobs)` : 'none in the last 24 hours'));
  replace(healthBox, card('Is everything running?', h('div', { class: 'health-row' }, items),
    { note: 'Each turns red or amber here when it needs you.' }));
}

/* ---------- the Clients tab's real-cost columns: what each client's jobs cost at OpenAI and Claude ---------- */
const costCache = new Map(); // days -> {at, map}

async function clientCosts(days) {
  const kept = costCache.get(days);
  if (kept && Date.now() - kept.at < 60000) return kept.map;
  const usage = await api.adminUsage(days);
  const map = new Map();
  for (const job of usage.jobs || []) {
    if (!job.client || (job.machine || usage.machine) !== usage.machine) continue; // this server's clients
    const row = map.get(job.client) || { cost: 0, tokens: 0 };
    row.cost += sum(job.calls || []).cost;
    row.tokens += job.tokens || 0;
    map.set(job.client, row);
  }
  costCache.set(days, { at: Date.now(), map });
  return map;
}

/* ---------- Settings ---------- */
const payBox = h('div');
const sheetBox = h('div');
const pricesBox = h('div');
const storageBox = h('div');
const settingsTab = h('div', { class: 'dash-body' }, dash.site, payBox, sheetBox, h('div', { class: 'settings-pair' }, pricesBox, storageBox));
let pay = null; // GET /api/pay/settings

async function loadSettings() {
  freshen(30000);
  renderPrices();
  renderSheet();
  renderStorage();
  if (!pay) replace(payBox, card('bKash', loadingRow('Loading…')));
  try {
    pay = await api.paySettings();
    renderPay();
  } catch (err) {
    replace(payBox, card('bKash', callout('error', err.message)));
  }
}

async function savePay(number, packages, btn, done) {
  setBusy(btn, true, 'Saving…');
  try {
    pay = await api.savePaySettings(number, packages);
    toast(done, { kind: 'success' });
    renderPay();
    if (board) board.reload(); // the "clients can't buy yet" note on To do follows
  } catch (err) {
    toast(err.message, { kind: 'error' });
    if (btn.isConnected) setBusy(btn, false);
  }
}

function renderPay() {
  const numberIn = inputEl({ id: 'pay-number', inputmode: 'numeric', maxlength: '20', placeholder: '01XXXXXXXXX', value: pay.number || '', class: 'input pay-number' });
  const saveBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Save');
  const numberForm = h('form', { class: 'pay-number-form' }, numberIn, saveBtn);
  numberForm.addEventListener('submit', (e) => {
    e.preventDefault();
    savePay(numberIn.value.trim(), pay.packages, saveBtn, numberIn.value.trim() ? 'Saved. Clients now see this number.' : 'Saved. Without a number, clients can’t buy.');
  });
  const tokensIn = inputEl({ type: 'number', min: '1', step: '1', inputmode: 'numeric', placeholder: '500' });
  const takaIn = inputEl({ type: 'number', min: '1', step: '1', inputmode: 'numeric', placeholder: '1500' });
  const addBtn = h('button', { type: 'submit', class: 'btn' }, icon('plus'), 'Add package');
  const addForm = h('form', { class: 'pay-package-form' }, field('Tokens', tokensIn), field('Taka (৳)', takaIn), addBtn);
  addForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const tokens = Number(tokensIn.value);
    const price = Number(takaIn.value);
    if (!Number.isInteger(tokens) || tokens < 1 || !Number.isInteger(price) || price < 1) {
      toast('A package needs a number of tokens and a price in taka, both whole numbers above 0.', { kind: 'warning' });
      return;
    }
    savePay(pay.number, [...pay.packages, { tokens, taka: price }], addBtn, `Added: ${fmtInt(tokens)} tokens for ${taka(price)}.`);
  });
  const list = pay.packages.length
    ? pay.packages.map((p) => {
      const remove = h('button', { type: 'button', class: 'btn btn-sm' }, 'Remove');
      remove.addEventListener('click', () => savePay(pay.number, pay.packages.filter((x) => x.tokens !== p.tokens), remove,
        `Removed the package of ${fmtInt(p.tokens)} tokens.`));
      return h('div', { class: 'pay-package' }, h('strong', null, `${fmtInt(p.tokens)} tokens · ${taka(p.taka)}`), remove);
    })
    : h('p', { class: 'hint' }, 'No package yet. Add one below.');
  const state = !pay.tokens_enabled ? callout('info', 'Tokens are off on this server (no client_assets/tokens.json), so nothing can be bought.')
    : pay.enabled ? null : callout('warning', 'Clients can’t buy yet: it needs a number and at least one package.');
  replace(payBox, card('bKash', [state,
    h('div', { class: 'pay-settings' },
      h('div', { class: 'field' },
        h('label', { class: 'field-label', for: 'pay-number' }, 'The bKash number clients pay to'), numberForm,
        h('p', { class: 'hint' }, 'A personal number: clients pay with Send Money. They see it when they press Buy tokens on their page.')),
      h('div', { class: 'field' }, h('span', { class: 'field-label' }, 'Packages clients can buy'), list, addForm)),
    pay.updated_at ? h('p', { class: 'hint' }, `Last changed ${fmtDateTime(pay.updated_at)}.`) : null],
  { note: 'Only you can change the number and the packages. Your partner cannot.' }));
}

/* The Google Sheet log and its script. The script (app/sheet_script.gs) lives in the sheet, where only the owner can
   put it: this card hands it over with this machine's secret filled in, and "Check the sheet" asks the sheet which
   version answers, so it is plain whether the paste and the new deployment took. */
async function copyScript(btn) {
  setBusy(btn, true, 'Copying…');
  try {
    const { script } = await api.sheetScript();
    if (await copyText(script)) {
      toast('Copied. Now paste it in the sheet’s Apps Script editor (step 2).', { kind: 'success' });
    } else { // no clipboard here: the text itself, to select and copy by hand
      const area = h('textarea', { class: 'input sheet-script', readonly: true, rows: '14', spellcheck: 'false' });
      area.value = script;
      modal({ title: 'The sheet’s script', body: [h('p', { class: 'hint' }, 'Select all of it (Ctrl+A / ⌘A), copy it, and paste it in the sheet’s Apps Script editor. Keep it private: it holds the sheet’s password.'), area] });
      area.focus();
      area.select();
    }
  } catch (err) {
    toast(err.message, { kind: 'error' });
  } finally {
    setBusy(btn, false);
  }
}

async function checkSheet(btn) {
  setBusy(btn, true, 'Asking the sheet…');
  try {
    const status = await api.sheetCheck();
    if (report) report.sheet = status;
    if (status.last_error) toast(`The sheet did not answer as expected: ${status.last_error}`, { kind: 'error' });
    else if (status.script === status.script_wanted) toast('The sheet runs the new script.', { kind: 'success' });
    else toast('The sheet answered, but still with the older script. In Apps Script, deploy a new version (step 3).', { kind: 'warning', timeout: 9000 });
    renderSheetLink();
    renderHealth();
  } catch (err) {
    toast(err.message, { kind: 'error' });
  } finally {
    setBusy(btn, false);
    renderSheet();
  }
}

function renderSheet() {
  const sh = report && report.sheet;
  if (!sh) {
    replace(sheetBox, card('Google Sheet', loadingRow('Loading…')));
    return;
  }
  if (!sh.enabled) {
    replace(sheetBox, card('Google Sheet', callout('info',
      `Not set up on this computer, so its rows are kept here (${fmtInt(sh.pending)} waiting). The sheet’s link and password go in the key file .google_sheet.`)));
    return;
  }
  const current = sh.script === sh.script_wanted;
  const copyBtn = h('button', { type: 'button', class: 'btn btn-sm btn-primary' }, icon('copy'), 'Copy the script');
  copyBtn.addEventListener('click', () => copyScript(copyBtn));
  const checkBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('refresh'), 'Check the sheet');
  checkBtn.addEventListener('click', () => checkSheet(checkBtn));
  const steps = h('ol', { class: 'sheet-steps' },
    h('li', null, copyBtn),
    h('li', null, 'In the Google Sheet: ', h('strong', null, 'Extensions › Apps Script'), '. Select everything in the editor, delete it, paste, and press Save.'),
    h('li', null, 'Still there: ', h('strong', null, 'Deploy › Manage deployments › the pencil › Version: New version › Deploy'), '. The address stays the same.'),
    h('li', null, 'Back here: ', checkBtn));
  const state = sh.last_error
    ? callout('error', `The last try failed: ${sh.last_error}. ${fmtInt(sh.pending)} rows are waiting here and go as soon as the sheet answers.`)
    : h('p', null, h('strong', null, sh.pending ? `${fmtInt(sh.pending)} rows waiting` : 'Up to date'),
      sh.last_ok_at ? ` · last sent ${fmtDateTime(sh.last_ok_at)}` : '',
      sh.sheet_url ? [' · ', h('a', { href: sh.sheet_url, target: '_blank', rel: 'noopener' }, 'Open the Google Sheet')] : null);
  replace(sheetBox, card('Google Sheet', [state,
    current
      ? h('details', { class: 'sheet-update' }, h('summary', null, `The sheet runs the current script (version ${sh.script}). Put it in again…`), steps)
      : [callout('warning', h('strong', null, 'The sheet needs its new script.'),
        ' Until then the log tab keeps working as before, but there is no bKash tab, a bKash number can lose its first 0 there, and the Open the Google Sheet button stays hidden. Do it once, before clients start paying:'), steps]],
  { note: 'Every job, top-up and bKash payment is also written there.' }));
}

function renderPrices() {
  const prices = (report && report.tokens && report.tokens.prices) || (board && board.overview() && board.overview().prices);
  replace(pricesBox, card('Token prices', prices
    ? [pricesParts(prices), h('p', { class: 'hint' }, 'Shown here to read. They are changed in the file client_assets/tokens.json, then the app is restarted.')]
    : h('p', { class: 'hint' }, 'Tokens are off on this server: there is no client_assets/tokens.json.')));
}

function renderStorage() {
  if (!report || !report.disk) {
    replace(storageBox, card('Storage', loadingRow('Loading…')));
    return;
  }
  const clean = report.cleanup || {};
  const rules = [];
  if (clean.days > 0) rules.push(`Ready-made download files are deleted after ${fmtInt(clean.days)} days`);
  if (clean.min_free_gb > 0) rules.push(`${clean.days > 0 ? 'and sooner' : 'The oldest ready-made download files are deleted'} when less than ${clean.min_free_gb} GB is free`);
  replace(storageBox, card('Storage', [
    h('p', null, h('strong', null, `${(report.disk.free / GB).toFixed(1)} GB`), ` free of ${(report.disk.total / GB).toFixed(1)} GB`),
    h('p', { class: 'hint' }, rules.length ? `${rules.join(', ')}. They are made again on the next download.`
      : 'Nothing is deleted automatically on this computer.')]));
}

/* ---------- the page ---------- */
// /supreme-dashboard?job=<id>#history (the old /?job=<id>#history): that job's view, over the History tab
const wanted = new URLSearchParams(window.location.search).get('job');
if (wanted) window.history.replaceState(null, '', '/supreme-dashboard#history');

function show(slot) {
  slot.appendChild(dash.filters); // the one filter bar, under the open tab
  freshen(1000); // opening the tab reads the numbers again (not when they were read a moment ago, as the page starts)
}

board = startBoard({
  title: 'Supreme dashboard',
  prefsKey: 'supremeboard.prefs',
  toolbar: [sheetLink, dash.syncBtn],
  headerLinks: () => [h('a', { class: 'btn btn-sm btn-ghost', href: '/' }, icon('arrow-left'), 'Image tools')],
  todoExtra: () => {
    freshen(30000); // To do is drawn again every minute (boards.js): "Is everything running?" follows
    return healthBox;
  },
  clientCosts,
  openJob, // a job's row on a client's page
  jobsTab: { el: jobsTab, onShow: () => show(jobsFilters) },
  extraTabs: [
    { id: 'history', label: 'History', el: historyEl, onShow: showHistory },
    { id: 'costs', label: 'LLM Costs', el: costsTab, onShow: () => show(costsFilters) },
    { id: 'settings', label: 'Settings', el: settingsTab, onShow: loadSettings },
  ],
  onRefresh: () => {
    costCache.clear();
    dash.reload();
    if (window.location.hash === '#settings') loadSettings();
  },
});
renderHealth();
freshen(5000); // the report also feeds "Is everything running?", the sheet's link and Settings
if (wanted && JOB_ID_RE.test(wanted)) openJob(wanted);
