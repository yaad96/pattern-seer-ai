/**
 * The Partner dashboard (/partner-dashboard; until 2026-11-24 "/client-dashboard", the "client dashboard"): the page
 * the head admin and the business partner both open. Its four tabs (To do, Clients, Payments, Jobs) are boards.js;
 * the Supreme dashboard (supremeboard.js) has the same four and its own two.
 * Its fifth tab, History (2026-11-24; the owner: "same for partner, a same History, without the real api cost, prompt
 * and 'api call' expander"), is the main page's History (history.js) for the jobs made with a client login: the
 * pictures, downloads and exports, and the tokens each job cost; no API cost, prompt or call list, and nothing can be
 * deleted or generated again from here.
 */
import { loadConfig } from './api.js';
import { h, icon, replace } from './dom.js';
import { startBoard } from './boards.js';
import { initHistory } from './history.js';
import { callout, loadingRow } from './ui.js';

const historyEl = h('div', null, loadingRow('Loading…'));
let history = null;

async function showHistory() {
  if (!history) {
    try {
      await loadConfig(); // the model names and the export choices History shows
    } catch (err) {
      replace(historyEl, callout('error', err.message));
      return;
    }
    if (!history) history = initHistory(historyEl, { scope: 'partner' });
  }
  history.activate();
}

startBoard({
  title: 'Partner dashboard',
  prefsKey: 'partnerboard.prefs',
  // the head admin (the only one who can take tokens back) gets the way to its own dashboard; the partner has none
  headerLinks: (overview) => (overview.can_take_back
    ? [h('a', { class: 'btn btn-sm btn-ghost', href: '/supreme-dashboard' }, icon('arrow-left'), 'Supreme dashboard')]
    : []),
  extraTabs: [{ id: 'history', label: 'History', el: historyEl, onShow: showHistory }],
});
