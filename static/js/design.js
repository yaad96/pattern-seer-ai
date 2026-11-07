/**
 * Design tab: the client describes a jersey in a chat. The first message creates a new design;
 * later messages refine the active parent variant (default: the first finished variant of the
 * latest successful job). The session (session id, job ids, active parent) persists in
 * localStorage and is restored on reload.
 */
import { api, fileUrl, getConfig } from './api.js';
import {
  h, icon, uid, clear, replace, appendAll, fmtDateTime, fmtRelative, normalizeHex, storage,
  imageFilesFrom, hasFiles, checkImageFile,
} from './dom.js';
import { createProgress, isActive, statusBadge, watchJob } from './jobs.js';
import { createResults, customerError, customerWarnings, modelLabel, renderBrief, withoutModel } from './results.js';
import { createOutputSettings, openPromptPreview, pickKnown } from './settings.js';
import {
  announce, callout, choiceCards, confirmDialog, field, inputEl, loadingRow, openLightbox, segmented, selectEl,
  switchEl, toast,
} from './ui.js';

const SESSION_KEY = 'design.session';
const JOB_ID_RE = /^\d{8}-\d{6}-[0-9a-f]{6}$/;
const SESSION_RE = /^[0-9a-f]{32}$/;
const OTHER_SPORT = '__other__';
const MAX_COLORS = 8; // DesignOptions.colors max_length
const STARTER_COLORS = ['#1F3A93', '#D4AF37', '#FFFFFF', '#111111', '#C8102E', '#00843D', '#FFD100', '#7A1F3D'];
const EXAMPLES = [
  'Maroon cricket shirt with gold tiger stripes on the sleeves and a navy collar',
  'Black and neon green esports jersey with sharp geometric shards',
  'Classic football kit: white with thin navy pinstripes and a V-neck',
  'Sky blue volleyball jersey with a wave pattern rising from the hem',
];

function sidebarSection(title, children) {
  return h('section', { class: 'sidebar-section' }, h('h3', null, title), children);
}

/** The settings sidebar; values() returns a complete DesignOptions object. */
function createDesignSettings(config, onChange, customer) {
  const defaults = config.defaults.design;
  const catalog = config.catalog;
  let ready = false; // no change notifications while the controls are being built
  const changed = () => { if (ready) onChange(); };

  const sports = catalog.sports;
  const wanted = String(defaults.sport || '').toLowerCase();
  const knownSport = sports.find((s) => s.value === defaults.sport)
    || sports.find((s) => s.value.toLowerCase().startsWith(wanted));
  const sportOther = inputEl({ maxlength: '60', placeholder: 'e.g. netball', value: knownSport ? '' : defaults.sport });
  const sportOtherField = field('Sport name', sportOther);
  const sportSel = selectEl(sports.concat([{ value: OTHER_SPORT, label: 'Other…' }]), knownSport ? knownSport.value : OTHER_SPORT, () => {
    sportOtherField.hidden = sportSel.value !== OTHER_SPORT;
    changed();
  });
  sportOtherField.hidden = sportSel.value !== OTHER_SPORT;

  const view = segmented({ legend: 'View', options: catalog.views, value: defaults.view, block: true, onChange: changed });
  const style = choiceCards({ legend: 'Style', options: catalog.render_styles, value: defaults.render_style, onChange: syncBackground });
  const collarSel = selectEl(catalog.collars, defaults.collar, changed);
  const sleevesSel = selectEl(catalog.sleeves, defaults.sleeves, changed);

  const policy = segmented({
    legend: 'Lettering',
    options: [{ value: 'as_described', label: 'As described' }, { value: 'none', label: 'No text or logos' }],
    value: defaults.text_policy,
    block: true,
    onChange: syncPolicy,
  });
  policy.el.appendChild(h('p', { class: 'hint' }, 'As described: only lettering the client asks for. No text: no names, numbers or logos at all.'));
  const teamName = inputEl({ maxlength: '60', placeholder: 'e.g. Dhaka Tigers', value: defaults.team_name });
  const playerName = inputEl({ maxlength: '40', placeholder: 'e.g. RAHMAN', value: defaults.player_name });
  const playerNumber = inputEl({ maxlength: '4', placeholder: 'e.g. 10', inputmode: 'numeric', value: defaults.player_number });
  function syncPolicy() {
    const none = policy.value === 'none';
    for (const input of [teamName, playerName, playerNumber]) input.disabled = none;
    changed();
  }

  const colors = [];
  for (const c of defaults.colors || []) {
    const hex = normalizeHex(c);
    if (hex) colors.push(hex);
  }
  const colorList = h('div', { class: 'color-list' });
  const addColorBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('plus'), 'Add colour');
  addColorBtn.addEventListener('click', () => {
    colors.push(STARTER_COLORS.find((c) => !colors.includes(c)) || '#888888');
    renderColors();
    changed();
    const pickers = colorList.querySelectorAll('input[type="color"]');
    if (pickers.length) pickers[pickers.length - 1].focus();
  });
  function renderColors() {
    clear(colorList);
    colors.forEach((hex, i) => {
      const picker = h('input', { type: 'color', value: hex.toLowerCase(), 'aria-label': `Colour ${i + 1}` });
      const text = inputEl({ class: 'input input-sm', value: hex, maxlength: '7', spellcheck: 'false', 'aria-label': `Colour ${i + 1} hex code` });
      picker.addEventListener('input', () => {
        colors[i] = picker.value.toUpperCase();
        text.value = colors[i];
        changed();
      });
      text.addEventListener('change', () => {
        const next = normalizeHex(text.value);
        if (next) {
          colors[i] = next;
          picker.value = next.toLowerCase();
        } else {
          toast(`“${text.value}” is not a hex colour like #1F3A93.`, { kind: 'warning' });
        }
        text.value = colors[i];
        changed();
      });
      const remove = h('button', { type: 'button', class: 'btn btn-sm btn-icon btn-ghost', 'aria-label': `Remove colour ${i + 1}` }, icon('x'));
      remove.addEventListener('click', () => {
        colors.splice(i, 1);
        renderColors();
        changed();
        addColorBtn.focus();
      });
      colorList.appendChild(h('div', { class: 'color-row' }, picker, text, remove));
    });
    addColorBtn.disabled = colors.length >= MAX_COLORS;
  }
  renderColors();

  const bgHint = h('p', { class: 'hint' });
  const background = segmented({
    legend: 'Background',
    options: [{ value: 'white', label: 'White' }, { value: 'transparent', label: 'Transparent' }],
    value: defaults.background,
    block: true,
    onChange: changed,
  });
  background.el.appendChild(bgHint);

  const writers = config.brief_writers || [];
  const writerEnabled = Boolean(config.brief_writer_enabled && writers.length);
  const defaultWriter = writers.find((w) => w.name === config.default_brief_writer);
  const useWriter = switchEl({
    label: 'Use the AI designer',
    hint: 'Turns the client’s words into a detailed design brief (colours, zones, lettering) before the image is made.',
    checked: defaults.use_brief_writer,
    onChange: () => {
      writerSel.disabled = !useWriter.input.checked;
      changed();
    },
  });
  const writerSel = selectEl(
    [{ value: 'auto', label: defaultWriter ? `Automatic (${defaultWriter.label})` : 'Automatic' }]
      .concat(writers.map((w) => ({ value: w.name, label: `${w.label} · ${w.model}` }))),
    defaults.brief_writer,
    changed,
  );
  writerSel.disabled = !useWriter.input.checked;

  const output = createOutputSettings({ defaults, autoLabel: 'Auto (from view and style)', onChange: syncBackground });

  function syncBackground() {
    const isPattern = style.value === 'pattern';
    const canTransparent = Boolean(output.model().supports_transparent);
    background.disableAll(isPattern);
    if (!isPattern) background.disable('transparent', !canTransparent);
    if ((isPattern || !canTransparent) && background.value === 'transparent') background.value = 'white';
    if (isPattern) bgHint.textContent = 'Pattern output is always a full-bleed image.';
    else if (canTransparent) bgHint.textContent = 'Transparent keeps only the garment (PNG with alpha).';
    else bgHint.textContent = 'The selected model cannot make transparent backgrounds.';
    changed();
  }
  syncBackground();
  syncPolicy();

  const sections = [
    sidebarSection('Jersey', [
      field('Sport', sportSel), sportOtherField, view.el, style.el,
      h('div', { class: 'field-grid' }, field('Collar', collarSel), field('Sleeves', sleevesSel)),
    ]),
    sidebarSection('Lettering', [
      policy.el,
      field('Team name', teamName),
      h('div', { class: 'field-grid' }, field('Player name', playerName), field('Number', playerNumber)),
    ]),
    sidebarSection('Colours', [
      h('p', { class: 'hint' }, 'Fixed palette the design must use (up to 8). Leave empty to let the designer choose.'),
      colorList,
      h('div', null, addColorBtn),
      background.el,
    ]),
    // the customer page: no AI designer choice and no output settings (the server's defaults are used)
    writerEnabled && !customer ? sidebarSection('AI designer', [useWriter.el, field('Designer model', writerSel)]) : null,
    customer ? null : sidebarSection('Output', [output.el]),
  ];
  ready = true;

  return {
    sections,
    writerEnabled,
    model: () => output.model(),
    clearLettering() {
      teamName.value = '';
      playerName.value = '';
      playerNumber.value = '';
    },
    values() {
      const renderStyle = style.value;
      const none = policy.value === 'none';
      const canTransparent = renderStyle !== 'pattern' && Boolean(output.model().supports_transparent);
      const sport = sportSel.value === OTHER_SPORT ? (sportOther.value.trim().slice(0, 60) || defaults.sport) : sportSel.value;
      return pickKnown(defaults, Object.assign({
        sport,
        view: view.value,
        render_style: renderStyle,
        collar: collarSel.value,
        sleeves: sleevesSel.value,
        text_policy: policy.value,
        team_name: none ? '' : teamName.value.trim().slice(0, 60),
        player_name: none ? '' : playerName.value.trim().slice(0, 40),
        player_number: none ? '' : playerNumber.value.trim().slice(0, 4),
        colors: colors.slice(0, MAX_COLORS),
        background: canTransparent ? background.value : 'white',
        use_brief_writer: writerEnabled && useWriter.input.checked,
        brief_writer: writerEnabled && !customer ? writerSel.value : 'auto',
      }, output.values()));
    },
  };
}

/** customer: the /customerep page for showing clients a demo: no AI designer or output settings, prompt, model
 * names or costs, and its own saved conversation. */
export function initDesign(root, { customer = false, keyPrefix = null } = {}) {
  const config = getConfig();
  const limits = config.limits;
  // each page keeps its own chat: /customerep and /experiment (customer), and /panels (the studio's full Design)
  const prefix = keyPrefix || (customer ? 'customer' : null);
  const sessionKey = prefix ? `${prefix}.design.session` : SESSION_KEY;
  const state = { sessionId: null, turns: [], parent: null, epoch: 0, attachments: [], sending: false, loading: false };
  const settings = createDesignSettings(config, () => updateComposer(), customer);
  const progressOpts = { stageText: customer ? withoutModel : null };

  /* ---------- sidebar (collapsible on small screens) ---------- */
  const sidebarId = uid('design-settings');
  const promptBtn = h('button', { type: 'button', class: 'btn btn-sm', hidden: customer }, icon('eye'), 'View prompt');
  promptBtn.addEventListener('click', showPrompt);
  const closeBtn = h('button', { type: 'button', class: 'btn btn-sm btn-icon btn-ghost sidebar-close', 'aria-label': 'Close settings' }, icon('x'));
  const sidebar = h('aside', { class: 'card design-sidebar', id: sidebarId, 'aria-label': 'Design settings' },
    h('div', { class: 'card-header sidebar-head' }, h('h2', { class: 'card-title' }, 'Settings'), h('span', { class: 'spacer' }), promptBtn, closeBtn),
    settings.sections);
  const backdrop = h('div', { class: 'sidebar-backdrop' });
  const toggleBtn = h('button', { type: 'button', class: 'btn btn-sm sidebar-toggle', 'aria-controls': sidebarId, 'aria-expanded': 'false' },
    icon('sliders'), 'Settings');
  function onSidebarKey(e) {
    if (e.key === 'Escape') closeSidebar();
  }
  function openSidebar() {
    sidebar.classList.add('is-open');
    backdrop.classList.add('is-open');
    toggleBtn.setAttribute('aria-expanded', 'true');
    document.addEventListener('keydown', onSidebarKey);
    closeBtn.focus();
  }
  function closeSidebar() {
    if (!sidebar.classList.contains('is-open')) return;
    sidebar.classList.remove('is-open');
    backdrop.classList.remove('is-open');
    toggleBtn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('keydown', onSidebarKey);
    toggleBtn.focus();
  }
  toggleBtn.addEventListener('click', openSidebar);
  closeBtn.addEventListener('click', closeSidebar);
  backdrop.addEventListener('click', closeSidebar);

  /* ---------- chat column ---------- */
  const sessionInfo = h('span', { class: 'muted' });
  const newBtn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('plus'), 'New design');
  newBtn.addEventListener('click', newDesign);
  const thread = h('section', { class: 'chat-thread', 'aria-label': 'Conversation', tabindex: '0' });

  const indicator = h('div', { hidden: true });
  const thumbs = h('div', { class: 'thumbs', hidden: true });
  const attachInput = h('input', { type: 'file', accept: 'image/*', multiple: true, hidden: true });
  const attachBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-icon', 'aria-label': 'Attach reference images', title: 'Attach images (logo, inspiration photo)' }, icon('paperclip'));
  const textarea = h('textarea', { id: uid('composer'), class: 'composer-input', rows: '1', maxlength: String(limits.max_message_chars) });
  const sendLabel = h('span', { class: 'composer-send-label' }, 'Send');
  const sendBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, icon('send'), sendLabel);
  const counter = h('span', { class: 'counter' });
  const hintId = uid('composer-hint');
  textarea.setAttribute('aria-describedby', hintId);
  const composer = h('form', { class: 'composer' },
    indicator, thumbs,
    h('div', { class: 'composer-box' },
      attachBtn,
      h('label', { class: 'visually-hidden', for: textarea.id }, 'Message to the designer'),
      textarea, sendBtn),
    h('div', { class: 'composer-foot' },
      h('span', { class: 'hint', id: hintId }, customer ? 'Enter to send · Shift+Enter for a new line · drop images to attach'
        : 'Enter to send · Shift+Enter for a new line · paste or drop images to attach'),
      counter),
    attachInput);
  composer.addEventListener('submit', (e) => {
    e.preventDefault();
    send();
  });
  attachBtn.addEventListener('click', () => attachInput.click());
  attachInput.addEventListener('change', () => {
    addAttachments(Array.from(attachInput.files || []));
    attachInput.value = '';
  });
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      send();
    }
  });
  textarea.addEventListener('input', () => {
    autosize();
    updateComposer();
  });

  const chat = h('div', { class: 'card chat' },
    h('div', { class: 'card-header' }, h('h2', { class: 'card-title' }, 'Design chat'), sessionInfo, h('span', { class: 'spacer' }), toggleBtn, newBtn),
    thread, composer);
  chat.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  chat.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    addAttachments(imageFilesFrom(e.dataTransfer));
  });

  replace(root, h('div', { class: 'design-layout' }, chat, sidebar), backdrop);

  /* ---------- composer state ---------- */
  function autosize() {
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
  }

  function busyTurn() {
    return state.turns.some((t) => !t.job || isActive(t.job.status));
  }

  function attachLimit() {
    const model = settings.model();
    const maxInputs = model && model.max_input_images ? model.max_input_images : limits.max_attachments;
    return Math.max(0, Math.min(limits.max_attachments, maxInputs - (state.parent ? 1 : 0)));
  }

  function updateComposer() {
    const running = busyTurn();
    const len = textarea.value.length;
    sendBtn.disabled = state.sending || state.loading || running || !textarea.value.trim();
    sendLabel.textContent = state.sending ? 'Sending…' : (running ? 'Working…' : 'Send');
    attachBtn.disabled = state.attachments.length >= attachLimit();
    textarea.placeholder = state.parent
      ? 'Describe the change, e.g. “make the sleeves navy and add gold cuffs”'
      : 'Describe the jersey: colours, pattern, collar, sleeves, team name…';
    counter.textContent = len > limits.max_message_chars * 0.8 ? `${len} / ${limits.max_message_chars}` : '';
    sessionInfo.textContent = state.turns.length ? `${state.turns.length} message${state.turns.length === 1 ? '' : 's'}` : '';
  }

  function useText(text) {
    textarea.value = text;
    autosize();
    updateComposer();
    textarea.focus();
  }

  function addAttachments(files) {
    for (const file of files) {
      const max = attachLimit();
      if (state.attachments.length >= max) {
        toast(`You can attach up to ${max} image${max === 1 ? '' : 's'} to this message.`, { kind: 'warning' });
        break;
      }
      const problem = checkImageFile(file, limits.max_upload_mb);
      if (problem) {
        toast(problem, { kind: 'error' });
        continue;
      }
      state.attachments.push({ file, url: URL.createObjectURL(file) });
    }
    renderThumbs();
    updateComposer();
  }

  function renderThumbs() {
    thumbs.hidden = !state.attachments.length;
    replace(thumbs, state.attachments.map((a, i) => {
      const remove = h('button', { type: 'button', class: 'thumb-remove', 'aria-label': `Remove attachment ${i + 1} (${a.file.name || 'image'})` }, icon('x'));
      remove.addEventListener('click', () => {
        URL.revokeObjectURL(a.url);
        state.attachments.splice(i, 1);
        renderThumbs();
        updateComposer();
        textarea.focus();
      });
      return h('div', { class: 'thumb' }, h('img', { src: a.url, alt: '' }), remove);
    }));
  }

  /* ---------- thread ---------- */
  function renderWelcome() {
    replace(thread, h('div', { class: 'empty chat-welcome' },
      h('div', { class: 'empty-icon' }, icon('chat')),
      h('p', { class: 'empty-title' }, 'Describe the jersey you want'),
      h('p', { class: 'empty-text' }, 'Write in your own words: colours, pattern, collar, sleeves, team name. Attach a logo or a photo for inspiration, then refine the result by chatting.'),
      h('div', { class: 'chips', role: 'group', 'aria-label': 'Examples' }, EXAMPLES.map((text) =>
        h('button', { type: 'button', class: 'chip', onClick: () => useText(text) }, text)))));
  }

  const findTurn = (jobId) => state.turns.find((t) => t.jobId === jobId) || null;

  function nearBottom() {
    return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 140;
  }

  function scrollToBottom(instant) {
    thread.scrollTo({ top: thread.scrollHeight, behavior: instant ? 'auto' : 'smooth' });
  }

  function parentRef(parent) {
    const turn = findTurn(parent.jobId);
    const job = turn && turn.job;
    const v = job ? job.variants[parent.variant - 1] : null;
    return h('div', { class: 'bubble-ref' },
      v && v.preview ? h('img', { src: fileUrl(job.id, v.preview), alt: '' }) : icon('wand'),
      `Refining variant ${parent.variant}${job && job.brief ? ` of “${job.brief.title}”` : ''}`);
  }

  function clientMessage(job, local) {
    const message = local ? local.message : (job.message || '');
    const parent = local ? local.parent : (job.parent ? { jobId: job.parent.job_id, variant: job.parent.variant } : null);
    const images = local
      ? local.objectUrls
      : (job.inputs || []).filter((name) => /^attach_\d+\.png$/.test(name)).map((name) => fileUrl(job.id, name));
    return h('div', { class: 'msg msg-client' },
      h('div', { class: 'bubble-client' },
        parent ? parentRef(parent) : null,
        h('p', { class: 'bubble-text' }, message),
        images.length ? h('div', { class: 'thumbs' }, images.map((src, i) =>
          h('button', {
            type: 'button', class: 'thumb', 'aria-label': `View attached image ${i + 1}`,
            onClick: () => openLightbox({ src, alt: `Attached image ${i + 1}`, caption: `Attached image ${i + 1}` }),
          }, h('img', { src, alt: '' })))) : null));
  }

  function defaultReply(job) {
    if (job.status === 'failed') return 'Sorry, this design could not be generated. Try again, or change the description.';
    const partial = job.status === 'partial' ? ' Some variants failed; the ones that worked are below.' : '';
    if (job.parent) return `Here is the updated design.${partial}`;
    const pick = job.variants.length > 1 ? ' Pick the one you like best and refine it.' : '';
    return `Here is a first design based on your description.${pick}${partial}`;
  }

  function aiText(job) {
    const parts = [];
    if (job.reply) {
      parts.push(h('div', { class: 'reply' }, job.reply.split(/\n{2,}/).map((p) => p.trim()).filter(Boolean).map((p) => h('p', null, p))));
    } else if (isActive(job.status)) {
      parts.push(h('p', { class: 'muted' }, job.parent ? 'Working on your changes…' : 'Working on your design…'));
    } else {
      parts.push(h('div', { class: 'reply' }, h('p', null, defaultReply(job))));
    }
    if (job.changes && job.changes.length) {
      parts.push(h('div', { class: 'changes' }, h('p', { class: 'changes-title' }, 'What changed'),
        h('ul', { class: 'plain-list' }, job.changes.map((c) => h('li', null, c)))));
    }
    if (job.status === 'failed' && job.error) parts.push(callout('error', customer ? customerError(job.error) : job.error));
    for (const w of customer ? customerWarnings(job.warnings) : job.warnings || []) parts.push(callout('warning', w));
    if (job.brief) parts.push(renderBrief(job.brief, customer ? null : job.brief_writer));
    return parts;
  }

  function renderTurn(turn, job, meta) {
    turn.job = job;
    replace(turn.badge, statusBadge(job.status));
    turn.time.textContent = fmtRelative(job.created_at);
    turn.time.title = fmtDateTime(job.created_at);
    const active = isActive(job.status);
    if (active) {
      if (!turn.progress) {
        turn.progress = createProgress(progressOpts);
        turn.progressSlot.appendChild(turn.progress.el);
      }
      turn.progress.update(job, meta);
    } else if (turn.progress) {
      turn.progress.stop();
      turn.progress.el.remove();
      turn.progress = null;
    }
    const sig = [job.status, job.reply || '', job.brief ? job.brief.title : '', (job.warnings || []).length, job.error || '', (job.changes || []).length].join('|');
    if (sig !== turn.textSig) {
      turn.textSig = sig;
      replace(turn.textSlot, aiText(job));
    }
    if (!turn.results) {
      turn.results = createResults(job, {
        compact: true,
        customer,
        activeParent: state.parent,
        onRefine: (j, index) => setParent({ jobId: j.id, variant: index }, true),
      });
      turn.resultsSlot.appendChild(turn.results.el);
    } else {
      turn.results.update(job);
    }
    if (!active && job.suggestions && job.suggestions.length && !turn.suggestSlot.firstChild) {
      turn.suggestSlot.appendChild(h('div', { class: 'chips', role: 'group', 'aria-label': 'Suggested next changes' },
        job.suggestions.map((s) => h('button', { type: 'button', class: 'chip', onClick: () => useText(s) }, icon('sparkles'), s))));
    }
    turn.meta.textContent = customer ? '' : [modelLabel(job.provider, job.model), job.aspect_ratio, job.resolution,
      job.brief_writer ? `AI designer: ${job.brief_writer}` : ''].filter(Boolean).join(' · ');
  }

  function addTurn(jobId, job, local) {
    if (!state.turns.length) clear(thread);
    const turn = {
      jobId, job: null, epoch: state.epoch, results: null, progress: null, unwatch: null, textSig: '',
      objectUrls: local ? local.objectUrls : [],
      badge: h('span'),
      time: h('span', { class: 'bubble-time' }),
      progressSlot: h('div'),
      textSlot: h('div', { class: 'result-stack' }),
      resultsSlot: h('div'),
      suggestSlot: h('div'),
      meta: h('p', { class: 'bubble-meta' }),
    };
    turn.clientEl = clientMessage(job, local);
    turn.aiEl = h('div', { class: 'msg msg-ai' },
      h('div', { class: 'avatar', 'aria-hidden': 'true' }, icon('sparkles')),
      h('article', { class: 'bubble-ai', 'aria-label': 'Designer reply' },
        h('div', { class: 'bubble-head' }, h('span', { class: 'bubble-name' }, 'Designer'), turn.badge, turn.time),
        turn.progressSlot, turn.textSlot, turn.resultsSlot, turn.suggestSlot, turn.meta));
    appendAll(thread, [turn.clientEl, turn.aiEl]);
    state.turns.push(turn);
    if (job) {
      renderTurn(turn, job, {});
    } else {
      replace(turn.badge, statusBadge('queued'));
      turn.progress = createProgress(progressOpts);
      turn.progressSlot.appendChild(turn.progress.el);
      turn.progress.update(null, {});
    }
    if (!job || isActive(job.status)) watch(turn);
    return turn;
  }

  function watch(turn) {
    turn.unwatch = watchJob(turn.jobId, (job, meta) => {
      if (!state.turns.includes(turn)) return;
      if (!job) {
        if (meta.gone) removeJob(turn.jobId);
        else if (meta.offline && turn.progress) turn.progress.update(null, meta);
        return;
      }
      const stick = nearBottom();
      renderTurn(turn, job, meta);
      if (meta.done) finishTurn(turn, job);
      updateComposer();
      if (stick) scrollToBottom();
    });
  }

  function finishTurn(turn, job) {
    turn.unwatch = null;
    const ok = job.variants.find((v) => v.status === 'succeeded');
    if (ok && turn.epoch === state.epoch) setParent({ jobId: job.id, variant: ok.index }, false);
    else renderIndicator();
    persist();
    const why = customer ? customerError(job.error) : job.error || '';
    const summary = job.status === 'failed' ? `The design failed. ${why}` : `Designer: ${job.reply || defaultReply(job)}`;
    announce(summary);
    if (root.hidden) toast(job.status === 'failed' ? 'A design in the chat failed.' : 'A new design is ready in the chat.', { kind: job.status === 'failed' ? 'error' : 'success' });
  }

  /* ---------- refine target ---------- */
  function latestSuccess() {
    for (let i = state.turns.length - 1; i >= 0; i -= 1) {
      const job = state.turns[i].job;
      const ok = job && job.variants.find((v) => v.status === 'succeeded');
      if (ok) return { jobId: job.id, variant: ok.index };
    }
    return null;
  }

  function validParent(p) {
    if (!p || typeof p.jobId !== 'string' || !Number.isInteger(p.variant)) return null;
    const turn = findTurn(p.jobId);
    const v = turn && turn.job ? turn.job.variants[p.variant - 1] : null;
    return v && v.status === 'succeeded' ? { jobId: p.jobId, variant: p.variant } : null;
  }

  function setParent(parent, manual) {
    if (manual) state.epoch += 1;
    state.parent = parent;
    for (const t of state.turns) if (t.results) t.results.markParent(parent);
    renderIndicator();
    updateComposer();
    persist();
    if (manual && parent) textarea.focus();
  }

  function renderIndicator() {
    clear(indicator);
    const latest = latestSuccess();
    if (state.parent) {
      const turn = findTurn(state.parent.jobId);
      const job = turn && turn.job;
      const v = job ? job.variants[state.parent.variant - 1] : null;
      indicator.appendChild(h('div', { class: 'refine-indicator', role: 'status' },
        v && v.preview ? h('img', { src: fileUrl(job.id, v.preview), alt: '' }) : icon('wand'),
        h('div', { class: 'refine-text' },
          h('strong', null, `Refining variant ${state.parent.variant}${job && job.brief ? ` of “${job.brief.title}”` : ''}`),
          h('span', { class: 'hint' }, 'Your next message changes this design.')),
        h('button', {
          type: 'button', class: 'btn btn-sm btn-icon btn-ghost',
          'aria-label': 'Stop refining. The next message starts a new design.', title: 'Start a new design instead',
          onClick: () => setParent(null, true),
        }, icon('x'))));
    } else if (latest) {
      indicator.appendChild(h('div', { class: 'refine-indicator is-new', role: 'status' },
        icon('sparkles'),
        h('div', { class: 'refine-text' },
          h('strong', null, 'New design'),
          h('span', { class: 'hint' }, 'Your next message starts a fresh design in this conversation.')),
        h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onClick: () => setParent(latest, true) }, 'Refine latest')));
    }
    indicator.hidden = !indicator.firstChild;
  }

  /* ---------- session ---------- */
  function persist() {
    if (!state.turns.length) {
      storage.remove(sessionKey);
      return;
    }
    storage.set(sessionKey, { sessionId: state.sessionId, jobIds: state.turns.map((t) => t.jobId), parent: state.parent });
  }

  function resetThread() {
    for (const t of state.turns) {
      if (t.unwatch) t.unwatch();
      if (t.progress) t.progress.stop();
      for (const url of t.objectUrls) URL.revokeObjectURL(url);
    }
    state.turns = [];
    clear(thread);
  }

  async function loadTurns(ids, preferred) {
    state.loading = true;
    updateComposer();
    replace(thread, loadingRow('Loading the conversation…'));
    const results = await Promise.all(ids.map((id) => api.job(id).then((job) => ({ job }), (err) => ({ err }))));
    state.loading = false;
    clear(thread);
    const jobs = results.filter((r) => r.job).map((r) => r.job);
    if (!jobs.length) {
      const failure = results.find((r) => r.err && r.err.status !== 404);
      if (failure) {
        // keep the stored session so a reload can restore it once the server is reachable
        const retry = h('button', { type: 'button', class: 'btn' }, icon('refresh'), 'Try again');
        retry.addEventListener('click', () => loadTurns(ids, preferred));
        replace(thread, h('div', { class: 'empty' }, callout('error', `Could not load the conversation: ${failure.err.message}`), retry));
      } else {
        state.sessionId = null;
        persist();
        renderWelcome();
      }
      renderIndicator();
      updateComposer();
      return;
    }
    if (!state.sessionId) {
      const withSession = jobs.find((j) => j.session_id && SESSION_RE.test(j.session_id));
      state.sessionId = withSession ? withSession.session_id : null;
    }
    for (const job of jobs) addTurn(job.id, job, null);
    state.parent = validParent(preferred) || latestSuccess();
    for (const t of state.turns) if (t.results) t.results.markParent(state.parent);
    renderIndicator();
    updateComposer();
    persist();
    scrollToBottom(true);
  }

  async function send() {
    const message = textarea.value.trim();
    if (state.sending || state.loading) return;
    if (!message) {
      textarea.focus();
      return;
    }
    if (busyTurn()) {
      toast('The designer is still working on the last message.', { kind: 'info' });
      return;
    }
    if (message.length > limits.max_message_chars) {
      toast(`The message is too long (max ${limits.max_message_chars} characters).`, { kind: 'error' });
      return;
    }
    const parent = state.parent;
    const attachments = state.attachments.slice();
    state.sending = true;
    updateComposer();
    let res;
    try {
      res = await api.design({
        message,
        options: settings.values(),
        sessionId: state.sessionId,
        parent,
        attachments: attachments.map((a) => a.file),
      });
    } catch (err) {
      state.sending = false;
      updateComposer();
      toast(err.message, { kind: 'error' });
      return;
    }
    state.sending = false;
    if (res.session_id && SESSION_RE.test(res.session_id)) state.sessionId = res.session_id;
    state.attachments = [];
    renderThumbs();
    textarea.value = '';
    autosize();
    addTurn(res.job_id, null, { message, parent, objectUrls: attachments.map((a) => a.url) });
    renderIndicator();
    updateComposer();
    persist();
    scrollToBottom();
    announce('Message sent. The designer is working on it.');
  }

  async function newDesign() {
    if (state.turns.length) {
      const ok = await confirmDialog({
        title: 'Start a new design?',
        message: 'This conversation stays in History, where you can continue it later.',
        confirmLabel: 'Start new design',
      });
      if (!ok) return;
    }
    resetThread();
    state.sessionId = null;
    state.parent = null;
    state.epoch += 1;
    settings.clearLettering();
    persist();
    renderWelcome();
    renderIndicator();
    updateComposer();
    textarea.focus();
  }

  function removeJob(jobId) {
    const turn = findTurn(jobId);
    if (!turn) return;
    if (turn.unwatch) turn.unwatch();
    if (turn.progress) turn.progress.stop();
    turn.clientEl.remove();
    turn.aiEl.remove();
    state.turns = state.turns.filter((t) => t !== turn);
    if (state.parent && state.parent.jobId === jobId) state.parent = latestSuccess();
    for (const t of state.turns) if (t.results) t.results.markParent(state.parent);
    if (!state.turns.length) {
      state.sessionId = null;
      renderWelcome();
    }
    renderIndicator();
    updateComposer();
    persist();
  }

  async function continueFrom(job) {
    const ok = job.variants.find((v) => v.status === 'succeeded');
    const target = { jobId: job.id, variant: ok ? ok.index : 1 };
    if (findTurn(job.id)) {
      setParent(validParent(target) || state.parent, true);
      findTurn(job.id).aiEl.scrollIntoView({ block: 'center' });
      return;
    }
    if (state.sending || state.loading) return;
    resetThread();
    state.parent = null;
    state.sessionId = job.session_id && SESSION_RE.test(job.session_id) ? job.session_id : null;
    let ids = [job.id];
    if (state.sessionId) {
      try {
        const list = await api.listJobs({ sessionId: state.sessionId, kind: 'design', limit: 500 });
        ids = list.map((s) => s.id).reverse();
        if (!ids.includes(job.id)) ids.push(job.id);
      } catch (err) {
        toast(`Only this design was loaded: ${err.message}`, { kind: 'warning' });
      }
    }
    await loadTurns(ids, target);
    state.epoch += 1; // the chosen variant stays the refine target even if older turns finish later
    if (state.parent) toast('Conversation loaded. Your next message refines the selected design.', { kind: 'success' });
    textarea.focus();
  }

  function showPrompt() {
    const tabs = [state.parent
      ? { kind: 'refine', label: 'Image prompt', note: 'Refinement prompt shown without the AI designer’s brief; the real request also sends the current design image.' }
      : { kind: 'design', label: 'Image prompt', note: settings.writerEnabled ? 'Shown without a brief. With the AI designer on, the real prompt also contains the brief it writes.' : null }];
    if (settings.writerEnabled) {
      tabs.push({ kind: 'brief', label: 'AI designer prompt', note: 'What the AI designer (text model) is asked to do with the client’s message.' });
    }
    openPromptPreview({
      tabs,
      buildRequest: (kind) => ({ kind, options: settings.values(), message: textarea.value.trim(), inputRatio: null }),
    });
  }

  /* ---------- start ---------- */
  const saved = storage.get(sessionKey, null);
  if (saved && Array.isArray(saved.jobIds) && saved.jobIds.length) {
    state.sessionId = typeof saved.sessionId === 'string' && SESSION_RE.test(saved.sessionId) ? saved.sessionId : null;
    loadTurns(saved.jobIds.filter((id) => typeof id === 'string' && JOB_ID_RE.test(id)), saved.parent);
  } else {
    renderWelcome();
  }
  updateComposer();

  return { addAttachments, continueFrom, removeJob };
}
