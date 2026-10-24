"""Model loading, unloading, keep-alive, and progress tracking."""
import gc
import json
import threading
import time

from ..config import DEFAULT_KEEPALIVE

# The Flask app, injected by the factory. Needed because background threads must
# open an app context to touch the database; importing the app here would create
# a cycle (app -> ml -> app).
_app = None

def bind_app(app):
    """Called once by the application factory."""
    global _app
    _app = app

def get_app():
    return _app

# ── CLIP state ───────────────────────────────────────────────────────────────
_clip_model = None
_clip_preprocess = None
_clip_tokenizer = None
_clip_lock = threading.Lock()
_clip_loading = False
_clip_ready = False

# ── OCR state ────────────────────────────────────────────────────────────────
_ocr_reader = None
_ocr_lock = threading.Lock()
# Serialises readtext(): the background worker and a region-OCR request can
# both want the one shared reader at the same moment.
_ocr_run_lock = threading.Lock()
_ocr_loading = False
_ocr_ready = False

def clip_ready():
    return _clip_ready

def ocr_ready():
    return _ocr_ready

# ── Job progress (drives the "3 of 12 · ~40s left" banner) ──────────────────
_job_lock = threading.Lock()
_job_total = 0
_job_done = 0
_job_started = None
_job_times = []


def job_reset(total):
    """Begin a new progress run with `total` items expected."""
    global _job_total, _job_done, _job_started, _job_times
    with _job_lock:
        _job_total   = total
        _job_done    = 0
        _job_started = time.time()
        _job_times   = []

def job_tick(duration):
    """Record that one item finished, taking `duration` seconds."""
    global _job_done
    with _job_lock:
        _job_done += 1
        _job_times.append(duration)
        # Keep only the last 20 samples so the ETA tracks recent speed.
        if len(_job_times) > 20:
            _job_times.pop(0)

def job_snapshot():
    """Return a JSON-safe view of progress for /api/status.

    The denominator is computed live rather than trusted from the start of the
    run, because items can be enqueued *while* the worker is draining (e.g. the
    user keeps photographing). Total is therefore "finished + still queued",
    which stays truthful as the queue grows.
    """
    with _job_lock:
        done = _job_done
        avg  = (sum(_job_times) / len(_job_times)) if _job_times else None
    # Queue lives in the worker module; imported late to avoid a circular import
    # (worker imports runtime for the model lifecycle).
    from .worker import tag_queue
    pending   = tag_queue.qsize()
    total     = done + pending
    remaining = pending
    eta       = int(avg * remaining) if (avg and remaining) else None
    return {
        "total":     total,
        "done":      done,
        "remaining": remaining,
        "eta_sec":   eta,
    }

# ── Model keep-alive ──────────────────────────────────────────────────────────
# Loading CLIP is expensive: the weights load AND the whole dictionary has to be
# re-encoded. Unloading the instant a job ends means a user who searches twice
# pays that cost twice. So instead of unloading immediately, we set an expiry
# timestamp; a reaper thread unloads only once nothing has touched the models
# for `keepalive` seconds. Any new search or tagging job pushes the expiry out.
_keepalive_until  = 0.0
_keepalive_lock   = threading.Lock()
_reaper_running   = False
DEFAULT_KEEPALIVE = 60          # seconds; user-overridable in Settings


def touch_models(seconds=None):
    """Mark the models as in-use, extending their lifetime."""
    global _keepalive_until
    if seconds is None:
        try:
            from ..services.settings import get_settings
            with _app.app_context():
                seconds = get_settings().model_keepalive_sec
        except Exception:
            seconds = DEFAULT_KEEPALIVE
    with _keepalive_lock:
        _keepalive_until = max(_keepalive_until, time.time() + max(0, seconds))
    _ensure_reaper()

def _ensure_reaper():
    """Start the background thread that unloads models after they go idle."""
    global _reaper_running
    with _keepalive_lock:
        if _reaper_running:
            return
        _reaper_running = True
    threading.Thread(target=_reaper_loop, daemon=True).start()

def _reaper_loop():
    """Poll until the keep-alive window expires, then free the models."""
    global _reaper_running
    try:
        while True:
            time.sleep(2)
            with _keepalive_lock:
                expiry = _keepalive_until
            # Never unload while the tagging worker is mid-job.
            from .worker import worker_running
            if worker_running():
                continue
            if time.time() >= expiry:
                if _clip_ready or _ocr_ready:
                    print("[keepalive] Idle window elapsed — unloading models")
                    unload_clip()
                    unload_ocr()
                break
    finally:
        with _keepalive_lock:
            _reaper_running = False

def ensure_clip_for_query(seconds=None):
    """Load CLIP (if needed) for a search, and keep it warm afterwards.

    Returns True if CLIP is usable. Called by the search 'smart search' button.
    """
    ok = _clip_ready or load_clip()
    if ok:
        touch_models(seconds)
    return ok



def load_clip():
    """Load the CLIP model into memory. Blocks until ready."""
    global _clip_model, _clip_preprocess, _clip_tokenizer, _clip_ready, _clip_loading
    with _clip_lock:
        if _clip_ready:
            return True
        if _clip_loading:
            return False
        _clip_loading = True
    try:
        import open_clip
        print("[CLIP] Loading ViT-B-32 …")
        model, _, preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="openai"
        )
        tokenizer = open_clip.get_tokenizer("ViT-B-32")
        model.eval()
        with _clip_lock:
            _clip_model      = model
            _clip_preprocess = preprocess
            _clip_tokenizer  = tokenizer
            _clip_ready      = True
            _clip_loading    = False
        print("[CLIP] Loaded ✓")
        return True
    except Exception as e:
        print(f"[CLIP] Load error: {e}")
        with _clip_lock:
            _clip_loading = False
        return False

def unload_clip():
    """Release CLIP from memory and run garbage collection."""
    global _clip_model, _clip_preprocess, _clip_tokenizer, _clip_ready
    with _clip_lock:
        if not _clip_ready:
            return
        _clip_model      = None
        _clip_preprocess = None
        _clip_tokenizer  = None
        _clip_ready      = False
        # Label embeddings belong to the model instance we just dropped.
    from .embeddings import invalidate_label_cache
    invalidate_label_cache()
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass
    print("[CLIP] Unloaded — RAM freed")


def load_ocr():
    """Load the EasyOCR reader into memory. Blocks until ready.

    Mirrors load_clip(): guarded by a lock + loading flag so two threads can
    never start a load simultaneously. Model weights (~100 MB) download once on
    first use and are cached on disk thereafter.
    """
    global _ocr_reader, _ocr_ready, _ocr_loading
    with _ocr_lock:
        if _ocr_ready:
            return True
        if _ocr_loading:
            return False
        _ocr_loading = True
    try:
        import easyocr
        print("[OCR] Loading EasyOCR (english) …")
        # gpu=False keeps it on CPU, consistent with the CLIP setup and avoids
        # requiring a CUDA build for home users.
        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        with _ocr_lock:
            _ocr_reader  = reader
            _ocr_ready   = True
            _ocr_loading = False
        print("[OCR] Loaded ✓")
        return True
    except Exception as e:
        print(f"[OCR] Load error: {e}")
        with _ocr_lock:
            _ocr_loading = False
        return False

def unload_ocr():
    """Release the OCR reader from memory."""
    global _ocr_reader, _ocr_ready
    with _ocr_lock:
        if not _ocr_ready:
            return
        _ocr_reader = None
        _ocr_ready  = False
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass
    print("[OCR] Unloaded — RAM freed")

def run_ocr(image):
    """Read text from an image. Returns a cleaned, space-joined string.

    `image` is a file path or a PIL image (the region-OCR crop is never written
    to disk).

    Returns "" if OCR isn't loaded or nothing legible was found. Results below
    a confidence threshold are discarded to keep label noise out of the tags.
    """
    with _ocr_lock:
        reader = _ocr_reader if _ocr_ready else None
    if reader is None:
        return ""
    try:
        # detail=1 returns (bbox, text, confidence) tuples so we can filter.
        if hasattr(image, "convert"):                 # PIL image
            import numpy as np
            source = np.asarray(image.convert("RGB"))
        else:
            source = str(image)
        with _ocr_run_lock:
            results = reader.readtext(source, detail=1)
        pieces = []
        for entry in results:
            try:
                _, text, conf = entry
            except Exception:
                continue
            text = (text or "").strip()
            # 0.35 keeps most real label text while dropping noise/artifacts.
            if text and conf is not None and conf >= 0.35:
                pieces.append(text)
        return " ".join(pieces).strip()
    except Exception as e:
        print(f"[OCR] read error: {e}")
        return ""


def get_clip():
    """Return (model, preprocess, tokenizer) if loaded, else (None, None, None)."""
    with _clip_lock:
        if _clip_ready:
            return _clip_model, _clip_preprocess, _clip_tokenizer
    return None, None, None


def status_snapshot():
    """Model + progress state for /api/status."""
    return {
        "clip_ready":   _clip_ready,
        "clip_loading": _clip_loading,
        "ocr_ready":    _ocr_ready,
        "ocr_loading":  _ocr_loading,
    }
