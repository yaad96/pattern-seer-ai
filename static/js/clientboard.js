/**
 * The client dashboard (/client-dashboard, 2026-11-23): each client's purse of our own tokens, what its jobs used,
 * and the tokens added to it. Each image is priced in client_assets/tokens.json (front, a back from its own picture,
 * a back made from the front (free), sleeve, a Design message); a failed image is free. For the owner's partner (the
 * partner login: read-only, and only the clients' job previews) and the studio login, which also adds tokens or takes
 * some back, always with a note, and records the taka a client paid for them. No API costs and no AI details.
 * Data: GET /api/tokens/clients and /api/tokens/clients/{slug}; POST …/{slug}/topup (app/main.py, app/tokens.py).
 * In English (the owner's choice, 2026-11-23: an internal page), so it doesn't load the Bangla layer (i18n.js).
 * The open client is in the address (#client=<slug>), so the studio Dashboard can link to it (and the skip link's
 * #main is never read as a client).
 */
import { api, newRequestId } from './api.js';
import { h, icon, replace, fmtDateTime, fmtInt, storage } from './dom.js';
import { jobsTable, pricesParts } from './tokenviews.js';
import { callout, confirmDialog, field, inputEl, loadingRow, segmented, setBusy, toast } from './ui.js';

const PREFS_KEY = 'clientboard.prefs';
const HASH_RE = /^#client=([a-z0-9]{1,32})$/;
const RANGES = [['0', 'All time'], ['7', 'Last 7 days'], ['30', 'Last 30 days'], ['90', 'Last 90 days']];
const MINUS = '−';

const prefs = { range: '0', ...(storage.get(PREFS_KEY, {}) || {}) };
if (!RANGES.some(([v]) => v === prefs.range)) prefs.range = '0';
let overview = null; // GET /api/tokens/clients
let detail = null; // GET /api/tokens/clients/{slug}, or {missing: slug}
let query = '';
let seq = 0;
const openChats = new Set(); // the Design chats opened on the client's jobs table (kept across reloads)

const board = document.getElementById('board');
const signed = (n) => (n > 0 ? `+${fmtInt(n)}` : n < 0 ? `${MINUS}${fmtInt(-n)}` : '0');
const plain = (n) => (n < 0 ? `${MINUS}${fmtInt(-n)}` : fmtInt(n));
const taka = (n) => `৳${fmtInt(n || 0)}`; // Bangladeshi taka, whole

function selected() {
  const m = HASH_RE.exec(window.location.hash);
  return m ? m[1] : '';
}
const clientHash = (slug) => (slug ? `#client=${slug}` : '#');

function periodText() {
  return (RANGES.find(([v]) => v === prefs.range) || RANGES[0])[1];
}

/* ---------- the page around the data ---------- */
const refreshBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('refresh'), 'Refresh');
refreshBtn.addEventListener('click', () => load());
const range = segmented({
  legend: 'Period', value: prefs.range, options: RANGES.map(([value, label]) => ({ value, label })),
  onChange: (v) => { prefs.range = v; storage.set(PREFS_KEY, prefs); load(); },
});
const picker = h('select', { class: 'select', 'aria-label': 'Client' });
picker.addEventListener('change', () => { window.location.hash = clientHash(picker.value); });
const search = inputEl({ type: 'search', placeholder: 'Search clients', 'aria-label': 'Search clients' });
search.addEventListener('input', () => {
  query = search.value.trim().toLowerCase();
  if (overview && !selected()) renderOverview();
});
const searchBox = h('label', { class: 'dash-type' }, h('span', { class: 'field-label' }, 'Search'), search);
const body = h('div', { class: 'dash-body' }, loadingRow('Loading…'));

replace(board,
  h('div', { class: 'history-toolbar' }, h('h2', null, 'Client dashboard'), h('span', { class: 'spacer' }), refreshBtn),
  h('div', { class: 'dash-filters' }, range.el,
    h('label', { class: 'dash-type' }, h('span', { class: 'field-label' }, 'Client'), picker), searchBox),
  body);

function renderHeader() {
  const items = [];
  if (overview.can_add) { // the studio login: back to its own Dashboard
    items.push(h('a', { class: 'btn btn-sm btn-ghost', href: '/#dashboard', title: 'Back to the main page' },
      icon('arrow-left'), 'Main page'));
  }
  if (overview.login_enabled) items.push(h('a', { class: 'btn btn-sm', href: '/logout' }, icon('logout'), 'Log out'));
  replace(document.getElementById('header-actions'), items);
}

function renderPicker(slug) {
  const names = overview.clients.map((c) => [c.slug, c.name]);
  if (slug && !names.some(([s]) => s === slug)) names.push([slug, slug]);
  replace(picker, h('option', { value: '' }, 'All clients'),
    names.map(([value, name]) => h('option', { value, 'data-no-translate': '' }, name)));
  picker.value = slug;
}

/* ---------- data ---------- */
async function load() {
  const mine = ++seq;
  const slug = selected();
  setBusy(refreshBtn, true, 'Loading…');
  try {
    const next = await api.tokenClients(prefs.range);
    let one = null;
    if (slug) {
      try {
        one = await api.tokenClient(slug, prefs.range);
      } catch (err) {
        if (err.status !== 404) throw err;
        one = { missing: slug };
      }
    }
    if (mine !== seq) return;
    overview = next;
    detail = one;
    render();
    document.getElementById('boot')?.remove();
    board.hidden = false;
  } catch (err) {
    if (mine !== seq) return;
    document.getElementById('boot')?.remove();
    board.hidden = false;
    replace(body, callout('error', err.message));
  } finally {
    if (mine === seq) setBusy(refreshBtn, false);
  }
}

window.addEventListener('hashchange', () => load());
// the skip link would set #main and so leave a client's page: focus the content without changing the address
const skip = document.querySelector('.skip-link');
if (skip) {
  skip.addEventListener('click', (e) => {
    e.preventDefault();
    document.getElementById('main').focus();
  });
}

/* ---------- render ---------- */
function render() {
  const slug = selected();
  renderHeader();
  renderPicker(slug);
  searchBox.hidden = Boolean(slug);
  if (slug && detail) renderDetail();
  else renderOverview();
}

function tile(label, value, sub) {
  return h('div', { class: 'dash-tile' },
    h('div', { class: 'dash-tile-label' }, label),
    h('div', { class: 'dash-tile-value' }, value),
    sub ? h('div', { class: 'dash-tile-sub' }, sub) : null);
}

function offNote() {
  return overview.enabled ? null
    : callout('info', 'Tokens are off on this server: there is no client_assets/tokens.json, so nothing is charged.');
}

function renderOverview() {
  const all = overview.clients;
  const list = query ? all.filter((c) => c.name.toLowerCase().includes(query) || c.slug.includes(query)) : all;
  const total = (key) => all.reduce((acc, c) => acc + (c[key] || 0), 0);
  let content;
  if (!all.length) {
    content = h('div', { class: 'empty' },
      h('div', { class: 'empty-icon' }, icon('chart')),
      h('p', { class: 'empty-title' }, 'No clients yet'),
      h('p', { class: 'empty-text' }, 'A client appears here once it has its own page.'));
  } else if (!list.length) {
    content = h('p', { class: 'hint' }, 'No client matches the search.');
  } else {
    content = h('table', { class: 'dash-table' },
      h('thead', null, h('tr', null, ['Client', 'Balance now', 'Used', 'Added', 'Taka received', 'Jobs', 'Last job']
        .map((t, i) => h('th', { class: i && i < 6 ? 'num' : null }, t)))),
      h('tbody', null, list.map((c) => h('tr', null,
        h('td', null, h('a', { href: clientHash(c.slug), 'data-no-translate': '' }, c.name),
          c.has_page ? null : [' ', h('span', { class: 'badge' }, 'No page now')]),
        h('td', { class: 'num' }, h('strong', null, plain(c.balance))),
        h('td', { class: 'num' }, fmtInt(c.used)),
        h('td', { class: 'num' }, signed(c.added)),
        h('td', { class: 'num' }, c.taka ? taka(c.taka) : '–'),
        h('td', { class: 'num' }, fmtInt(c.jobs)),
        h('td', { class: 'nowrap' }, c.last_job_at ? fmtDateTime(c.last_job_at) : '–')))));
  }
  replace(body,
    offNote(),
    h('div', { class: 'dash-tiles' },
      tile('Clients', fmtInt(all.length)),
      tile('Tokens in all purses', plain(total('balance')), h('span', null, 'Balance now')),
      tile('Used', fmtInt(total('used')), h('span', null, periodText())),
      tile('Added', signed(total('added')), h('span', null, periodText())),
      tile('Taka received', taka(total('taka')), h('span', null, periodText())),
      tile('Jobs', fmtInt(total('jobs')), h('span', null, periodText()))),
    h('section', { class: 'card' },
      h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'Clients')),
      h('div', { class: 'card-body dash-scroll' }, content)),
    pricesCard(overview.prices));
}

function renderDetail() {
  const back = h('a', { class: 'btn btn-sm btn-ghost', href: '#' }, icon('arrow-left'), 'All clients');
  if (detail.missing) {
    replace(body, h('div', { class: 'cb-head' }, back), callout('error', 'No such client.'));
    return;
  }
  const c = detail.client;
  const made = detail.jobs.filter((j) => !j.deleted).length;
  replace(body,
    offNote(),
    h('div', { class: 'cb-head' }, back, h('h3', { class: 'cb-name', 'data-no-translate': '' }, c.name),
      c.has_page ? null : h('span', { class: 'badge' }, 'No page now')),
    h('div', { class: 'dash-tiles' },
      tile('Balance now', plain(c.balance), h('span', null, c.available < c.balance
        ? `${fmtInt(Math.max(0, c.available))} free · ${fmtInt(c.balance - c.available)} held for running jobs`
        : 'Tokens left for new jobs')),
      tile('Used', fmtInt(c.used), h('span', null, periodText())),
      tile('Added', signed(c.added), h('span', null, periodText())),
      tile('Taka received', taka(c.taka), h('span', null, periodText())),
      tile('Jobs', fmtInt(made), h('span', null, periodText()))),
    jobsCard(detail.jobs),
    detail.can_add ? addCard(c) : null,
    topupsCard(detail.topups, detail.can_add),
    pricesCard(detail.prices));
}

function jobsCard(jobs) {
  let content;
  if (!jobs.length) {
    content = h('div', { class: 'empty' },
      h('p', { class: 'empty-title' }, 'No jobs in this period'),
      h('p', { class: 'empty-text' }, 'This client’s jobs appear here with the tokens each one used.'));
  } else {
    content = jobsTable(jobs, { openChats, ids: true });
  }
  return h('section', { class: 'card' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'Jobs'),
      h('span', { class: 'muted' }, periodText())),
    h('div', { class: 'card-body dash-scroll' }, content));
}

function topupsCard(topups, canAdd) {
  const content = topups.length
    ? h('table', { class: 'dash-table' },
      h('thead', null, h('tr', null, ['Time', 'Tokens', 'Taka received', 'Note', 'By', 'Balance after']
        .map((t, i) => h('th', { class: i === 1 || i === 2 || i === 5 ? 'num' : null }, t)))),
      h('tbody', null, topups.map((t) => h('tr', null,
        h('td', { class: 'nowrap' }, fmtDateTime(t.at)),
        h('td', { class: 'num' }, h('strong', null, signed(t.tokens))),
        h('td', { class: 'num' }, typeof t.taka === 'number' ? taka(t.taka) : '–'),
        h('td', { 'data-no-translate': '' }, t.note || ''),
        h('td', null, t.by === 'studio' ? 'Head admin' : t.by || ''),
        h('td', { class: 'num' }, t.balance === undefined || t.balance === null ? '–' : plain(t.balance))))))
    : h('p', { class: 'hint' }, 'No tokens added yet.');
  return h('section', { class: 'card' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'Tokens added and taka received')),
    h('div', { class: 'card-body dash-scroll' }, content,
      canAdd ? null : h('p', { class: 'hint' }, 'Only the head admin adds tokens.')));
}

function addCard(c) {
  const tokensIn = inputEl({ type: 'number', name: 'tokens', step: '1', inputmode: 'numeric', placeholder: '1000' });
  const takaIn = inputEl({ type: 'number', name: 'taka', step: '1', min: '0', inputmode: 'numeric', placeholder: '5000' });
  const noteIn = inputEl({ name: 'note', maxlength: '300', placeholder: 'e.g. how they paid, and its reference' });
  const addBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, icon('plus'), 'Add tokens');
  const form = h('form', { class: 'cb-add' }, field('Number of tokens', tokensIn), field('Taka received (৳)', takaIn),
    field('Note', noteIn), addBtn);
  // this form's top-up until it is answered: the same tokens, taka and note sent again (after a lost answer) are the
  // same request, which the server records once (2026-11-23 review CHAOS-4)
  let pending = null; // {id, sig}
  const clearForm = () => {
    tokensIn.value = '';
    takaIn.value = '';
    noteIn.value = '';
    pending = null;
  };
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const n = Number(tokensIn.value);
    if (!tokensIn.value.trim() || !Number.isInteger(n) || n === 0) {
      toast('Enter a whole number other than 0.', { kind: 'warning' });
      tokensIn.focus();
      return;
    }
    if (Math.abs(n) > 10000000) { // the server's limit for one change
      toast('At most 10,000,000 tokens in one change.', { kind: 'warning' });
      tokensIn.focus();
      return;
    }
    const paid = takaIn.value.trim() === '' ? null : Number(takaIn.value);
    if (paid !== null && (!Number.isInteger(paid) || paid < 0)) {
      toast('Enter the taka received as a whole number, or leave it empty.', { kind: 'warning' });
      takaIn.focus();
      return;
    }
    if (paid !== null && n < 0) {
      toast('Taka received goes with tokens added, not with tokens taken back.', { kind: 'warning' });
      takaIn.focus();
      return;
    }
    const note = noteIn.value.trim();
    if (!note) {
      toast('Write a note: what the tokens are for.', { kind: 'warning' });
      noteIn.focus();
      return;
    }
    let balance = c.balance;
    let available = c.available;
    try { // the purse now: jobs may have been charged since the page was loaded
      ({ balance, available } = (await api.tokenClient(c.slug, prefs.range)).client);
    } catch (_) {
      /* the dialog shows the last balance seen; the server does the sum and the checks either way */
    }
    if (n < 0 && typeof available === 'number' && -n > available) { // the server would refuse it: say so now
      toast(`Only ${fmtInt(Math.max(0, available))} tokens can be taken back now.`, { kind: 'warning' });
      return;
    }
    const ok = await confirmDialog({
      title: n > 0 ? `Add ${fmtInt(n)} tokens?` : `Take back ${fmtInt(-n)} tokens?`,
      message: `Balance: ${plain(balance)} → ${plain(balance + n)}${paid !== null ? ` · Taka received: ${taka(paid)}` : ''}`,
      confirmLabel: n > 0 ? 'Add tokens' : 'Take back tokens',
      danger: n < 0,
    });
    if (!ok) return;
    const sig = JSON.stringify([n, paid, note]);
    if (!pending || pending.sig !== sig) pending = { id: newRequestId(), sig };
    const requestId = pending.id;
    setBusy(addBtn, true, 'Saving…');
    try {
      const res = await api.addTokens(c.slug, n, note, paid, requestId);
      toast(res.repeated ? `That top-up was already recorded, so it was not added again. Balance: ${plain(res.balance)}`
        : `Done. New balance: ${plain(res.balance)}`, { kind: 'success' });
      clearForm();
      await load();
    } catch (err) {
      let landed = false;
      if (err.status === 0) { // no answer: the top-up may still have been recorded. Look before saying it failed.
        try {
          landed = (await api.tokenClient(c.slug, prefs.range)).topups.some((t) => t.request_id === requestId);
        } catch (_) {
          /* still no connection: say so below; pressing again can't record it twice */
        }
      }
      if (landed) {
        toast('The connection dropped, but the top-up was recorded. It was not added twice.', { kind: 'success' });
        clearForm();
        await load();
      } else {
        toast(err.status === 0 ? `${err.message} Pressing Add tokens again can't record it twice.` : err.message,
          { kind: 'error' });
      }
    } finally {
      if (addBtn.isConnected) setBusy(addBtn, false);
    }
  });
  return h('section', { class: 'card' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'Add tokens')),
    h('div', { class: 'card-body' }, form,
      h('p', { class: 'hint' }, 'Taka received: what the client paid for these tokens (leave it empty for free tokens). A negative number of tokens takes tokens back (a correction). Every change is kept with its note, the time and who made it.')));
}

function pricesCard(prices) {
  if (!prices) return null;
  return h('section', { class: 'card' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'Token prices')),
    h('div', { class: 'card-body' },
      pricesParts(prices),
      h('p', { class: 'hint' }, 'A part that fails is not charged. Making it again is charged when it works.')));
}

load();
