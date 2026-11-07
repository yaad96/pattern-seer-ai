/**
 * Shared results view used by Recreate, Design and History: variant cards with preview,
 * lightbox, before/after comparison, palette, PNG download and EPS/SVG/PDF export; plus the
 * AI designer's brief card.
 */
import { api, fileUrl, getConfig } from './api.js';
import { h, icon, clear, replace, appendAll, fmtBytes, fmtDims, fmtInt, fmtPrintSize, fmtSeconds, fmtUsd, isHex, parseRatio, uid } from './dom.js';
import { announce, copyToast, openLightbox, setBusy, toast } from './ui.js';

export function modelLabel(providerName, modelId) {
  const config = getConfig();
  const provider = config && config.providers.find((p) => p.name === providerName);
  const model = provider && provider.models.find((m) => m.id === modelId);
  return model ? model.label : (modelId || '');
}

/* /customerep, the page for showing clients a demo: no model or provider names, costs or internal details. */
const CUSTOMER_WARNINGS = new Set([
  'The front panel failed, so the sleeve was drawn from the reference only.',
  'The front panel failed, so the sleeve was drawn from the reference only and the back could not be made.',
  'The back panel failed, so the sleeve was drawn from the reference only.',
]);
/** The job warnings a client may see (the others name models or the app's own set-up). */
export const customerWarnings = (warnings) => (warnings || []).filter((w) => CUSTOMER_WARNINGS.has(w));
/** An error as a client sees it: the provider's own message names it (e.g. "Could not reach the OpenAI API."). */
export function customerError(message) {
  return /safety/i.test(message || '') ? 'This picture could not be used. Try a different picture.'
    : 'It could not be made this time. Please try again.';
}
/** A progress stage without the model's name ("Drawing the front panel with GPT Image 2.5 …" -> "Drawing the front panel"). */
export function withoutModel(stage) {
  for (const provider of getConfig().providers) {
    for (const model of provider.models) {
      const tail = ` with ${model.label}`;
      if (stage.endsWith(tail)) return stage.slice(0, -tail.length);
    }
  }
  return stage;
}

/**
 * Variant grid that updates in place (cards are keyed by variant index and only rebuilt when
 * their status changes, so polling never resets sliders or open panels).
 * options: compare (show before/after vs. the job's first input), onRefine(job, index),
 * activeParent ({jobId, variant}), compact, vectorsOpen, onRetry(job, variant) (a failed Recreate image: generate it
 * again; resolves once the server has accepted), customer (the client demo page: no model names or costs).
 */
export function createResults(job, options) {
  const opts = options || {};
  const grid = h('div', { class: ['variant-grid', opts.compact && 'is-compact'] });
  // Jobs with a front, back and sleeve also get those three together in one EPS (older 5-panel jobs do not).
  const combined = ['front', 'back', 'sleeve'].every((p) => job.variants.some((v) => v.panel === p));
  const zip = job.variants.some((v) => v.panel) ? createPanelZips(job.id, combined, opts.customer) : null;
  const cards = new Map();
  let parent = opts.activeParent || null;
  function update(next) {
    for (const v of next.variants) {
      let card = cards.get(v.index);
      if (!card) {
        card = createVariantCard(v.index, opts, v.label);
        cards.set(v.index, card);
        grid.appendChild(card.el);
      }
      card.update(next, v);
      card.markParent(parent);
    }
    if (zip) zip.update(next);
  }
  update(job);
  return {
    el: zip ? h('div', { class: 'result-stack' }, zip.el, grid) : grid,
    update,
    markParent(p) {
      parent = p;
      for (const card of cards.values()) card.markParent(p);
    },
  };
}

/* Panel jobs: two buttons above the cards, each opening a menu of ZIP downloads; they turn on once all panels are
   done. EPS: every panel's EPS at one preset (detailed first, the default; or raster), plus (`combined`) one EPS
   with the front, back and sleeve together. PNG: every panel's PNG, at the print size (the default) or exactly as
   the AI made it. Items: [value, label, busy text, announcement, tooltip]. */
const TRACING = ['Tracing the panels and making the ZIP…', 'Tracing the panels for the ZIP. This can take a little while…'];
const MAKING = 'Making the ZIP. This can take a little while…';
const EPS_ZIP = [
  ['detailed', 'Detailed, 32 colours (default)', ...TRACING],
  ['standard', 'Standard, 16 colours', ...TRACING],
  ['flat', 'Flat, 8 colours', ...TRACING],
  ['raster', 'Raster EPS (exact pixels)', 'Making the raster EPS files and the ZIP…', MAKING,
    'The HD image is placed inside the EPS. Use it for photos or complex gradients.'],
];
const PNG_ZIP = [
  ['print', 'Print size, 300 dpi (default)', 'Making the print-size PNGs and the ZIP…', MAKING],
  ['original', 'Original size (as the AI made it)', 'Making the ZIP…', MAKING],
];

function createPanelZips(jobId, combined, customer) {
  const menus = [
    createZipMenu('eps', 'Download all panel eps zip (detailed)', EPS_ZIP, (preset) => api.panelsZip(jobId, preset),
      combined ? 'Each ZIP has every panel\'s EPS, plus one EPS with the front, back and sleeve together.' : null),
    // the customer page offers the print size only (no original-size PNGs there)
    createZipMenu('png', 'Download all panel png zip', customer ? PNG_ZIP.slice(0, 1) : PNG_ZIP, (size) => api.panelsPngZip(jobId, size),
      customer ? 'Every panel at its 3XL print size at 300 dpi (big files).'
        : 'Print size: every panel at its 3XL print size at 300 dpi (big files). Original size: the images exactly as the AI made them.'),
  ];
  const hint = h('span', { class: 'hint' }, 'Turns on when all panels are generated.');
  return {
    el: h('div', { class: 'panel-zips' }, menus.map((m) => m.el), hint),
    update(job) {
      const panels = job.variants.filter((v) => v.panel);
      const ready = panels.length > 0 && panels.every((v) => v.status === 'succeeded');
      for (const m of menus) m.setReady(ready);
      hint.hidden = ready;
    },
  };
}

function createZipMenu(kind, label, items, fetchZip, note) {
  const menuId = uid('panel-zip');
  const button = h('button', {
    type: 'button', class: 'btn btn-sm', disabled: true, 'aria-expanded': false, 'aria-controls': menuId,
    onClick: () => (menu.hidden ? open() : close(false)),
  }, icon('download'), label, icon('chevron', 'chev'));
  const noteId = uid('panel-zip-note');
  const menu = h('div', { class: 'panel-zip-menu', id: menuId, hidden: true },
    note ? h('p', { class: 'hint panel-zip-note', id: noteId }, note) : null,
    items.map(([value, text, , , tip]) => h('button', {
      type: 'button', class: 'btn btn-sm btn-ghost', dataset: { preset: value }, onClick: () => download(value),
      'aria-describedby': note ? noteId : null, title: tip || null,
    }, text)));
  const el = h('div', { class: 'panel-zip', dataset: { zip: kind } }, button, menu);
  let ready = false;
  let busy = false;

  function outside(e) {
    if (!el.contains(e.target)) close(false);
  }
  // While the menu is open, Escape closes the menu only, wherever the focus is (a click on the note or the menu's
  // padding moves it out), and not the History drawer (a modal <dialog>) around it.
  function onEscape(e) {
    if (!el.isConnected) return close(false);
    if (e.key !== 'Escape' || menu.hidden) return;
    e.preventDefault(); // cancels the dialog's own close on Escape
    e.stopPropagation();
    close(true);
  }
  function open() {
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', onEscape, true);
    menu.querySelector('button').focus();
  }
  function close(refocus) {
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', outside);
    document.removeEventListener('keydown', onEscape, true);
    if (refocus) button.focus();
  }

  async function download(value) {
    const [, , busyText, announcement] = items.find((item) => item[0] === value);
    close(false);
    busy = true;
    setBusy(button, true, busyText);
    announce(announcement);
    try {
      const { blob, filename } = await fetchZip(value);
      saveBlob(blob, filename);
      announce('The ZIP is downloading.');
    } catch (err) {
      toast(`ZIP download failed: ${err.message}`, { kind: 'error' });
    } finally {
      busy = false;
      setBusy(button, false);
      button.disabled = !ready;
    }
  }

  return {
    el,
    setReady(value) {
      ready = value;
      if (!busy) button.disabled = !ready;
      if (!ready && !menu.hidden) close(false);
    },
  };
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = h('a', { href: url, download: filename, hidden: true });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

/* Panel jobs: what the AI reference check found for the panel (VariantInfo.source). The mark (full /
   half / empty circle, see app.css) mirrors how much of the part the reference shows. */
const SOURCE_BADGES = new Map([
  ['reference', ['From reference', 'source-full']],
  ['partial', ['Partly guessed', 'source-partial']],
  ['inferred', ['Guessed', 'source-none']],
  ['mirror', ['Mirrored from the front', 'source-full']], // the back, made from the front panel without an AI image
  ['copy', ['Copied from the front', 'source-full']],
]);

function sourceBadge(v) {
  const hit = SOURCE_BADGES.get(v.source);
  if (!hit) return null;
  const note = typeof v.source_note === 'string' ? v.source_note.trim() : '';
  return h('span', { class: ['badge', 'source-badge', hit[1]], title: note || null }, hit[0]);
}

function createVariantCard(index, opts, label) {
  const title = label || `Variant ${index}`;
  const el = h('article', { class: 'variant-card', 'aria-label': `${title}` });
  // Kept across re-renders: the reference check can label a panel after it has finished.
  const sourceSlot = h('span', { class: 'variant-source' });
  let shownStatus = null;
  let shownSource = null;
  let refineBtn = null;
  let retryBtn = null;
  let retried = false; // a retry of this image was accepted: say how it ended
  let jobId = null;

  function head(badge, extra) {
    return h('div', { class: 'variant-head' }, h('h4', { class: 'variant-title' }, `${title}`), badge, sourceSlot, extra);
  }

  function renderPending(job, v) {
    const ratio = parseRatio(v.aspect_ratio || job.aspect_ratio) || 1;
    appendAll(el, [
      h('div', { class: 'variant-media' },
        h('div', { class: 'variant-frame skeleton', style: { '--ar': ratio.toFixed(4) } },
          h('div', { class: 'variant-pending' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Generating…'))),
      h('div', { class: 'variant-body' }, head(h('span', { class: 'badge' }, 'In progress'))),
    ]);
  }

  /** "Generate this panel again" for a failed Recreate image (one new image call; a back made from the front
   * needs none). A back made from a failed front waits for the front instead. */
  function retryRow(job, v) {
    if (!opts.onRetry || job.kind !== 'recreate') return null;
    const front = job.variants.find((x) => x.panel === 'front');
    // a back without its own picture is made from the front (a job without a front draws its back from the picture)
    const derived = v.panel === 'back' && Boolean(front) && ['mirror', 'copy'].includes(job.options && job.options.back);
    if (derived && front.status !== 'succeeded') {
      return h('p', { class: 'hint' }, 'Generate the front panel again: this back is made from it.');
    }
    const btn = h('button', { type: 'button', class: 'btn btn-sm btn-primary', disabled: isRunning(job), onClick: async () => {
      setBusy(btn, true, 'Starting…');
      try {
        await opts.onRetry(job, v);
      } catch (err) {
        setBusy(btn, false);
        toast(`Could not start it again: ${err.message}`, { kind: 'error' });
        return;
      }
      retried = true;
      show(job, { ...v, status: 'pending' }); // at once: it may fail again before the next update arrives
    } }, icon('refresh'), v.panel ? 'Generate this panel again' : 'Generate this image again');
    retryBtn = btn;
    return h('div', { class: 'variant-retry' }, btn, opts.customer ? null
      : h('span', { class: 'hint' }, derived ? 'Made from the front panel; no new AI image.' : 'Uses one new AI image.'));
  }

  function renderFailed(job, v) {
    const cost = opts.customer ? [] : imageCost(job, v);  // a failed call can still have been billed
    appendAll(el, [
      h('div', { class: 'variant-media variant-error' }, icon('alert'), h('strong', null, v.panel ? 'This panel failed' : 'This image failed'),
        h('p', null, opts.customer ? customerError(v.error) : v.error || 'The image model returned no image.'),
        cost.length ? h('p', { class: 'hint' }, dotSeparated(cost)) : null, retryRow(job, v)),
      h('div', { class: 'variant-body' }, head(h('span', { class: 'badge badge-danger badge-dot' }, 'Failed'))),
    ]);
  }

  function show(job, v) {
    shownStatus = v.status;
    clear(el);
    refineBtn = null;
    retryBtn = null;
    el.classList.toggle('is-failed', v.status === 'failed');
    if (v.status === 'succeeded') renderDone(job, v);
    else if (v.status === 'failed') renderFailed(job, v);
    else renderPending(job, v);
  }

  function renderDone(job, v) {
    const id = job.id;
    const dpi = getConfig().print_dpi;
    const ratio = v.width && v.height ? v.width / v.height : (parseRatio(job.aspect_ratio) || 1);
    const previewSrc = fileUrl(id, v.preview || v.png);
    const fullName = v.raw || v.png;
    // The back of a panel job with its own back picture is compared with that picture, everything else with the first.
    // a back or sleeve with its own picture (input_2.png / input_3.png) is compared with it, everything else with the first
    const own = (name) => (job.inputs && job.inputs.includes(name) ? name : null);
    const compareInput = (v.panel === 'back' && own('input_2.png')) || (v.panel === 'sleeve' && own('input_3.png'))
      || (job.inputs && job.inputs[0]);
    const compareSrc = opts.compare && compareInput ? fileUrl(id, compareInput) : null;
    const upscaled = Boolean(v.raw_width && v.raw_height && (v.raw_width !== v.width || v.raw_height !== v.height));
    let view = 'result';

    const openFull = () => openLightbox({
      src: fileUrl(id, fullName),
      alt: `${title}`,
      caption: opts.customer ? title : `${title} · ${fmtDims(v.raw_width || v.width, v.raw_height || v.height)}`,
      downloadUrl: fileUrl(id, v.png, true),
      downloadLabel: 'Download PNG',
    });

    const frame = h('div', { class: 'variant-frame checker', style: { '--ar': ratio.toFixed(4) } });
    const resultBtn = compareSrc ? h('button', { type: 'button', class: 'btn btn-sm', onClick: () => setView('result') }, 'Result') : null;
    const compareBtn = compareSrc
      ? h('button', { type: 'button', class: 'btn btn-sm', onClick: () => setView('compare') }, icon('compare'), 'Before / after')
      : null;
    const viewSwitch = compareSrc ? h('div', { class: 'view-switch variant-actions', role: 'group', 'aria-label': `${title} view` }, resultBtn, compareBtn) : null;

    const vectors = createVectorPanel(id, v, {
      open: opts.vectorsOpen,
      onPreview(asset) {
        openLightbox({
          src: fileUrl(id, asset.svg),
          alt: `${title} vector preview`,
          caption: `${title} · vector preview (${asset.preset}${asset.colors ? `, ${asset.colors} colours` : ''})`,
          downloadUrl: fileUrl(id, asset.svg, true),
          downloadLabel: 'Download SVG',
        });
      },
    });

    function setView(next) {
      view = next;
      clear(frame);
      frame.classList.toggle('checker', view !== 'compare');
      if (view === 'compare') {
        appendAll(frame, [
          createCompare(compareSrc, previewSrc, index),
          h('button', { type: 'button', class: 'btn btn-sm btn-icon media-expand', 'aria-label': `Open variant ${index} full size`, onClick: openFull }, icon('maximize')),
        ]);
      } else {
        frame.appendChild(mediaButton(previewSrc, `Open variant ${index} full size`, openFull));
      }
      if (resultBtn) resultBtn.setAttribute('aria-pressed', String(view === 'result'));
      if (compareBtn) compareBtn.setAttribute('aria-pressed', String(view === 'compare'));
    }

    const meta = [fmtDims(v.width, v.height), fmtPrintSize(v.width, v.height, dpi)];
    if (upscaled) meta.push(`upscaled from ${fmtDims(v.raw_width, v.raw_height)}`);
    if (job.model && !opts.customer) meta.push(modelLabel(job.provider, job.model));
    if (v.latency_s) meta.push(fmtSeconds(v.latency_s));
    if (!opts.customer) meta.push(...imageCost(job, v));

    if (opts.onRefine && job.kind === 'design') {
      refineBtn = h('button', { type: 'button', class: 'btn btn-sm btn-wrap', 'aria-pressed': 'false', onClick: () => opts.onRefine(job, index) },
        icon('wand'), h('span', null, 'Refine this'));
    }

    appendAll(el, [
      h('div', { class: 'variant-media' }, frame),
      h('div', { class: 'variant-body' },
        head(h('span', { class: 'badge badge-success badge-dot' }, 'Ready'), viewSwitch),
        // the customer page shows no sizes, print size, upscaling or time
        opts.customer ? null : h('ul', { class: 'meta-list' }, meta.filter(Boolean).map((m) => h('li', null, m))),
        v.palette && v.palette.length ? createPalette(v.palette) : null,
        h('div', { class: 'variant-actions' },
          h('a', { class: 'btn btn-sm btn-primary', href: fileUrl(id, v.png, true), download: '' }, icon('download'), 'Download PNG'),
          upscaled && v.raw && !opts.customer // not on the customer page
            ? h('a', { class: 'btn btn-sm', href: fileUrl(id, v.raw, true), download: '', title: 'The image exactly as the model returned it' }, 'Original size PNG')
            : null,
          refineBtn),
        vectors.el,
        v.model_text && !opts.customer ? h('details', { class: 'model-notes' }, h('summary', null, 'Model notes'), h('p', null, v.model_text)) : null),
    ]);
    setView('result');
  }

  return {
    el,
    update(job, v) {
      jobId = job.id;
      const source = `${v.source || ''}|${v.source_note || ''}`;
      if (source !== shownSource) {
        shownSource = source;
        replace(sourceSlot, opts.customer ? null : sourceBadge(v)); // no From reference / Partly guessed for clients
      }
      // One image of a job is generated again at a time, so the other failed cards wait for it.
      if (retryBtn && !retryBtn.hasAttribute('aria-busy')) retryBtn.disabled = isRunning(job);
      if (v.status === shownStatus) return;
      if (retried && v.status !== 'pending') {
        retried = false;
        if (v.status === 'succeeded') toast(`${title} is ready.`, { kind: 'success' });
        else toast(`${title} failed again: ${opts.customer ? customerError(v.error) : v.error || 'unknown error'}`, { kind: 'error' });
      }
      show(job, v);
    },
    markParent(p) {
      if (!refineBtn) return;
      const active = Boolean(p && p.jobId === jobId && p.variant === index);
      refineBtn.setAttribute('aria-pressed', String(active));
      refineBtn.lastChild.textContent = active ? 'Refining this' : 'Refine this';
    },
  };
}

const isRunning = (job) => job.status === 'queued' || job.status === 'running';

function mediaButton(src, label, onClick) {
  const img = h('img', { src, alt: '', decoding: 'async' });
  const btn = h('button', { type: 'button', class: 'media-button', 'aria-label': label, onClick }, img);
  img.addEventListener('error', () => {
    btn.disabled = true;
    clear(btn).appendChild(h('span', { class: 'variant-pending' }, icon('image'), 'Image unavailable'));
  });
  return btn;
}

function createCompare(beforeSrc, afterSrc, index) {
  const wrap = h('div', { class: 'compare' });
  const range = h('input', {
    type: 'range', class: 'compare-range', min: '0', max: '100', step: '1', value: '50',
    'aria-label': `Variant ${index}: slide to compare the original (left) with the result (right)`,
  });
  const setPos = () => {
    wrap.style.setProperty('--pos', `${range.value}%`);
    range.setAttribute('aria-valuetext', `${range.value}% original shown`);
  };
  range.addEventListener('input', setPos);
  appendAll(wrap, [
    h('img', { class: 'compare-img', src: beforeSrc, alt: '' }),
    h('div', { class: 'compare-after' }, h('img', { class: 'compare-img', src: afterSrc, alt: '' })),
    h('span', { class: 'compare-label compare-label-before', 'aria-hidden': 'true' }, 'Original'),
    h('span', { class: 'compare-label compare-label-after', 'aria-hidden': 'true' }, 'Result'),
    range,
    h('span', { class: 'compare-handle', 'aria-hidden': 'true' }, h('span', { class: 'compare-knob' }, icon('compare'))),
  ]);
  setPos();
  return wrap;
}

export function createPalette(entries) {
  const list = h('ul', { class: 'swatches', 'aria-label': 'Main colours (select to copy the hex code)' });
  for (const entry of entries) {
    if (!entry || !isHex(entry.hex)) continue;
    const hex = entry.hex.toUpperCase();
    const pct = typeof entry.pct === 'number' ? `${entry.pct < 10 ? entry.pct.toFixed(1) : Math.round(entry.pct)}%` : '';
    list.appendChild(h('li', null,
      h('button', {
        type: 'button', class: 'swatch', title: `Copy ${hex}`,
        'aria-label': `Copy colour ${hex}${pct ? `, ${pct} of the image` : ''}`,
        onClick: () => copyToast(hex),
      }, h('span', { class: 'swatch-color', style: { 'background-color': hex } }), h('span', null, hex),
      pct ? h('span', { class: 'swatch-pct' }, pct) : null)));
  }
  return list;
}

const countOf = (value) => (Number.isInteger(value) && value >= 0 ? value : null);
const pdfOf = (asset) => (typeof asset.pdf === 'string' && asset.pdf ? asset.pdf : null);
// A vector export made before the layered tracer: no colour layers and no PDF; the server traces it again.
const isLegacyVector = (asset) => asset.preset !== 'raster' && countOf(asset.layers) === null;

/** VectorAsset stats, one string per item: colour layers and anchor points, or today's colours and paths for older exports. */
function assetStats(asset) {
  const layers = countOf(asset.layers);
  const anchors = countOf(asset.anchors);
  const parts = [];
  if (layers !== null || anchors !== null) {
    if (layers !== null) parts.push(`${fmtInt(layers)} colour layer${layers === 1 ? '' : 's'}`);
    if (anchors !== null) parts.push(`${fmtInt(anchors)} anchor point${anchors === 1 ? '' : 's'}`);
  } else {
    if (asset.colors) parts.push(`${asset.colors} colours`);
    if (asset.paths) parts.push(`${fmtInt(asset.paths)} paths`);
  }
  if (asset.eps_bytes) parts.push(`EPS ${fmtBytes(asset.eps_bytes)}`);
  if (pdfOf(asset) && asset.pdf_bytes) parts.push(`PDF ${fmtBytes(asset.pdf_bytes)}`);
  return parts;
}

/** Items joined by " · ", each in its own span so the Bangla layer translates them one by one. */
const unknownCalls = (n) => `${n} call${n === 1 ? '' : 's'} of unknown cost`;

/** What one image's API calls cost, for its card ([] when nothing was recorded for it). */
function imageCost(job, v) {
  if (job.cost_usd === null || job.cost_usd === undefined) return [];  // a job made before costs were recorded
  if (!v.api_calls) return v.source === 'mirror' || v.source === 'copy' ? ['No API call'] : [];  // a back made from the front
  const unknown = v.uncosted_calls || 0;
  const parts = [unknown === v.api_calls ? 'Cost unknown' : `Cost ${fmtUsd(v.cost_usd || 0)}`];
  if (unknown && unknown < v.api_calls) parts.push(unknownCalls(unknown));
  if (v.api_calls > 1) parts.push(`${v.api_calls} API calls`);
  return parts;
}

/** The job's API cost ("Total cost $0.1123 · 1 call of unknown cost"), or '' when nothing was recorded. */
export function jobCostText(job) {
  if (job.cost_usd === null || job.cost_usd === undefined) return '';
  const images = job.variants.reduce((sum, v) => sum + (v.cost_usd || 0), 0);
  const designer = Math.round((job.cost_usd - images) * 1e6) / 1e6;  // the reference check or design brief
  return [
    `${job.cost_partial ? 'Recorded calls cost' : 'Total cost'} ${fmtUsd(job.cost_usd)}`,
    designer > 0 ? `of which the AI designer ${fmtUsd(designer)}` : '',
    job.uncosted_calls ? unknownCalls(job.uncosted_calls) : '',
    job.cost_partial ? 'Earlier images were made before costs were recorded' : '',
  ].filter(Boolean).join(' · ');
}

/** Short cost parts for a History card. */
export function cardCost(summary) {
  if (typeof summary.cost_usd !== 'number') return [];
  return [
    fmtUsd(summary.cost_usd),
    summary.uncosted_calls ? unknownCalls(summary.uncosted_calls) : '',
    summary.cost_partial ? 'partly recorded' : '',
  ].filter(Boolean);
}

export function dotSeparated(items) {
  return items.map((item, i) => [i ? ' · ' : null, h('span', null, item)]);
}

function statsLine(asset) {
  const parts = assetStats(asset);
  return parts.length ? h('span', { class: 'vector-stats' }, dotSeparated(parts)) : null;
}

/** One row per catalog vector preset: Create -> progress -> Download EPS / SVG / PDF + stats + preview toggle. */
function createVectorPanel(jobId, variant, { open, onPreview }) {
  const presets = getConfig().catalog.vector_presets || [];
  const assets = new Map(Object.entries(variant.vectors || {}));
  const busy = new Set();
  const rows = h('div', { class: 'vector-rows' });
  const panel = h('details', { class: 'vector-export', open: Boolean(open) },
    h('summary', null, icon('vector'), 'EPS, SVG & PDF export', icon('chevron', 'chev')),
    rows);

  function row(preset) {
    const asset = assets.get(preset.value);
    const actions = h('div', { class: 'vector-actions' });
    if (busy.has(preset.value)) {
      actions.appendChild(h('span', { class: 'vector-busy' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        preset.value === 'raster' ? 'Building EPS…' : 'Tracing vectors…'));
    } else if (asset) {
      const pdf = pdfOf(asset);
      appendAll(actions, [
        h('a', { class: 'btn btn-sm btn-primary', href: fileUrl(jobId, asset.eps, true), download: '' }, icon('download'), 'EPS'),
        asset.svg ? h('a', { class: 'btn btn-sm', href: fileUrl(jobId, asset.svg, true), download: '' }, icon('download'), 'SVG') : null,
        pdf ? h('a', { class: 'btn btn-sm', href: fileUrl(jobId, pdf, true), download: '' }, icon('download'), 'PDF') : null,
        asset.svg
          ? h('button', { type: 'button', class: 'btn btn-sm', 'aria-haspopup': 'dialog', onClick: () => onPreview(asset) },
            icon('eye'), 'Preview')
          : null,
        isLegacyVector(asset)
          ? h('button', { type: 'button', class: 'btn btn-sm', onClick: () => run(preset) }, icon('vector'), 'Remake with layers + PDF')
          : null,
      ]);
    } else {
      // One click makes all three vector files; the PDF and SVG buttons only appear afterwards, so the label names them.
      actions.appendChild(h('button', { type: 'button', class: 'btn btn-sm', onClick: () => run(preset) }, icon('vector'),
        preset.value === 'raster' ? 'Create EPS' : 'Create EPS, SVG & PDF'));
    }
    return h('div', { class: 'vector-row', tabindex: '-1', dataset: { preset: preset.value } },
      h('div', { class: 'vector-info' }, h('span', { class: 'vector-label' }, preset.label), h('span', { class: 'hint' }, preset.description)),
      actions,
      asset ? statsLine(asset) : null);
  }

  function render() {
    clear(rows);
    for (const preset of presets) rows.appendChild(row(preset));
  }

  function focusRow(value) {
    const target = Array.from(rows.children).find((r) => r.dataset.preset === value);
    if (!target) return;
    const control = target.querySelector('a, button');
    (control || target).focus();
  }

  async function run(preset) {
    const hadFocus = rows.contains(document.activeElement);
    busy.add(preset.value);
    render();
    if (hadFocus) focusRow(preset.value);
    announce(preset.value === 'raster' ? 'Building the EPS file…' : 'Tracing vectors. This can take a few seconds…');
    try {
      const asset = await api.vectorize(jobId, variant.index, preset.value);
      assets.set(preset.value, asset);
      announce(`${preset.label} is ready to download.`);
    } catch (err) {
      toast(`Export failed: ${err.message}`, { kind: 'error' });
    } finally {
      busy.delete(preset.value);
      render();
      if (hadFocus) focusRow(preset.value);
    }
  }

  render();
  return { el: panel };
}

/* ---------- AI designer brief ---------- */

const TEXT_KINDS = {
  team_name: 'Team name', player_name: 'Player name', number: 'Number', sponsor: 'Sponsor', slogan: 'Slogan', other: 'Text',
};
const asList = (value) => (Array.isArray(value) ? value : []);

function briefSection(title, content) {
  return h('section', null, h('h4', { class: 'section-title' }, title), content);
}

export function specList(pairs) {
  const dl = h('dl', { class: 'spec-list' });
  for (const [key, value] of pairs) {
    if (!value) continue;
    appendAll(dl, [h('dt', null, String(key)), h('dd', null, String(value))]);
  }
  return dl;
}

export function bulletList(items) {
  return h('ul', { class: 'plain-list' }, items.map((item) => h('li', null, String(item))));
}

/** Collapsible card for a DesignBrief (app/schemas.py); every value is rendered as text. */
export function renderBrief(brief, writer) {
  const palette = asList(brief.palette).filter((c) => c && isHex(c.hex));
  const body = h('div', { class: 'brief-body' });
  if (palette.length) {
    body.appendChild(briefSection('Palette', h('ul', { class: 'brief-palette' }, palette.map((c) => {
      const hex = c.hex.toUpperCase();
      return h('li', null, h('button', { type: 'button', class: 'brief-color', title: `Copy ${hex}`, onClick: () => copyToast(hex) },
        h('span', { class: 'swatch-color', style: { 'background-color': hex } }),
        h('span', { class: 'brief-color-text' }, h('strong', null, c.name || hex), h('span', null, c.role ? `${hex} · ${c.role}` : hex))));
    }))));
  }
  body.appendChild(briefSection('Specification', specList([
    ['Sport', brief.sport], ['Garment', brief.garment], ['Fit', brief.fit], ['Collar', brief.collar],
    ['Sleeves', brief.sleeves], ['Cuffs & hem', brief.cuffs_and_hem], ['Base colour', brief.base_color],
    ['Pattern', brief.pattern], ['Mood', brief.mood], ['Style', asList(brief.style_keywords).join(', ')],
  ])));
  const zones = asList(brief.zones).filter((z) => z && (z.zone || z.design));
  if (zones.length) body.appendChild(briefSection('Zones', specList(zones.map((z) => [z.zone || 'Zone', z.design]))));
  const texts = asList(brief.text_elements).filter(Boolean);
  body.appendChild(briefSection('Lettering', texts.length
    ? bulletList(texts.map((t) => [`${TEXT_KINDS[t.kind] || 'Text'}: “${t.content}”`, t.placement, t.style].filter(Boolean).join(' · ')))
    : h('p', { class: 'hint' }, 'No lettering.')));
  const logos = asList(brief.logos).filter(Boolean);
  if (logos.length) body.appendChild(briefSection('Logos & crests', bulletList(logos.map((l) => [l.description, l.placement].filter(Boolean).join(' · ')))));
  const avoid = asList(brief.avoid);
  if (avoid.length) body.appendChild(briefSection('Avoid', bulletList(avoid)));
  const assumptions = asList(brief.assumptions);
  if (assumptions.length) body.appendChild(briefSection('Assumptions to confirm', bulletList(assumptions)));
  if (writer) body.appendChild(h('p', { class: 'hint' }, `Written by the AI designer (${writer}).`));

  return h('details', { class: 'brief' },
    h('summary', null, icon('layers'),
      h('span', { class: 'brief-head' }, h('span', { class: 'brief-kicker' }, 'Design brief'), h('span', { class: 'brief-title' }, brief.title || 'Untitled design')),
      h('span', { class: 'brief-dots', 'aria-hidden': 'true' }, palette.slice(0, 6).map((c) => h('span', { class: 'brief-dot', style: { 'background-color': c.hex } }))),
      icon('chevron', 'chev')),
    body);
}
