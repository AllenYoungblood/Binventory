// ─────────────────────────────────────────────────────────────────────────────
// BINventory — scanner.js
//
// The code reader overlay: a full-screen camera view with a framing guide.
// While it's open, the part of the picture inside the frame is sent to
// /api/scan about once a second (the same server-side decoder as a photo —
// QR, EAN/UPC, AprilTag), and the overlay closes on the first code it reads.
//
//   openScanner({title, hint}) → Promise<{decoded, matches} | null>
//
// null means the person closed it. With no camera (permission refused, plain
// HTTP, an old browser) it falls back to the phone's photo picker, so every
// caller gets the same answer either way. "Take a photo instead" is always
// there too: a still photo reads small or distant codes better than video.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { icon } from './icons.js';

const INTERVAL_MS = 900;     // one frame in flight at a time, at most this often
const MARGIN = 0.15;         // send a little more than the frame, for codes on its edge

let ui = null;

function build() {
  const el = document.createElement('div');
  el.id = 'scanner-overlay';
  el.className = 'scanner';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'true');
  el.innerHTML = `
    <video playsinline muted autoplay></video>
    <div class="scanner-top">
      <div><div class="scanner-title"></div><div class="scanner-hint"></div></div>
      <button class="scanner-close" aria-label="Close the scanner">${icon('x')}</button>
    </div>
    <div class="scanner-frame" aria-hidden="true">
      <span class="c tl"></span><span class="c tr"></span><span class="c bl"></span><span class="c br"></span>
      <span class="scanner-line"></span>
    </div>
    <div class="scanner-bottom">
      <div class="scanner-status" aria-live="polite"></div>
      <div class="scanner-actions">
        <button class="btn btn-secondary scanner-torch" style="display:none;">${icon('flashlight')} Light</button>
        <label class="btn btn-secondary">${icon('camera')} Take a photo instead
          <input type="file" accept="image/*" capture="environment" style="display:none"></label>
      </div>
    </div>`;
  document.body.appendChild(el);
  return {
    root: el, video: el.querySelector('video'), frame: el.querySelector('.scanner-frame'),
    title: el.querySelector('.scanner-title'), hint: el.querySelector('.scanner-hint'),
    status: el.querySelector('.scanner-status'), close: el.querySelector('.scanner-close'),
    torch: el.querySelector('.scanner-torch'), file: el.querySelector('input[type=file]'),
  };
}

async function post(blob) {
  const fd = new FormData();
  fd.append('photo', blob, 'frame.jpg');
  const r = await fetch('/api/scan', {method: 'POST', body: fd});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || 'Scan failed');
  return data;
}

/** The video pixels under the on-screen frame (video uses object-fit: cover). */
function grabFrame(video, frame) {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return null;
  const box = video.getBoundingClientRect(), f = frame.getBoundingClientRect();
  const scale = Math.max(box.width / vw, box.height / vh);
  const ox = (box.width - vw * scale) / 2, oy = (box.height - vh * scale) / 2;
  const mx = f.width * MARGIN, my = f.height * MARGIN;
  let x = (f.left - box.left - mx - ox) / scale, y = (f.top - box.top - my - oy) / scale;
  let w = (f.width + 2 * mx) / scale, h = (f.height + 2 * my) / scale;
  x = Math.max(0, x); y = Math.max(0, y); w = Math.min(vw - x, w); h = Math.min(vh - y, h);
  const c = document.createElement('canvas');
  c.width = Math.round(w); c.height = Math.round(h);
  c.getContext('2d').drawImage(video, x, y, w, h, 0, 0, c.width, c.height);
  return new Promise(res => c.toBlob(res, 'image/jpeg', 0.9));
}

/** The phone's photo picker, for when there's no live camera. */
function pickPhoto() {
  return new Promise(resolve => {
    const input = document.createElement('input');
    input.type = 'file'; input.accept = 'image/*'; input.capture = 'environment';
    input.addEventListener('change', async () => {
      const file = input.files[0];
      if (!file) return resolve(null);
      try { resolve(await post(file)); } catch (e) { resolve({decoded: [], matches: [], error: e.message}); }
    }, {once: true});
    input.click();
  });
}

export async function openScanner({ title = 'Scan a code', hint = 'Fit the label or barcode inside the frame' } = {}) {
  if (!navigator.mediaDevices?.getUserMedia) return pickPhoto();
  ui = ui || build();
  ui.title.textContent = title;
  ui.hint.textContent = hint;
  ui.status.textContent = 'Starting the camera…';
  ui.root.classList.remove('found');

  // Add Item may already have the camera open; share it rather than asking
  // for it twice (some phones refuse a second stream).
  const shared = S.cameraStream && S.cameraStream.active ? S.cameraStream : null;
  let stream = shared;
  if (!stream) {
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment', width: {ideal: 1920}, height: {ideal: 1080} } });
    } catch (e) {
      return pickPhoto();
    }
  }
  ui.video.srcObject = stream;
  ui.root.classList.add('open');
  document.body.style.overflow = 'hidden';

  const track = stream.getVideoTracks()[0];
  const caps = track?.getCapabilities ? track.getCapabilities() : {};
  let torchOn = false;
  ui.torch.style.display = caps.torch ? '' : 'none';
  ui.torch.onclick = async () => {
    torchOn = !torchOn;
    try { await track.applyConstraints({advanced: [{torch: torchOn}]}); } catch (e) { /* not supported after all */ }
    ui.torch.classList.toggle('btn-primary', torchOn);
  };

  return new Promise(resolve => {
    let done = false, busy = false, timer = null, tries = 0;
    const finish = result => {
      if (done) return;
      done = true;
      clearInterval(timer);
      if (!shared) stream.getTracks().forEach(t => t.stop());
      ui.video.srcObject = null;
      ui.root.classList.remove('open');
      document.body.style.overflow = '';
      document.removeEventListener('keydown', onKey);
      resolve(result);
    };
    const onKey = e => { if (e.key === 'Escape') finish(null); };
    document.addEventListener('keydown', onKey);
    ui.close.onclick = () => finish(null);
    ui.file.value = '';
    ui.file.onchange = async () => {
      const file = ui.file.files[0];
      if (!file) return;
      ui.status.textContent = 'Reading the photo…';
      try {
        const data = await post(file);
        if (data.decoded.length) return finish(data);
        ui.status.textContent = 'No code in that photo. Try closer, flatter, or in better light.';
      } catch (e) { ui.status.textContent = e.message; }
    };

    const tick = async () => {
      if (busy || done) return;
      const grab = grabFrame(ui.video, ui.frame);
      if (!grab) return;
      busy = true;
      try {
        const blob = await grab;
        const data = await post(blob);
        if (done) return;
        if (data.decoded.length) {
          ui.root.classList.add('found');
          ui.status.textContent = `Read ${data.decoded[0].value}`;
          if (navigator.vibrate) navigator.vibrate(60);
          setTimeout(() => finish(data), 350);
          return;
        }
        tries += 1;
        ui.status.textContent = tries < 6 ? 'Looking for a code…'
          : 'Still looking. Move closer, hold steady, or take a photo instead.';
      } catch (e) {
        ui.status.textContent = e.message;
      } finally { busy = false; }
    };
    ui.video.onloadedmetadata = () => { ui.status.textContent = 'Looking for a code…'; };
    timer = setInterval(tick, INTERVAL_MS);
  });
}
