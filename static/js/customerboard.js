/**
 * A client page's "Customer dashboard" tab (/client_<name>, 2026-11-23; the owner's name for it): the client's own
 * purse of our tokens: what is left, what each of its jobs used (a failed panel is free) and what it paid and when
 * (GET /api/tokens/mine). In the client's language, Bangla, through i18n.js. Without API costs and without the
 * notes on top-ups. The head admin sees the same tab for the client whose page it is on.
 * "Buy tokens" (2026-11-24, app/payments.py; the owner: "lets do manual"): the client picks a package, sends the
 * money with bKash (Send Money, to the number the head admin set) and types the TrxID bKash gave. The payment then
 * waits, listed here, until the head admin or the partner has checked it: the tokens are added, or it is rejected
 * with a reason. While no number or package is set, the button only says buying is coming.
 */
import { api, newRequestId } from './api.js';
import { h, icon, replace, fmtDateTime, fmtInt } from './dom.js';
import { jobsTable, pricesParts } from './tokenviews.js';
import { onPurse, purse, refreshPurse } from './tokens.js';
import { callout, closeDialog, copyText, field, inputEl, loadingRow, modal, setBusy, toast } from './ui.js';

const MINUS = '−';
const taka = (n) => `৳${fmtInt(n || 0)}`;
const signed = (n) => (n < 0 ? `${MINUS}${fmtInt(-n)}` : `+${fmtInt(n)}`);
const plain = (n) => (n < 0 ? `${MINUS}${fmtInt(-n)}` : fmtInt(n));
const TRX_RE = /^[A-Za-z0-9]{8,16}$/;
const NUMBER_RE = /^(?:\+?88)?01[3-9]\d{8}$/;
// why a payment was rejected, as the client reads it (the dashboards have their own, shorter words)
const REASONS = {
  not_found: 'This TrxID was not found in bKash. Check the TrxID and send it again.',
  amount: 'The amount does not match the package. Please contact the head admin.',
  used: 'This TrxID was already used.',
};

export function initCustomerBoard(root, { client }) {
  let data = null;
  let active = false;
  let seq = 0;
  const openChats = new Set(); // the Design chats opened, kept across reloads
  const refreshBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('refresh'), 'Refresh');
  refreshBtn.addEventListener('click', () => load());
  const buyBtn = h('button', { type: 'button', class: 'btn btn-sm btn-primary' }, icon('plus'), 'Buy tokens');
  buyBtn.addEventListener('click', () => buy());
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
      // tokens were added (a payment verified) or used since the header's chip last looked: it follows at once,
      // not at its next half minute
      const held = purse();
      if (held && next.enabled && held.balance !== next.balance) refreshPurse();
    } catch (err) {
      if (mine === seq) replace(body, callout('error', err.message));
    } finally {
      if (mine === seq) setBusy(refreshBtn, false);
    }
  }

  /* ---------- Buy tokens: a bKash payment, checked by hand ---------- */
  function buy() {
    if (!data || !data.buy || !data.buy.enabled) {
      toast('Buying tokens here is coming soon. For now, please contact the head admin.', { kind: 'info' });
      return;
    }
    if (!data.charged) { // the head admin looking at a client's page
      toast('Only the client’s own login can send a payment. You can add tokens on the Partner dashboard.', { kind: 'info' });
      return;
    }
    const { packages, number } = data.buy;
    const full = data.prices ? (data.prices.front || 0) + (data.prices.back_picture || 0) + (data.prices.sleeve || 0) : 0;
    let chosen = packages[packages.length - 1]; // the largest first: the usual one
    const requestId = newRequestId(); // this dialog's payment: sent again after a lost answer, it is recorded once
    const amount = h('strong', null);
    const cards = h('div', { class: 'buy-packages', role: 'radiogroup', 'aria-label': 'Package' });
    const drawCards = () => {
      replace(cards, packages.map((p) => {
        const sets = full ? Math.floor(p.tokens / full) : 0;
        const input = h('input', { type: 'radio', name: 'buy-package', class: 'visually-hidden', checked: p === chosen });
        input.addEventListener('change', () => {
          chosen = p;
          drawCards();
        });
        return h('label', { class: ['buy-package', p === chosen && 'is-on'] }, input,
          h('span', { class: 'buy-package-main' }, h('span', null, `${fmtInt(p.tokens)} tokens`), ' · ', h('span', null, taka(p.taka))),
          sets ? h('span', { class: 'hint' }, `About ${fmtInt(sets)} full sets`) : null);
      }));
      replace(amount, taka(chosen.taka));
    };
    drawCards();
    const trxIn = inputEl({ maxlength: '20', autocapitalize: 'characters', spellcheck: 'false', class: 'input buy-trx' });
    const fromIn = inputEl({ inputmode: 'numeric', maxlength: '16', placeholder: '01XXXXXXXXX' });
    const copyBtn = h('button', { type: 'button', class: 'btn btn-sm' }, 'Copy the number');
    copyBtn.addEventListener('click', async () => {
      const ok = await copyText(number);
      toast(ok ? 'The number was copied.' : 'Copy failed. Select the text and press Ctrl+C.', { kind: ok ? 'success' : 'error', timeout: ok ? 2000 : undefined });
    });
    const sendBtn = h('button', { type: 'button', class: 'btn btn-primary' }, 'Send payment');
    const cancelBtn = h('button', { type: 'button', class: 'btn' }, 'Cancel');
    const dlg = modal({
      title: 'Buy tokens',
      className: 'buy-dialog',
      body: h('div', { class: 'buy-steps' },
        h('div', { class: 'field' }, h('span', { class: 'field-label' }, '1. Choose a package'), cards),
        h('div', { class: 'field' }, h('span', { class: 'field-label' }, '2. Send the money with bKash'),
          h('div', { class: 'buy-pay' },
            h('div', null,
              h('p', { class: 'hint' }, 'Use Send Money in the bKash app, to this number'),
              h('p', { class: 'buy-number', 'data-no-translate': '' }, number),
              h('p', null, h('span', null, 'Amount:'), ' ', amount)),
            copyBtn)),
        h('div', { class: 'field' }, h('span', { class: 'field-label' }, '3. Type the TrxID bKash gave you'),
          h('div', { class: 'field-grid' }, field('TrxID', trxIn), field('The bKash number you paid from', fromIn))),
        h('p', { class: 'buy-note' }, 'Your tokens are added as soon as your TrxID is verified. Verification can take up to 24 hours.')),
      footer: [cancelBtn, sendBtn],
    });
    cancelBtn.addEventListener('click', () => closeDialog(dlg));
    sendBtn.addEventListener('click', async () => {
      const trx = trxIn.value.replace(/\s/g, '');
      const from = fromIn.value.replace(/[\s\-()]/g, '');
      if (!TRX_RE.test(trx)) {
        toast('Enter the TrxID exactly as bKash shows it: 8 to 16 letters and digits.', { kind: 'warning' });
        trxIn.focus();
        return;
      }
      if (!NUMBER_RE.test(from)) {
        toast('Enter the bKash number you paid from as 11 digits, like 01XXXXXXXXX.', { kind: 'warning' });
        fromIn.focus();
        return;
      }
      setBusy(sendBtn, true, 'Sending…');
      try {
        await api.buyTokens(chosen.tokens, trx, from, requestId);
        closeDialog(dlg);
        toast('Payment sent. Your tokens are added as soon as your TrxID is verified.', { kind: 'success' });
        load();
      } catch (err) {
        toast(err.status === 0 ? 'Cannot reach the server. Check your internet and press Send payment again: it can’t be recorded twice.'
          : err.message, { kind: 'error' });
        if (sendBtn.isConnected) setBusy(sendBtn, false);
      }
    });
    trxIn.focus();
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
      (data.bkash || []).length ? h('section', { class: 'card' },
        h('div', { class: 'card-header' }, h('h3', { class: 'card-title' }, 'bKash payments')),
        h('div', { class: 'card-body dash-scroll' }, bkash(data.bkash))) : null,
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

  /** The client's own bKash payments: waiting, verified (its tokens added) or rejected with the reason. */
  function bkash(list) {
    const status = (p) => {
      if (p.status === 'approved') return h('span', { class: 'badge badge-success' }, 'Verified · tokens added');
      if (p.status === 'rejected') {
        return [h('span', { class: 'badge badge-danger' }, 'Rejected'),
          h('div', { class: 'dash-sub' }, h('span', null, 'Reason:'), ' ',
            p.reason === 'other' ? h('span', { 'data-no-translate': '' }, p.reason_text || '') : h('span', null, REASONS[p.reason] || ''))];
      }
      return [h('span', { class: 'badge badge-warning' }, 'Waiting for verification'),
        h('div', { class: 'dash-sub' }, 'Your tokens are added as soon as it is verified.')];
    };
    return h('table', { class: 'dash-table' },
      h('thead', null, h('tr', null, ['Time', 'TrxID', 'Paid', 'Tokens', 'Status']
        .map((t, i) => h('th', { class: i === 2 || i === 3 ? 'num' : null }, t)))),
      h('tbody', null, list.map((p) => h('tr', null,
        h('td', { class: 'nowrap' }, fmtDateTime(p.at)),
        h('td', null, h('code', { class: 'pay-code' }, p.trx)),
        h('td', { class: 'num' }, taka(p.taka)),
        h('td', { class: 'num' }, p.status === 'approved' ? h('strong', null, signed(p.tokens)) : fmtInt(p.tokens)),
        h('td', null, status(p))))));
  }

  function payments(list) {
    if (!list.length) return h('p', { class: 'hint' }, 'No tokens added yet.');
    return h('table', { class: 'dash-table' },
      h('thead', null, h('tr', null, ['Time', 'Tokens', 'Paid', 'Balance after']
        .map((t, i) => h('th', { class: i ? 'num' : null }, t)))),
      h('tbody', null, list.map((p) => h('tr', null,
        h('td', { class: 'nowrap' }, fmtDateTime(p.at),
          p.tokens < 0 ? h('div', { class: 'dash-sub' }, 'Correction') : p.trx ? h('div', { class: 'dash-sub', 'data-no-translate': '' }, `bKash ${p.trx}`) : null),
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
