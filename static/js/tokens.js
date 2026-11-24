/**
 * Our tokens (app/tokens.py, 2026-11-23) on a client page (/client_<name>): its purse in the header and what a job
 * needs next to Generate / Send. Only a client page's own login is charged: the server holds a job's price before it
 * starts, refuses a job the purse can't cover (402), and charges each image that comes out (a failed one is free).
 * The studio login sees the client's purse there, but its own jobs are free. Without a client, or on a server
 * without client_assets/tokens.json, nothing is shown.
 */
import { api } from './api.js';
import { h, fmtInt } from './dom.js';

/** What a token charge is for, by the ledger's item names (also on the client dashboard and the studio Dashboard). */
export const TOKEN_ITEMS = {
  front: 'Front',
  back_picture: 'Back (own picture)',
  back_same: 'Back (same as front)',
  sleeve: 'Sleeve',
  design: 'Design message',
};

const REFRESH_MS = 30000;
let client = '';
let state = null; // GET /api/tokens/balance: {enabled, client, balance, available, prices, charged}
const listeners = new Set();

/** The page's purse when tokens are on, else null. */
export function purse() {
  return state && state.enabled && state.prices ? state : null;
}

/** fn(purse()) now and whenever the purse changes; returns the unsubscribe function. */
export function onPurse(fn) {
  listeners.add(fn);
  fn(purse());
  return () => listeners.delete(fn);
}

/** Read the purse again: after a job starts or ends, and when the page comes back into view. A failed read keeps
 * the last one (the chip and hints are a convenience; the server decides). */
export async function refreshPurse() {
  if (!client) return null;
  try {
    state = await api.tokenBalance(client);
  } catch (_) {
    return purse();
  }
  for (const fn of listeners) fn(purse());
  return purse();
}

/** Start reading `slug`'s purse (a client page); nothing for other pages. */
export function startPurse(slug) {
  if (client || !slug) return;
  client = slug;
  refreshPurse();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshPurse();
  });
  // the client's other devices (and the studio's top-ups) change the purse too: read it again now and then
  window.setInterval(() => {
    if (!document.hidden) refreshPurse();
  }, REFRESH_MS);
}

/** A panel job's items as the server prices them (app/tokens.panel_items with pipeline.derives_back): a back is
 * free when it is made from the front, else drawn from its own picture (or, without the front, the reference). */
export function panelItems(options) {
  const panels = options.panels || [];
  const fromFront = panels.includes('front') && options.back !== 'image';
  return panels.map((p) => (p === 'back' ? (fromFront ? 'back_same' : 'back_picture') : p));
}

function priceOf(p, items) {
  return items.reduce((acc, item) => acc + (p.prices[item] || 0), 0);
}

/** Why a job of `items` can't start ('' when it can): the client's own purse can't cover it. The same words as the
 * server's 402. Never for the studio login, whose jobs are free. */
export function shortage(items) {
  const p = purse();
  if (!p || !p.charged) return '';
  const need = priceOf(p, items);
  if (need <= p.available) return '';
  return `Not enough tokens: this needs ${need}, you have ${Math.max(0, p.available)}. Please ask the head admin to add more.`;
}

/** The hint next to Generate / Send: "This needs N tokens · You have M" (each: "Each message: N tokens");
 * for the studio login the client's price, not charged. null when tokens are off. */
export function needHint(items, { each = false } = {}) {
  const p = purse();
  if (!p) return null;
  const need = priceOf(p, items);
  const price = each ? `Each message: ${fmtInt(need)} tokens`
    : p.charged ? `This needs ${fmtInt(need)} tokens` : `Client price: ${fmtInt(need)} tokens`;
  return [h('span', null, price), ' · ',
    h('span', null, p.charged ? `You have ${fmtInt(Math.max(0, p.available))}` : 'Not charged on the head admin login')];
}

/** The header's purse: "Tokens M" (what a new job can use: the balance less what running jobs hold). With `href`,
 * a link (the client page's "Customer dashboard" tab). */
export function purseChip({ href } = {}) {
  const value = h('span', { class: 'token-chip-value' });
  const chip = h(href ? 'a' : 'span', { class: 'token-chip', href: href || null, hidden: true },
    h('span', null, 'Tokens'), ' ', value);
  onPurse((p) => {
    chip.hidden = !p;
    if (!p) return;
    value.textContent = fmtInt(Math.max(0, p.available));
    const cheapest = Math.min(...Object.values(p.prices).filter((v) => v > 0)); // Infinity when everything is free
    const low = p.charged && Number.isFinite(cheapest) && p.available < cheapest; // not even one panel or message
    chip.classList.toggle('is-empty', low);
    chip.title = low ? 'Not enough tokens for a new job. Please ask the head admin to add more.'
      : p.charged ? 'Your tokens. Each panel and each design message uses tokens; a panel that fails is free.'
        : 'This client’s tokens. Jobs made with the head admin login are not charged.';
  });
  return chip;
}
