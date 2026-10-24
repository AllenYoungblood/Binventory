// ─────────────────────────────────────────────────────────────────────────────
// BINventory — plugins.js
//
// The Plugins tab: one panel per plugin holding everything about it — on/off,
// what it may do, its settings, action buttons, live status, and its own page
// if it serves one (shown in a frame, so a plugin's page never shares the
// app's page). Also the plugin-declared extras elsewhere: rows on the item
// sheet, and "link" buttons on the item sheet and bin view that open a
// plugin's page (e.g. Print label).
//
// Everything is rendered from what plugins declare in plugin.toml; plugins
// ship no JavaScript into this page. Handlers are bound with
// addEventListener, so nothing needs to go on the main.js window bridge.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { api } from './api.js';
import { esc, showView, toast } from './ui.js';
import { setIconText, setSwitch } from './icons.js';

const STATUS = {
  loaded:       ['On',                  'var(--success)', 'var(--success-soft)'],
  disabled:     ['Off',                 'var(--muted)', 'var(--border)'],
  missing_deps: ['Missing dependency',  'var(--warn)', 'var(--warn-soft)'],
  incompatible: ['Incompatible',        'var(--danger)', 'var(--danger-soft)'],
  invalid:      ['Broken manifest',     'var(--danger)', 'var(--danger-soft)'],
  error:        ['Failed to load',      'var(--danger)', 'var(--danger-soft)'],
};

const PERMISSION_TEXT = {
  'items:read': 'read items', 'items:write': 'tag and rename items',
  'bins:read': 'read bins', 'bins:write': 'change bins',
  'settings:read': 'read settings', 'settings:write': 'change settings',
  'network:external': 'contact services on the internet',
  'network:listen': 'open a network port for other programs',
  'web:routes': 'add its own page to the app',
  'search:read': 'search the inventory',
  'lighting:control': 'control lights', 'vocab:write': 'change the vocabulary',
};

// ── Data ─────────────────────────────────────────────────────────────────────
export async function fetchPlugins() {
  try { S._plugins = await api('/api/plugins'); } catch (e) { S._plugins = S._plugins || []; throw e; }
  return S._plugins;
}

// ── The Plugins tab ──────────────────────────────────────────────────────────
export async function loadPlugins() {
  const tabs = document.getElementById('plugin-tabs');
  const panel = document.getElementById('plugin-panel');
  if (!tabs || !panel) return;
  try { await fetchPlugins(); }
  catch (e) { panel.textContent = 'Could not load plugins: ' + e.message; return; }
  if (!S._plugins.length) {
    tabs.innerHTML = '';
    panel.innerHTML = '<div class="muted text-sm">No plugins installed. Add one to the <code>plugins/</code> folder and restart.</div>';
    return;
  }
  if (!S._plugins.some(p => p.id === S._pluginSelected)) S._pluginSelected = S._plugins[0].id;
  tabs.innerHTML = '';
  S._plugins.forEach(p => {
    const chip = document.createElement('div');
    const [, fg] = STATUS[p.status] || STATUS.error;
    chip.className = 'chip' + (p.id === S._pluginSelected ? ' active' : '');
    chip.innerHTML = `<span class="dot" style="background:${fg};"></span> ${esc(p.name)}`;
    chip.addEventListener('click', () => { S._pluginSelected = p.id; S._pluginPath = null; loadPlugins(); });
    tabs.appendChild(chip);
  });
  const p = S._plugins.find(x => x.id === S._pluginSelected);
  panel.innerHTML = '';
  panel.appendChild(pluginCard(p));
  if (p.page) panel.appendChild(pluginFrame(p, S._pluginPath || p.page.path));
}

/** Open a plugin's page in the Plugins tab, e.g. from a Label button. */
export async function openPluginPage(pluginId, path) {
  S._pluginSelected = pluginId;
  S._pluginPath = path;
  showView('plugins');            // which calls loadPlugins()
}

function pluginFrame(p, path) {
  const wrap = document.createElement('div');
  wrap.style.cssText = 'margin-top:12px;';
  wrap.innerHTML = `<div class="wled-section-title" style="margin-bottom:6px;">${esc(p.page.title)}</div>`;
  const frame = document.createElement('iframe');
  frame.src = `/p/${encodeURIComponent(p.id)}${path}`;
  frame.title = p.page.title;
  frame.style.cssText = 'width:100%;min-height:70vh;border:1px solid var(--border);border-radius:10px;background:var(--bg);';
  // Same origin, so the frame can grow to its content instead of scrolling twice.
  frame.addEventListener('load', () => {
    try {
      const doc = frame.contentDocument;
      const fit = () => { frame.style.height = (doc.documentElement.scrollHeight + 4) + 'px'; };
      fit();
      new ResizeObserver(fit).observe(doc.body);
    } catch (e) { /* cross-origin or gone: keep the fixed height */ }
  });
  wrap.appendChild(frame);
  return wrap;
}

function pluginCard(p) {
  const card = document.createElement('div');
  card.className = 'schedule-panel';
  card.dataset.plugin = p.id;
  const [label, fg, bg] = STATUS[p.status] || STATUS.error;
  const canToggle = ['loaded', 'disabled', 'error'].includes(p.status);
  const perms = p.permissions.map(x => PERMISSION_TEXT[x] || x).join(' · ');

  card.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
      <div style="min-width:0;">
        <div style="font-size:0.95rem;font-weight:700;">${esc(p.name)}
          <span style="font-weight:400;color:var(--muted);font-size:0.74rem;">${esc(p.version)}</span></div>
      </div>
      <button data-role="toggle" style="flex-shrink:0;font-size:0.78rem;font-weight:700;padding:6px 12px;border-radius:20px;border:none;
        cursor:${canToggle ? 'pointer' : 'default'};background:${bg};color:${fg};">${label}</button>
    </div>
    ${p.description ? `<div style="font-size:0.78rem;color:var(--muted);margin-top:5px;line-height:1.4;">${esc(p.description)}</div>` : ''}
    ${perms ? `<div style="font-size:0.72rem;color:var(--muted);margin-top:6px;">Can: ${esc(perms)}</div>` : ''}
    ${p.error ? `<div style="font-size:0.74rem;color:${fg};margin-top:6px;word-break:break-word;">${esc(p.error)}</div>` : ''}
    <div data-role="body"></div>`;

  if (canToggle) {
    card.querySelector('[data-role=toggle]').addEventListener('click', () => togglePlugin(p));
  }
  if (p.status === 'loaded') renderPluginBody(card.querySelector('[data-role=body]'), p);
  return card;
}

function renderPluginBody(body, p) {
  (p.status_rows || []).forEach(r => body.appendChild(statusRow(r)));
  if (p.settings.length) {
    const h = document.createElement('div');
    h.className = 'wled-section-title';
    h.style.cssText = 'margin:12px 0 0;';
    h.textContent = 'Settings';
    body.appendChild(h);
  }
  p.settings.forEach(f => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:10px;';
    row.innerHTML = `<div style="min-width:0;"><div style="font-size:0.82rem;">${esc(f.label)}</div>
      ${f.help ? `<div style="font-size:0.72rem;color:var(--muted);margin-top:2px;line-height:1.35;">${esc(f.help)}</div>` : ''}</div>`;
    row.appendChild(settingControl(p, f));
    body.appendChild(row);
  });
  if (p.actions.length) {
    const bar = document.createElement('div');
    bar.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;';
    p.actions.forEach(a => {
      const b = document.createElement('button');
      b.className = 'btn btn-secondary btn-sm';
      b.textContent = a.label;
      b.addEventListener('click', () => runPluginAction(p, a, b));
      bar.appendChild(b);
    });
    body.appendChild(bar);
  }
}

// Live rows a plugin reports (api.set_status). `code` values get a Copy button.
function statusRow(r) {
  const row = document.createElement('div');
  row.style.cssText = 'margin-top:8px;font-size:0.78rem;';
  row.innerHTML = `<div style="color:var(--muted);">${esc(r.label)}</div>`;
  r.values.forEach(v => {
    const line = document.createElement('div');
    line.style.cssText = 'display:flex;align-items:center;gap:6px;margin-top:2px;word-break:break-word;';
    if (r.type === 'code') {
      line.innerHTML = `<code style="flex:1;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:4px 6px;font-size:0.74rem;">${esc(v)}</code>`;
      line.appendChild(copyButton(v));
    } else if (r.type === 'link') {
      line.innerHTML = `<a href="${esc(v)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent2);">${esc(v)}</a>`;
    } else {
      line.textContent = v;
    }
    row.appendChild(line);
  });
  return row;
}

function copyButton(value) {
  const b = document.createElement('button');
  b.className = 'btn btn-secondary btn-sm';
  b.textContent = 'Copy';
  b.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(value); toast('Copied'); }
    catch (e) { window.prompt('Copy this:', value); }   // clipboard API needs a secure context
  });
  return b;
}

// A value an action wants shown exactly once (e.g. a new access token).
function showReveal(pluginId, reveal) {
  const card = document.querySelector(`[data-plugin="${pluginId}"]`);
  if (!card) { window.prompt(reveal.label || 'Copy this:', reveal.value); return; }
  const box = document.createElement('div');
  box.style.cssText = 'margin-top:10px;padding:8px 10px;border:1px solid var(--warn);border-radius:8px;background:var(--warn-soft);font-size:0.78rem;';
  box.innerHTML = `<div style="font-weight:700;color:var(--warn);">${esc(reveal.label || 'Copy this now')}</div>
    ${reveal.help ? `<div style="color:var(--muted);margin-top:2px;">${esc(reveal.help)}</div>` : ''}`;
  const line = document.createElement('div');
  line.style.cssText = 'display:flex;align-items:center;gap:6px;margin-top:6px;';
  line.innerHTML = `<code style="flex:1;word-break:break-all;font-size:0.74rem;">${esc(reveal.value)}</code>`;
  line.appendChild(copyButton(reveal.value));
  box.appendChild(line);
  card.appendChild(box);
}

function settingControl(p, f) {
  let el;
  if (f.type === 'toggle') {
    el = document.createElement('button');
    const paint = on => setSwitch(el, on, f.label);
    paint(!!f.value);
    el.addEventListener('click', async () => {
      const saved = await savePluginSetting(p, f.key, !f.value);
      if (saved) { f.value = !f.value; paint(f.value); }
    });
    return el;
  }
  if (f.type === 'secret') return secretControl(p, f);
  if (f.type === 'select') {
    el = document.createElement('select');
    // An option gated on a toggle (option_requires) shows only while it's on.
    const on = key => !!(p.settings.find(g => g.key === key) || {}).value;
    f.options.filter(o => !(f.option_requires || {})[o] || on(f.option_requires[o])).forEach(o => {
      const opt = document.createElement('option');
      opt.value = o;
      opt.textContent = (f.option_labels || {})[o] || o;
      if (o === f.value) opt.selected = true;
      el.appendChild(opt);
    });
  } else {
    el = document.createElement('input');
    el.type = f.type === 'number' ? 'number' : 'text';
    el.value = f.value ?? '';
  }
  el.style.maxWidth = '45%';
  el.addEventListener('change', () => savePluginSetting(p, f.key, el.value));
  return el;
}

/** An API key: never shown, only "saved". Type to replace; Clear removes it. */
function secretControl(p, f) {
  const box = document.createElement('div');
  box.style.cssText = 'display:flex;gap:6px;align-items:center;max-width:55%;';
  const el = document.createElement('input');
  el.type = 'password';
  el.autocomplete = 'off';
  el.placeholder = f.is_set ? 'Saved — type to replace' : 'Not set';
  el.style.minWidth = '0';
  el.addEventListener('change', async () => {
    if (el.value && await savePluginSetting(p, f.key, el.value)) { el.value = ''; el.placeholder = 'Saved — type to replace'; }
  });
  box.appendChild(el);
  if (f.is_set) {
    const clear = document.createElement('button');
    clear.className = 'btn btn-secondary btn-sm';
    clear.textContent = 'Clear';
    clear.addEventListener('click', () => savePluginSetting(p, f.key, ''));
    box.appendChild(clear);
  }
  return box;
}

async function togglePlugin(p) {
  const enable = p.status !== 'loaded';
  try {
    const r = await api(`/api/plugins/${p.id}`, {method: 'POST', body: JSON.stringify({enabled: enable})});
    toast(r.status === 'loaded' ? `${p.name} enabled` :
          r.status === 'disabled' ? `${p.name} disabled` : `${p.name}: ${r.error}`, r.status === 'error');
  } catch (e) { toast(e.message, true); }
  loadPlugins();
}

async function savePluginSetting(p, key, value) {
  try {
    await api(`/api/plugins/${p.id}/settings`, {method: 'POST', body: JSON.stringify({[key]: value})});
    toast('Saved');
    // Status may change (e.g. a server restart), and a plugin page may read
    // its settings — redraw the panel so both are current.
    setTimeout(loadPlugins, 600);
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

async function runPluginAction(p, a, btn) {
  if (a.confirm && !confirm(a.confirm)) return;
  btn.disabled = true;
  try {
    const r = await api(`/api/plugins/${p.id}/actions/${a.name}`, {method: 'POST', body: '{}'});
    toast(r.message || (r.queued != null ? `Queued ${r.queued} item${r.queued === 1 ? '' : 's'}` : 'Done'));
    if (r.reveal) { await loadPlugins(); showReveal(p.id, r.reveal); return; }
    if (p.status_rows && p.status_rows.length) loadPlugins();
  } catch (e) { toast(e.message, true); }
  btn.disabled = false;
}

// ── Links: plugin buttons on the item sheet and the bin view ─────────────────
/** Buttons for every enabled plugin's [[links]] at `where` ("item" | "bin"). */
export async function pluginLinkButtons(where, id) {
  if (!S._plugins || !S._plugins.length) { try { await fetchPlugins(); } catch (e) { return []; } }
  const out = [];
  S._plugins.filter(p => p.status === 'loaded').forEach(p => (p.links || [])
    .filter(l => l.where === where)
    .forEach(l => {
      const b = document.createElement('button');
      b.className = 'btn btn-secondary btn-sm';
      if (l.icon) setIconText(b, l.icon, l.label); else b.textContent = l.label;
      b.addEventListener('click', () => {
        document.querySelectorAll('.overlay.active').forEach(o => o.classList.remove('active'));
        openPluginPage(p.id, l.path.replace('{id}', encodeURIComponent(id)));
      });
      out.push(b);
    }));
  return out;
}

export async function renderBinPluginLinks(binId) {
  const box = document.getElementById('bin-plugin-links');
  if (!box) return;
  const buttons = await pluginLinkButtons('bin', binId);
  if (S.currentBinId !== binId) return;
  box.innerHTML = '';
  buttons.forEach(b => box.appendChild(b));
  box.style.display = buttons.length ? 'flex' : 'none';
}

// ── Item detail ──────────────────────────────────────────────────────────────
export async function renderPluginSections(itemId) {
  const box = document.getElementById('detail-plugin-sections');
  if (!box) return;
  let sections = [];
  try { sections = await api(`/api/items/${itemId}/plugin-sections`); } catch (e) { /* optional */ }
  const links = await pluginLinkButtons('item', itemId);
  if (S.currentItemId !== itemId) return;          // user opened another item
  box.innerHTML = sections.map(s => `
    <div style="font-size:0.76rem;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-bottom:10px;">
      <div style="font-weight:700;margin-bottom:4px;">${esc(s.title)}</div>
      ${s.rows.map(r => `<div style="word-break:break-word;margin-top:2px;">
        <span style="color:var(--muted);">${esc(r.label)}:</span>
        ${r.values.map(v => r.type === 'link'
            ? `<a href="${esc(v)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent2);">${esc(v)}</a>`
            : esc(v)).join(', ')}
      </div>`).join('')}
    </div>`).join('');
  if (links.length) {
    const bar = document.createElement('div');
    bar.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;';
    links.forEach(b => bar.appendChild(b));
    box.appendChild(bar);
  }
}
