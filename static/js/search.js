// ─────────────────────────────────────────────────────────────────────────────
// BINventory — search.js
//
// Text, semantic, and reverse-image search.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { api } from './api.js';
import { renderItemGrid } from './items.js';
import { pollClipStatus } from './settings.js';
import { esc } from './ui.js';
import { icon, setIconText } from './icons.js';

// ── SEARCH ────────────────────────────────────────────────────────────────────
export function scheduleSearch() {
  clearTimeout(S.searchTimer);
  S.searchTimer = setTimeout(doSearch, 350);
}

export async function doSearch(smart=false) {
  const q = document.getElementById('search-input').value.trim();
  const status = document.getElementById('search-status');
  const results = document.getElementById('search-results');
  if (!q) { results.innerHTML = ''; status.textContent = ''; return; }

  S._viewingParentBin = false;   // search results always show their own bin line
  status.textContent = (smart || S._simplified) ? 'Searching (loading ML model if needed)…' : 'Searching…';
  try {
      // In simplified mode every search is semantic — the user shouldn't have to
    // know the difference between keyword and ML search.
    const useSmart = smart || S._simplified;
    const url = `/api/search?q=${encodeURIComponent(q)}${useSmart ? '&smart=1' : ''}`;
    const data = await api(url);
    // Response is {mode, results}: "semantic" used the ML model, "keyword"
    // fell back to plain text matching because the model wasn't loaded.
    const items = data.results || [];
    const mode  = data.mode || 'keyword';

    if (!items.length) {
      const hint = (mode === 'keyword')
        ? `<div class="empty" style="grid-column:1/-1"><div class="empty-icon">${icon('search')}</div>
             No keyword matches for "${esc(q)}".<br>
             <span style="font-size:0.82rem;">Tap ${icon('sparkles')} for smart search — it matches by meaning, not just words.</span>
           </div>`
        : `<div class="empty" style="grid-column:1/-1"><div class="empty-icon">${icon('search')}</div>No results for "${esc(q)}"</div>`;
      results.innerHTML = hint;
      status.textContent = '';
    } else {
      const badge = mode === 'semantic' ? `${icon('sparkles')} smart` : `${icon('scan-text')} keyword`;
      status.innerHTML = `${items.length} result${items.length!==1?'s':''} · <span style="color:var(--accent2)">${badge}</span>`
        + (mode === 'keyword' ? ` <span style="color:var(--muted)">— tap ${icon('sparkles')} to match by meaning</span>` : '');
      renderItemGrid(results, items, mode === 'semantic');
    }
    pollClipStatus();
  } catch(e) {
    status.textContent = 'Search error';
  }
}

// ── PHOTO SEARCH (reverse image lookup) ─────────────────────────────────────
// Sends a photo to the server, which embeds it with CLIP and ranks stored items
// by visual similarity. Always loads the model — there's no keyword fallback
// for an image.

// ── PHOTO SEARCH (reverse image lookup) ─────────────────────────────────────
// Sends a photo to the server, which embeds it with CLIP and ranks stored items
// by visual similarity. Always loads the model — there's no keyword fallback
// for an image.
export async function doPhotoSearch(evt) {
  const file = evt.target.files[0];
  evt.target.value = '';           // allow re-picking the same file later
  if (!file) return;

  const status  = document.getElementById('search-status');
  const results = document.getElementById('search-results');
  const btn     = document.getElementById('photo-search-btn');

  btn.style.opacity = '0.5';
  status.textContent = 'Analyzing photo — loading ML model if needed…';
  results.innerHTML = '';

  try {
    const fd = new FormData();
    fd.append('photo', file);
    const res  = await fetch('/api/search/photo', {method:'POST', body: fd});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Search failed');

    S._viewingParentBin = false;
    const items = data.results || [];
    if (!items.length) {
      results.innerHTML = `<div class="empty" style="grid-column:1/-1">
        <div class="empty-icon">${icon('camera')}</div>
        No visual matches found.<br>
        <span style="font-size:0.82rem;">${data.note || 'Try a clearer photo, or a different angle.'}</span>
      </div>`;
      status.textContent = '';
    } else {
      status.innerHTML = `${items.length} visual match${items.length!==1?'es':''} · `
        + `<span style="color:var(--accent2)">${icon('camera')} photo search</span>`;
      renderItemGrid(results, items, true);
    }
    pollClipStatus();
  } catch(e) {
    status.textContent = 'Photo search failed: ' + e.message;
  }
  btn.style.opacity = '1';
}

// Explicit "load the model and search semantically" action (the sparkles button).

// Explicit "load the model and search semantically" action (the sparkles button).
export async function doSmartSearch() {
  const btn = document.getElementById('smart-search-btn');
  btn.disabled = true; setIconText(btn, 'loader-circle');
  await doSearch(true);
  btn.disabled = false; setIconText(btn, 'sparkles');
}

// ── ITEM GRID RENDERER ────────────────────────────────────────────────────────
