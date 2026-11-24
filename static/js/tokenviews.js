/**
 * What a client's jobs cost in our tokens, as a table (2026-11-23): shared by the partner's client dashboard
 * (clientboard.js, English) and a client page's "Customer dashboard" tab (customerboard.js, Bangla through
 * i18n.js, which only the client page loads). Each job shows the tokens of each part (a failed part is free); a Design chat is one row whose
 * "Messages (N)" opens its messages with the client's words and the designer's reply.
 */
import { fileUrl } from './api.js';
import { h, icon, replace, fmtDateTime, fmtInt } from './dom.js';
import { statusBadge } from './jobs.js';
import { TOKEN_ITEMS, panelItems } from './tokens.js';
import { openLightbox } from './ui.js';

export const ITEM_ORDER = ['front', 'back_picture', 'back_same', 'sleeve', 'design'];

/** One priced part of a job: "Front N", "Back (same as front) free"; a failed one "Sleeve failed, not charged". */
export function part(item, tokens, failed) {
  return h('span', { class: ['cb-part', failed && 'is-failed'] }, h('span', null, TOKEN_ITEMS[item] || 'Image'), ' ',
    failed ? h('span', null, 'failed, not charged') : h('strong', null, tokens ? fmtInt(tokens) : 'free'));
}

/** What a job asked for, before it is charged (a running job): priced as the server prices it (tokens.js). */
const planned = (job) => (job.kind === 'design' ? ['design'] : panelItems(job));

/** A job's picture, larger on a click (the partner login may see the previews of the clients' jobs; a client its own). */
function picture(job) {
  return job && job.preview
    ? h('button', {
      type: 'button', class: 'cb-thumb-btn', 'aria-label': 'View the picture larger',
      onClick: () => openLightbox({ src: fileUrl(job.id, job.preview), alt: '', caption: fmtDateTime(job.created_at) }),
    }, h('img', { class: 'dash-thumb', src: fileUrl(job.id, job.preview), alt: '', loading: 'lazy' }))
    : h('span', { class: 'dash-thumb is-empty' });
}

/* ---------- Design chats (2026-11-23): a chat's messages nest under one row ---------- */
const isChat = (job) => job.kind === 'design' && Boolean(job.chat);

/** The jobs (newest first) with each Design chat's messages together, where its latest message is. */
function byChat(jobs) {
  const out = [];
  const chats = new Map();
  for (const job of jobs) {
    if (!isChat(job)) out.push([job]);
    else if (chats.has(job.chat)) chats.get(job.chat).push(job);
    else {
      const group = [job];
      chats.set(job.chat, group);
      out.push(group);
    }
  }
  return out;
}

/** A chat's status from its messages': working while one is, else done / partly done / failed. */
function chatStatus(statuses) {
  if (statuses.some((s) => s === 'queued' || s === 'running')) return 'running';
  const ok = statuses.filter((s) => s === 'succeeded').length;
  return ok === statuses.length ? 'succeeded' : ok || statuses.includes('partial') ? 'partial' : 'failed';
}

/** A Design chat: one row with its tokens in all, which opens its messages (oldest first) with the client's words,
 * the designer's reply and each message's tokens. */
function chatRows(group, openChats) {
  const msgs = [...group].sort((a, b) => a.id.localeCompare(b.id)); // oldest first (ids sort by creation, to the ms)
  const first = msgs[0];
  const last = msgs[msgs.length - 1];
  const open = openChats.has(first.chat);
  const shown = [...msgs].reverse().find((j) => j.preview);
  const named = msgs.find((j) => j.title);
  const failed = msgs.filter((j) => j.failed.length).length;
  const running = msgs.some((j) => j.running);
  const tokens = msgs.reduce((acc, j) => acc + (j.tokens || 0), 0);
  const toggle = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', 'aria-expanded': String(open) },
    icon('chevron', open ? 'chev is-open' : 'chev'), `Messages (${fmtInt(msgs.length)})`);
  const main = h('tr', { class: ['dash-job', 'dash-chat', open && 'is-open'] },
    h('td', { class: 'nowrap' }, fmtDateTime(first.created_at),
      fmtDateTime(last.created_at) !== fmtDateTime(first.created_at)
        ? h('div', { class: 'dash-sub' }, `to ${fmtDateTime(last.created_at)}`) : null),
    h('td', null, picture(shown)),
    h('td', null,
      h('div', { class: 'dash-chat-title', 'data-no-translate': '' }, named ? named.title : 'Design chat'),
      h('div', { class: 'cb-parts' },
        h('span', { class: 'cb-part' }, h('span', null, `${fmtInt(msgs.length)} design ${msgs.length === 1 ? 'message' : 'messages'}`)),
        failed ? h('span', { class: ['cb-part', 'is-failed'] }, h('span', null, `${fmtInt(failed)} failed, not charged`)) : null)),
    h('td', { class: 'num' }, h('strong', null, fmtInt(tokens)),
      running ? h('div', { class: 'dash-sub' }, 'More when the running one finishes') : null),
    h('td', null, statusBadge(chatStatus(msgs.map((j) => j.status)))),
    h('td', { class: 'nowrap' }, toggle));
  const detail = h('tr', { class: 'dash-calls', hidden: !open }, h('td', { colspan: '6' }, open ? messagesTable(msgs) : null));
  toggle.addEventListener('click', () => {
    const now = !openChats.has(first.chat);
    if (now) openChats.add(first.chat);
    else openChats.delete(first.chat);
    toggle.setAttribute('aria-expanded', String(now));
    toggle.querySelector('.chev').classList.toggle('is-open', now);
    main.classList.toggle('is-open', now);
    detail.hidden = !now;
    replace(detail.firstChild, now ? messagesTable(msgs) : null);
  });
  return h('tbody', null, main, detail);
}

function messagesTable(msgs) {
  return h('table', { class: 'dash-table dash-calltable dash-msgtable' },
    h('thead', null, h('tr', null, ['Time', 'Picture', 'Message', 'Tokens', 'Status']
      .map((t, i) => h('th', { class: i === 3 ? 'num' : null }, t)))),
    h('tbody', null, msgs.map((job) => h('tr', { class: 'dash-msgrow' },
      h('td', { class: 'nowrap' }, fmtDateTime(job.created_at)),
      h('td', null, picture(job)),
      h('td', { class: 'dash-msgcell' },
        h('div', { class: 'dash-badges' }, h('span', { class: 'badge' }, job.refine ? 'Change request' : 'New design')),
        job.message ? h('div', { class: 'dash-msg', 'data-no-translate': '' }, job.message) : null,
        job.reply ? h('div', { class: 'dash-reply' }, h('span', null, 'Designer: '),
          h('span', { 'data-no-translate': '' }, job.reply)) : null),
      h('td', { class: 'num' }, job.running ? h('span', { class: 'muted' }, 'Charged when it finishes')
        : job.failed.length ? part('design', 0, true) : h('strong', null, fmtInt(job.tokens))),
      h('td', null, statusBadge(job.status))))));
}

function jobRow(job, ids) {
  const thumb = picture(job);
  const parts = job.running
    ? planned(job).map((item) => h('span', { class: 'cb-part' }, h('span', null, TOKEN_ITEMS[item] || 'Image')))
    : [...[...job.items].sort((a, b) => ITEM_ORDER.indexOf(a.item) - ITEM_ORDER.indexOf(b.item))
      .map((i) => part(i.item, i.tokens, false)), ...(job.waived || []).map((item) => part(item, 0, false)),
      ...job.failed.map((item) => part(item, 0, true))];
  return h('tr', null,
    h('td', { class: 'nowrap' }, fmtDateTime(job.created_at), ids ? h('div', { class: 'dash-id', 'data-no-translate': '' }, job.id) : null),
    h('td', null, thumb),
    h('td', null, h('div', { class: 'cb-parts' }, parts.length ? parts : h('span', { class: 'muted' }, '–'))),
    h('td', { class: 'num' }, job.running ? h('span', { class: 'muted' }, 'Charged when it finishes') : h('strong', null, fmtInt(job.tokens))),
    h('td', null, job.deleted ? h('span', { class: 'badge badge-danger' }, 'Deleted') : statusBadge(job.status)),
    h('td', null));
}


/** The jobs (newest first) as a table, or null when there are none. openChats: the chats kept open (a Set of chat
 * ids, kept by the page across reloads); ids: show each job's id (the partner's page). */
export function jobsTable(jobs, { openChats, ids = false }) {
  if (!jobs.length) return null;
  return h('table', { class: 'dash-table' },
    h('thead', null, h('tr', null, ['Time', 'Picture', 'What was made', 'Tokens', 'Status', '']
      .map((t, i) => h('th', { class: i === 3 ? 'num' : null }, t)))),
    byChat(jobs).map((g) => (isChat(g[0]) ? chatRows(g, openChats) : h('tbody', null, jobRow(g[0], ids)))));
}

/** The token prices, one pill per item ("Back (same as front) free"). */
export function pricesParts(prices) {
  return h('div', { class: 'cb-parts' }, ITEM_ORDER.map((item) => part(item, prices[item], false)));
}
