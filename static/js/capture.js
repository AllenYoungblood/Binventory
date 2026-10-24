// ─────────────────────────────────────────────────────────────────────────────
// BINventory — capture.js
//
// Everything that produces a photo: single Add Item, the camera, Bulk Add,
// rotation, and the duplicate-match check.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { api } from './api.js';
import { loadBinItems, loadBins, renderBinsView } from './bins.js';
import { barcodeFields, resetItemBarcode } from './codes.js';
import { openItemDetail } from './items.js';
import { pollClipStatus } from './settings.js';
import { closeSheet, esc, openSheet, toast } from './ui.js';
import { icon, setIconText, setSwitch } from './icons.js';

export function updateItemOcrBtn() {
  const b = document.getElementById('item-ocr-toggle');
  if (!b) return;
  setSwitch(b, S._itemOcrOn, 'Read text on this item');
}

export function toggleItemOcr() {
  S._itemOcrOn = !S._itemOcrOn;
  updateItemOcrBtn();
}

// ── OCR: settings sheet ─────────────────────────────────────────────────────

// ── ADD ITEM ──────────────────────────────────────────────────────────────────
export function openAddItemSheet() {
  const bin = S.allBins.find(b => b.id === S.currentBinId);
  document.getElementById('add-item-bin-name').textContent = bin ? bin.name : '';
  document.getElementById('item-label').value = '';
  document.getElementById('item-notes').value = '';
  document.getElementById('btn-save-item').disabled = true;
  S.capturedBlob = null;
  resetItemBarcode();
  // Seed the per-item OCR toggle from the user's configured default.
  S._itemOcrOn = !!S._ocrSettings.ocr_default_on;
  updateItemOcrBtn();
  resetCameraUI();
  openSheet('overlay-add-item');
}

export async function startCamera() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width:{ideal:1280}, height:{ideal:720} }
    });
    S.cameraStream = stream;
    const vid = document.getElementById('camera-video');
    vid.srcObject = stream;
    vid.style.display = 'block';
    document.getElementById('camera-placeholder').style.display = 'none';
    document.getElementById('photo-preview').style.display = 'none';
    document.getElementById('btn-capture').disabled = false;
    document.getElementById('btn-start-cam').disabled = true;
  } catch(e) {
    toast('Camera access denied — use Upload instead', true);
  }
}

export function capturePhoto() {
  const vid    = document.getElementById('camera-video');
  const canvas = document.getElementById('camera-canvas');
  canvas.width  = vid.videoWidth;
  canvas.height = vid.videoHeight;
  canvas.getContext('2d').drawImage(vid, 0, 0);
  canvas.toBlob(blob => {
    S.capturedBlob = blob;
    showPreview(URL.createObjectURL(blob));
  }, 'image/jpeg', 0.85);
  stopCamera();
}

export function handleFileUpload(e) {
  const file = e.target.files[0];
  if (!file) return;
  S.capturedBlob = file;
  showPreview(URL.createObjectURL(file));
  e.target.value = '';
}

// Rotate the not-yet-uploaded photo. We redraw it through a canvas so the
// bytes we upload are already upright — the server then never has to guess.

// Rotate the not-yet-uploaded photo. We redraw it through a canvas so the
// bytes we upload are already upright — the server then never has to guess.
export async function rotateStaged(deg) {
  if (!S.capturedBlob) return;
  const img = new Image();
  const url = URL.createObjectURL(S.capturedBlob);
  await new Promise(res => { img.onload = res; img.src = url; });

  const cv = document.createElement('canvas');
  const swap = Math.abs(deg) === 90;
  cv.width  = swap ? img.height : img.width;
  cv.height = swap ? img.width  : img.height;

  const ctx = cv.getContext('2d');
  ctx.translate(cv.width / 2, cv.height / 2);
  ctx.rotate(deg * Math.PI / 180);
  ctx.drawImage(img, -img.width / 2, -img.height / 2);

  cv.toBlob(b => {
    S.capturedBlob = b;
    URL.revokeObjectURL(url);
    showPreview(URL.createObjectURL(b));
  }, 'image/jpeg', 0.9);
}

export function showPreview(url) {
  const prev = document.getElementById('photo-preview');
  prev.src = url;
  prev.style.display = 'block';
  const rc = document.getElementById('rotate-controls');
  if (rc) rc.style.display = 'flex';
  const mc = document.getElementById('match-check-row');
  if (mc) mc.style.display = 'block';
  document.getElementById('camera-video').style.display = 'none';
  document.getElementById('camera-placeholder').style.display = 'none';
  document.getElementById('btn-save-item').disabled = false;
  document.getElementById('btn-capture').disabled = true;
  document.getElementById('btn-start-cam').disabled = false;
}

export function stopCamera() {
  if (S.cameraStream) {
    S.cameraStream.getTracks().forEach(t => t.stop());
    S.cameraStream = null;
  }
  document.getElementById('camera-video').style.display = 'none';
  document.getElementById('btn-capture').disabled = true;
  document.getElementById('btn-start-cam').disabled = false;
}

export function resetCameraUI() {
  stopCamera();
  const rc = document.getElementById('rotate-controls');
  if (rc) rc.style.display = 'none';
  const mc = document.getElementById('match-check-row');
  if (mc) mc.style.display = 'none';
  document.getElementById('photo-preview').style.display = 'none';
  document.getElementById('camera-placeholder').style.display = 'flex';
  document.getElementById('camera-placeholder').style.alignItems = 'center';
  document.getElementById('camera-placeholder').style.justifyContent = 'center';
  document.getElementById('camera-placeholder').style.flexDirection = 'column';
}

// ── BULK ADD ────────────────────────────────────────────────────────────────
// Rapid intake: keep the camera live and fire off one upload per tap. Uploads
// are deliberately fire-and-forget — we don't await them before re-arming the
// shutter, so the user can keep shooting at their own pace while requests
// settle in the background.

export async function openBulkAdd() {
  const bin = S.allBins.find(b => b.id === S.currentBinId);
  document.getElementById('bulk-bin-name').textContent =
    bin ? (bin.path ? `${bin.path} / ${bin.name}` : bin.name) : '';
  S._bulkCount = 0;
  document.getElementById('bulk-count').textContent = '0';
  document.getElementById('bulk-thumb').style.display = 'none';
  document.getElementById('bulk-camera-error').style.display = 'none';
  document.getElementById('overlay-bulk').classList.add('active');
  await startBulkCamera();
}

export async function startBulkCamera() {
  const video = document.getElementById('bulk-video');
  const errBox = document.getElementById('bulk-camera-error');
  const errMsg = document.getElementById('bulk-error-msg');
  try {
    S._bulkStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width:{ideal:1920}, height:{ideal:1080} }
    });
    video.srcObject = S._bulkStream;
    video.style.display = 'block';
    errBox.style.display = 'none';
  } catch(e) {
    video.style.display = 'none';
    errBox.style.display = 'flex';
    // The overwhelmingly common cause is a non-secure origin: browsers block
    // getUserMedia on http://<lan-ip>. Say so plainly instead of showing a
    // generic permission error.
    const insecure = !window.isSecureContext;
    errMsg.innerHTML = insecure
      ? `<strong>Camera blocked — insecure connection</strong><br><br>
         Browsers only allow live camera access over HTTPS.<br>
         You're on <code>${location.protocol}//${location.host}</code>.<br>
         Restart the server to enable HTTPS, then open
         <code>https://${location.host}</code>
         <br><br><span style="font-size:0.8rem">See the README for details.</span>`
      : `<strong>Camera unavailable</strong><br><br>${esc(e.message)}<br><br>
         Check that camera permission is allowed for this site.`;
  }
}

export function stopBulkCamera() {
  if (S._bulkStream) {
    S._bulkStream.getTracks().forEach(t => t.stop());
    S._bulkStream = null;
  }
}

export function closeBulkAdd() {
  stopBulkCamera();
  document.getElementById('overlay-bulk').classList.remove('active');
  // Refresh so the newly captured items show up in the bin.
  if (S._bulkCount > 0) {
    loadBins().then(() => { renderBinsView(); loadBinItems(); });
    toast(`${S._bulkCount} item${S._bulkCount!==1?'s':''} added — tagging in background`);
    pollClipStatus();
  }
}

export async function bulkCapture(withOcr) {
  if (S._bulkBusy || !S._bulkStream) return;
  S._bulkBusy = true;

  const video  = document.getElementById('bulk-video');
  const canvas = document.getElementById('bulk-canvas');
  const flash  = document.getElementById('bulk-flash');
  const status = document.getElementById('bulk-status');

  // Grab the frame.
  canvas.width  = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);

  // Immediate visual + haptic feedback so the user knows the shot registered
  // and can move to the next item without waiting for the upload.
  flash.style.opacity = '0.85';
  setTimeout(() => { flash.style.opacity = '0'; }, 120);
  if (navigator.vibrate) navigator.vibrate(35);

  canvas.toBlob(async (blob) => {
    // Optimistic counter — the upload almost always succeeds, and waiting would
    // stall the intake rhythm. Failures decrement it and surface a toast.
    S._bulkCount++;
    document.getElementById('bulk-count').textContent = S._bulkCount;

    const thumb = document.getElementById('bulk-thumb');
    thumb.src = URL.createObjectURL(blob);
    thumb.style.display = 'block';

    setIconText(status, 'loader-circle', withOcr ? 'Uploading with OCR…' : 'Uploading…');
    status.style.display = 'block';

    try {
      const fd = new FormData();
      fd.append('bin_id', S.currentBinId);
      fd.append('photo', blob, 'capture.jpg');
      fd.append('run_ocr', withOcr ? 'true' : 'false');
      const res = await fetch('/api/items', {method:'POST', body: fd});
      if (!res.ok) throw new Error('Upload failed');
      setIconText(status, 'check', `${S._bulkCount} saved`);
      setTimeout(() => { status.style.display = 'none'; }, 900);
    } catch(e) {
      S._bulkCount--;
      document.getElementById('bulk-count').textContent = S._bulkCount;
      setIconText(status, 'circle-x', 'Failed — retry that item');
      status.style.background = 'rgba(180,40,40,.85)';
      setTimeout(() => {
        status.style.display = 'none';
        status.style.background = 'rgba(0,0,0,.7)';
      }, 2200);
    }
    S._bulkBusy = false;
  }, 'image/jpeg', 0.85);
}

// ── FIND MATCHING ITEMS (from the Add Item screen) ──────────────────────────
// Before adding something, check whether you already own one. Runs the same
// reverse image search using the photo already staged for upload.

// ── FIND MATCHING ITEMS (from the Add Item screen) ──────────────────────────
// Before adding something, check whether you already own one. Runs the same
// reverse image search using the photo already staged for upload.
export async function findMatchingItems() {
  if (!S.capturedBlob) { toast('Take or upload a photo first', true); return; }

  const list   = document.getElementById('matches-list');
  const status = document.getElementById('matches-status');
  list.innerHTML = '';
  status.textContent = 'Analyzing photo — loading ML model if needed…';
  openSheet('overlay-matches');

  try {
    const fd = new FormData();
    fd.append('photo', S.capturedBlob, 'query.jpg');
    // Slightly looser than the search page: here we're warning about possible
    // duplicates, so surfacing a few extra near-misses is useful.
    fd.append('threshold', '0.50');
    fd.append('limit', '10');

    const res  = await fetch('/api/search/photo', {method:'POST', body: fd});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Search failed');

    const items = data.results || [];
    if (!items.length) {
      status.textContent = '';
      list.innerHTML = `<div class="empty">
        <div class="empty-icon">${icon('circle-check')}</div>
        No similar items found.<br>
        <span style="font-size:0.82rem;">This looks like something new.</span>
      </div>`;
      pollClipStatus();
      return;
    }

    status.textContent = `${items.length} possible match${items.length!==1?'es':''} — tap one for options`;
    items.forEach(item => list.appendChild(buildMatchCard(item)));
    pollClipStatus();
  } catch(e) {
    status.textContent = 'Match search failed: ' + e.message;
  }
}

// One result row, with the three actions the user asked for:
// see details · add this item to that bin · light up that bin (when WLED is on)

// One result row, with the three actions the user asked for:
// see details · add this item to that bin · light up that bin (when WLED is on)
export function buildMatchCard(item) {
  const row = document.createElement('div');
  row.className = 'card';
  row.style.padding = '10px';

  // The bin this match lives in — that's the bin we'd file the new item into.
  const binLabel = item.bin_path ? `${item.bin_path} / ${item.bin_name}` : item.bin_name;
  const pct = item.score != null ? Math.round(item.score * 100) : null;
  // WLED "Find" only makes sense if the global toggle is on AND that bin is set up.
  const canFind = item.bin_wled;   // bin's own config decides, per bins.js

  row.innerHTML = `
    <div style="display:flex;gap:10px;">
      <img src="${item.thumb_url || item.photo_url}" style="width:70px;height:70px;border-radius:8px;object-fit:cover;flex-shrink:0;background:#000;">
      <div style="flex:1;min-width:0;">
        <div style="font-weight:600;font-size:0.9rem;">${esc(item.label || 'Unlabeled')}</div>
        <div class="text-sm muted" style="display:flex;align-items:center;gap:5px;margin-top:2px;">
          <span class="bin-pip" style="background:${item.bin_color}"></span>${esc(binLabel)}
        </div>
        ${pct != null ? `<div style="font-size:0.72rem;color:var(--accent2);margin-top:3px;">${pct}% visual match</div>` : ''}
      </div>
    </div>
    <div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap;">
      <button class="btn btn-secondary btn-sm" data-act="details">${icon('eye')} Details</button>
      <button class="btn btn-primary btn-sm" data-act="addhere">${icon('inbox')} Add to this bin</button>
      ${canFind ? `<button class="find-btn" data-act="find">${icon('lightbulb')} Find bin</button>` : ''}
    </div>
  `;

  row.querySelector('[data-act="details"]').onclick = () => {
    closeSheet('overlay-matches');
    openItemDetail(item);
  };
  row.querySelector('[data-act="addhere"]').onclick = () => addItemToMatchedBin(item);
  const findBtn = row.querySelector('[data-act="find"]');
  if (findBtn) findBtn.onclick = (e) => findMatchedBin(e, item);

  return row;
}

// File the item currently being added into the same bin as the match, so
// duplicates end up stored together.

// File the item currently being added into the same bin as the match, so
// duplicates end up stored together.
export async function addItemToMatchedBin(item) {
  const targetBin = item.bin_id;
  const bin = S.allBins.find(b => b.id === targetBin);
  if (!confirm(`Add the new item to "${bin ? bin.name : item.bin_name}"?`)) return;
  S.currentBinId = targetBin;                       // saveItem() posts to this bin
  const nameEl = document.getElementById('add-item-bin-name');
  if (nameEl) nameEl.textContent = bin ? bin.name : item.bin_name;
  closeSheet('overlay-matches');
  toast(`Will be added to ${bin ? bin.name : item.bin_name}`);
}

// Light up the matched item's bin so the user can walk over and compare.

// Light up the matched item's bin so the user can walk over and compare.
export async function findMatchedBin(evt, item) {
  const btn = evt.currentTarget || evt.target;
  btn.classList.add('firing');
  setIconText(btn, 'lightbulb', 'Lit!');
  try {
    const r = await api(`/api/wled/find-item/${item.id}`, {method:'POST', body:'{}'});
    if (r.duration > 0) {
      setTimeout(async () => {
        try { await api('/api/wled/off', {method:'POST', body: JSON.stringify({ip: r.ip})}); } catch(e) {}
        btn.classList.remove('firing'); setIconText(btn, 'lightbulb', 'Find bin');
      }, r.duration * 1000);
    } else {
      setTimeout(() => { btn.classList.remove('firing'); setIconText(btn, 'lightbulb', 'Find bin'); }, 1500);
    }
  } catch(e) {
    toast('WLED error: ' + e.message, true);
    btn.classList.remove('firing'); setIconText(btn, 'lightbulb', 'Find bin');
  }
}

export async function saveItem() {
  if (!S.capturedBlob) { toast('Take or upload a photo first'); return; }
  const btn = document.getElementById('btn-save-item');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div> Saving…';

  const form = (force = false) => {
    const fd = new FormData();
    fd.append('bin_id', S.currentBinId);
    fd.append('photo', S.capturedBlob, 'photo.jpg');
    fd.append('label', document.getElementById('item-label').value.trim());
    fd.append('notes', document.getElementById('item-notes').value.trim());
    fd.append('run_ocr', S._itemOcrOn ? 'true' : 'false');
    barcodeFields(fd, force);
    return fd;
  };

  try {
    let r = await fetch('/api/items', {method:'POST', body: form()});
    if (r.status === 409) {
      // The barcode is already on something else. Nothing was saved yet.
      const d = await r.json();
      if (!confirm(`${d.error}

Add it to this item too?`)) {
        btn.disabled = false; btn.innerHTML = 'Save Item';
        return;
      }
      r = await fetch('/api/items', {method:'POST', body: form(true)});
    }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    closeSheet('overlay-add-item');
    stopCamera();

    if (S._autoTag) {
      toast(`Saved — queued for tagging`);
    } else {
      toast(`Saved — auto-tag is OFF, tap "Tag All" when ready`);
    }

    await loadBins();
    renderBinsView();
    loadBinItems();
  } catch(e) {
    toast('Save failed: ' + e.message, true);
  }
  btn.disabled = false;
  btn.innerHTML = 'Save Item';
}

// ── INVENTORY ─────────────────────────────────────────────────────────────────


// ── Unified rotation (backlog D3) ───────────────────────────────────────────
// There were two rotate controls: one for a photo staged in Add Item (canvas,
// client-side) and one for a photo already saved (server-side). The user should
// see ONE control; which path runs is an implementation detail of where the
// photo currently lives.
export async function rotatePhoto(deg = 90) {
  if (S.capturedBlob) return rotateStaged(deg);          // not uploaded yet
  if (S.currentItemId) {
    const { rotateSavedItem } = await import('./items.js');
    return rotateSavedItem();                            // already stored
  }
}
