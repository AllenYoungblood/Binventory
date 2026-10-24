// ─────────────────────────────────────────────────────────────────────────────
// BINventory — settings.js
//
// Settings sheet, model status polling, OCR preferences, keep-alive, and
// Simplified View.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { api } from './api.js';
import { openShoppingList } from './items.js';
import { esc, fmtEta, toast } from './ui.js';
import { icon, setIconText, setSwitch, dot } from './icons.js';

export function pollClipStatus() {
  fetch('/api/status').then(r=>r.json()).then(d => {
    const badge   = document.getElementById('clip-badge');
    const qctrls  = document.getElementById('queue-controls');
    const qbadge  = document.getElementById('queue-badge');
    const runBtn  = document.getElementById('runtags-btn');
    const autoBtn = document.getElementById('autotag-btn');

    S._autoTag      = d.auto_tag;
    S._scheduleHour = d.schedule_hour;
    S._scheduleMins = d.schedule_min;

    const busy = d.worker_running || d.queue_size > 0 || d.tagging_now;

    if (busy || d.clip_ready) {
      qctrls.style.display = 'flex';
    }

    if (busy) {
      // ── Working: show "N of M" plus an ETA when we can estimate one ──────
      const p = d.progress || {};
      const done  = p.done  || 0;
      const total = p.total || 0;
      let label = '';
      // Name whichever engine(s) are actually loaded right now.
      if (d.ocr_ready && d.clip_ready)      label += 'Tagging+OCR ';
      else if (d.ocr_ready)                  label += 'OCR ';
      else                                   label += 'Tagging ';
      if (total > 0) label += `${done} of ${total}`;
      else           label += `(${d.queue_size} left)`;
      if (p.eta_sec != null && p.eta_sec > 0) label += ` · ~${fmtEta(p.eta_sec)}`;

      setIconText(badge, 'loader-circle', label);
      badge.className   = 'clip-status ready';

      qbadge.textContent   = total > 0 ? `${Math.round((done/total)*100)}%` : `${d.queue_size} left`;
      qbadge.style.display = 'inline-block';
      runBtn.style.display = 'none';
      setIconText(autoBtn, 'loader-circle', 'Running…');
      autoBtn.style.background = 'var(--success-soft)';
      autoBtn.style.color = 'var(--success)';
    } else if (d.clip_ready) {
      // loaded but idle
      setIconText(badge, 'circle-check', 'ML Ready (idle)');
      badge.className   = 'clip-status ready';
      qbadge.style.display = 'none';
      updateAutoTagBtn();
    } else if (d.clip_loading) {
      setIconText(badge, 'loader-circle', 'Loading ML…');
      badge.className   = 'clip-status loading';
      qbadge.style.display = 'none';
    } else {
      // unloaded / not running
      setIconText(badge, 'moon', 'ML Unloaded');
      badge.className   = 'clip-status loading';
      qbadge.style.display = 'none';
      qctrls.style.display = 'flex';
      updateAutoTagBtn();
    }

    // update settings sheet if open
    updateSettingsSheet(d);

    const interval = busy ? 2000 : (d.clip_ready ? 10000 : 15000);
    setTimeout(pollClipStatus, interval);
  }).catch(() => setTimeout(pollClipStatus, 4000));
}

export function updateAutoTagBtn() {
  const btn    = document.getElementById('autotag-btn');
  const runBtn = document.getElementById('runtags-btn');
  const sbtn   = document.getElementById('settings-autotag-btn');
  // Simplified mode has its own copy of this toggle.
  const simple = document.getElementById('simple-autotag-btn');
  if (simple) {
    setSwitch(simple, S._autoTag, 'Auto-tag');
  }
  if (S._autoTag) {
    [btn, sbtn].forEach(b => { if (b) { b.innerHTML = `${dot('ok')} Auto-tag on`; b.style.background = 'var(--success-soft)'; b.style.color = 'var(--success)'; } });
    if (runBtn) runBtn.style.display = 'none';
  } else {
    [btn, sbtn].forEach(b => { if (b) { b.innerHTML = `${icon('pause')} Auto-tag off`; b.style.background = 'var(--warn-soft)'; b.style.color = 'var(--warn)'; } });
    if (runBtn) runBtn.style.display = 'inline-block';
  }
}

// Format a seconds count into a compact human string for the banner.

// ── OCR: settings sheet ─────────────────────────────────────────────────────
export async function loadOcrSettings() {
  try {
    S._ocrSettings = await api('/api/settings/ocr');
    S._simplified  = !!S._ocrSettings.simplified_view;
    renderOcrSettings();
    applySimplified();
  } catch(e) {}
  loadNaming();
}

export function styleToggle(id, on) {
  const b = document.getElementById(id);
  if (!b) return;
  setSwitch(b, on);
}

export async function saveThresholds() {
  const t = parseFloat(document.getElementById('set-text-threshold').value);
  const i = parseFloat(document.getElementById('set-image-threshold').value);
  if ([t, i].some(v => isNaN(v) || v < 0.05 || v > 0.95)) { toast('Use values between 0.05 and 0.95', true); return; }
  try {
    const r = await api('/api/settings/ocr', {method:'POST', body: JSON.stringify({text_threshold: t, image_threshold: i})});
    S._ocrSettings = {...S._ocrSettings, text_threshold: r.text_threshold, image_threshold: r.image_threshold};
    toast(`Search sensitivity saved — text ${r.text_threshold}, photo ${r.image_threshold}`);
  } catch(e) { toast(e.message, true); }
}

// ── Name priority ──────────────────────────────────────────────────────────
// The server stores the order lowest-first; the list shows it highest-first,
// which is how people read a priority list.
export async function loadNaming() {
  try { renderNaming(await api('/api/settings/naming')); } catch(e) {}
}

function renderNaming(d) {
  S._naming = d.order.map(o => o.key);
  const box = document.getElementById('naming-order');
  if (!box) return;
  const rows = [...d.order].reverse();
  box.innerHTML = rows.map((o, i) => `
    <div class="time-row" style="gap:6px;margin-bottom:4px;">
      <span style="min-width:18px;color:var(--muted);font-size:0.8rem;">${i + 1}.</span>
      <span style="flex:1;font-size:0.86rem;">${esc(o.label)}</span>
      <button class="btn btn-secondary btn-sm" ${i === 0 ? 'disabled' : ''} onclick="moveNaming('${o.key}', 1)" title="Higher priority" aria-label="Higher priority">${icon('arrow-up')}</button>
      <button class="btn btn-secondary btn-sm" ${i === rows.length - 1 ? 'disabled' : ''} onclick="moveNaming('${o.key}', -1)" title="Lower priority" aria-label="Lower priority">${icon('arrow-down')}</button>
    </div>`).join('');
  const note = document.getElementById('naming-default-note');
  if (note) note.textContent = d.is_default ? 'Using the default order' : 'Custom order';
}

export async function moveNaming(key, dir) {
  const order = [...S._naming];               // lowest first
  const i = order.indexOf(key), j = i + dir;  // +1 = up the list = higher priority
  if (i < 0 || j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  try {
    renderNaming(await api('/api/settings/naming', {method:'PUT', body: JSON.stringify({order})}));
  } catch(e) { toast(e.message, true); }
}

export async function resetNaming() {
  try {
    renderNaming(await api('/api/settings/naming', {method:'PUT', body: JSON.stringify({reset: true})}));
    toast('Name priority reset to default');
  } catch(e) { toast(e.message, true); }
}

export function renderOcrSettings() {
  const tt = document.getElementById('set-text-threshold');
  const it = document.getElementById('set-image-threshold');
  if (tt && S._ocrSettings.text_threshold != null)  tt.value = S._ocrSettings.text_threshold;
  if (it && S._ocrSettings.image_threshold != null) it.value = S._ocrSettings.image_threshold;
  styleToggle('set-ocr-default', S._ocrSettings.ocr_default_on);
  styleToggle('set-ocr-tags',    S._ocrSettings.ocr_as_tags);
  styleToggle('set-ocr-trunc',   S._ocrSettings.ocr_truncate);
  const lenEl = document.getElementById('set-ocr-trunc-len');
  if (lenEl) lenEl.value = S._ocrSettings.ocr_truncate_len;
  // The length field is only meaningful when truncation is enabled.
  const row = document.getElementById('ocr-trunc-len-row');
  if (row) row.style.opacity = S._ocrSettings.ocr_truncate ? '1' : '0.4';
}

export function toggleOcrSetting(key) {
  S._ocrSettings[key] = !S._ocrSettings[key];
  renderOcrSettings();
}

export async function saveOcrSettings() {
  const len = parseInt(document.getElementById('set-ocr-trunc-len').value) || 255;
  try {
    S._ocrSettings = await api('/api/settings/ocr', {method:'POST', body: JSON.stringify({
      ocr_default_on:   S._ocrSettings.ocr_default_on,
      ocr_as_tags:      S._ocrSettings.ocr_as_tags,
      ocr_truncate:     S._ocrSettings.ocr_truncate,
      ocr_truncate_len: Math.max(1, Math.min(255, len)),
    })});
    renderOcrSettings();
    toast('OCR settings saved');
  } catch(e) { toast(e.message, true); }
}

export async function rerunOcr(scope) {
  const msg = scope === 'all'
    ? 'Re-run OCR on EVERY item? This can take a while.'
    : 'Re-run OCR on items that already have text?';
  if (!confirm(msg)) return;
  try {
    const r = await api('/api/settings/rerun-ocr', {method:'POST', body: JSON.stringify({scope})});
    toast(`${r.queued} item${r.queued!==1?'s':''} queued for OCR`);
    pollClipStatus();
  } catch(e) { toast(e.message, true); }
}

// ── SIMPLIFIED VIEW ─────────────────────────────────────────────────────────
// One flag that hides advanced controls across the whole app. Applied on boot
// and whenever toggled, so the UI never shows a mix of both modes.

export function toggleSimplified() {
  S._simplified = !S._simplified;
  applySimplified();
  api('/api/settings/ocr', {method:'POST', body: JSON.stringify({simplified_view: S._simplified})})
    .then(() => toast(S._simplified ? 'Simplified view on' : 'Full view restored'))
    .catch(e => toast(e.message, true));
}

export function applySimplified() {
  const show = el => { if (el) el.style.display = ''; };
  const hide = el => { if (el) el.style.display = 'none'; };
  const $ = id => document.getElementById(id);

  // Settings sheet: swap which block of controls is visible.
  const btn = $('set-simplified');
  if (btn) {
    setSwitch(btn, S._simplified, 'Simplified view');
  }
  if ($('simple-settings'))   $('simple-settings').style.display   = S._simplified ? 'block' : 'none';
  if ($('advanced-settings')) $('advanced-settings').style.display = S._simplified ? 'none'  : 'block';

  // Shopping list (cart) is an advanced workflow — hidden when simplified.
  const cart = document.querySelector('button[onclick="openShoppingList()"]');
  if (cart) cart.style.display = S._simplified ? 'none' : '';

  // Add Item is hidden in favour of Bulk Add, which is the low-friction path.
  if ($('bin-add-item-btn')) $('bin-add-item-btn').style.display = S._simplified ? 'none' : '';
  if ($('bin-bulk-add-btn')) $('bin-bulk-add-btn').style.display = '';

  // Item detail: hide tag chips and the manual model-run buttons.
  // Only ever HIDE the per-item rows here. Whether they show in full mode
  // depends on the item (has OCR text? is it active?), which openItemDetail
  // decides — forcing them visible re-showed the previous item's OCR text.
  if ($('detail-tags')) $('detail-tags').style.display = S._simplified ? 'none' : '';
  if (S._simplified) {
    ['detail-retag-row', 'detail-ocr-text', 'detail-label-source'].forEach(id => hide($(id)));
  }

  // The sparkles (smart search) button is redundant when every search is semantic anyway.
  if ($('smart-search-btn')) $('smart-search-btn').style.display = S._simplified ? 'none' : '';

  // Keep the simplified auto-tag button label in sync.
  updateAutoTagBtn();
}

// Simplified mode collapses "Tag All" and "Re-run OCR" into one action.

// Simplified mode collapses "Tag All" and "Re-run OCR" into one action.
export async function runAllPending() {
  const st = document.getElementById('simple-status');
  if (st) st.textContent = 'Queuing…';
  try {
    const r = await api('/api/settings/run-all', {method:'POST', body:'{}'});
    if (st) st.textContent = r.queued
      ? `${r.queued} item${r.queued!==1?'s':''} queued for processing`
      : 'Everything is already processed';
    pollClipStatus();
  } catch(e) { if (st) st.textContent = 'Error: ' + e.message; }
}

// ── Keep-alive setting ──────────────────────────────────────────────────────

// ── Keep-alive setting ──────────────────────────────────────────────────────
export async function saveKeepalive() {
  const v = parseInt(document.getElementById('set-keepalive').value);
  if (isNaN(v) || v < 0 || v > 3600) { toast('Enter 0–3600 seconds', true); return; }
  try {
    await api('/api/settings/ocr', {method:'POST', body: JSON.stringify({model_keepalive_sec: v})});
    toast(v === 0 ? 'Models will unload immediately' : `Models stay warm for ${v}s`);
  } catch(e) { toast(e.message, true); }
}

// ── Dictionaries ────────────────────────────────────────────────────────────

export function updateSettingsSheet(d) {
  // queue status line
  const qs = document.getElementById('settings-queue-status');
  if (qs) {
    if (d.worker_running || d.tagging_now) {
      setIconText(qs, 'loader-circle', `Tagging in progress — ${d.queue_size} item${d.queue_size!==1?'s':''} remaining`);
      qs.style.color = 'var(--success)';
    } else if (d.queue_size > 0) {
      qs.textContent = `${d.queue_size} item${d.queue_size!==1?'s':''} queued, worker starting…`;
      qs.style.color = 'var(--warn)';
    } else {
      qs.textContent = 'No items pending.';
      qs.style.color = 'var(--muted)';
    }
  }
  // schedule next-run
  const nrl = document.getElementById('next-run-label');
  const clb = document.getElementById('clear-sched-btn');
  if (nrl) {
    if (d.next_run) {
      const t = new Date(d.next_run);
      nrl.textContent = `Next run: ${t.toLocaleString()}`;
      nrl.style.display = 'block';
      if (clb) clb.style.display = 'inline-block';
    } else {
      nrl.style.display = 'none';
      if (clb) clb.style.display = 'none';
    }
  }
  // fill schedule inputs from server state
  if (d.schedule_hour !== null && d.schedule_hour !== undefined) {
    const hi = document.getElementById('sched-hour');
    const mi = document.getElementById('sched-min');
    if (hi) hi.value = d.schedule_hour;
    if (mi) mi.value = d.schedule_min;
  }
  // clip memory status
  const cms  = document.getElementById('clip-mem-status');
  const ulb  = document.getElementById('unload-clip-btn');
  if (cms) {
    if (d.worker_running || d.tagging_now) {
      cms.innerHTML = `${dot('busy')} Loaded — actively tagging`;
      if (ulb) ulb.style.display = 'none';
    } else if (d.clip_ready) {
      cms.innerHTML = `${dot('warn')} Loaded and idle — using RAM`;
      if (ulb) ulb.style.display = 'inline-block';
    } else {
      cms.innerHTML = `${dot()} Not loaded — no RAM used`;
      if (ulb) ulb.style.display = 'none';
    }
  }
}

export async function toggleAutoTag() {
  S._autoTag = !S._autoTag;
  updateAutoTagBtn();
  await fetch('/api/settings/autotag', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({enabled: S._autoTag})
  });
  pollClipStatus();
}

export async function runAllTags() {
  try {
    const r = await api('/api/settings/runtags', {method:'POST', body:'{}'});
    toast(r.queued > 0
      ? `${r.queued} item${r.queued!==1?'s':''} queued — tagging starting…`
      : 'All items already tagged');
    pollClipStatus();
  } catch(e) { toast('Error: ' + e.message, true); }
}

export async function saveSchedule() {
  const hour = parseInt(document.getElementById('sched-hour').value);
  const min  = parseInt(document.getElementById('sched-min').value);
  if (isNaN(hour) || hour < 0 || hour > 23) { toast('Hour must be 0–23', true); return; }
  if (isNaN(min)  || min  < 0 || min  > 59) { toast('Minute must be 0–59', true); return; }
  try {
    await api('/api/settings/schedule', {method:'POST', body: JSON.stringify({hour, minute: min})});
    toast(`Schedule set — runs daily at ${String(hour).padStart(2,'0')}:${String(min).padStart(2,'0')}`);
    pollClipStatus();
  } catch(e) { toast('Error: ' + e.message, true); }
}

export async function clearSchedule() {
  try {
    await api('/api/settings/schedule', {method:'POST', body: JSON.stringify({hour: null})});
    toast('Schedule cleared');
    pollClipStatus();
  } catch(e) { toast('Error: ' + e.message, true); }
}

export async function unloadClip() {
  try {
    await api('/api/settings/unloadclip', {method:'POST', body:'{}'});
    toast('CLIP unloaded — RAM freed');
    pollClipStatus();
  } catch(e) { toast('Error: ' + e.message, true); }
}

// ── VIEWS ─────────────────────────────────────────────────────────────────────
