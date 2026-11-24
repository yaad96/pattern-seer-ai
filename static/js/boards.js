/**
 * The two dashboards' shared tabs (2026-11-24; the owner's names for them): the Partner dashboard
 * (/partner-dashboard: the head admin and the partner login) and the Supreme dashboard (/supreme-dashboard: the head
 * admin only), which has the same four tabs plus its own (supremeboard.js).
 *   To do     bKash payments waiting to be checked (approve / reject), adding tokens by hand, clients running low
 *   Clients   each client's purse, and a client's own page (#client=<slug>): its jobs, payments and tokens added;
 *             and "Add a customer", the Customer EntryPoint Builder (app/customers.py): a new client page from a form
 *   Payments  every bKash payment, and the tokens added or taken back by hand
 *   Jobs      every client's jobs, day by day, with the tokens each part cost
 * Our tokens and taka only: never the API costs (the Supreme dashboard adds those itself). Both logins check payments
 * and add tokens; only the head admin takes tokens back (the server decides). Phone numbers are shown in full to
 * both (the owner, 2026-11-24: "no concealing of any phone numbers in my app anywhere"). In English (the owner's
 * choice, 2026-11-23: internal pages), without the Bangla layer.
 * Data: /api/tokens/clients, /api/tokens/clients/{slug}, /api/tokens/jobs, /api/tokens/topups, /api/pay/requests
 * (app/main.py, app/tokens.py, app/payments.py).
 */
import { api, newRequestId } from './api.js';
import { h, icon, replace, fmtDateTime, fmtInt, storage } from './dom.js';
import { jobsTable, pricesParts } from './tokenviews.js';
import { callout, closeDialog, confirmDialog, copyText, field, inputEl, loadingRow, modal, segmented, setBusy, toast } from './ui.js';

const HASH_CLIENT = /^#client=([a-z0-9]{1,32})$/;
const RANGES = [['0', 'All time'], ['7', 'Last 7 days'], ['30', 'Last 30 days'], ['90', 'Last 90 days']];
const MINUS = '−';
const WHO = { studio: 'Head admin', partner: 'Partner' };
const REASONS = [['not_found', 'TrxID not found in bKash'], ['amount', 'The amount does not match'],
  ['used', 'This TrxID was used before']];
const STATUS = {
  waiting: ['Waiting to be checked', 'badge-warning'],
  approved: ['Approved · tokens added', 'badge-success'],
  rejected: ['Rejected', 'badge-danger'],
};
const REFRESH_MS = 60000; // a new payment shows on "To do" within a minute, without pressing Refresh

export const signed = (n) => (n > 0 ? `+${fmtInt(n)}` : n < 0 ? `${MINUS}${fmtInt(-n)}` : '0');
export const plain = (n) => (n < 0 ? `${MINUS}${fmtInt(-n)}` : fmtInt(n));
export const taka = (n) => `৳${fmtInt(n || 0)}`; // Bangladeshi taka, whole
const who = (by) => WHO[by] || by || '';
const clientHash = (slug) => (slug ? `#client=${slug}` : '#clients');

export function tile(label, value, sub, extraClass) {
  return h('div', { class: ['dash-tile', extraClass] },
    h('div', { class: 'dash-tile-label' }, label),
    h('div', { class: 'dash-tile-value' }, value),
    sub ? h('div', { class: 'dash-tile-sub' }, sub) : null);
}

export function card(title, body, { note, actions, bodyClass } = {}) {
  return h('section', { class: 'card' },
    h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, title),
      note ? h('span', { class: 'muted' }, note) : null, actions ? [h('span', { class: 'spacer' }), actions] : null),
    h('div', { class: ['card-body', bodyClass] }, body));
}

function dayOf(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function dayLabel(key) {
  const [y, m, d] = key.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
}

/**
 * Start a dashboard in #board. options:
 *   title, prefsKey; headerLinks(overview) -> elements for the header's right side (before Log out);
 *   toolbar: elements for the title row; extraTabs: [{id, label, el, onShow()}] after the four shared ones;
 *   jobsTab: {el, onShow()} to use instead of the shared Jobs tab (the Supreme dashboard's has the costs);
 *   todoExtra() -> elements under the To do cards; clientCosts(days) -> Promise<Map slug -> {cost, tokens}> for the
 *   Clients tab's real-cost columns (the Supreme dashboard); onRefresh() when Refresh is pressed.
 */
export function startBoard(options) {
  const { title, prefsKey, headerLinks, toolbar = [], extraTabs = [], jobsTab = null, todoExtra = null,
    clientCosts = null, onRefresh = null } = options;
  const prefs = { range: '0', jobsRange: '7', jobsClient: '', payFilter: 'all', ...(storage.get(prefsKey, {}) || {}) };
  if (!RANGES.some(([v]) => v === prefs.range)) prefs.range = '0';
  if (!RANGES.some(([v]) => v === prefs.jobsRange)) prefs.jobsRange = '7';
  const savePrefs = () => storage.set(prefsKey, prefs);
  const TABS = [['todo', 'To do'], ['clients', 'Clients'], ['payments', 'Payments'], ['jobs', 'Jobs'],
    ...extraTabs.map((t) => [t.id, t.label])];

  let overview = null; // GET /api/tokens/clients
  let pays = null; // GET /api/pay/requests
  let detail = null; // GET /api/tokens/clients/{slug}, or {missing: slug}
  let jobs = null; // GET /api/tokens/jobs
  let topups = null; // GET /api/tokens/topups
  let costs = null; // the Supreme dashboard: Map slug -> {cost, tokens} for the Clients tab's period
  let query = '';
  let seq = 0;
  const checking = new Map(); // a waiting payment's id -> 'confirm' | 'reject' (the row's open step)
  const openChats = new Set(); // the Design chats opened in a jobs table (kept across reloads)

  const board = document.getElementById('board');
  const refreshBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('refresh'), 'Refresh');
  refreshBtn.addEventListener('click', () => {
    load();
    if (onRefresh) onRefresh();
  });
  const tabBar = h('div', { class: 'board-tabs', role: 'tablist', 'aria-label': 'Dashboard sections' });
  const panel = h('div', { class: 'dash-body board-panel', role: 'tabpanel' }, loadingRow('Loading…'));
  replace(board,
    h('div', { class: 'history-toolbar' }, h('h2', null, title), h('span', { class: 'spacer' }), toolbar, refreshBtn),
    tabBar, panel);

  /* ---------- where we are: the tab and, on Clients, the open client ---------- */
  function place() {
    const hash = window.location.hash;
    const m = HASH_CLIENT.exec(hash);
    if (m) return { tab: 'clients', slug: m[1] };
    const tab = hash.slice(1);
    return { tab: TABS.some(([id]) => id === tab) ? tab : 'todo', slug: '' };
  }

  const periodText = () => (RANGES.find(([v]) => v === prefs.range) || RANGES[0])[1];
  const nameOf = (slug) => ((overview && overview.clients.find((c) => c.slug === slug)) || {}).name || slug;
  const waitingList = () => (pays ? pays.payments.filter((p) => p.status === 'waiting') : []);

  function renderTabs() {
    const { tab } = place();
    const count = waitingList().length;
    replace(tabBar, TABS.map(([id, label]) => h('a', {
      class: 'board-tab', role: 'tab', href: `#${id}`, 'aria-selected': String(id === tab),
    }, label, id === 'todo' && count ? h('span', { class: 'badge badge-warning board-count' }, fmtInt(count)) : null)));
  }

  function renderHeader() {
    const items = headerLinks ? headerLinks(overview) : [];
    if (overview.login_enabled) items.push(h('a', { class: 'btn btn-sm', href: '/logout' }, icon('logout'), 'Log out'));
    replace(document.getElementById('header-actions'), items);
  }

  /* ---------- data ---------- */
  async function load({ quiet = false } = {}) {
    const mine = ++seq;
    const { tab, slug } = place();
    if (!quiet) setBusy(refreshBtn, true, 'Loading…');
    try {
      const [nextOverview, nextPays] = await Promise.all([api.tokenClients(prefs.range), api.payRequests()]);
      let one = null;
      let nextJobs = jobs;
      let nextTopups = topups;
      let nextCosts = costs;
      if (tab === 'clients' && slug) {
        try {
          one = await api.tokenClient(slug, prefs.range);
        } catch (err) {
          if (err.status !== 404) throw err;
          one = { missing: slug };
        }
      }
      if (tab === 'clients' && clientCosts) nextCosts = await clientCosts(Number(prefs.range));
      if (tab === 'jobs' && !jobsTab) nextJobs = await api.tokenJobs(prefs.jobsRange, prefs.jobsClient);
      if (tab === 'payments') nextTopups = await api.tokenTopups(0);
      if (mine !== seq) return;
      overview = nextOverview;
      pays = nextPays;
      detail = one;
      jobs = nextJobs;
      topups = nextTopups;
      costs = nextCosts;
      for (const id of [...checking.keys()]) { // a payment someone else checked meanwhile: its open step goes
        if (!waitingList().some((p) => p.id === id)) checking.delete(id);
      }
      render();
    } catch (err) {
      if (mine !== seq) return;
      if (!quiet || !overview) {
        mounted = null;
        replace(panel, callout('error', err.message));
      }
    } finally {
      if (mine === seq) {
        if (!quiet) setBusy(refreshBtn, false);
        document.getElementById('boot')?.remove();
        board.hidden = false;
      }
    }
  }

  window.addEventListener('hashchange', () => {
    renderTabs();
    showExtra();
    load();
  });
  // a new payment appears by itself, as long as nobody is in the middle of something on the page
  window.setInterval(() => {
    const at = document.activeElement;
    const typing = at && panel.contains(at) && ['INPUT', 'SELECT', 'TEXTAREA'].includes(at.tagName);
    const busy = checking.size || typing || document.querySelector('dialog[open]');
    if (!document.hidden && place().tab === 'todo' && !busy) load({ quiet: true });
  }, REFRESH_MS);
  // the skip link would set #main and so leave the tab: focus the content without changing the address
  const skip = document.querySelector('.skip-link');
  if (skip) {
    skip.addEventListener('click', (e) => {
      e.preventDefault();
      document.getElementById('main').focus();
    });
  }

  /* ---------- render ---------- */
  let mounted = null; // the tab of the page's own (extraTabs, jobsTab) whose element is in the panel now

  /** Show the open tab when it is one of the page's own; false for a shared one. Its element is put in and its
   * onShow() called once per opening: a reload of the shared data must not rebuild a form someone is typing in. */
  function showExtra() {
    const { tab } = place();
    const extra = (tab === 'jobs' && jobsTab ? jobsTab : extraTabs.find((t) => t.id === tab)) || null;
    if (extra && mounted !== extra) {
      replace(panel, extra.el);
      if (extra.onShow) extra.onShow();
    }
    mounted = extra;
    return Boolean(extra);
  }

  function render() {
    const { tab, slug } = place();
    renderHeader();
    renderTabs();
    if (showExtra()) return;
    if (tab === 'todo') renderTodo();
    else if (tab === 'clients') (slug && detail ? renderDetail : renderOverview)();
    else if (tab === 'payments') renderPayments();
    else renderJobs();
  }

  function offNote() {
    return overview.enabled ? null
      : callout('info', 'Tokens are off on this server: there is no client_assets/tokens.json, so nothing is charged.');
  }

  /* ---------- bKash payments (app/payments.py) ---------- */
  async function decide(pay, action, btn, reason, text) {
    setBusy(btn, true, action === 'approve' ? 'Adding…' : 'Rejecting…');
    try {
      if (action === 'approve') {
        const res = await api.payApprove(pay.id);
        toast(`${fmtInt(pay.tokens)} tokens added to ${pay.client_name}. New balance: ${plain(res.balance)}`, { kind: 'success' });
      } else {
        await api.payReject(pay.id, reason, text);
        toast(`Rejected. ${pay.client_name} sees the reason on their page.`, { kind: 'success' });
      }
    } catch (err) { // e.g. the other of you checked it a moment ago: say so, then show how things stand
      toast(err.message, { kind: err.status === 409 ? 'warning' : 'error' });
    } finally {
      checking.delete(pay.id);
      await load({ quiet: true });
    }
  }

  /** A table of payments. Waiting ones get Approve / Reject, each with a second step: approving asks to look in the
   * bKash app first; rejecting asks why (the client sees the reason). `withClient`: a Client column. */
  function paysTable(list, { withClient = true } = {}) {
    const cols = withClient ? 8 : 7;
    const rows = [];
    for (const pay of list) {
      const mode = pay.status === 'waiting' ? checking.get(pay.id) : null;
      const [label, tone] = STATUS[pay.status] || [pay.status, ''];
      const under = pay.status === 'approved' ? `${who(pay.checked_by)} · ${fmtDateTime(pay.checked_at)}`
        : pay.status === 'rejected' ? `${who(pay.checked_by)} · ${fmtDateTime(pay.checked_at)} · ${pay.reason_label}` : null;
      const actions = [];
      if (pay.status === 'waiting' && !mode) {
        actions.push(
          h('button', { type: 'button', class: 'btn btn-sm btn-primary', onClick: () => { checking.set(pay.id, 'confirm'); render(); } }, 'Approve'),
          h('button', { type: 'button', class: 'btn btn-sm btn-danger', onClick: () => { checking.set(pay.id, 'reject'); render(); } }, 'Reject'));
      } else if (mode) {
        const back = h('button', { type: 'button', class: 'btn btn-sm', onClick: () => { checking.delete(pay.id); render(); } }, 'Back');
        if (mode === 'confirm') {
          const yes = h('button', { type: 'button', class: 'btn btn-sm btn-success' }, icon('check'), `Seen it: add ${fmtInt(pay.tokens)} tokens`);
          yes.addEventListener('click', () => decide(pay, 'approve', yes));
          actions.push(yes);
        }
        actions.push(back);
      }
      rows.push(h('tr', { class: mode ? 'dash-job is-open' : null },
        h('td', { class: 'nowrap' }, fmtDateTime(pay.at)),
        withClient ? h('td', null, h('a', { href: clientHash(pay.client) }, pay.client_name)) : null,
        h('td', { class: 'num' }, taka(pay.taka)),
        h('td', { class: 'num' }, fmtInt(pay.tokens)),
        h('td', null, h('code', { class: 'pay-code' }, pay.trx)),
        h('td', null, h('code', { class: 'pay-code' }, pay.sender)),
        h('td', null, h('span', { class: ['badge', tone] }, label), under ? h('div', { class: 'dash-sub' }, under) : null),
        h('td', { class: 'nowrap pay-actions' }, actions)));
      if (mode === 'confirm') {
        rows.push(h('tr', { class: 'dash-calls' }, h('td', { colspan: String(cols) },
          h('div', { class: 'pay-step is-ok' }, icon('info'),
            h('span', null, `First check in the bKash app: TrxID ${pay.trx} for ${taka(pay.taka)} has arrived. Then press the green button.`)))));
      } else if (mode === 'reject') {
        const other = inputEl({ maxlength: '200', placeholder: 'or write another reason', 'aria-label': 'Another reason' });
        const send = h('button', { type: 'button', class: 'btn btn-sm btn-danger' }, 'Reject with this reason');
        send.addEventListener('click', () => {
          if (!other.value.trim()) {
            toast('Write the reason, or choose one of the three.', { kind: 'warning' });
            other.focus();
            return;
          }
          decide(pay, 'reject', send, 'other', other.value.trim());
        });
        rows.push(h('tr', { class: 'dash-calls' }, h('td', { colspan: String(cols) },
          h('div', { class: 'pay-step is-no' },
            h('strong', null, 'Why? The client will see the reason:'),
            REASONS.map(([code, text]) => {
              const btn = h('button', { type: 'button', class: 'btn btn-sm' }, text);
              btn.addEventListener('click', () => decide(pay, 'reject', btn, code, ''));
              return btn;
            }),
            h('span', { class: 'pay-other' }, other, send)))));
      }
    }
    return h('table', { class: 'dash-table' },
      h('thead', null, h('tr', null, ['Sent', withClient ? 'Client' : null, 'Taka', 'Tokens', 'TrxID', 'Sent from', 'Status', 'Check']
        .filter((t) => t !== null).map((t) => h('th', { class: t === 'Taka' || t === 'Tokens' ? 'num' : null }, t)))),
      h('tbody', null, rows));
  }

  /* ---------- To do ---------- */
  function renderTodo() {
    const waiting = [...waitingList()].reverse(); // the oldest first: it has waited longest
    const check = card('bKash payments to check',
      waiting.length ? h('div', { class: 'dash-scroll' }, paysTable(waiting))
        : h('p', { class: 'pay-clear' }, icon('check'), 'All checked. A new payment appears here as soon as a client sends one.'),
      { note: waiting.length ? `${fmtInt(waiting.length)} waiting. Look for the TrxID and the amount in the bKash app, then approve or reject.` : null });
    replace(panel, offNote(),
      pays.buying ? null : callout('info', overview.can_take_back
        ? 'Clients can’t buy tokens yet: set the bKash number and at least one package under Settings.'
        : 'Clients can’t buy tokens yet: the head admin has not set the bKash number and the packages.'),
      check, addCard(null), lowCard(), todoExtra ? todoExtra() : null);
  }

  /** Clients who can no longer pay for a full set with their own back picture (the dearest job). */
  function lowCard() {
    const p = overview.prices;
    if (!p) return null;
    const full = (p.front || 0) + (p.back_picture || 0) + (p.sleeve || 0);
    const low = overview.clients.filter((c) => c.has_page && c.balance < full);
    return card('Clients running low', low.length
      ? h('table', { class: 'dash-table' }, h('tbody', null, low.map((c) => h('tr', null,
        h('td', null, h('a', { href: clientHash(c.slug) }, c.name)),
        h('td', null, h('span', { class: 'badge badge-danger' }, `${plain(c.balance)} tokens left`)),
        h('td', { class: 'muted' }, c.last_job_at ? `Last job ${fmtDateTime(c.last_job_at)}` : 'No job yet')))))
      : h('p', { class: 'hint' }, 'No client is running low.'),
    { note: `Fewer tokens than a full set costs (${fmtInt(full)}), so you can remind them.` });
  }

  /* ---------- adding tokens by hand (and, for the head admin, taking some back) ---------- */
  /** `c`: the client whose page this is, or null for the To do tab's form, which has a client picker. */
  function addCard(c) {
    const canTakeBack = Boolean(overview.can_take_back);
    const pick = c ? null : h('select', { class: 'select', id: 'add-client' },
      h('option', { value: '' }, 'Choose a client'), overview.clients.map((x) => h('option', { value: x.slug }, x.name)));
    const tokensIn = inputEl({ type: 'number', name: 'tokens', step: '1', min: canTakeBack ? null : '1', inputmode: 'numeric', placeholder: '1000' });
    const takaIn = inputEl({ type: 'number', name: 'taka', step: '1', min: '0', inputmode: 'numeric', placeholder: '5000' });
    const noteIn = inputEl({ name: 'note', maxlength: '300', placeholder: 'e.g. how they paid, and its reference' });
    const addBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, icon('plus'), 'Add tokens');
    const form = h('form', { class: ['cb-add', pick && 'has-client'] }, pick ? field('Client', pick) : null,
      field('Number of tokens', tokensIn), field('Taka received (৳)', takaIn), field('Note', noteIn), addBtn);
    // this form's change until it is answered: the same client, tokens, taka and note sent again (after a lost
    // answer) are the same request, which the server records once (2026-11-23 review CHAOS-4)
    let pending = null; // {id, sig}
    const clearForm = () => {
      tokensIn.value = '';
      takaIn.value = '';
      noteIn.value = '';
      pending = null;
    };
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const slug = c ? c.slug : pick.value;
      if (!slug) {
        toast('Choose the client first.', { kind: 'warning' });
        pick.focus();
        return;
      }
      const n = Number(tokensIn.value);
      if (!tokensIn.value.trim() || !Number.isInteger(n) || n === 0) {
        toast(canTakeBack ? 'Enter a whole number other than 0.' : 'Enter a whole number above 0.', { kind: 'warning' });
        tokensIn.focus();
        return;
      }
      if (n < 0 && !canTakeBack) {
        toast('Only the head admin can take tokens back.', { kind: 'warning' });
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
      let balance = null;
      let available = null;
      try { // the purse now: jobs may have been charged since the page was loaded
        ({ balance, available } = (await api.tokenClient(slug, prefs.range)).client);
      } catch (_) {
        /* the dialog then shows no balance; the server does the sum and the checks either way */
      }
      if (n < 0 && typeof available === 'number' && -n > available) { // the server would refuse it: say so now
        toast(`Only ${fmtInt(Math.max(0, available))} tokens can be taken back now.`, { kind: 'warning' });
        return;
      }
      const ok = await confirmDialog({
        title: n > 0 ? `Add ${fmtInt(n)} tokens to ${nameOf(slug)}?` : `Take back ${fmtInt(-n)} tokens from ${nameOf(slug)}?`,
        message: `${balance === null ? '' : `Balance: ${plain(balance)} → ${plain(balance + n)}`}${paid !== null ? ` · Taka received: ${taka(paid)}` : ''}` || 'This is recorded with your note.',
        confirmLabel: n > 0 ? 'Add tokens' : 'Take back tokens',
        danger: n < 0,
      });
      if (!ok) return;
      const sig = JSON.stringify([slug, n, paid, note]);
      if (!pending || pending.sig !== sig) pending = { id: newRequestId(), sig };
      const requestId = pending.id;
      setBusy(addBtn, true, 'Saving…');
      try {
        const res = await api.addTokens(slug, n, note, paid, requestId);
        toast(res.repeated ? `That change was already recorded, so it was not made again. Balance: ${plain(res.balance)}`
          : `Done. ${nameOf(slug)}’s new balance: ${plain(res.balance)}`, { kind: 'success' });
        clearForm();
        await load();
      } catch (err) {
        let landed = false;
        if (err.status === 0) { // no answer: it may still have been recorded. Look before saying it failed.
          try {
            landed = (await api.tokenClient(slug, prefs.range)).topups.some((t) => t.request_id === requestId);
          } catch (_) {
            /* still no connection: say so below; pressing again can't record it twice */
          }
        }
        if (landed) {
          toast('The connection dropped, but the change was recorded. It was not made twice.', { kind: 'success' });
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
    return card(canTakeBack ? 'Add or take back tokens' : 'Add tokens', [form,
      h('p', { class: 'hint' }, 'For money that came another way (cash, bank), or free tokens: a bKash payment is added by approving it. Taka received: what the client paid for these tokens (leave it empty for free tokens). '
        + (canTakeBack ? 'A negative number of tokens takes tokens back (a correction); only the head admin can do that. '
          : 'Taking tokens back stays with the head admin. ')
        + 'Every change is kept with its note, the time and who made it.')]);
  }

  /* ---------- Clients ---------- */
  function clientFilters(slug) {
    const range = segmented({
      legend: 'Period', value: prefs.range, options: RANGES.map(([value, label]) => ({ value, label })),
      onChange: (v) => { prefs.range = v; savePrefs(); load(); },
    });
    const names = overview.clients.map((c) => [c.slug, c.name]);
    if (slug && !names.some(([s]) => s === slug)) names.push([slug, slug]);
    const picker = h('select', { class: 'select', 'aria-label': 'Client' }, h('option', { value: '' }, 'All clients'),
      names.map(([value, name]) => h('option', { value }, name)));
    picker.value = slug;
    picker.addEventListener('change', () => { window.location.hash = clientHash(picker.value); });
    const search = inputEl({ type: 'search', placeholder: 'Search clients', 'aria-label': 'Search clients', value: query });
    search.addEventListener('input', () => {
      query = search.value.trim().toLowerCase();
      replace(listBox, clientsList());
    });
    return h('div', { class: 'dash-filters' }, range.el,
      h('label', { class: 'dash-type' }, h('span', { class: 'field-label' }, 'Client'), picker),
      slug ? null : h('label', { class: 'dash-type' }, h('span', { class: 'field-label' }, 'Search'), search));
  }

  const listBox = h('div', { class: 'dash-scroll' });
  const money = (v) => `$${v >= 1 ? v.toFixed(2) : v.toFixed(3)}`;

  function clientsList() {
    const all = overview.clients;
    const list = query ? all.filter((c) => c.name.toLowerCase().includes(query) || c.slug.includes(query)) : all;
    if (!all.length) {
      return h('div', { class: 'empty' },
        h('div', { class: 'empty-icon' }, icon('chart')),
        h('p', { class: 'empty-title' }, 'No clients yet'),
        h('p', { class: 'empty-text' }, 'A client appears here once it has its own page.'));
    }
    if (!list.length) return h('p', { class: 'hint' }, 'No client matches the search.');
    const heads = ['Client', 'Phone', 'Balance now', 'Used', 'Added', 'Taka received', 'Jobs',
      ...(costs ? ['Real cost', 'Real cost per 100 tokens'] : []), 'Last job'];
    const words = new Set(['Client', 'Phone', 'Last job']); // the other columns are numbers
    return h('table', { class: 'dash-table' },
      h('thead', null, h('tr', null, heads.map((t) => h('th', {
        class: [words.has(t) ? null : 'num', t.startsWith('Real cost') ? 'only-you' : null],
      }, t)))),
      h('tbody', null, list.map((c) => {
        const k = costs ? costs.get(c.slug) || { cost: 0, tokens: 0 } : null;
        return h('tr', null,
          h('td', null, h('a', { href: clientHash(c.slug) }, c.name),
            c.has_page ? null : [' ', h('span', { class: 'badge' }, 'No page now')]),
          h('td', { class: 'nowrap' }, c.phone ? h('a', { href: `tel:${c.phone.replace(/[^0-9+]/g, '')}` }, c.phone) : '–'),
          h('td', { class: 'num' }, h('strong', null, plain(c.balance))),
          h('td', { class: 'num' }, fmtInt(c.used)),
          h('td', { class: 'num' }, signed(c.added)),
          h('td', { class: 'num' }, c.taka ? taka(c.taka) : '–'),
          h('td', { class: 'num' }, fmtInt(c.jobs)),
          k ? h('td', { class: 'num' }, money(k.cost)) : null,
          k ? h('td', { class: 'num' }, k.tokens ? money((k.cost / k.tokens) * 100) : '–') : null,
          h('td', { class: 'nowrap' }, c.last_job_at ? fmtDateTime(c.last_job_at) : '–'));
      })));
  }

  function renderOverview() {
    const all = overview.clients;
    const total = (key) => all.reduce((acc, c) => acc + (c[key] || 0), 0);
    const realCost = costs ? [...costs.values()].reduce((acc, k) => acc + k.cost, 0) : null;
    replace(listBox, clientsList());
    replace(panel, offNote(), clientFilters(''),
      h('div', { class: 'dash-tiles' },
        tile('Clients', fmtInt(all.length)),
        tile('Tokens in all purses', plain(total('balance')), h('span', null, 'Balance now')),
        tile('Used', fmtInt(total('used')), h('span', null, periodText())),
        tile('Added', signed(total('added')), h('span', null, periodText())),
        tile('Taka received', taka(total('taka')), h('span', null, periodText())),
        realCost === null ? tile('Jobs', fmtInt(total('jobs')), h('span', null, periodText()))
          : tile('Real cost · only you', money(realCost), h('span', null, 'What OpenAI and Claude charged'), 'only-you')),
      card('Clients', [listBox, h('p', { class: 'hint' }, costs
        ? 'Press a client’s name for their own page. The real-cost columns exist only here, not on the Partner dashboard.'
        : 'Press a client’s name for their own page: their jobs, payments and tokens.')],
      { actions: h('button', { type: 'button', class: 'btn btn-sm btn-primary', onClick: () => openBuilder() }, icon('plus'), 'Add a customer') }),
      pricesCard(overview.prices));
  }

  function renderDetail() {
    const back = h('a', { class: 'btn btn-sm btn-ghost', href: '#clients' }, icon('arrow-left'), 'All clients');
    if (detail.missing) {
      replace(panel, clientFilters(detail.missing), h('div', { class: 'cb-head' }, back), callout('error', 'No such client.'));
      return;
    }
    const c = detail.client;
    const made = detail.jobs.filter((j) => !j.deleted).length;
    const k = costs ? costs.get(c.slug) || { cost: 0, tokens: 0 } : null;
    const jobsBody = detail.jobs.length ? jobsTable(detail.jobs, { openChats, ids: true }) : h('div', { class: 'empty' },
      h('p', { class: 'empty-title' }, 'No jobs in this period'),
      h('p', { class: 'empty-text' }, 'This client’s jobs appear here with the tokens each one used.'));
    replace(panel, offNote(), clientFilters(c.slug),
      h('div', { class: 'cb-head' }, back, h('h3', { class: 'cb-name' }, c.name),
        c.has_page ? null : h('span', { class: 'badge' }, 'No page now')),
      h('div', { class: 'dash-tiles' },
        tile('Balance now', plain(c.balance), h('span', null, c.available < c.balance
          ? `${fmtInt(Math.max(0, c.available))} free · ${fmtInt(c.balance - c.available)} held for running jobs`
          : 'Tokens left for new jobs')),
        tile('Used', fmtInt(c.used), h('span', null, periodText())),
        tile('Added', signed(c.added), h('span', null, periodText())),
        tile('Taka received', taka(c.taka), h('span', null, periodText())),
        k ? tile('Real cost · only you', money(k.cost), h('span', null, k.tokens ? `${money((k.cost / k.tokens) * 100)} per 100 tokens` : periodText()), 'only-you')
          : tile('Jobs', fmtInt(made), h('span', null, periodText()))),
      entryCard(detail.entry),
      card('Jobs', jobsBody, { note: periodText(), bodyClass: 'dash-scroll' }),
      card('bKash payments', detail.payments.length ? paysTable(detail.payments, { withClient: false })
        : h('p', { class: 'hint' }, 'This client has sent no bKash payment yet.'),
      { note: 'Every payment this client sent, whatever happened to it.', bodyClass: 'dash-scroll' }),
      addCard(c),
      topupsCard(detail.topups),
      pricesCard(detail.prices));
  }

  /* ---------- a client's entry point, and the Customer EntryPoint Builder (app/customers.py) ---------- */
  async function copy(text, what) {
    const ok = await copyText(text);
    toast(ok ? `${what} copied.` : 'Copy failed. Select the text and press Ctrl+C.', { kind: ok ? 'success' : 'error', timeout: ok ? 2000 : undefined });
  }

  const copyBtn = (text, what) => h('button', { type: 'button', class: 'btn btn-sm', onClick: () => copy(text, what) }, icon('copy'), 'Copy');
  const codeRow = (label, text, what) => [h('dt', null, label), h('dd', { class: 'entry-value' }, h('code', { class: 'pay-code' }, text), copyBtn(text, what))];

  /** How a client gets in: the link, the login, the phone number. The password is the head admin's to see here (the
   * server sends it to no one else); the partner sees it when it makes the customer. */
  function entryCard(entry) {
    if (!entry) return null;
    const link = `${window.location.origin}${entry.path}`;
    return card('Entry point', h('dl', { class: 'spec-list entry-list' },
      h('dt', null, 'Link'), h('dd', { class: 'entry-value' }, h('a', { href: entry.path, target: '_blank', rel: 'noopener' }, link), copyBtn(link, 'The link')),
      entry.username ? codeRow('Username', entry.username, 'The username') : [h('dt', null, 'Login'), h('dd', null, 'No login of its own: only the head admin can open the page.')],
      entry.password !== undefined ? codeRow('Password', entry.password, 'The password')
        : entry.username ? [h('dt', null, 'Password'), h('dd', { class: 'muted' }, 'Shown when the customer was made. The head admin can see it on the Supreme dashboard.')] : null,
      h('dt', null, 'Phone'), h('dd', null, entry.phone ? h('a', { href: `tel:${entry.phone.replace(/[^0-9+]/g, '')}` }, entry.phone) : '–'),
      entry.made_at ? [h('dt', null, 'Made'), h('dd', null, `${fmtDateTime(entry.made_at)} by the ${who(entry.made_by).toLowerCase()}`)] : null),
    { note: 'What the customer needs to get in.' });
  }

  /** "Add a customer": name, phone, the link's name (suggested, editable) and the logo; a preview with the colours
   * picked from the logo; then the page, its login and the welcome tokens are made at once. */
  function openBuilder() {
    const origin = window.location.origin;
    const welcome = overview.welcome || 0;
    const nameIn = inputEl({ maxlength: '80', placeholder: 'e.g. Dhaka Tigers FC', autocomplete: 'off' });
    const phoneIn = inputEl({ type: 'tel', maxlength: '24', placeholder: '01XXXXXXXXX', autocomplete: 'off' });
    const slugIn = inputEl({ maxlength: '32', spellcheck: 'false', autocapitalize: 'none', autocomplete: 'off', class: 'input builder-slug' });
    const fileIn = h('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp', class: 'builder-file' });
    const slugNote = h('p', { class: 'hint' }, 'Small letters and digits. It is suggested from the name; you can change it.');
    const look = h('div', { class: 'builder-look' }, h('p', { class: 'hint' }, 'Choose the logo to see how the page will look.'));
    const createBtn = h('button', { type: 'button', class: 'btn btn-primary' }, icon('plus'), 'Create the entry point');
    const cancelBtn = h('button', { type: 'button', class: 'btn' }, 'Cancel');
    const body = h('div', { class: 'builder' },
      h('div', { class: 'field-grid' }, field('Customer name', nameIn), field('Phone number', phoneIn, 'To reach the customer. Shown in full on both dashboards.')),
      h('div', { class: 'field' }, h('span', { class: 'field-label' }, 'Entry point link'),
        h('div', { class: 'builder-link-row' }, h('span', { class: 'builder-origin' }, `${origin}/client_`), slugIn), slugNote),
      h('div', { class: 'field' }, h('span', { class: 'field-label' }, 'Logo'), fileIn,
        h('p', { class: 'hint' }, 'A PNG, JPEG or WebP picture, up to 5 MB. It is used as uploaded.')),
      look,
      welcome ? h('p', { class: 'hint' }, `The customer starts with ${fmtInt(welcome)} tokens.`) : null);
    const dlg = modal({ title: 'Customer EntryPoint Builder', className: 'builder-dialog', body, footer: [cancelBtn, createBtn] });
    let slugEdited = false;
    let slugFree = false;
    let colors = null; // the three colours: picked from the logo until one is changed here
    let seq = 0;
    let timer = 0;

    function drawLook(res) {
      const mock = (mode) => {
        const el = h('div', { class: ['builder-mock', `is-${mode}`] },
          h('div', { class: 'builder-band' }),
          h('div', { class: 'builder-head' }, h('span', { class: 'builder-logo' }, h('img', { src: res.mark, alt: '' })),
            h('span', { class: 'builder-name' }, nameIn.value.trim() || 'Customer name')),
          h('div', { class: 'builder-body' }, h('span', { class: 'builder-step' }, '1'), h('span', { class: 'builder-text' }, 'A link'),
            h('span', { class: 'builder-btn' }, 'Generate')),
          h('span', { class: 'builder-tag' }, mode === 'light' ? 'Light' : 'Dark'));
        for (const [key, value] of Object.entries({ ...res.light, ...(mode === 'dark' ? res.dark : {}) })) el.style.setProperty(key, value);
        return el;
      };
      const pickers = [['primary', 'Main'], ['secondary', 'Second'], ['tertiary', 'Third']].map(([role, label]) => {
        const input = h('input', { type: 'color', value: res.colors[role], 'aria-label': `${label} colour` });
        input.addEventListener('change', () => {
          colors = { ...res.colors, [role]: input.value };
          ask(true);
        });
        return h('label', { class: 'builder-color' }, input, h('span', null, label));
      });
      replace(look,
        h('div', { class: 'builder-mocks' }, mock('light'), mock('dark')),
        h('div', { class: 'builder-colors' }, h('span', { class: 'field-label' }, 'Colours'), pickers,
          h('span', { class: 'hint' }, colors ? 'Changed by you.' : 'Picked from the logo. Change one if it doesn’t look right.')),
        h('div', { class: 'builder-signin' }, h('span', { class: 'hint' }, 'On the sign-in page:'), h('img', { src: res.logo, alt: '' })));
    }

    /** Ask the server: the suggested link name, whether the typed one is free, and (with the logo) how it looks. */
    async function ask(withLogo) {
      const mine = ++seq;
      const form = new FormData();
      form.append('name', nameIn.value);
      form.append('slug', slugEdited ? slugIn.value : '');
      const file = fileIn.files[0];
      if (withLogo && file) {
        form.append('logo', file, file.name);
        if (colors) form.append('colors', JSON.stringify(colors));
      }
      try {
        const res = await api.customerPreview(form);
        if (mine !== seq || !dlg.isConnected) return;
        if (!slugEdited) slugIn.value = nameIn.value.trim() ? res.suggested : '';
        const slug = slugIn.value;
        slugFree = Boolean(slug) && !(slugEdited && res.slug_problem);
        slugNote.classList.toggle('is-bad', Boolean(slugEdited && res.slug_problem));
        slugNote.textContent = slugEdited && res.slug_problem ? res.slug_problem
          : slug ? `Free. The customer’s page will be ${origin}/client_${slug}` : 'Small letters and digits. It is suggested from the name; you can change it.';
        if (res.logo) drawLook(res);
      } catch (err) {
        if (mine !== seq || !dlg.isConnected) return;
        if (withLogo && file) replace(look, callout('error', err.message));
        else toast(err.message, { kind: 'error' });
      }
    }

    const soon = (withLogo) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => ask(withLogo), 350);
    };
    nameIn.addEventListener('input', () => soon(false));
    nameIn.addEventListener('change', () => { if (fileIn.files[0]) ask(true); }); // the name in the preview follows
    slugIn.addEventListener('input', () => {
      const cleaned = slugIn.value.toLowerCase().replace(/[^a-z0-9]/g, '');
      if (cleaned !== slugIn.value) slugIn.value = cleaned;
      slugEdited = Boolean(cleaned);
      soon(false);
    });
    fileIn.addEventListener('change', () => {
      colors = null;
      if (fileIn.files[0]) {
        replace(look, loadingRow('Preparing the preview…'));
        ask(true);
      }
    });
    cancelBtn.addEventListener('click', () => closeDialog(dlg));

    function showResult(res) {
      const c = res.client;
      const link = `${origin}${c.path}`;
      const message = [`${c.name}: আপনার জার্সি ডিজাইন পেজ রেডি।`, `লিংক: ${link}`, `ইউজারনেম: ${c.username}`, `পাসওয়ার্ড: ${c.password}`,
        res.tokens ? `আপনার অ্যাকাউন্টে ${res.tokens} টোকেন অ্যাড করা হয়েছে।` : null].filter(Boolean).join('\n');
      const area = h('textarea', { class: 'input builder-message', readonly: true, rows: '5', 'aria-label': 'A message for the customer' });
      area.value = message;
      const done = h('button', { type: 'button', class: 'btn btn-primary' }, 'Done');
      done.addEventListener('click', () => closeDialog(dlg));
      replace(dlg.querySelector('.modal-body'), h('div', { class: 'builder' },
        h('p', { class: 'pay-clear' }, icon('check'), `${c.name}’s entry point is ready and works now.`),
        h('dl', { class: 'spec-list entry-list' },
          h('dt', null, 'Link'), h('dd', { class: 'entry-value' }, h('a', { href: c.path, target: '_blank', rel: 'noopener' }, link), copyBtn(link, 'The link')),
          codeRow('Username', c.username, 'The username'), codeRow('Password', c.password, 'The password'),
          h('dt', null, 'Phone'), h('dd', null, c.phone),
          h('dt', null, 'Tokens'), h('dd', null, res.tokens ? `${fmtInt(res.tokens)} welcome tokens added` : 'None added (tokens are off, or the welcome tokens are set to 0)')),
        callout(overview.can_take_back ? 'info' : 'warning', overview.can_take_back
          ? 'You can see this login again on the customer’s page here, under Clients.'
          : 'Copy the password now: after you close this, only the head admin can see it again.'),
        h('div', { class: 'field' }, h('span', { class: 'field-label' }, 'A message to send the customer'), area,
          h('div', null, h('button', { type: 'button', class: 'btn btn-sm', onClick: () => copy(message, 'The message') }, icon('copy'), 'Copy the message')))));
      replace(dlg.querySelector('.modal-footer'), done);
      done.focus();
    }

    createBtn.addEventListener('click', async () => {
      const file = fileIn.files[0];
      const problems = [[!nameIn.value.trim(), 'Enter the customer’s name.', nameIn], [!phoneIn.value.trim(), 'Enter the customer’s phone number.', phoneIn],
        [!slugIn.value, 'Enter the link’s name.', slugIn], [!file, 'Choose the customer’s logo.', fileIn]];
      const hit = problems.find(([bad]) => bad);
      if (hit) {
        toast(hit[1], { kind: 'warning' });
        hit[2].focus();
        return;
      }
      const form = new FormData();
      form.append('name', nameIn.value);
      form.append('phone', phoneIn.value);
      form.append('slug', slugIn.value);
      if (colors) form.append('colors', JSON.stringify(colors));
      form.append('logo', file, file.name);
      setBusy(createBtn, true, 'Creating…');
      try {
        const res = await api.customerCreate(form);
        showResult(res);
        load({ quiet: true });
      } catch (err) {
        toast(err.message, { kind: 'error' });
        if (createBtn.isConnected) setBusy(createBtn, false);
      }
    });
    nameIn.focus();
  }

  /** Tokens added to or taken from purses. `withClient`: a Client column (the Payments tab's list of every client). */
  function topupsTable(list, { withClient = false } = {}) {
    if (!list.length) return h('p', { class: 'hint' }, 'No tokens added yet.');
    const heads = ['Time', withClient ? 'Client' : null, 'Tokens', 'Taka received', 'Note', 'By', 'Balance after'].filter(Boolean);
    return h('table', { class: 'dash-table' },
      h('thead', null, h('tr', null, heads.map((t) => h('th', {
        class: t === 'Tokens' || t === 'Taka received' || t === 'Balance after' ? 'num' : null,
      }, t)))),
      h('tbody', null, list.map((t) => h('tr', null,
        h('td', { class: 'nowrap' }, fmtDateTime(t.at)),
        withClient ? h('td', null, h('a', { href: clientHash(t.client) }, t.client_name)) : null,
        h('td', { class: ['num', t.tokens < 0 && 'is-minus'] }, h('strong', null, signed(t.tokens))),
        h('td', { class: 'num' }, typeof t.taka === 'number' ? taka(t.taka) : '–'),
        h('td', null, t.trx ? [h('span', { class: 'badge badge-accent' }, 'bKash'), ' ', h('code', { class: 'pay-code' }, t.trx)] : t.note || ''),
        h('td', null, who(t.by)),
        h('td', { class: 'num' }, t.balance === undefined || t.balance === null ? '–' : plain(t.balance))))));
  }

  function topupsCard(list) {
    return card('Tokens added and taka received', topupsTable(list), { bodyClass: 'dash-scroll' });
  }

  function pricesCard(prices) {
    if (!prices) return null;
    return card('Token prices', [pricesParts(prices),
      h('p', { class: 'hint' }, 'A part that fails is not charged. Making it again is charged when it works.')]);
  }

  /* ---------- Payments ---------- */
  function renderPayments() {
    const all = pays.payments;
    const count = (status) => all.filter((p) => p.status === status).length;
    const chips = [['all', 'All', all.length], ['waiting', 'Waiting', count('waiting')],
      ['approved', 'Approved', count('approved')], ['rejected', 'Rejected', count('rejected')]];
    if (!chips.some(([key]) => key === prefs.payFilter)) prefs.payFilter = 'all';
    const order = { waiting: 0, approved: 1, rejected: 1 }; // the waiting first, then the checked, newest first
    const shown = all.filter((p) => prefs.payFilter === 'all' || p.status === prefs.payFilter)
      .sort((a, b) => (order[a.status] - order[b.status]) || 0);
    const approved = all.filter((p) => p.status === 'approved');
    replace(panel, offNote(),
      h('div', { class: 'dash-tiles' },
        tile('Waiting to be checked', fmtInt(count('waiting'))),
        tile('Approved', fmtInt(approved.length), h('span', null, 'All time')),
        tile('Taka received by bKash', taka(approved.reduce((acc, p) => acc + p.taka, 0)), h('span', null, 'All time')),
        tile('Rejected', fmtInt(count('rejected')), h('span', null, 'All time'))),
      card('bKash payments', shown.length ? paysTable(shown) : h('p', { class: 'hint' }, all.length ? 'None with this status.' : 'No client has sent a bKash payment yet.'), {
        bodyClass: 'dash-scroll',
        actions: h('div', { class: 'pay-chips', role: 'group', 'aria-label': 'Status' }, chips.map(([key, label, n]) => h('button', {
          type: 'button', class: 'btn btn-sm pay-chip', 'aria-pressed': String(prefs.payFilter === key),
          onClick: () => { prefs.payFilter = key; savePrefs(); renderPayments(); },
        }, `${label} (${fmtInt(n)})`))),
      }),
      card('Tokens added or taken back by hand', topups ? topupsTable(topups.topups, { withClient: true }) : loadingRow('Loading…'),
        { note: 'For every client. A bKash payment’s tokens are in the list above.', bodyClass: 'dash-scroll' }));
  }

  /* ---------- Jobs: every client's jobs, day by day (tokens only) ---------- */
  function renderJobs() {
    const range = segmented({
      legend: 'Period', value: prefs.jobsRange, options: RANGES.map(([value, label]) => ({ value, label })),
      onChange: (v) => { prefs.jobsRange = v; savePrefs(); load(); },
    });
    const picker = h('select', { class: 'select', 'aria-label': 'Client' }, h('option', { value: '' }, 'All clients'),
      overview.clients.map((c) => h('option', { value: c.slug }, c.name)));
    picker.value = prefs.jobsClient;
    if (picker.value !== prefs.jobsClient) prefs.jobsClient = ''; // a client that is gone
    picker.addEventListener('change', () => { prefs.jobsClient = picker.value; savePrefs(); load(); });
    const groups = new Map();
    for (const job of (jobs ? jobs.jobs : [])) {
      const key = dayOf(job.created_at);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(job);
    }
    const dayCards = [...groups].map(([day, list]) => {
      const tokens = list.reduce((acc, j) => acc + (j.tokens || 0), 0);
      return card(day ? dayLabel(day) : 'Unknown day', jobsTable(list, { openChats, ids: true, client: true }), {
        note: `${fmtInt(list.length)} ${list.length === 1 ? 'job' : 'jobs'} · ${fmtInt(tokens)} tokens`, bodyClass: 'dash-scroll',
      });
    });
    replace(panel, offNote(),
      h('div', { class: 'dash-filters' }, range.el,
        h('label', { class: 'dash-type' }, h('span', { class: 'field-label' }, 'Client'), picker),
        h('p', { class: 'hint board-filter-note' }, 'Every job your clients made, newest first, with the tokens each part cost.')),
      !jobs ? loadingRow('Loading…') : dayCards.length ? h('div', { class: 'dash-days' }, dayCards)
        : h('div', { class: 'empty' },
          h('div', { class: 'empty-icon' }, icon('chart')),
          h('p', { class: 'empty-title' }, 'No jobs in this period'),
          h('p', { class: 'empty-text' }, 'Jobs your clients make appear here with the tokens each one used.')));
  }

  load();
  return {
    /** Read everything again (the Supreme dashboard, after it changed something the tabs show). */
    reload: () => load(),
    overview: () => overview,
    payments: () => pays,
  };
}
