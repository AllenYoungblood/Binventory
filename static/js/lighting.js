// ─────────────────────────────────────────────────────────────────────────────
// BINventory — lighting.js
//
// WLED integration: global settings, per-bin zone configuration, effects,
// live preview, and the Find actions.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { api } from './api.js';
import { loadBins, openBin, renderBinsView } from './bins.js';
import { closeSheet, esc, openSheet, toast } from './ui.js';
import { icon, setIconText, setSwitch, dot } from './icons.js';

export async function loadWledGlobal() {
  try {
    const d = await api('/api/wled/settings');
    S._wledGlobalEnabled = d.enabled;
    S._wledDefaultIp     = d.default_ip;
    updateWledGlobalUI();
  } catch(e) {}
}

export function updateWledGlobalUI() {
  const btn = document.getElementById('wled-global-toggle');
  if (!btn) return;
  setSwitch(btn, S._wledGlobalEnabled, 'WLED lighting');
  if (document.getElementById('wled-global-ip')) {
    document.getElementById('wled-global-ip').value = S._wledDefaultIp;
  }
}

export function toggleWledGlobal() {
  S._wledGlobalEnabled = !S._wledGlobalEnabled;
  updateWledGlobalUI();
}

export async function saveWledGlobal() {
  const ip = document.getElementById('wled-global-ip').value.trim();
  try {
    await api('/api/wled/settings', {method:'POST', body: JSON.stringify({
      enabled: S._wledGlobalEnabled,
      default_ip: ip,
    })});
    S._wledDefaultIp = ip;
    closeSheet('overlay-wled-global');
    toast('WLED settings saved');
    // refresh bin view to show/hide find buttons
    await loadBins();
    renderBinsView();
  } catch(e) { toast(e.message, true); }
}

export async function pingWled(ip, resultId) {
  const el = document.getElementById(resultId);
  if (!ip) { el.textContent = 'Enter an IP first'; el.style.color = 'var(--danger)'; return; }
  el.textContent = 'Pinging…'; el.style.color = 'var(--muted)';
  try {
    const d = await api('/api/wled/ping', {method:'POST', body: JSON.stringify({ip})});
    setIconText(el, 'circle-check', `${d.name}  •  ${d.led_count} LEDs  •  v${d.version}`);
    el.style.color = 'var(--success)';
    S._wledTotalLeds = d.led_count || 30;
    renderLedStrip();
  } catch(e) {
    setIconText(el, 'circle-x', e.message || 'Cannot reach device');
    el.style.color = 'var(--danger)';
  }
}

// ── WLED BIN SETUP ────────────────────────────────────────────────────────────

// ── WLED BIN SETUP ────────────────────────────────────────────────────────────
export async function openWledSheet(binId) {
  S._wledCurrentBinId = binId;
  const bin = S.allBins.find(b => b.id === binId);
  if (!bin) return;
  document.getElementById('wled-bin-name').textContent = bin.name;

  // Everything comes from the bin's lighting config (bin_lighting). The bin
  // row no longer carries any lighting fields (phase 4b).
  let cfg = {};
  try { cfg = await api(`/api/bins/${binId}/lighting`); } catch (e) { /* defaults below */ }

  document.getElementById('wled-bin-ip').value        = cfg.ip || S._wledDefaultIp || '';
  document.getElementById('wled-bin-enabled').checked = !!cfg.enabled;
  document.getElementById('wled-bin-duration').value  = cfg.duration ?? 10;

  S._wS = {
    start:      cfg.seg_start  ?? 0,
    len:        cfg.seg_len    || 5,
    color:      cfg.color      || '#ffffff',
    brightness: cfg.brightness ?? 128,
    effect:     cfg.effect === 'pulse' ? 'pulse' : 'solid',
    deviceId:   cfg.device_id || '',
    layout:     cfg.layout || 'strip',
    matrixW:    cfg.matrix_w || 0,
    matrixH:    cfg.matrix_h || 0,
    zoneX:      cfg.zone_x ?? 0,
    zoneY:      cfg.zone_y ?? 0,
    zoneW:      cfg.zone_w || 2,
    zoneH:      cfg.zone_h || 2,
  };

  document.getElementById('wled-bin-color').value      = S._wS.color;
  document.getElementById('wled-bin-brightness').value = S._wS.brightness;
  updateBrightnessLabel();
  setEffect(S._wS.effect, false);
  renderLedStrip();
  openSheet('overlay-wled-bin');

  await populateDevicePicker(S._wS.deviceId, cfg.ip);
  // A bin with no controller and no legacy IP gets the default controller
  // preselected — there is usually only one.
  if (!S._wS.deviceId && !cfg.ip) {
    const def = (S._devices || []).find(d => d.is_default) || (S._devices || [])[0];
    if (def) {
      document.getElementById('wled-bin-device').value = def.id;
      S._wS.deviceId = def.id;
      onBinDeviceChange();
      return;
    }
  }
  // A bin saved before its controller was probed carries layout "strip" even
  // when the controller is a matrix — adopt the device's geometry instead.
  const dev = (S._devices || []).find(d => d.id === S._wS.deviceId);
  if (dev && dev.layout === 'matrix' && S._wS.layout !== 'matrix') {
    onBinDeviceChange();
    return;
  }
  applyLayoutUI(S._wS.layout);
  if (S._wS.layout === 'matrix') renderMatrixGrid();
  sendPreview();
}

export function renderLedStrip() {
  const strip = document.getElementById('wled-led-strip');
  if (!strip) return;
  const total   = Math.min(S._wledTotalLeds, 60);   // show max 60 in visualizer
  const start   = S._wS.start;
  const end     = start + S._wS.len;
  const hexCol  = S._wS.color;
  const alpha   = (S._wS.brightness / 255).toFixed(2);

  strip.innerHTML = '';
  for (let i = 0; i < total; i++) {
    const pip = document.createElement('div');
    pip.className = 'led-pip';
    if (i >= start && i < end) {
      pip.classList.add('active');
      pip.style.background = hexCol;
      pip.style.opacity    = alpha;
      pip.style.boxShadow  = `0 0 6px 2px ${hexCol}88`;
    }
    strip.appendChild(pip);
  }
  document.getElementById('wled-strip-label').textContent =
    `LEDs ${S._wS.start}–${S._wS.start + S._wS.len - 1} (${S._wS.len} LEDs)`;
}

export function jogStrip(delta) {
  S._wS.start = Math.max(0, S._wS.start + delta);
  renderLedStrip();
  sendPreview();
}

export function resizeStrip(delta) {
  S._wS.len = Math.max(1, S._wS.len + delta);
  renderLedStrip();
  sendPreview();
}

export function setEffect(eff, preview=true) {
  S._wS.effect = eff;
  document.getElementById('effect-solid').classList.toggle('active', eff === 'solid');
  document.getElementById('effect-pulse').classList.toggle('active', eff === 'pulse');
  if (preview) sendPreview();
}

export function updateBrightnessLabel() {
  const v = document.getElementById('wled-bin-brightness').value;
  document.getElementById('wled-brightness-label').textContent = v;
  S._wS.brightness = parseInt(v);
  renderLedStrip();
}

// Debounced live preview sender

export function sendPreview() {
  S._wS.color      = document.getElementById('wled-bin-color')?.value      || S._wS.color;
  S._wS.brightness = parseInt(document.getElementById('wled-bin-brightness')?.value || S._wS.brightness);
  renderLedStrip();
  clearTimeout(S._previewTimer);
  S._previewTimer = setTimeout(async () => {
    const target = wledTarget();
    if (!target.device_id && !target.ip) return;
    try {
      // Unsaved sheet values go straight to the controller; the server builds
      // a matrix or strip payload with the same code Find uses.
      await api('/api/wled/preview', {method:'POST', body: JSON.stringify({
        ...target,
        layout:     S._wS.layout,
        seg_start:  S._wS.start,  seg_len: S._wS.len,
        zone_x:     S._wS.zoneX,  zone_y:  S._wS.zoneY,
        zone_w:     S._wS.zoneW,  zone_h:  S._wS.zoneH,
        color:      S._wS.color,
        brightness: S._wS.brightness,
        effect:     S._wS.effect,
      })});
    } catch(e) { toast('Preview failed: ' + e.message, true); }
  }, 300);
}

// The controller the bin sheet currently points at: the picked device, else
// the legacy typed IP. Every preview/ping/off goes through this so they all
// talk to the same hardware the bin will be saved against.
function wledTarget() {
  const devId = document.getElementById('wled-bin-device')?.value || '';
  const dev = (S._devices || []).find(d => d.id === devId);
  if (dev) return {device_id: dev.id, ip: dev.ip};
  return {ip: document.getElementById('wled-bin-ip')?.value.trim() || ''};
}

export async function wledPreviewOff() {
  const target = wledTarget();
  if (!target.ip) target.ip = S._wledDefaultIp;
  if (!target.ip) return;
  try { await api('/api/wled/off', {method:'POST', body: JSON.stringify(target)}); } catch(e) {}
}

export async function pingWledBin() {
  await pingWled(wledTarget().ip, 'wled-bin-ping-result');
}

export async function saveWledBin() {
  const target   = wledTarget();
  const enabled  = document.getElementById('wled-bin-enabled').checked;
  const duration = parseInt(document.getElementById('wled-bin-duration').value) || 10;
  if (!target.ip) { toast('Choose a controller for this bin', true); return; }
  try {
    await api(`/api/bins/${S._wledCurrentBinId}/lighting`, {method:'PUT', body: JSON.stringify({
      enabled:    enabled,
      device_id:  target.device_id || '',
      ip:         target.ip,            // kept for bins with no device record
      layout:     S._wS.layout,
      matrix_w:   S._wS.matrixW, matrix_h: S._wS.matrixH,
      zone_x:     S._wS.zoneX,   zone_y:   S._wS.zoneY,
      zone_w:     S._wS.zoneW,   zone_h:   S._wS.zoneH,
      seg_start:  S._wS.start,   seg_len:  S._wS.len,
      color:      S._wS.color,   brightness: S._wS.brightness,
      effect:     S._wS.effect,  duration: duration,
    })});
    await loadBins();
    closeSheet('overlay-wled-bin');
    wledPreviewOff();
    // refresh bin detail header
    openBin(S._wledCurrentBinId);
    toast('WLED config saved');
  } catch(e) { toast(e.message, true); }
}

// ── FIND ──────────────────────────────────────────────────────────────────────

export async function findBin(binId) {
  const btn = document.getElementById('bin-find-btn');
  if (btn) { btn.classList.add('firing'); setIconText(btn, 'lightbulb', 'Lit!'); }
  clearTimeout(S._findOffTimer);
  try {
    const r = await api(`/api/wled/find/${binId}`, {method:'POST', body:'{}'});
    if (r.duration > 0) {
      S._findOffTimer = setTimeout(async () => {
        try { await api('/api/wled/off', {method:'POST', body: JSON.stringify({ip: r.ip})}); } catch(e) {}
        if (btn) { btn.classList.remove('firing'); setIconText(btn, 'lightbulb', 'Find'); }
      }, r.duration * 1000);
    } else {
      if (btn) { setTimeout(() => { btn.classList.remove('firing'); setIconText(btn, 'lightbulb', 'Find'); }, 1500); }
    }
  } catch(e) {
    toast('WLED error: ' + e.message, true);
    if (btn) { btn.classList.remove('firing'); setIconText(btn, 'lightbulb', 'Find'); }
  }
}

export async function findBinFromItem(evt) {
  if (!S.currentItemId) { toast('No item selected', true); return; }
  // Read the button now — currentTarget is cleared once we await.
  const btn = evt.currentTarget || evt.target;
  btn.classList.add('firing');
  setIconText(btn, 'lightbulb', 'Lit!');
  clearTimeout(S._findOffTimer);
  try {
    const r = await api(`/api/wled/find-item/${S.currentItemId}`, {method:'POST', body:'{}'});
    if (r.duration > 0) {
      S._findOffTimer = setTimeout(async () => {
        try { await api('/api/wled/off', {method:'POST', body: JSON.stringify({ip: r.ip})}); } catch(e) {}
        btn.classList.remove('firing');
        setIconText(btn, 'lightbulb', "Find This Item's Bin"); }, 1500);
    }
  } catch(e) {
    toast('WLED error: ' + e.message, true);
    btn.classList.remove('firing');
    setIconText(btn, 'lightbulb', "Find This Item's Bin");
  }
}


// ── Inline-handler entry points ─────────────────────────────────────────────
// The markup used to call findBin(currentBinId) / openWledSheet(currentBinId).
// Under ES modules only the FUNCTION names are bridged onto window — a bare
// `currentBinId` in the attribute resolves against window, where it does not
// exist, so the click threw a silent ReferenceError and nothing happened.
// These wrappers take no arguments and read the open bin from shared state.

export function findCurrentBin() {
  if (!S.currentBinId) return;
  return findBin(S.currentBinId);
}

export function openCurrentBinWled() {
  if (!S.currentBinId) return;
  return openWledSheet(S.currentBinId);
}


// ═══════════════════════════════════════════════════════════════════════════
// WLED CONTROLLER MANAGEMENT (phase 5)
//
// Controllers are records with a human-readable name, not bare IP strings
// scattered across bins. Renaming or re-addressing one updates every bin using
// it, and the bin setup sheet picks from a list instead of retyping an address.
// ═══════════════════════════════════════════════════════════════════════════

export async function loadDevices() {
  try {
    S._devices = await api('/api/wled/devices');
  } catch (e) {
    S._devices = [];
  }
  // Layout (strip vs matrix) is only known after a probe. A controller that has
  // never answered one — e.g. created by the v6 migration, or just added — would
  // otherwise read as a strip until someone thinks to press Re-query.
  const unprobed = S._devices.filter(d => !d.last_seen);
  if (unprobed.length) {
    await Promise.all(unprobed.map(d =>
      api(`/api/wled/devices/${d.id}/probe`, {method:'POST', body:'{}'}).catch(() => null)));
    try { S._devices = await api('/api/wled/devices'); } catch (e) {}
  }
  return S._devices;
}

export async function openDeviceManager() {
  await loadDevices();
  renderDeviceList();
  openSheet('overlay-devices');
}

function renderDeviceList() {
  const box = document.getElementById('device-list');
  if (!box) return;
  const devices = S._devices || [];
  if (!devices.length) {
    box.innerHTML = `<div class="empty"><div class="empty-icon">${icon('lightbulb')}</div>` +
      'No controllers yet.<br><span style="font-size:0.82rem">Add one above to get started.</span></div>';
    return;
  }
  box.innerHTML = '';
  devices.forEach(d => {
    // Reachability is worth surfacing: a controller that has never answered is
    // the most likely reason a bin's Find does nothing.
    const status = d.last_status === 'ok'
      ? `<span style="color:var(--success)">${dot('ok')} reachable</span>`
      : (d.last_status ? `<span style="color:var(--danger)">${dot('bad')} ${esc(d.last_status.slice(0,40))}</span>`
                       : `<span style="color:var(--muted)">not yet probed</span>`);
    const geometry = d.layout === 'matrix'
      ? `<span style="color:var(--accent2)">${icon('layout-grid')} ${d.matrix_w}×${d.matrix_h} matrix</span>`
      : `${icon('rectangle-horizontal')} ${d.led_count || '?'} LED strip`;
    const row = document.createElement('div');
    row.className = 'card';
    row.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <input type="text" value="${esc(d.name)}" data-id="${d.id}" class="dev-name"
               style="flex:2;min-width:120px;font-weight:600;">
        <input type="text" value="${esc(d.ip)}" data-id="${d.id}" class="dev-ip"
               style="flex:1;min-width:110px;">
        ${d.is_default ? '<span class="tag">default</span>' : ''}
      </div>
      <div class="text-sm muted" style="margin:6px 0;">
        ${geometry} · ${status} · used by ${d.bins_using} bin${d.bins_using !== 1 ? 's' : ''}
        ${d.firmware ? ' · v' + esc(d.firmware) : ''}
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;">
        <button class="btn btn-primary btn-sm" data-act="save">Save</button>
        <button class="btn btn-secondary btn-sm" data-act="probe">${icon('refresh-cw')} Re-query</button>
        ${d.is_default ? '' : '<button class="btn btn-secondary btn-sm" data-act="default">Make default</button>'}
        <button class="btn btn-danger btn-sm" data-act="del">Delete</button>
      </div>`;
    row.querySelector('[data-act="save"]').onclick = () => saveDevice(d.id, row);
    row.querySelector('[data-act="probe"]').onclick = (e) => probeDevice(d.id, e.target);
    const defBtn = row.querySelector('[data-act="default"]');
    if (defBtn) defBtn.onclick = () => makeDefaultDevice(d.id);
    row.querySelector('[data-act="del"]').onclick = () => deleteDevice(d.id, d.name);
    box.appendChild(row);
  });
}

export async function addDevice() {
  const name = document.getElementById('new-device-name').value.trim();
  const ip   = document.getElementById('new-device-ip').value.trim();
  const out  = document.getElementById('add-device-result');
  if (!ip) { out.textContent = 'Enter an IP address'; out.style.color = 'var(--danger)'; return; }
  try {
    const d = await api('/api/wled/devices', {method:'POST', body: JSON.stringify({name, ip})});
    document.getElementById('new-device-name').value = '';
    document.getElementById('new-device-ip').value = '';
    out.textContent = `Added "${d.name}".`;
    out.style.color = 'var(--success)';
    await loadDevices(); renderDeviceList();
  } catch (e) { out.textContent = e.message; out.style.color = 'var(--danger)'; }
}

async function saveDevice(id, row) {
  const name = row.querySelector('.dev-name').value.trim();
  const ip   = row.querySelector('.dev-ip').value.trim();
  try {
    await api(`/api/wled/devices/${id}`, {method:'PUT', body: JSON.stringify({name, ip})});
    toast('Controller updated — every bin using it now points at the new address');
    await loadDevices(); renderDeviceList();
  } catch (e) { toast(e.message, true); }
}

async function probeDevice(id, btn) {
  const label = btn.innerHTML;
  btn.disabled = true; setIconText(btn, 'loader-circle');
  try {
    const d = await api(`/api/wled/devices/${id}/probe`, {method:'POST', body:'{}'});
    toast(d.layout === 'matrix'
      ? `Detected a ${d.matrix_w}×${d.matrix_h} matrix`
      : `Detected a ${d.led_count} LED strip`);
    await loadDevices(); renderDeviceList();
  } catch (e) { toast('Could not reach controller: ' + e.message, true); }
  btn.disabled = false; btn.innerHTML = label;
}

async function makeDefaultDevice(id) {
  try {
    await api(`/api/wled/devices/${id}`, {method:'PUT', body: JSON.stringify({is_default:true})});
    await loadDevices(); renderDeviceList();
  } catch (e) { toast(e.message, true); }
}

async function deleteDevice(id, name) {
  if (!confirm(`Remove controller "${name}"?\n\nBins using it keep their stored address, so lighting that works today will keep working.`)) return;
  try {
    const r = await api(`/api/wled/devices/${id}`, {method:'DELETE'});
    toast(`Removed — ${r.bins_unlinked} bin(s) unlinked`);
    await loadDevices(); renderDeviceList();
  } catch (e) { toast(e.message, true); }
}

// ── Device picker inside the per-bin sheet ──────────────────────────────────
export async function populateDevicePicker(selectedId, legacyIp) {
  await loadDevices();
  const sel = document.getElementById('wled-bin-device');
  if (!sel) return;
  sel.innerHTML = '<option value="">— No controller —</option>';
  (S._devices || []).forEach(d => {
    const o = document.createElement('option');
    o.value = d.id;
    o.textContent = `${d.name} (${d.ip})` +
      (d.layout === 'matrix' ? ` · ${d.matrix_w}×${d.matrix_h} matrix` : '');
    if (d.id === selectedId) o.selected = true;
    sel.appendChild(o);
  });
  // A bin configured before the registry has an IP but no device — surface the
  // raw field so it stays editable rather than appearing unconfigured.
  const legacyRow = document.getElementById('wled-legacy-ip-row');
  if (legacyRow) legacyRow.style.display = (!selectedId && legacyIp) ? 'block' : 'none';
}

export async function onBinDeviceChange() {
  const sel = document.getElementById('wled-bin-device');
  const dev = (S._devices || []).find(d => d.id === sel.value);
  if (!dev) { applyLayoutUI('strip'); return; }
  // Choosing a controller is a clear intent to light this bin; an unticked
  // "enabled" box was why Find silently refused (400) after saving.
  const en = document.getElementById('wled-bin-enabled');
  if (en) en.checked = true;
  // Adopt the controller's geometry immediately so the sheet shows the right
  // controls — this is what was missing when an array behaved as a strip.
  if (dev.layout === 'matrix' && dev.matrix_w && dev.matrix_h) {
    S._wS.layout = 'matrix';
    S._wS.matrixW = dev.matrix_w;
    S._wS.matrixH = dev.matrix_h;
    S._wS.zoneX = Math.min(S._wS.zoneX || 0, dev.matrix_w - 1);
    S._wS.zoneY = Math.min(S._wS.zoneY || 0, dev.matrix_h - 1);
    S._wS.zoneW = Math.max(1, Math.min(S._wS.zoneW || 2, dev.matrix_w));
    S._wS.zoneH = Math.max(1, Math.min(S._wS.zoneH || 2, dev.matrix_h));
    applyLayoutUI('matrix');
    renderMatrixGrid();
  } else {
    S._wS.layout = 'strip';
    applyLayoutUI('strip');
  }
  sendPreview();
}

function applyLayoutUI(layout) {
  const m = document.getElementById('matrix-zone-section');
  const st = document.getElementById('strip-zone-section');
  if (m)  m.style.display  = layout === 'matrix' ? 'block' : 'none';
  if (st) st.style.display = layout === 'matrix' ? 'none'  : 'block';
}

// ── 2D zone editor ─────────────────────────────────────────────────────────
export function renderMatrixGrid() {
  const grid = document.getElementById('matrix-grid');
  if (!grid) return;
  const W = S._wS.matrixW || 0, H = S._wS.matrixH || 0;
  if (!W || !H) return;
  document.getElementById('matrix-dims').textContent = `${W}×${H}`;
  document.getElementById('matrix-pos').textContent  = `(${S._wS.zoneX}, ${S._wS.zoneY})`;
  document.getElementById('matrix-size').textContent = `${S._wS.zoneW}×${S._wS.zoneH}`;

  // Cap cell size so a large panel still fits the sheet.
  const cell = Math.max(6, Math.min(18, Math.floor(300 / W)));
  grid.style.gridTemplateColumns = `repeat(${W}, ${cell}px)`;
  grid.innerHTML = '';
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const lit = x >= S._wS.zoneX && x < S._wS.zoneX + S._wS.zoneW &&
                  y >= S._wS.zoneY && y < S._wS.zoneY + S._wS.zoneH;
      const d = document.createElement('div');
      d.style.cssText = `width:${cell}px;height:${cell}px;border-radius:2px;cursor:pointer;` +
        (lit ? `background:${S._wS.color};box-shadow:0 0 4px ${S._wS.color}88;`
             : 'background:#222;border:1px solid var(--border);');
      // Tapping a cell moves the zone's top-left corner there.
      d.onclick = () => {
        S._wS.zoneX = Math.min(x, Math.max(0, W - S._wS.zoneW));
        S._wS.zoneY = Math.min(y, Math.max(0, H - S._wS.zoneH));
        renderMatrixGrid(); sendPreview();
      };
      grid.appendChild(d);
    }
  }
}

export function matrixMove(dx, dy) {
  const W = S._wS.matrixW, H = S._wS.matrixH;
  S._wS.zoneX = Math.max(0, Math.min(W - S._wS.zoneW, S._wS.zoneX + dx));
  S._wS.zoneY = Math.max(0, Math.min(H - S._wS.zoneH, S._wS.zoneY + dy));
  renderMatrixGrid(); sendPreview();
}

export function matrixResize(dw, dh) {
  const W = S._wS.matrixW, H = S._wS.matrixH;
  S._wS.zoneW = Math.max(1, Math.min(W - S._wS.zoneX, S._wS.zoneW + dw));
  S._wS.zoneH = Math.max(1, Math.min(H - S._wS.zoneY, S._wS.zoneH + dh));
  renderMatrixGrid(); sendPreview();
}
