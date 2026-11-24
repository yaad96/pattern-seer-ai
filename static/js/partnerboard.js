/**
 * The Partner dashboard (/partner-dashboard; until 2026-11-24 "/client-dashboard", the "client dashboard"): the page
 * the head admin and the business partner both open. Its four tabs (To do, Clients, Payments, Jobs) are boards.js;
 * the Supreme dashboard (supremeboard.js) has the same four and its own two.
 */
import { h, icon } from './dom.js';
import { startBoard } from './boards.js';

startBoard({
  title: 'Partner dashboard',
  prefsKey: 'partnerboard.prefs',
  // the head admin (the only one who can take tokens back) gets the way to its own dashboard; the partner has none
  headerLinks: (overview) => (overview.can_take_back
    ? [h('a', { class: 'btn btn-sm btn-ghost', href: '/supreme-dashboard' }, icon('arrow-left'), 'Supreme dashboard')]
    : []),
});
