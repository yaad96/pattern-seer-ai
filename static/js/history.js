/**
 * History tab: recent jobs as cards (kind filter, refresh, load more) and a detail drawer with
 * results, downloads, EPS export, the AI reference check, prompts, brief, "Continue in chat" and Delete.
 */
import { api, fileUrl, getConfig } from './api.js';
import { h, icon, uid, clear, replace, fmtDateTime, fmtRelative } from './dom.js';
import { createProgress, isActive, statusBadge, watchJob } from './jobs.js';
import { createCallLog } from './calls.js';
import { bulletList, cardCost, createResults, dotSeparated, jobCostText, modelLabel, renderBrief, specList } from './results.js';
import { promptBlock } from './settings.js';
import { callout, closeDialog, confirmDialog, loadingRow, openLightbox, segmented, setBusy, showDialog, toast } from './ui.js';

const PAGE = 60;
const MAX_LIMIT = 500; // GET /api/jobs limit upper bound
const KIND_LABEL = { recreate: 'Recreate', design: 'Design' };

const truncate = (text, n) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);

function section(title, ...content) {
  return h('section', null, h('h3', { class: 'section-title' }, title), content);
}

/* Panel jobs: the AI reference check (Job.reference_analysis), one row per garment part in panel order.
   Jobs from 2026-11-01 checked left_sleeve, right_sleeve and collar instead of sleeve. */
const PART_LABELS = new Map([
  ['front', 'Front panel'], ['back', 'Back panel'], ['sleeve', 'Sleeve panel'],
  ['left_sleeve', 'Left sleeve'], ['right_sleeve', 'Right sleeve'], ['collar', 'Collar'],
]);
const VISIBILITY = new Map([
  ['full', ['Fully visible', 'source-full']],
  ['partial', ['Partly visible', 'source-partial']],
  ['none', ['Not visible', 'source-none']],
]);
const textOf = (value) => (typeof value === 'string' ? value.trim() : '');

function referenceCheck(analysis) {
  const byPart = new Map();
  for (const item of Array.isArray(analysis.parts) ? analysis.parts : []) {
    // The first entry wins when a part repeats, as it does for the panel labels.
    if (item && typeof item.part === 'string' && !byPart.has(item.part)) byPart.set(item.part, item);
  }
  const order = [...PART_LABELS.keys()].filter((p) => byPart.has(p))
    .concat([...byPart.keys()].filter((p) => !PART_LABELS.has(p)));
  const views = textOf(analysis.views);
  const summary = textOf(analysis.summary);
  if (!order.length && !views && !summary) return null;
  const rows = order.map((part) => {
    const item = byPart.get(part);
    const [label, tone] = VISIBILITY.get(item.visibility) || [textOf(item.visibility) || '—', null];
    const note = textOf(item.note);
    return [
      h('dt', null, PART_LABELS.get(part) || part.replace(/_/g, ' ')),
      h('dd', null, h('span', { class: ['badge', 'source-badge', tone] }, label), note ? h('span', { class: 'refcheck-note' }, note) : null),
    ];
  });
  return section('Reference check',
    rows.length ? h('dl', { class: 'spec-list refcheck-list' }, rows) : null,
    views ? h('p', { class: 'hint' }, h('span', null, 'Views in the reference'), ': ', views) : null,
    summary ? h('p', { class: 'hint' }, summary) : null);
}

// The Settings table reuses the forms' labels and the catalog's choice labels (i18n.js translates them).
const OPTION_LABELS = {
  mode: 'Output type', remove_logos: 'Logos & crests', remove_sponsors: 'Sponsor marks',
  remove_names: 'Player / team names', remove_numbers: 'Numbers', remove_other_text: 'Other text',
  seamless: 'Seamless tile', correct_colors: 'Correct colours', notes: 'Extra instructions for the AI',
  print_long_edge: 'Print size', provider: 'Image AI', render_style: 'Style', text_policy: 'Lettering',
  player_number: 'Number', colors: 'Colours', use_brief_writer: 'Use the AI designer', brief_writer: 'Designer model',
  back: 'Back', front_crop: 'Front crop', back_crop: 'Back crop', panels: 'Panels',
  sleeve_crop: 'Sleeve crop', reference_check: 'AI reference check',
};
const OPTION_CHOICES = {
  fidelity: [{ value: 'exact', label: 'Exact (same-to-same)' }, { value: 'clean', label: 'Clean up' }],
  background: [{ value: 'white', label: 'White' }, { value: 'transparent', label: 'Transparent' }],
  text_policy: [{ value: 'as_described', label: 'As described' }, { value: 'none', label: 'No text or logos' }],
  brief_writer: [{ value: 'auto', label: 'Automatic' }],
  back: [{ value: 'mirror', label: 'Mirror of the front' }, { value: 'copy', label: 'Copy of the front' }, { value: 'image', label: 'Back picture' }],
};
const CATALOG_CHOICES = {
  mode: 'recreate_modes', render_style: 'render_styles', view: 'views', aspect_ratio: 'aspect_ratios',
  resolution: 'resolutions', print_long_edge: 'print_sizes', sport: 'sports', collar: 'collars', sleeves: 'sleeves',
};

function choicesFor(key) {
  const config = getConfig();
  if (!config) return OPTION_CHOICES[key] || [];
  if (key === 'provider') return config.providers.map((p) => ({ value: p.name, label: p.label }));
  if (key === 'brief_writer') return OPTION_CHOICES.brief_writer.concat((config.brief_writers || []).map((w) => ({ value: w.name, label: w.label })));
  return OPTION_CHOICES[key] || config.catalog[CATALOG_CHOICES[key]] || [];
}

function optionValue(key, value) {
  if (['front_crop', 'back_crop', 'sleeve_crop'].includes(key) && Array.isArray(value) && value.length === 4) { // l, t, r, b fractions
    const pct = (v) => Math.round(v * 100);
    return `${pct(value[0])}–${pct(value[2])}% × ${pct(value[1])}–${pct(value[3])}%`;
  }
  const choice = choicesFor(key).find((c) => c.value === value);
  if (choice) return choice.label;
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  if (key === 'panels' && Array.isArray(value)) { // " · " so the Bangla layer translates each name
    return value.map((p) => ({ front: 'Front', back: 'Back', sleeve: 'Sleeve' }[p] || p)).join(' · ');
  }
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—';
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

function optionRows(options) {
  const panels = options && options.mode === 'panels';
  // back only means something for panels; panels have a fixed print size and fixed ratios, so their Print size and
  // Aspect ratio values are unused. Variants is hidden in the forms (always 1 since 2026-11-03), so it isn't listed.
  const panelOnly = (key) => ['back', 'back_crop', 'panels', 'sleeve_crop', 'reference_check'].includes(key);
  const unusedInPanels = (key) => key === 'print_long_edge' || key === 'aspect_ratio';
  const shown = Object.keys(options || {}).filter((key) => key !== 'variants'
    && (panelOnly(key) ? panels : !(panels && unusedInPanels(key))));
  return shown.map((key) => {
    const label = OPTION_LABELS[key] || key.replace(/_/g, ' ');
    return [label.charAt(0).toUpperCase() + label.slice(1), optionValue(key, options[key])];
  });
}

export function initHistory(root, { onContinue, onDeleted }) {
  const state = { kind: '', limit: PAGE, items: [], loaded: false, seq: 0 };

  const filter = segmented({
    legend: 'Show',
    options: [{ value: '', label: 'All' }, { value: 'recreate', label: 'Recreate' }, { value: 'design', label: 'Design' }],
    value: '',
    onChange: (value) => {
      state.kind = value;
      state.limit = PAGE;
      refresh();
    },
  });
  filter.el.querySelector('legend').classList.add('visually-hidden');
  const count = h('span', { class: 'muted' });
  const refreshBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('refresh'), 'Refresh');
  refreshBtn.addEventListener('click', () => refresh());
  const grid = h('div', { class: 'history-grid' });
  const more = h('div', { class: 'history-more' });
  const loadMore = h('button', { type: 'button', class: 'btn' }, 'Load more');
  loadMore.addEventListener('click', () => {
    state.limit = Math.min(MAX_LIMIT, state.limit + PAGE);
    refresh();
  });
  replace(root,
    h('div', { class: 'history-toolbar' }, h('h2', null, 'History'), count, h('span', { class: 'spacer' }), filter.el, refreshBtn),
    grid, more);

  async function refresh() {
    state.seq += 1;
    const mine = state.seq;
    setBusy(refreshBtn, true, 'Refreshing…');
    if (!state.loaded) replace(grid, loadingRow('Loading history…'));
    try {
      const items = await api.listJobs({ limit: state.limit, kind: state.kind || undefined });
      if (mine !== state.seq) return;
      state.items = items;
      state.loaded = true;
      render();
    } catch (err) {
      if (mine !== state.seq) return;
      if (!state.loaded) replace(grid, callout('error', `Could not load the history: ${err.message}`));
      else toast(err.message, { kind: 'error' });
    } finally {
      if (mine === state.seq) setBusy(refreshBtn, false);
    }
  }

  function card(s) {
    const title = s.kind === 'design' ? (s.message || 'Design') : 'Recreate from image';
    const meta = [modelLabel(s.provider, s.model), `${s.succeeded}/${s.variants} variant${s.variants === 1 ? '' : 's'}`];
    meta.push(...cardCost(s));
    return h('button', { type: 'button', class: 'job-card', dataset: { jobId: s.id }, onClick: () => openJob(s.id) },
      h('span', { class: 'job-card-media' },
        s.preview
          ? h('img', { src: fileUrl(s.id, s.preview), alt: '', loading: 'lazy', decoding: 'async' })
          : icon(s.kind === 'design' ? 'chat' : 'image'),
        s.status !== 'succeeded' ? h('span', { class: 'job-card-status' }, statusBadge(s.status)) : null),
      h('span', { class: 'job-card-body' },
        h('span', { class: 'job-card-top' },
          h('span', { class: 'badge badge-accent' }, KIND_LABEL[s.kind] || s.kind),
          h('span', { title: fmtDateTime(s.created_at) }, fmtRelative(s.created_at))),
        h('span', { class: 'job-card-title' }, title),
        h('span', { class: 'job-card-meta' }, dotSeparated(meta.filter(Boolean)))));
  }

  function render() {
    const n = state.items.length;
    count.textContent = n ? `${n}${n >= state.limit ? '+' : ''} job${n === 1 ? '' : 's'}` : '';
    if (!n) {
      replace(grid, h('div', { class: 'empty' },
        h('div', { class: 'empty-icon' }, icon('history')),
        h('p', { class: 'empty-title' }, 'Nothing here yet'),
        h('p', { class: 'empty-text' }, state.kind
          ? `No ${KIND_LABEL[state.kind].toLowerCase()} jobs yet.`
          : 'Everything you make in Recreate and Design is listed here.')));
      clear(more);
      return;
    }
    const focused = grid.contains(document.activeElement) ? document.activeElement.dataset.jobId : null;
    replace(grid, state.items.map(card));
    if (focused) {
      const again = Array.from(grid.children).find((c) => c.dataset.jobId === focused);
      if (again) again.focus();
    }
    replace(more, n >= state.limit && state.limit < MAX_LIMIT ? loadMore : null);
  }

  /* ---------- detail drawer ---------- */
  async function openJob(jobId) {
    const titleId = uid('drawer-title');
    const dlg = h('dialog', { class: 'drawer', 'aria-labelledby': titleId });
    const title = h('h2', { class: 'drawer-title', id: titleId }, 'Job details');
    const sub = h('div', { class: 'drawer-sub' });
    const body = h('div', { class: 'drawer-body' }, loadingRow('Loading…'));
    const closeBtn = h('button', { type: 'button', class: 'btn btn-icon btn-ghost', 'aria-label': 'Close details' }, icon('x'));
    closeBtn.addEventListener('click', () => closeDialog(dlg));
    dlg.appendChild(h('div', { class: 'drawer-panel' },
      h('header', { class: 'drawer-header' }, h('div', { class: 'drawer-heading' }, title, sub), closeBtn),
      body));
    let unwatch = null;
    let retried = false;
    showDialog(dlg, () => {
      if (unwatch) unwatch();
      if (retried) refresh(); // the job's card in the grid shows its new status
    });
    let job;
    try {
      job = await api.job(jobId);
    } catch (err) {
      replace(body, callout('error', err.message));
      return;
    }
    if (!dlg.isConnected) return;
    const watch = () => {
      if (unwatch) unwatch();
      unwatch = watchJob(jobId, (next, meta) => { if (next) view.update(next, meta); });
    };
    // A failed image generated again from the drawer: the job runs again, so watch it until it is done.
    const onRetry = async (j, v) => {
      await api.retryVariant(j.id, v.index);
      retried = true;
      view.restartClock();
      if (dlg.isConnected) watch();
    };
    const view = renderDrawer(job, { dlg, title, sub, body, onRetry });
    if (isActive(job.status)) watch();
  }

  function renderDrawer(initial, { dlg, title, sub, body, onRetry }) {
    let job = initial;
    let progress = null;
    let sig = '';
    title.textContent = job.kind === 'design' ? truncate(job.message || 'Design', 90) : 'Recreate from image';
    const badge = h('span');
    const cost = h('span');
    replace(sub,
      h('span', { class: 'badge badge-accent' }, KIND_LABEL[job.kind] || job.kind),
      badge,
      h('span', null, fmtDateTime(job.created_at)),
      cost,
      h('span', { class: 'mono' }, job.id));

    const actions = h('div', { class: 'drawer-actions' });
    if (job.kind === 'design' && onContinue) {
      const continueBtn = h('button', { type: 'button', class: 'btn btn-primary' }, icon('chat'), 'Continue in chat');
      continueBtn.addEventListener('click', () => {
        closeDialog(dlg);
        onContinue(job);
      });
      actions.appendChild(continueBtn);
    }
    const deleteBtn = h('button', { type: 'button', class: 'btn btn-danger' }, icon('trash'), 'Delete');
    deleteBtn.addEventListener('click', async () => {
      const ok = await confirmDialog({
        title: 'Delete this job?',
        message: 'Its images and EPS/SVG/PDF files are removed from the server. This cannot be undone.',
        confirmLabel: 'Delete',
        danger: true,
      });
      if (!ok) return;
      setBusy(deleteBtn, true, 'Deleting…');
      try {
        await api.deleteJob(job.id);
      } catch (err) {
        setBusy(deleteBtn, false);
        toast(err.message, { kind: 'error' });
        return;
      }
      closeDialog(dlg);
      toast('Job deleted.', { kind: 'success' });
      state.items = state.items.filter((item) => item.id !== job.id);
      render();
      if (onDeleted) onDeleted(job.id);
    });
    actions.appendChild(deleteBtn);

    const progressSlot = h('div');
    const textSlot = h('div', { class: 'result-stack' });
    const results = createResults(job, { compare: job.kind === 'recreate', vectorsOpen: false, onRetry });
    const detailsSlot = h('div', { class: 'result-stack' });
    const callLog = createCallLog(job);  // kept across re-renders, so it stays open while calls come in
    replace(body, actions, progressSlot, textSlot, section('Variants', results.el), detailsSlot, callLog.el);

    function textParts() {
      const parts = [];
      if (job.status === 'failed' && job.error) parts.push(callout('error', job.error));
      for (const w of job.warnings || []) parts.push(callout('warning', w));
      const o = job.options || {};
      const panelJob = job.kind === 'recreate' && o.mode === 'panels';
      // the first chosen panel (the server stores them in front, back, sleeve order); jobs before 2026-11-04 had
      // no choice: the front
      const first = (Array.isArray(o.panels) && o.panels[0]) || 'front';
      const PICTURE = { front: 'Front picture', back: 'Back picture', sleeve: 'Sleeve picture' };
      const inputs = (job.inputs || []).map((name, i) => {
        const src = fileUrl(job.id, name);
        // A recreate job has one reference picture. A panel job with more than one has the first chosen panel's
        // (input_1.png), maybe the back's own (input_2.png) and the sleeve's (input_3.png, since 2026-11-04), so
        // each is named by its file, not its place in the list.
        const own = { 'input_1.png': PICTURE[first], 'input_2.png': PICTURE.back, 'input_3.png': PICTURE.sleeve }[name];
        const recreateLabel = panelJob && job.inputs.length > 1 && own ? own : 'Reference image';
        const label = job.kind === 'recreate' ? recreateLabel : `Attached image ${i + 1}`;
        return h('button', {
          type: 'button', class: 'thumb', 'aria-label': `View ${label.toLowerCase()}`,
          onClick: () => openLightbox({ src, alt: label, caption: label, downloadUrl: fileUrl(job.id, name, true), downloadLabel: 'Download' }),
        }, h('img', { src, alt: '' }));
      });
      if (job.kind === 'design') {
        parts.push(section('Client message', h('p', { class: 'quote' }, job.message || '')));
        if (job.parent) parts.push(h('p', { class: 'hint' }, `Refinement of variant ${job.parent.variant} of job ${job.parent.job_id}.`));
        if (inputs.length) parts.push(section('Attached images', h('div', { class: 'thumbs' }, inputs)));
        if (job.reply) parts.push(section('Designer reply', h('p', null, job.reply)));
        if (job.changes && job.changes.length) parts.push(section('What changed', bulletList(job.changes)));
        if (job.suggestions && job.suggestions.length) parts.push(section('Suggestions', bulletList(job.suggestions)));
        if (job.brief) parts.push(renderBrief(job.brief, job.brief_writer));
      } else if (inputs.length) {
        parts.push(section('Reference image', h('div', { class: 'thumbs' }, inputs)));
      }
      if (job.reference_analysis && typeof job.reference_analysis === 'object') parts.push(referenceCheck(job.reference_analysis));
      return parts;
    }

    function detailParts() {
      const parts = [section('Settings', specList([
        ['Model', modelLabel(job.provider, job.model)],
        ['Aspect ratio', job.aspect_ratio],
        ['Resolution', job.resolution],
        ['AI designer', job.brief_writer],
      ].concat(optionRows(job.options))))];
      if (job.prompt) {
        parts.push(h('details', { class: 'vector-export' },
          h('summary', null, icon('eye'), 'Image prompt sent', icon('chevron', 'chev')),
          h('div', { class: 'result-stack', style: { padding: '12px' } },
            promptBlock('System prompt', job.prompt.system), promptBlock('User prompt', job.prompt.user))));
      }
      if (job.brief_prompt) {
        parts.push(h('details', { class: 'vector-export' },
          h('summary', null, icon('layers'), 'AI designer prompt', icon('chevron', 'chev')),
          h('div', { class: 'result-stack', style: { padding: '12px' } },
            promptBlock('System prompt', job.brief_prompt.system), promptBlock('User prompt', job.brief_prompt.user))));
      }
      return parts;
    }

    let since = 0; // set when a failed image is generated again: its clock starts then
    function update(next, meta) {
      job = next;
      replace(badge, statusBadge(job.status));
      if (isActive(job.status)) {
        if (!progress) {
          progress = createProgress(since ? { since } : undefined);
          progressSlot.appendChild(progress.el);
        }
        progress.update(job, meta || {});
      } else if (progress) {
        progress.stop();
        progress.el.remove();
        progress = null;
      }
      cost.textContent = jobCostText(job);
      const nextSig = [job.status, job.error || '', (job.warnings || []).length, job.reply || '',
        job.brief ? 1 : 0, job.prompt ? 1 : 0, job.brief_prompt ? 1 : 0, job.reference_analysis ? 1 : 0].join('|');
      if (nextSig !== sig) {
        sig = nextSig;
        replace(textSlot, textParts());
        replace(detailsSlot, detailParts());
      }
      results.update(job);
      callLog.update(job);
      deleteBtn.disabled = isActive(job.status);
      deleteBtn.title = deleteBtn.disabled ? 'A running job cannot be deleted.' : '';
    }

    update(job, {});
    return {
      update,
      restartClock() {
        since = Date.now();
        if (progress) {
          progress.stop();
          progress.el.remove();
          progress = null;
        }
      },
    };
  }

  return {
    activate() {
      refresh();
    },
  };
}
