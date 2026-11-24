/**
 * A client page's "Customer dashboard" tab (/client_<name>, 2026-11-23; the owner's name for it): the client's own
 * purse of our tokens: what is left, what each of its jobs used (a failed panel is free) and what it paid and when
 * (GET /api/tokens/mine). In the client's language, Bangla, through i18n.js. Without API costs and without the
 * studio's notes on top-ups. The studio login sees the same tab for the client whose page it is on. "Buy tokens" is
 * there but buys nothing yet (the owner: "add the buy token button, but dont do anything with it"): it says so.
 */
import { api } from './api.js';
import { h, icon, replace, fmtDateTime, fmtInt } from './dom.js';
import { jobsTable, pricesParts } from './tokenviews.js';
import { onPurse } from './tokens.js';
import { callout, loadingRow, setBusy, toast } from './ui.js';

const MINUS = '−';
const taka = (n) => `৳${fmtInt(n || 0)}`;
const signed = (n) => (n < 0 ? `${MINUS}${fmtInt(-n)}` : `+${fmtInt(n)}`);
const plain = (n) => (n < 0 ? `${MINUS}${fmtInt(-n)}` : fmtInt(n));

export function initCustomerBoard(root, { client }) {
  let data = null;
  let active = false;
  let seq = 0;
  const openChats = new Set(); // the Design chats opened, kept across reloads
  const refreshBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('refresh'), 'Refresh');
  refreshBtn.addEventListener('click', () => load());
  // not wired to any payment yet: it only says so
  const buyBtn = h('button', { type: 'button', class: 'btn btn-sm btn-primary' }, icon('plus'), 'Buy tokens');
  buyBtn.addEventListener('click', () => toast('Buying tokens here is coming soon. For now, please contact the head admin.', { kind: 'info' }));
  const body = h('div', { class: 'dash-body' }, loadingRow('Loading…'));
  replace(root, h('div', { class: 'customerboard' },
    h('div', { class: 'history-toolbar' }, h('h2', null, 'Customer dashboard'), h('span', { class: 'spacer' }), refreshBtn, buyBtn),
    body));

  async function load() {
    const mine = ++seq;
    setBusy(refreshBtn, true, 'Loading…');
    try {
      const next = await api.myTokens(client);
      if (mine !== seq) return;
      data = next;
      render();
    } catch (err) {
      if (mine === seq) replace(body, callout('error', err.message));
    } finally {
      if (mine === seq) setBusy(refreshBtn, false);
    }
  }

  function tile(label, value, sub) {
    return h('div', { class: 'dash-tile' },
      h('div', { class: 'dash-tile-label' }, label),
      h('div', { class: 'dash-tile-value' }, value),
      sub ? h('div', { class: 'dash-tile-sub' }, sub) : null);
  }

  function render() {
    if (!data.enabled) {
      replace(body, callout('info', 'Tokens are not in use.'));
      return;
    }
    replace(body,
      data.charged ? null : callout('info', 'Seen with the head admin login: these are this client’s tokens.'),
      h('div', { class: 'dash-tiles' },
        tile('Tokens left', fmtInt(Math.max(0, data.available)), h('span', null, 'For new jobs')),
        tile('Tokens used', fmtInt(data.used), h('span', null, 'In all')),
        tile('Tokens added', fmtInt(data.added), h('span', null, 'In all')),
        tile('Paid', taka(data.taka), h('span', null, 'In all'))),
      h('section', { class: 'card' },
        h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'Token prices')),
        h('div', { class: 'card-body' }, pricesParts(data.prices),
          h('p', { class: 'hint' }, 'A panel that fails is free; making it again is charged only when it works.'))),
      h('section', { class: 'card' },
        h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'Token use')),
        h('div', { class: 'card-body dash-scroll' }, jobsTable(data.jobs, { openChats }) || h('div', { class: 'empty' },
          h('p', { class: 'empty-title' }, 'No jobs yet'),
          h('p', { class: 'empty-text' }, 'Your jobs appear here with the tokens each one used.')))),
      h('section', { class: 'card' },
        h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'Payments and tokens added')),
        h('div', { class: 'card-body dash-scroll' }, payments(data.payments))));
  }

  function payments(list) {
    if (!list.length) return h('p', { class: 'hint' }, 'No tokens added yet.');
    return h('table', { class: 'dash-table' },
      h('thead', null, h('tr', null, ['Time', 'Tokens', 'Paid', 'Balance after']
        .map((t, i) => h('th', { class: i ? 'num' : null }, t)))),
      h('tbody', null, list.map((p) => h('tr', null,
        h('td', { class: 'nowrap' }, fmtDateTime(p.at), p.tokens < 0 ? h('div', { class: 'dash-sub' }, 'Correction') : null),
        h('td', { class: 'num' }, h('strong', null, signed(p.tokens))),
        h('td', { class: 'num' }, typeof p.taka === 'number' ? taka(p.taka) : '–'),
        h('td', { class: 'num' }, typeof p.balance === 'number' ? plain(p.balance) : '–')))));
  }

  // a job ended or tokens were added (the purse is read again): the open tab follows
  onPurse(() => {
    if (active) load();
  });

  return {
    activate() {
      active = true;
      load();
    },
    deactivate() {
      active = false;
    },
  };
}
