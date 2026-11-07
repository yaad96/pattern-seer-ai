/**
 * Output settings shared by Recreate and Design (provider, model, resolution, aspect ratio,
 * print size, seed) and the "View prompt" dialog. Everything model-specific comes
 * from GET /api/config; nothing is hard-coded. Variants is hidden (see createOutputSettings).
 */
import { api, getConfig } from './api.js';
import { h, icon, fmtInt, replace } from './dom.js';
import { modelLabel } from './results.js';
import { callout, copyText, field, inputEl, loadingRow, modal, segmented, selectEl, setOptions } from './ui.js';

const RES_ORDER = ['1K', '2K', '4K'];
const MAX_SEED = 2147483647;

/** The option object to send: exactly the server model's keys (extra="forbid"), defaults for the rest. */
export function pickKnown(defaults, values) {
  const out = {};
  for (const key of Object.keys(defaults)) {
    out[key] = Object.prototype.hasOwnProperty.call(values, key) ? values[key] : defaults[key];
  }
  return out;
}

const labelOf = (list, value) => {
  const hit = list.find((item) => String(item.value) === String(value));
  return hit ? hit.label : String(value);
};

/** Same rule as pipeline._clamp_resolution: highest supported <= requested, else the lowest. */
function clampResolution(requested, supported) {
  if (supported.includes(requested)) return requested;
  const rank = RES_ORDER.indexOf(requested);
  const sorted = supported.slice().sort((a, b) => RES_ORDER.indexOf(a) - RES_ORDER.indexOf(b));
  const lower = sorted.filter((r) => RES_ORDER.indexOf(r) <= rank);
  return lower.length ? lower[lower.length - 1] : sorted[0];
}

function findModel(provider, id) {
  return provider.models.find((m) => m.id === id)
    || provider.models.find((m) => m.id === provider.default_model)
    || provider.models[0];
}

/**
 * defaults: the server's option defaults (config.defaults.recreate / .design).
 * autoLabel: label of the "auto" aspect ratio. onChange(): any value changed.
 */
export function createOutputSettings({ defaults, autoLabel, onChange }) {
  const config = getConfig();
  const catalog = config.catalog;
  const providers = config.providers;
  // const maxVariants = config.limits.max_variants || 4;  // Variants is hidden (below)
  let provider = providers.find((p) => p.name === defaults.provider)
    || providers.find((p) => p.name === config.default_provider)
    || providers[0];
  let model = findModel(provider, defaults.model);
  const changed = () => { if (onChange) onChange(); };

  const providerSel = selectEl(providers.map((p) => ({ value: p.name, label: p.label })), provider.name, (name) => {
    provider = providers.find((p) => p.name === name) || provider;
    model = findModel(provider, null);
    refreshModels();
    changed();
  });
  const modelSel = selectEl([], null, (id) => {
    model = provider.models.find((m) => m.id === id) || model;
    refreshModel();
    changed();
  });
  const resSel = selectEl([], null, changed);
  const aspectSel = selectEl([], null, changed);
  const printSel = selectEl(catalog.print_sizes, defaults.print_long_edge, changed);
  // Variants (up to 4 images from one click) is hidden for every output type: the owner found it would confuse
  // clients (2026-11-03). Every job makes one image (panels: one set); the server still accepts 1-4. To bring it
  // back, restore the lines marked "Variants" here, in `el` and in values(), and let history.js optionRows() list
  // `variants` again.
  // const variants = segmented({
  //   legend: 'Variants',
  //   options: Array.from({ length: maxVariants }, (_, i) => ({ value: i + 1, label: String(i + 1) })),
  //   value: defaults.variants || 1,
  //   onChange: changed,
  //   block: true,
  // });
  const seedInput = inputEl({ type: 'number', min: '0', max: String(MAX_SEED), step: '1', inputmode: 'numeric', placeholder: 'Random' });
  seedInput.addEventListener('change', changed);

  const providerField = field('Image AI', providerSel);
  providerField.classList.add('span-2');
  providerField.hidden = providers.length < 2;
  const modelField = field('Model', modelSel);
  modelField.classList.add('span-2');
  const modelHint = h('p', { class: 'hint', id: `${modelSel.id}-hint` });
  modelSel.setAttribute('aria-describedby', modelHint.id);
  modelField.appendChild(modelHint);
  const printHintText = `Upscales the final PNG to this long edge (Lanczos) and tags it ${config.print_dpi} dpi.`;
  const printField = field('Print size', printSel, printHintText);
  printField.classList.add('span-2');
  const printHint = printField.querySelector('.hint');
  const seedField = field('Seed', seedInput, 'Optional. Reuse a seed to get a similar result.');
  const resField = field('Resolution', resSel);
  const aspectField = field('Aspect ratio', aspectSel);

  function refreshModel() {
    const res = model.resolutions.length ? model.resolutions : RES_ORDER;
    setOptions(resSel, res.map((r) => ({ value: r, label: labelOf(catalog.resolutions, r) })),
      clampResolution(resSel.value || defaults.resolution, res));
    const ratios = [{ value: 'auto', label: autoLabel }]
      .concat(catalog.aspect_ratios.filter((a) => a.value !== 'auto' && model.aspect_ratios.includes(a.value)))
      .concat(model.aspect_ratios.filter((r) => !catalog.aspect_ratios.some((a) => a.value === r)).map((r) => ({ value: r, label: r })));
    const wanted = aspectSel.value || defaults.aspect_ratio;
    setOptions(aspectSel, ratios, ratios.some((r) => r.value === wanted) ? wanted : 'auto');
    seedField.hidden = !model.supports_seed;
    modelHint.textContent = [model.notes, model.supports_transparent ? 'Can make transparent backgrounds.' : '']
      .filter(Boolean).join(' ');
  }

  function refreshModels() {
    setOptions(modelSel, provider.models.map((m) => ({ value: m.id, label: m.label })), model.id);
    refreshModel();
  }
  refreshModels();

  const el = h('div', { class: 'field-grid' },
    providerField, modelField,
    resField, aspectField,
    printField,
    /* variants.el, (Variants) */ seedField);

  return {
    el,
    model: () => model,
    /** Turn Print size off (with `note` as its hint) for outputs that have a fixed print size, e.g. panels. */
    lockPrint(locked, note) {
      printSel.disabled = locked;
      printHint.textContent = locked ? note : printHintText;
    },
    /** Hide Aspect ratio for outputs with fixed ratios (panels): "auto" is sent, and Resolution takes the row. */
    hideAspect(hidden) {
      aspectField.hidden = hidden;
      resField.classList.toggle('span-2', hidden);
    },
    values() {
      const raw = seedInput.value.trim();
      const seed = /^\d+$/.test(raw) ? Number(raw) : null;
      return {
        provider: provider.name,
        model: model.id,
        resolution: resSel.value,
        aspect_ratio: aspectField.hidden ? 'auto' : aspectSel.value,
        print_long_edge: printSel.disabled ? 0 : Number(printSel.value) || 0,
        variants: 1, // Variants: Number(variants.value) || 1,
        seed: model.supports_seed && seed !== null && seed <= MAX_SEED ? seed : null,
      };
    },
  };
}

/* ---------- prompt preview ---------- */

async function copyFromButton(btn, text) {
  const label = btn.lastChild;
  if (!btn.dataset.label) btn.dataset.label = label.textContent;
  const ok = await copyText(text);
  label.textContent = ok ? 'Copied' : 'Copy failed';
  setTimeout(() => { label.textContent = btn.dataset.label; }, 1600);
}

export function promptBlock(title, text) {
  const btn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('copy'), h('span', null, 'Copy'));
  btn.addEventListener('click', () => copyFromButton(btn, text));
  return h('section', { class: 'prompt-block' },
    h('div', { class: 'prompt-block-head' },
      h('h3', null, title), h('span', { class: 'hint' }, `${fmtInt(text.length)} characters`), h('span', { class: 'spacer' }), btn),
    h('pre', { class: 'prompt-text', tabindex: '0', 'aria-label': title }, text));
}

/**
 * tabs: [{kind, label, note}] with kind in recreate|design|refine|brief.
 * buildRequest(kind) -> {kind, options, message, inputRatio} for POST /api/prompt-preview.
 */
export function openPromptPreview({ tabs, buildRequest }) {
  let active = tabs[0];
  let combined = '';
  let seq = 0;
  const content = h('div', { class: 'result-stack' });
  const switcher = tabs.length > 1
    ? segmented({
      legend: 'Which prompt',
      options: tabs.map((t) => ({ value: t.kind, label: t.label })),
      value: active.kind,
      onChange: (kind) => {
        active = tabs.find((t) => t.kind === kind) || active;
        load();
      },
    })
    : null;
  const copyAll = h('button', { type: 'button', class: 'btn', disabled: true }, icon('copy'), h('span', null, 'Copy system + user'));
  copyAll.addEventListener('click', () => copyFromButton(copyAll, combined));
  const dlg = modal({ title: 'Prompt preview', body: [switcher && switcher.el, content], footer: [copyAll] });

  async function load() {
    seq += 1;
    const mine = seq;
    copyAll.disabled = true;
    replace(content, loadingRow('Building the prompt…'));
    let res;
    try {
      res = await api.promptPreview(buildRequest(active.kind));
    } catch (err) {
      if (mine === seq) replace(content, callout('error', err.message));
      return;
    }
    if (mine !== seq || !dlg.isConnected) return;
    combined = `${res.system.trim()}\n\n---\n\n${res.user.trim()}`;
    copyAll.disabled = false;
    const providerInfo = getConfig().providers.find((p) => p.name === res.provider);
    replace(content,
      h('div', { class: 'prompt-meta' },
        h('span', { class: 'badge badge-accent' }, providerInfo ? providerInfo.label : res.provider),
        h('span', { class: 'badge' }, modelLabel(res.provider, res.model)),
        h('span', { class: 'badge' }, `Aspect ${res.aspect_ratio}`),
        h('span', { class: 'badge' }, res.resolution)),
      active.note ? callout('info', active.note) : null,
      (res.warnings || []).map((w) => callout('warning', w)),
      promptBlock('System prompt', res.system),
      promptBlock('User prompt', res.user));
  }
  load();
}
