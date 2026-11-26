/**
 * Entry point: loads GET /api/config, renders the provider status and the demo banner, wires the tabs (Recreate and
 * Design; a client's page also has its own History and Customer dashboard) and routes pasted or dropped images to
 * the active tab. The head admin's History is a tab of the Supreme dashboard since 2026-11-25.
 */
import './i18n.js';
import { api, loadConfig } from './api.js';
import { h, icon, replace, storage, imageFilesFrom, hasFiles } from './dom.js';
import { initDesign } from './design.js';
import { initHistory } from './history.js';
import { initJobsBox } from './jobsbox.js';
import { initCustomerBoard } from './customerboard.js';
import { initRecreate } from './recreate.js';
import { withoutModel } from './results.js';
import { purseChip, refreshPurse, startPurse } from './tokens.js';
import { callout, toast } from './ui.js';

const TABS = ['recreate', 'design'];
// The head admin's dashboard is its own page since 2026-11-24, /supreme-dashboard (supremeboard.js), linked from
// the header here; it was this page's fourth tab, #dashboard, which still leads there. Since 2026-11-25 the head
// admin's History is a tab of that dashboard too (the owner: "in supreme dashboard, move the history inside it"):
// #history here leads there, with ?job=<id> to that job's view.
// /customerep, the page for showing clients a demo (the server marks it): Recreate (production panels only), Design
// and, since 2026-11-26, its own History (the jobs made with the demo login), like a client's page without the
// tokens; without the model status, the output settings, costs or prompts.
// /experiment is a copy of it (studio login only) for trying other image models, with its own saved state.
// /panels (studio login only, 2026-11-07): the studio's production panels, which the studio page links to. It is the
// /customerep flow with the studio's details (output settings, View prompt, costs, image details), the back mirrored
// or copied, and the full Design tab; no History tab; its own saved state; a link back to the studio page.
// /client_<name> (2026-11-22, app/clients.py): a client's own /customerep, in the client's name, logo and colours
// (data-client=<name>), with its own saved state and a sign-out that returns to its own sign-in page. Since
// 2026-11-23 it shows the client's tokens in the header and what a job needs next to Generate / Send (tokens.js),
// and has a third tab, the client's own "Customer dashboard" (customerboard.js), which the header's chip opens.
const MODE = document.documentElement.dataset.app || 'studio';
const CLIENT = document.documentElement.dataset.client || '';
const CUSTOMER = MODE === 'customer' || MODE === 'experiment';
const PANELS_PAGE = MODE === 'panels';
const KEY = CLIENT ? `client_${CLIENT}` : MODE; // saved-state prefix of the pages with their own state

function isDemo(config) {
  return config.providers.length === 1 && config.providers[0].name === 'mock';
}

function renderStatus(config) {
  const demo = isDemo(config);
  const provider = config.providers.find((p) => p.name === config.default_provider) || config.providers[0];
  const writer = config.brief_writer_enabled
    ? (config.brief_writers || []).find((w) => w.name === config.default_brief_writer)
    : null;
  const label = demo ? 'Demo mode (no AI)' : `${provider.label}${writer ? ` · AI designer: ${writer.label}` : ''}`;
  const details = [
    `Image AI: ${config.providers.map((p) => `${p.label} (${p.default_model})`).join(', ')}`,
    `AI designer: ${writer ? `${writer.label} (${writer.model})` : 'off'}`,
    `Print resolution: ${config.print_dpi} dpi`,
    `Version ${config.app.version}`,
  ].join('\n');
  replace(document.getElementById('provider-status'),
    h('span', { class: ['status-pill', demo && 'is-demo'], title: details },
      h('span', { class: 'status-dot', 'aria-hidden': 'true' }), h('span', null, label)));
}

function renderBanner(config) {
  if (!isDemo(config)) return;
  document.getElementById('banner-slot').appendChild(h('div', { class: 'banner', role: 'note' },
    h('div', { class: 'banner-inner' }, icon('alert'),
      h('p', null, h('strong', null, 'Demo mode — images are placeholders, not AI.'),
        ' No OpenAI API key found. Paste your OpenAI key into the ', h('code', null, '.openai_api_key'),
        ' file in the project folder and restart the app. (Claude cannot create images; a Claude key only powers the AI designer.)'))));
}

/** While the site is closed on the dashboard (app/site_switch.py), a reminder on the studio's pages: everyone else
 * gets a "page not found" (404). Called again when the dashboard switches it. */
function renderSiteBanner(closed) {
  const old = document.getElementById('site-banner');
  if (old) old.remove();
  if (!closed) return;
  document.getElementById('banner-slot').prepend(h('div', { class: 'banner is-closed', id: 'site-banner', role: 'status' },
    h('div', { class: 'banner-inner' }, icon('alert'),
      h('p', null, h('strong', null, 'The site is closed.'), ' ',
        'Only your head admin login can use it. Everyone else, the demo link included, gets a “page not found” (404) error. Open it again on the Supreme dashboard, under Settings.'))));
}

function renderCustomerBanner(config) {
  if (!isDemo(config)) return;
  document.getElementById('banner-slot').appendChild(h('div', { class: 'banner', role: 'note' },
    h('div', { class: 'banner-inner' }, icon('alert'), h('p', null, h('strong', null, 'Demo mode: these images are placeholders, not AI.')))));
}

/** The header's right-hand buttons on every page: `extras` (the /panels page's "Main page" link), then Log out
 * whenever the login is on (2026-11-10: the owner asked for it on every page, the client demo page included). */
function renderHeaderActions(config, extras = []) {
  const items = [...extras];
  if (config.login_enabled) {
    const href = CLIENT ? `/logout?next=${encodeURIComponent(window.location.pathname)}` : '/logout';
    items.push(h('a', { class: 'btn btn-sm', href }, icon('logout'), 'Log out'));
  }
  if (items.length) document.querySelector('.app-header-inner').appendChild(h('div', { class: 'header-actions' }, items));
}

function initTabs(onShow, names = TABS) {
  const tabs = names.map((name) => document.getElementById(`tab-${name}`));
  const panels = names.map((name) => document.getElementById(`panel-${name}`));
  const tabKey = CUSTOMER || PANELS_PAGE ? `${KEY}.tab` : 'tab';
  let current = null;

  function select(name, focus) {
    const index = Math.max(0, names.indexOf(name));
    current = names[index];
    tabs.forEach((tab, i) => {
      const on = i === index;
      tab.setAttribute('aria-selected', String(on));
      tab.tabIndex = on ? 0 : -1;
      panels[i].hidden = !on;
    });
    if (focus) tabs[index].focus();
    if (window.location.hash !== `#${current}`) window.history.replaceState(null, '', `#${current}`);
    storage.set(tabKey, current);
    onShow(current);
  }

  tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => select(names[i], false));
    tab.addEventListener('keydown', (e) => {
      const target = { ArrowRight: i + 1, ArrowLeft: i - 1 + names.length, Home: 0, End: names.length - 1 }[e.key];
      if (target === undefined) return;
      e.preventDefault();
      select(names[target % names.length], true);
    });
  });
  window.addEventListener('hashchange', () => select(window.location.hash.slice(1), false));
  const fromHash = window.location.hash.slice(1);
  select(names.includes(fromHash) ? fromHash : storage.get(tabKey, 'recreate'), false);
  return { current: () => current, select };
}

function wireGlobalInput(tabs, recreate, design) {
  document.addEventListener('paste', (e) => {
    if (document.querySelector('dialog[open]')) return;
    const files = imageFilesFrom(e.clipboardData);
    if (!files.length) return;
    if (tabs.current() === 'recreate') {
      e.preventDefault();
      recreate.acceptFile(files[0]);
    } else if (tabs.current() === 'design') {
      e.preventDefault();
      design.addAttachments(files);
    }
  });
  // Dropping a file outside a drop target must not navigate away from the app.
  window.addEventListener('dragover', (e) => {
    if (hasFiles(e) && !e.defaultPrevented) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'none';
    }
  });
  window.addEventListener('drop', (e) => {
    if (hasFiles(e)) e.preventDefault();
  });
}

function renderBootError(bootEl, message) {
  const retry = h('button', { type: 'button', class: 'btn btn-primary' }, icon('refresh'), 'Try again');
  retry.addEventListener('click', () => {
    replace(bootEl, h('span', { class: 'spinner', 'aria-hidden': 'true' }), CLIENT ? 'Loading…' : 'Loading Jersey HD Studio…');
    boot();
  });
  replace(bootEl, h('div', { class: 'empty' },
    h('div', { class: 'empty-icon' }, icon('alert')),
    h('p', { class: 'empty-title' }, CLIENT ? 'This page could not start' : 'Jersey HD Studio could not start'),
    h('p', { class: 'empty-text' }, message),
    retry));
}

async function boot() {
  const bootEl = document.getElementById('boot');
  let config;
  try {
    config = await loadConfig();
  } catch (err) {
    renderBootError(bootEl, err.message);
    return;
  }
  if (CUSTOMER) {
    try {
      renderCustomerBanner(config);
      startPurse(CLIENT); // a client page: its purse of our tokens (nothing elsewhere)
      renderHeaderActions(config, CLIENT ? [purseChip({ href: '#tokens' })] : []);
      let tabs = null;
      let recreate = null;
      let historyTab = null;
      // The demo page /customerep is treated like a client's page, without the tokens (the owner, 2026-11-26: "treat
      // Demo as any other customer. So all the features of a customer, except token-based usage should be there"):
      // it has its own History too, of the jobs made with the demo login. /experiment has none.
      const DEMO_PAGE = MODE === 'customer' && !CLIENT;
      const OWN_HISTORY = Boolean(CLIENT) || DEMO_PAGE;
      // a job still being made after it left the Result area (jobsbox.js): a page with its own History opens it
      // there; /experiment has none, so there it is shown in the Result area again
      const jobsBox = initJobsBox({
        key: `${KEY}.bgJobs`,
        history: OWN_HISTORY,
        stageText: withoutModel,
        openJob: (jobId) => {
          if (historyTab) {
            tabs.select('history', false);
            historyTab.open(jobId);
          } else {
            tabs.select('recreate', false);
            recreate.show(jobId);
          }
        },
        onEnded: () => {
          if (CLIENT) refreshPurse(); // charged now: each image that came out
          if (historyTab && tabs && tabs.current() === 'history') historyTab.activate();
        },
      });
      recreate = initRecreate(document.getElementById('panel-recreate'), { customer: true, keyPrefix: KEY, client: CLIENT, jobsBox });
      const design = initDesign(document.getElementById('panel-design'), { customer: true, keyPrefix: KEY, client: CLIENT });
      // a client page also has its own History (history.js, the jobs that client made; since 2026-11-24) and its
      // "Customer dashboard" (customerboard.js, the tokens); the demo page has the History (the demo login's jobs)
      // and no tokens; /experiment has neither
      const board = CLIENT ? initCustomerBoard(document.getElementById('panel-tokens'), { client: CLIENT }) : null;
      historyTab = OWN_HISTORY ? initHistory(document.getElementById('panel-history'), {
        scope: 'customer',
        client: CLIENT,
        demo: DEMO_PAGE,
        onContinue: (job) => {
          tabs.select('design', false);
          design.continueFrom(job);
        },
      }) : null;
      tabs = board
        ? initTabs((name) => {
          if (name === 'tokens') board.activate();
          else board.deactivate();
          if (name === 'history') historyTab.activate();
        }, ['recreate', 'design', 'history', 'tokens'])
        : historyTab
          ? initTabs((name) => { if (name === 'history') historyTab.activate(); }, ['recreate', 'design', 'history'])
          : initTabs(() => {}, ['recreate', 'design']);
      wireGlobalInput(tabs, recreate, design);
      bootEl.remove();
    } catch (err) {
      replace(bootEl, callout('error', `Something went wrong while starting the app: ${err.message}`));
      throw err;
    }
    return;
  }
  if (PANELS_PAGE) {
    try {
      renderStatus(config);
      renderSiteBanner(config.site_closed);
      renderBanner(config);
      renderHeaderActions(config,
        [h('a', { class: 'btn btn-sm btn-ghost', href: '/', title: 'Back to the main page' }, icon('arrow-left'), 'Main page')]);
      let tabs = null;
      let recreate = null;
      const jobsBox = initJobsBox({ // no History tab here: a background job is shown in the Result area again
        key: `${MODE}.bgJobs`,
        openJob: (jobId) => {
          tabs.select('recreate', false);
          recreate.show(jobId);
        },
      });
      recreate = initRecreate(document.getElementById('panel-recreate'), { customer: true, staff: true, keyPrefix: MODE, jobsBox });
      const design = initDesign(document.getElementById('panel-design'), { keyPrefix: MODE });
      tabs = initTabs(() => {}, ['recreate', 'design']); // no History tab (it is on the studio page)
      wireGlobalInput(tabs, recreate, design);
      bootEl.remove();
    } catch (err) {
      replace(bootEl, callout('error', `Something went wrong while starting the app: ${err.message}`));
      throw err;
    }
    return;
  }
  // the old addresses of what moved to the Supreme dashboard (opened afresh or typed into the open page): the
  // Dashboard tab, and History with one job's view (/?job=<id>#history)
  const toSupreme = () => {
    const { hash, search } = window.location;
    if (hash !== '#dashboard' && hash !== '#history') return false;
    const job = hash === '#history' ? new URLSearchParams(search).get('job') : null;
    window.location.replace(`/supreme-dashboard${job ? `?job=${encodeURIComponent(job)}` : ''}${hash === '#history' ? '#history' : ''}`);
    return true;
  };
  if (toSupreme()) return;
  window.addEventListener('hashchange', toSupreme);
  try {
    renderStatus(config);
    renderSiteBanner(config.site_closed);
    renderBanner(config);
    renderHeaderActions(config,
      [h('a', { class: 'btn btn-sm btn-ghost', href: '/supreme-dashboard' }, icon('chart'), 'Supreme dashboard')]);
    let tabs = null;
    let recreate = null;
    const jobsBox = initJobsBox({ // no History tab here (it is in the Supreme dashboard): the job is shown again here
      key: 'recreate.bgJobs',
      openJob: (jobId) => {
        tabs.select('recreate', false);
        recreate.show(jobId);
      },
    });
    recreate = initRecreate(document.getElementById('panel-recreate'), { jobsBox });
    const design = initDesign(document.getElementById('panel-design'));
    tabs = initTabs(() => {});
    // History's "Continue in chat" (now in the Supreme dashboard): /?continue=<job id>#design goes on with that
    // design here, once the chat saved in this browser is back
    const wanted = new URLSearchParams(window.location.search).get('continue');
    if (wanted && /^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$/.test(wanted)) {
      window.history.replaceState(null, '', '/#design');
      tabs.select('design', false);
      design.ready.then(() => api.job(wanted)).then((job) => design.continueFrom(job))
        .catch((err) => toast(err.message, { kind: 'error' }));
    }
    wireGlobalInput(tabs, recreate, design);
    bootEl.remove();
  } catch (err) {
    replace(bootEl, callout('error', `Something went wrong while starting the app: ${err.message}`));
    throw err;
  }
}

boot();
