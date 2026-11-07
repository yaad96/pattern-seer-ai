/**
 * A job's API call log (GET .../calls, app/pipeline.py CALL_LOG): every paid request, in order, with what was
 * sent (prompt, images, settings), what came back (request id, token counts, the image or text), any error and
 * what it cost. Loaded when the section is first opened.
 */

import { api } from './api.js';
import { h, icon, replace, fmtBytes, fmtDateTime, fmtInt, fmtSeconds, fmtUsd } from './dom.js';
import { specList } from './results.js';
import { promptBlock } from './settings.js';
import { callout, loadingRow } from './ui.js';

const PURPOSE = { image: 'Image', reference_check: 'Reference check', brief: 'Design brief' };
const IMAGE_ROLE = {
  reference: 'Reference picture',
  back_reference: 'Back picture',
  current_design: 'Front panel / current design',
  inspiration: 'Attached image',
};
// Token counts in the order people read them; anything else the provider sends is listed after these.
const TOKENS = [
  ['input_tokens', 'Input tokens'],
  ['input_tokens_details.text_tokens', 'Input text tokens'],
  ['input_tokens_details.image_tokens', 'Input image tokens'],
  ['input_tokens_details.cached_tokens', 'Cached input tokens'],
  ['cache_read_input_tokens', 'Cache read tokens'],
  ['cache_creation_input_tokens', 'Cache write tokens'],
  ['output_tokens', 'Output tokens'],
  ['output_tokens_details.image_tokens', 'Output image tokens'],
  ['output_tokens_details.text_tokens', 'Output text tokens'],
  ['output_tokens_details.reasoning_tokens', 'Reasoning tokens'],
  ['output_tokens_details.thinking_tokens', 'Thinking tokens'],
];

// Price row keys (app/pricing.py) and the providers' stop reasons, in words.
const PRICE_KEYS = {
  input: 'Input', output: 'Output', text_input: 'Text input', image_input: 'Image input', image_output: 'Image output',
  text_output: 'Text output', cached_input: 'Cached input', cache_read: 'Cache read', cache_write_5m: 'Cache write (5 min)',
  cache_write_1h: 'Cache write (1 h)', cache_write_30m: 'Cache write (30 min)', cache_write_12h: 'Cache write (12 h)',
};
const STOP_REASONS = {
  end_turn: 'Finished', completed: 'Finished', max_tokens: 'Cut off at the token limit', incomplete: 'Cut off',
  refusal: 'Declined', failed: 'Failed',
};

/** The collapsible "API calls (N)" section for a job's details: {el, update(job)}. Made once per drawer, so it
 * stays open while the job runs; a new call refreshes the count, and the open list in place. */
export function createCallLog(job) {
  const count = h('span', null, `API calls (${job.api_calls || 0})`);
  const body = h('div', { class: 'call-log' });
  const el = h('details', { class: 'vector-export' }, h('summary', null, icon('info'), count, icon('chevron', 'chev')), body);
  let shown = -1; // the number of calls in the loaded list (-1: not loaded)
  let calls = job.api_calls || 0;

  async function load() {
    shown = calls;
    if (!body.firstChild) replace(body, loadingRow('Loading…'));
    try {
      const list = await api.jobCalls(job.id);
      replace(body, list.length
        ? list.map(renderCall)
        : h('p', { class: 'hint' }, 'No API calls are logged for this job. Jobs made before the call log was added have none.'));
    } catch (err) {
      shown = -1;
      replace(body, callout('error', `Could not load the API calls: ${err.message}`));
    }
  }
  el.addEventListener('toggle', () => { if (el.open && shown !== calls) load(); });
  return {
    el,
    update(next) {
      calls = next.api_calls || 0;
      count.textContent = `API calls (${calls})`;
      if (el.open && shown !== calls) load();
    },
  };
}

function renderCall(c) {
  const ok = c.status === 'ok';
  const what = c.purpose === 'image' ? (c.label || `Variant ${c.variant}`) : (PURPOSE[c.purpose] || c.purpose);
  const tags = [];
  if (c.trigger === 'retry') tags.push(h('span', { class: 'badge' }, 'Manual retry'));
  if (c.attempt > 1) tags.push(h('span', { class: 'badge' }, `Attempt ${c.attempt}`));
  const summary = h('summary', null,
    h('span', { class: 'call-n' }, `#${c.n}`),
    h('strong', null, what),
    h('span', { class: ['badge', 'badge-dot', ok ? 'badge-success' : 'badge-danger'] }, ok ? 'OK' : 'Failed'),
    ...tags,
    h('span', { class: 'call-cost' }, costText(c)),
    icon('chevron', 'chev'));

  const facts = specList([
    ['Started', c.started_at ? fmtDateTime(c.started_at) : ''],
    ['Time taken', fmtSeconds(c.latency_s)],
    ['Model', c.model && c.requested_model && c.model !== c.requested_model ? `${c.model} (asked for ${c.requested_model})` : c.model],
    ['Endpoint', c.endpoint],
    ['Request id', c.request_id],
    ['Stop reason', STOP_REASONS[c.stop_reason] || c.stop_reason],
    ['Cost', costText(c)],
    ['Price used', priceText(c.price)],
    ...tokenRows(c.usage),
  ]);
  const parts = [facts];
  const params = Object.entries(c.params || {}).filter(([, v]) => v !== null && v !== undefined);
  if (params.length) {
    parts.push(section('Settings sent', h('p', { class: 'mono call-params', 'data-no-translate': '' },
      params.map(([k, v]) => `${k} = ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · '))));
  }
  if ((c.inputs || []).length) {
    parts.push(section('Images sent', h('ul', { class: 'plain-list' }, c.inputs.map((img, i) => h('li', null,
      `${i + 1}. `, IMAGE_ROLE[img.label] || img.label, img.source ? h('span', { class: 'mono' }, ` · ${img.source}`) : null,
      ` · ${[dims(img), fmtBytes(img.bytes)].filter(Boolean).join(' · ')}`, fingerprint(img.sha256))))));
  }
  if (c.output && c.purpose === 'image') {
    parts.push(section('Image returned', h('p', null,
      c.output.saved_as ? h('span', { class: 'mono' }, c.output.saved_as) : null,
      ` · ${[dims(c.output), fmtBytes(c.output.bytes)].filter(Boolean).join(' · ')}`, fingerprint(c.output.sha256))));
  } else if (c.output) {
    parts.push(section('Reply', h('pre', { class: 'prompt-text', tabindex: '0', 'data-no-translate': '' }, JSON.stringify(c.output, null, 2))));
  }
  if (c.error) {
    parts.push(callout('error', [c.error.message, c.error.status_code ? `HTTP ${c.error.status_code}` : null].filter(Boolean).join(' · ')));
    if (c.error.detail) parts.push(h('pre', { class: 'prompt-text', tabindex: '0', 'data-no-translate': '' }, c.error.detail));
  }
  if (c.prompt) {
    parts.push(h('details', { class: 'call-prompt' }, h('summary', null, 'Prompt sent'),
      c.prompt.system ? promptBlock('System prompt', c.prompt.system) : null,
      promptBlock('User prompt', c.prompt.user || '')));
  }
  return h('details', { class: ['call', ok ? null : 'is-failed'] }, summary, h('div', { class: 'call-body' }, parts));
}

function costText(c) {
  if (typeof c.cost_usd === 'number') return fmtUsd(c.cost_usd);
  return 'Cost unknown';
}

function priceText(price) {
  if (!price) return '';
  if (price.note) return price.note;
  const per = Object.entries(price.per_1m_tokens || {}).map(([k, v]) => `${PRICE_KEYS[k] || k} $${v}`);
  if (!per.length) return '';
  per.push(`per 1M tokens${price.as_of ? ` (prices of ${price.as_of})` : ''}`);
  if (price.multiplier && price.multiplier !== 1) per.push(`× ${price.multiplier}`);
  return per.join(' · ');
}

function tokenRows(usage) {
  if (!usage) return [];
  const flat = {};
  (function walk(obj, prefix) {
    for (const [k, v] of Object.entries(obj)) {
      if (typeof v === 'number') flat[prefix + k] = v;
      else if (v && typeof v === 'object' && !Array.isArray(v)) walk(v, `${prefix}${k}.`);
    }
  }(usage, ''));
  const rows = TOKENS.filter(([k]) => k in flat).map(([k, label]) => [label, fmtInt(flat[k])]);
  const known = new Set(TOKENS.map(([k]) => k).concat(['total_tokens']));
  for (const [k, v] of Object.entries(flat)) if (!known.has(k) && v) rows.push([k, fmtInt(v)]);
  return rows;
}

function section(title, content) {
  return h('div', { class: 'call-section' }, h('h4', null, title), content);
}

function dims(o) {
  return o && o.width && o.height ? `${fmtInt(o.width)} × ${fmtInt(o.height)} px` : '';
}

function fingerprint(sha) {
  return sha ? h('span', { class: 'mono hint', title: sha, 'data-no-translate': '' }, ` · sha256 ${sha.slice(0, 12)}…`) : null;
}
