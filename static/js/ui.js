// ─────────────────────────────────────────────────────────────────────────────
// BINventory — ui.js
//
// Generic UI primitives: view switching, bottom-sheet open/close, toasts, and
// small formatting helpers. No feature logic.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { stopCamera } from './capture.js';
import { loadInventoryView } from './items.js';
import { loadPlugins } from './plugins.js';

// Format a seconds count into a compact human string for the banner.
export function fmtEta(sec) {
  if (sec < 60) return `${sec}s left`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return s ? `${m}m ${s}s left` : `${m}m left`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m left`;
}

// ── OCR: per-item toggle on the Add Item screen ─────────────────────────────

// ── VIEWS ─────────────────────────────────────────────────────────────────────
export function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));

  if (name === 'bins') {
    document.getElementById('view-bins').classList.add('active');
    document.getElementById('tab-bins').classList.add('active');
  } else if (name === 'bin-detail') {
    document.getElementById('view-bin-detail').classList.add('active');
    document.getElementById('tab-bins').classList.add('active');
  } else if (name === 'inventory') {
    document.getElementById('view-inventory').classList.add('active');
    document.getElementById('tab-inventory').classList.add('active');
    loadInventoryView();
  } else if (name === 'search') {
    document.getElementById('view-search').classList.add('active');
    document.getElementById('tab-search').classList.add('active');
    setTimeout(() => document.getElementById('search-input').focus(), 200);
  } else if (name === 'scan') {
    document.getElementById('view-scan').classList.add('active');
    document.getElementById('tab-scan').classList.add('active');
    // A USB/Bluetooth scanner types into whatever has focus — make that the box.
    setTimeout(() => document.getElementById('scan-input').focus(), 200);
  } else if (name === 'plugins') {
    document.getElementById('view-plugins').classList.add('active');
    document.getElementById('tab-plugins').classList.add('active');
    loadPlugins();
  }
}

// ── API HELPERS ───────────────────────────────────────────────────────────────

// ── SHEETS ────────────────────────────────────────────────────────────────────
export function openSheet(id)  { document.getElementById(id).classList.add('active'); }

export function closeSheet(id) { document.getElementById(id).classList.remove('active'); }

export function closeAllSheets(e) {
  if (e.target.classList.contains('overlay')) {
    e.target.classList.remove('active');
    stopCamera();
  }
}

// ── TOAST ─────────────────────────────────────────────────────────────────────

export function toast(msg, err=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = err ? 'var(--danger)' : '#fff';
  t.style.color = err ? '#fff' : '#111';
  t.classList.add('show');
  clearTimeout(S.toastTimer);
  S.toastTimer = setTimeout(() => t.classList.remove('show'), 3000);
}

// ── UTILS ─────────────────────────────────────────────────────────────────────

// ── UTILS ─────────────────────────────────────────────────────────────────────
export function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── WLED GLOBAL ───────────────────────────────────────────────────────────────
