import os
import json
import uuid
import threading
import time
import gc
import requests as _requests
from datetime import datetime
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from PIL import Image, ImageOps
import io
import base64

# ── paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Database location. Defaults to the app folder for simple local runs, but is
# overridable via BINVENTORY_DB_DIR so Docker can point it at a persisted
# volume (/app/data). Without this, the SQLite file would live inside the
# container's writable layer and be LOST on every rebuild.
DB_DIR = Path(os.environ.get("BINVENTORY_DB_DIR", BASE_DIR))
DB_DIR.mkdir(parents=True, exist_ok=True)

def _open_upright(source):
    """Open an image and apply its EXIF orientation so the pixels match what
    the photographer actually saw.

    WHY THIS MATTERS: phone camera sensors are mounted in landscape. When you
    shoot a portrait photo, the sensor still writes landscape pixels and tags
    the file with an EXIF "Orientation" value telling viewers how to rotate it.
    Browsers and photo apps honour that tag, so the image looks upright to you —
    but PIL's Image.open() returns the RAW sensor pixels and ignores the tag.

    Without this correction, stored photos come out rotated 90°, which wrecks
    OCR (rotated text is unreadable) and degrades CLIP's recognition accuracy.

    ImageOps.exif_transpose() reads the tag, physically rotates the pixels to
    match, and strips the now-redundant tag so nothing double-rotates later.
    """
    img = Image.open(source)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        # Malformed/absent EXIF — fall back to the image as-is rather than fail.
        pass
    return img.convert("RGB")

app = Flask(__name__, static_folder="static")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_DIR}/binventory.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB

db = SQLAlchemy(app)

# ── models ─────────────────────────────────────────────────────────────────────
class Bin(db.Model):
    id        = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name      = db.Column(db.String(100), nullable=False, unique=True)
    location  = db.Column(db.String(200), default="")
    color     = db.Column(db.String(20), default="#6366f1")
    created   = db.Column(db.DateTime, default=datetime.utcnow)
    items     = db.relationship("Item", backref="bin", lazy=True, cascade="all, delete-orphan")
    # WLED integration
    wled_enabled    = db.Column(db.Boolean, default=False)
    wled_ip         = db.Column(db.String(64), default="")
    wled_seg_start  = db.Column(db.Integer, default=0)    # first LED index
    wled_seg_len    = db.Column(db.Integer, default=5)    # number of LEDs
    wled_color      = db.Column(db.String(20), default="#ffffff")
    wled_brightness = db.Column(db.Integer, default=128)  # 0-255
    wled_effect     = db.Column(db.String(20), default="solid")  # solid | pulse | chase
    wled_duration   = db.Column(db.Integer, default=10)   # seconds, 0=forever
    # Chase-effect parameters (only used when wled_effect == "chase")
    wled_chase_start = db.Column(db.Integer, default=0)   # LED index the comet starts from
    wled_chase_size  = db.Column(db.Integer, default=3)   # length of the moving comet, in LEDs
    wled_chase_speed = db.Column(db.Integer, default=50)  # ms per LED step (lower = faster)

class AppSettings(db.Model):
    """Single-row table for app-wide preferences (OCR behaviour, etc.).

    Kept separate from WLEDSettings so each subsystem owns its own config and
    neither has to know about the other.
    """
    id = db.Column(db.Integer, primary_key=True)
    # Default state of the OCR toggle on the Add Item screen.
    ocr_default_on   = db.Column(db.Boolean, default=False)
    # Whether OCR text should also be merged into the item's searchable tags.
    ocr_as_tags      = db.Column(db.Boolean, default=True)
    # Whether to truncate OCR text before storing it as tags.
    ocr_truncate     = db.Column(db.Boolean, default=True)
    # Truncation limit in characters (capped at 255 by the API).
    ocr_truncate_len = db.Column(db.Integer, default=255)
    # Seconds the CLIP/OCR models stay resident after the last search or tag job
    # before being unloaded. Higher = faster repeat queries, more idle RAM.
    model_keepalive_sec = db.Column(db.Integer, default=60)

class LearnedLabel(db.Model):
    """Vocabulary the app teaches itself from the user's own corrections.

    Every time you rename an item to something the dictionary didn't contain,
    that word is recorded here and folded into the candidate pool. Over time the
    vocabulary converges on *your* actual belongings rather than a generic list
    someone guessed at up front — which is usually far more effective than
    bolting on an ever-bigger static dictionary.
    """
    id    = db.Column(db.Integer, primary_key=True)
    text  = db.Column(db.String(120), unique=True, nullable=False)
    uses  = db.Column(db.Integer, default=1)      # how often it's been applied
    added = db.Column(db.DateTime, default=datetime.utcnow)

class WLEDSettings(db.Model):
    """Global WLED defaults shared across bins."""
    id              = db.Column(db.Integer, primary_key=True)
    default_ip      = db.Column(db.String(64), default="")
    enabled         = db.Column(db.Boolean, default=False)

class Item(db.Model):
    id          = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    bin_id      = db.Column(db.String(36), db.ForeignKey("bin.id"), nullable=False)
    photo_path  = db.Column(db.String(300), nullable=False)
    label       = db.Column(db.String(200), default="")
    tags        = db.Column(db.Text, default="")   # comma-separated CLIP tags
    embedding   = db.Column(db.Text, default="")   # JSON float array
    notes       = db.Column(db.Text, default="")
    created     = db.Column(db.DateTime, default=datetime.utcnow)

    # ── lifecycle status ───────────────────────────────────────────────────
    # "active"      — normal, lives in bin_id
    # "checked_out" — temporarily out, origin_bin_id remembers where it lives
    # "consumed"    — used up, shows in the shopping list
    status         = db.Column(db.String(20), default="active", index=True)
    origin_bin_id  = db.Column(db.String(36), default="")   # bin to return to / find
    checked_out_at = db.Column(db.DateTime, default=None)
    consumed_at    = db.Column(db.DateTime, default=None)

    # How the item's label/name was produced (for provenance & debugging):
    #   "ml"            — CLIP auto-generated the label from the photo
    #   "user"          — a human typed the label
    #   "scanned"       — filled directly from a scanned barcode/QR payload
    #   "scanned_search"— scanned code was looked up via a free API/search
    # Used by the (planned) barcode feature and shown as a small provenance tag.
    label_source   = db.Column(db.String(20), default="ml")

    # ── OCR ────────────────────────────────────────────────────────────────
    # ocr_text      : raw text read off the photo (full, untruncated)
    # ocr_requested : whether the user asked for OCR on this specific item
    #                 (set from the Add Item toggle; drives the worker)
    # ocr_done      : set once OCR has actually run, so re-runs are explicit
    ocr_text       = db.Column(db.Text, default="")
    ocr_requested  = db.Column(db.Boolean, default=False)
    ocr_done       = db.Column(db.Boolean, default=False)

# ── CLIP state ────────────────────────────────────────────────────────────────
_clip_model       = None
_clip_preprocess  = None
_clip_tokenizer   = None
_clip_lock        = threading.Lock()
_clip_loading     = False
_clip_ready       = False

# ── OCR state ─────────────────────────────────────────────────────────────────
# Managed exactly like CLIP: loaded on demand by the worker, unloaded when the
# queue drains, so it uses no RAM while idle. EasyOCR is used because it is a
# pure pip install (no separate Tesseract binary to install on Windows) and it
# runs on the PyTorch runtime we already depend on.
_ocr_reader   = None
_ocr_lock     = threading.Lock()
_ocr_loading  = False
_ocr_ready    = False

# ── Tag queue & worker ────────────────────────────────────────────────────────
import queue as _queue_module
_tag_queue         = _queue_module.Queue()
_worker_lock       = threading.Lock()
_worker_running    = False
_auto_tag          = True
_currently_tagging = None

# ── Job progress tracking (drives the "3 of 12 · ~40s left" banner) ───────────
# _job_total   : how many items were in this run when it started
# _job_done    : how many have finished so far
# _job_started : wall-clock time the run began (for ETA maths)
# _job_times   : rolling list of per-item durations, used to average the ETA
_job_lock    = threading.Lock()
_job_total   = 0
_job_done    = 0
_job_started = None
_job_times   = []

def _job_reset(total):
    """Begin a new progress run with `total` items expected."""
    global _job_total, _job_done, _job_started, _job_times
    with _job_lock:
        _job_total   = total
        _job_done    = 0
        _job_started = time.time()
        _job_times   = []

def _job_tick(duration):
    """Record that one item finished, taking `duration` seconds."""
    global _job_done
    with _job_lock:
        _job_done += 1
        _job_times.append(duration)
        # Keep only the last 20 samples so the ETA tracks recent speed.
        if len(_job_times) > 20:
            _job_times.pop(0)

def _job_snapshot():
    """Return a JSON-safe view of progress for /api/status.

    The denominator is computed live rather than trusted from the start of the
    run, because items can be enqueued *while* the worker is draining (e.g. the
    user keeps photographing). Total is therefore "finished + still queued",
    which stays truthful as the queue grows.
    """
    with _job_lock:
        done = _job_done
        avg  = (sum(_job_times) / len(_job_times)) if _job_times else None
    pending   = _tag_queue.qsize()
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

def _touch_models(seconds=None):
    """Mark the models as in-use, extending their lifetime."""
    global _keepalive_until
    if seconds is None:
        try:
            with app.app_context():
                seconds = _get_settings().model_keepalive_sec
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
            if _worker_running:
                continue
            if time.time() >= expiry:
                if _clip_ready or _ocr_ready:
                    print("[keepalive] Idle window elapsed — unloading models")
                    _unload_clip()
                    _unload_ocr()
                break
    finally:
        with _keepalive_lock:
            _reaper_running = False

def ensure_clip_for_query(seconds=None):
    """Load CLIP (if needed) for a search, and keep it warm afterwards.

    Returns True if CLIP is usable. Called by the search 'smart search' button.
    """
    ok = _clip_ready or _load_clip()
    if ok:
        _touch_models(seconds)
    return ok

# ── Scheduler ─────────────────────────────────────────────────────────────────
_scheduler     = BackgroundScheduler(daemon=True)
_schedule_hour = None    # None = disabled
_schedule_min  = 0

def _load_clip():
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

def _unload_clip():
    """Release CLIP from memory and run garbage collection."""
    global _clip_model, _clip_preprocess, _clip_tokenizer, _clip_ready
    with _clip_lock:
        if not _clip_ready:
            return
        _clip_model      = None
        _clip_preprocess = None
        _clip_tokenizer  = None
        _clip_ready      = False
    _invalidate_label_cache()   # embeddings are tied to the unloaded model
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass
    print("[CLIP] Unloaded — RAM freed")

def _load_ocr():
    """Load the EasyOCR reader into memory. Blocks until ready.

    Mirrors _load_clip(): guarded by a lock + loading flag so two threads can
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

def _unload_ocr():
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

def run_ocr(image_path):
    """Read text from an image file. Returns a cleaned, space-joined string.

    Returns "" if OCR isn't loaded or nothing legible was found. Results below
    a confidence threshold are discarded to keep label noise out of the tags.
    """
    with _ocr_lock:
        reader = _ocr_reader if _ocr_ready else None
    if reader is None:
        return ""
    try:
        # detail=1 returns (bbox, text, confidence) tuples so we can filter.
        results = reader.readtext(str(image_path), detail=1)
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

def _get_settings():
    """Fetch (creating if needed) the single AppSettings row.

    Must be called inside an app context. Centralising this means every caller
    gets the same defaults and we never have to null-check the row.
    """
    s = AppSettings.query.first()
    if not s:
        s = AppSettings()
        db.session.add(s)
        db.session.commit()
    return s

def _learn_label(text):
    """Record a user-supplied label so future photos can be tagged with it.

    Skips very short/very long strings and anything already known. Bumps a use
    counter for existing entries so popular terms can be prioritised later.
    """
    text = (text or "").strip()
    if not (2 < len(text) <= 60):
        return False
    # Ignore anything already in the static dictionary (case-insensitive).
    if text.lower() in {l.lower() for l in BASE_CANDIDATE_LABELS}:
        return False
    existing = LearnedLabel.query.filter(
        db.func.lower(LearnedLabel.text) == text.lower()
    ).first()
    if existing:
        existing.uses = (existing.uses or 1) + 1
        db.session.commit()
        return False
    db.session.add(LearnedLabel(text=text))
    db.session.commit()
    _refresh_candidate_labels()
    return True

def _refresh_candidate_labels():
    """Rebuild CANDIDATE_LABELS = static dictionary + everything learned.

    Mutates the module-level list in place so any code holding a reference sees
    the update; the embedding cache notices the change and rebuilds itself.
    """
    global CANDIDATE_LABELS
    try:
        learned = [l.text for l in LearnedLabel.query.all()]
    except Exception:
        learned = []
    CANDIDATE_LABELS = list(dict.fromkeys([*BASE_CANDIDATE_LABELS, *learned]))
    return len(CANDIDATE_LABELS)

def _merge_tags(existing, *new_terms):
    """Append terms to a comma-separated tag string, de-duplicated, order-stable."""
    have = [t.strip() for t in (existing or "").split(",") if t.strip()]
    lower = {t.lower() for t in have}
    for term in new_terms:
        term = (term or "").strip()
        if term and term.lower() not in lower:
            have.append(term)
            lower.add(term.lower())
    return ", ".join(have)

def _ensure_worker():
    """Start the background tagging worker thread if not already running."""
    global _worker_running
    with _worker_lock:
        if _worker_running:
            return
        _worker_running = True
    threading.Thread(target=_tag_worker, daemon=True).start()

def _tag_worker():
    """
    Background thread that drains the tag queue.

    Lifecycle:
      1. Work out whether this batch needs CLIP, OCR, or both.
      2. Load only the model(s) actually required.
      3. Process every queued item, recording timings for the ETA banner.
      4. Unload everything so idle RAM usage returns to ~zero.

    Loading lazily per-batch matters: an OCR-only re-run shouldn't pull the
    1.4 GB CLIP model into memory, and vice versa.
    """
    global _worker_running, _currently_tagging

    print("[worker] Starting …")

    # Snapshot the queue size so the progress banner has a denominator.
    _job_reset(_tag_queue.qsize())

    clip_loaded = False
    ocr_loaded  = False

    while True:
        try:
            item_id = _tag_queue.get(timeout=3)
        except _queue_module.Empty:
            break   # queue drained

        started = time.time()
        _currently_tagging = item_id
        try:
            with app.app_context():
                item = db.session.get(Item, item_id)
                if not item:
                    continue

                settings = _get_settings()
                img_path = UPLOAD_DIR / item.photo_path

                needs_clip = not item.embedding
                needs_ocr  = item.ocr_requested and not item.ocr_done

                # ── Load models on demand, only what's needed ──────────────
                if needs_clip and not clip_loaded:
                    clip_loaded = _load_clip()
                    if not clip_loaded:
                        print("[worker] CLIP unavailable — skipping visual tagging")
                if needs_ocr and not ocr_loaded:
                    ocr_loaded = _load_ocr()
                    if not ocr_loaded:
                        print("[worker] OCR unavailable — skipping text extraction")

                pil = None
                changed = False

                # ── CLIP visual tagging ───────────────────────────────────
                if needs_clip and clip_loaded:
                    pil    = Image.open(img_path).convert("RGB")
                    labels = top_labels(pil)
                    emb    = embed_image(pil)
                    item.tags      = _merge_tags(item.tags, *labels)
                    item.embedding = json.dumps(emb) if emb else ""
                    # Only auto-name from CLIP if nothing better exists yet.
                    if not item.label and labels:
                        item.label        = labels[0]
                        item.label_source = "ml"
                    changed = True
                    print(f"[worker] CLIP tagged {item_id}: {', '.join(labels[:4])}")

                # ── OCR text extraction ───────────────────────────────────
                if needs_ocr and ocr_loaded:
                    text = run_ocr(img_path)
                    item.ocr_text = text
                    item.ocr_done = True
                    if text:
                        # OCR text takes priority for the item name when the
                        # user hasn't typed one — a printed part number beats
                        # a generic visual guess like "electronic".
                        if not item.label or item.label_source in ("", "ml"):
                            item.label        = text[:200]
                            item.label_source = "ocr"
                        # Optionally index the text as searchable tags.
                        if settings.ocr_as_tags:
                            snippet = text
                            if settings.ocr_truncate:
                                limit = max(1, min(255, settings.ocr_truncate_len or 255))
                                snippet = snippet[:limit]
                            item.tags = _merge_tags(item.tags, snippet)
                        print(f"[worker] OCR read {item_id}: {text[:60]}")
                    else:
                        print(f"[worker] OCR found no text on {item_id}")
                    changed = True

                if changed:
                    db.session.commit()
        except Exception as e:
            print(f"[worker] Error on {item_id}: {e}")
        finally:
            _currently_tagging = None
            _job_tick(time.time() - started)
            _tag_queue.task_done()

    # ── Done — keep models warm briefly instead of unloading immediately ──
    # A user who tags then searches (or tags again) shouldn't pay the reload +
    # dictionary re-encode cost. The reaper frees them once truly idle.
    with _worker_lock:
        _worker_running = False
    if clip_loaded or ocr_loaded:
        _touch_models()
        print("[worker] Done — models kept warm for the keep-alive window")
    else:
        print("[worker] Done — idle")

def enqueue_item(item_id):
    """Add item to tag queue. Starts the worker if auto-tag is on."""
    _tag_queue.put(item_id)
    if _auto_tag:
        _ensure_worker()

def _enqueue_all_untagged():
    """Queue every item that still needs CLIP tagging OR a requested OCR pass."""
    with app.app_context():
        items = Item.query.filter(
            (Item.embedding == "") | (Item.embedding == None) |
            ((Item.ocr_requested == True) & (Item.ocr_done == False))
        ).all()
        count = len(items)
        for item in items:
            _tag_queue.put(item.id)
    if count:
        print(f"[Scheduler] Queued {count} items needing processing")
        _ensure_worker()
    return count

def _apply_schedule(hour, minute):
    """Set or clear the nightly tagging schedule."""
    global _schedule_hour, _schedule_min
    _scheduler.remove_all_jobs()
    if hour is None:
        _schedule_hour = None
        print("[Scheduler] Schedule cleared")
    else:
        _schedule_hour = hour
        _schedule_min  = minute
        _scheduler.add_job(
            _enqueue_all_untagged,
            CronTrigger(hour=hour, minute=minute),
            id="nightly_tag",
            replace_existing=True,
        )
        print(f"[Scheduler] Set to run daily at {hour:02d}:{minute:02d}")

# ══════════════════════════════════════════════════════════════════════════════
# CLIP CANDIDATE LABEL POOL — loaded from the ./dictionaries folder
# ──────────────────────────────────────────────────────────────────────────────
# Every *.txt file in dictionaries/ is a category. Format:
#   - one term per line
#   - blank lines ignored
#   - lines beginning with # are comments
#
# To DISABLE a category, rename the file so it ends in ".off"
#   e.g.  06_kitchen.txt  ->  06_kitchen.txt.off
# To ADD your own, just drop in a new .txt file.
# Restart the server after editing (or use the Reload button in Settings).
#
# These labels generate the human-readable tag chips AND power keyword search
# when the CLIP model isn't loaded, so a good vocabulary matters.
# ══════════════════════════════════════════════════════════════════════════════
DICT_DIR = BASE_DIR / "dictionaries"

def load_dictionaries():
    """Read every enabled dictionary file. Returns (terms, per_file_counts)."""
    terms, counts = [], {}
    if not DICT_DIR.exists():
        print(f"[vocab] No dictionaries folder at {DICT_DIR}")
        return terms, counts
    for path in sorted(DICT_DIR.glob("*.txt")):
        try:
            words = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    words.append(line)
            terms.extend(words)
            counts[path.name] = len(words)
        except Exception as e:
            print(f"[vocab] Could not read {path.name}: {e}")
    return terms, counts

def dictionary_status():
    """List every dictionary file with its term count and enabled state."""
    out = []
    if not DICT_DIR.exists():
        return out
    for path in sorted(DICT_DIR.iterdir()):
        if path.suffix not in (".txt", ".off"):
            continue
        enabled = path.name.endswith(".txt")
        try:
            n = sum(1 for l in path.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.strip().startswith("#"))
        except Exception:
            n = 0
        out.append({"file": path.name, "terms": n, "enabled": enabled})
    return out

# Static vocabulary read from disk at import time.
_dict_terms, _dict_counts = load_dictionaries()
BASE_CANDIDATE_LABELS = list(dict.fromkeys(_dict_terms))
print(f"[vocab] Loaded {len(BASE_CANDIDATE_LABELS)} terms from "
      f"{len(_dict_counts)} dictionary file(s)")

# The live vocabulary = static dictionary + words learned from your renames.
# Rebuilt at startup and whenever a new label is learned.
CANDIDATE_LABELS = list(BASE_CANDIDATE_LABELS)

def embed_image(pil_image):
    """Return a float list embedding for a PIL image, or None if CLIP not ready."""
    import torch
    model, preprocess, _ = get_clip()
    if model is None:
        return None
    with torch.no_grad():
        tensor = preprocess(pil_image).unsqueeze(0)
        feat   = model.encode_image(tensor)
        feat   = feat / feat.norm(dim=-1, keepdim=True)
    return feat[0].tolist()

def embed_text(text):
    """Return a float list embedding for a text query."""
    import torch
    model, _, tokenizer = get_clip()
    if model is None:
        return None
    with torch.no_grad():
        tokens = tokenizer([text])
        feat   = model.encode_text(tokens)
        feat   = feat / feat.norm(dim=-1, keepdim=True)
    return feat[0].tolist()

# ── Cached label embeddings ───────────────────────────────────────────────────
# The label vocabulary never changes while the server runs, so its CLIP text
# embeddings are computed ONCE and reused for every photo.
#
# WHY THIS MATTERS ENORMOUSLY: encoding text is a full transformer forward pass
# per label. Without caching, tagging one photo meant 1 image encode + N text
# encodes (N = every word in the dictionary). With ~2,300 labels that made the
# text side ~99% of the work, and it repeated for every single photo.
#
# With the cache, per-photo cost becomes: 1 image encode + one (1 x 512) @
# (512 x N) matrix multiply. That matmul is microseconds even at N = 20,000,
# so the dictionary can grow almost without limit at no per-photo cost. The
# only price is a one-time encode when the model loads (batched below).
_label_emb        = None   # torch tensor, shape (N, 512), L2-normalised
_label_emb_labels = None   # the exact label list the cache was built from
_label_emb_lock   = threading.Lock()

def _label_cache_path(labels):
    """Path for the on-disk label-embedding cache.

    The filename embeds a hash of the exact vocabulary, so changing, adding or
    removing any label produces a different filename and the stale cache is
    simply ignored (no manual invalidation needed).
    """
    import hashlib
    h = hashlib.sha256("\u0000".join(labels).encode("utf-8")).hexdigest()[:16]
    return DB_DIR / "label_cache" / f"labels_{h}.pt"

def _get_label_embeddings():
    """Return cached (embeddings, labels), building them on first use.

    Rebuilds automatically if CANDIDATE_LABELS changed (e.g. user-taught words
    were added at runtime) or if CLIP was unloaded and reloaded.
    """
    global _label_emb, _label_emb_labels
    import torch
    model, _, tokenizer = get_clip()
    if model is None:
        return None, None

    with _label_emb_lock:
        # Cache is valid only if it was built from the current vocabulary.
        if _label_emb is not None and _label_emb_labels == CANDIDATE_LABELS:
            return _label_emb, _label_emb_labels

        labels = list(CANDIDATE_LABELS)

        # Try the on-disk cache first. Keyed by a hash of the vocabulary, so
        # editing the dictionary automatically invalidates it. This turns the
        # one-time encode into a near-instant file read on every later startup.
        cache_file = _label_cache_path(labels)
        if cache_file.exists():
            try:
                data = torch.load(cache_file, map_location="cpu")
                if data.shape[0] == len(labels):
                    _label_emb        = data
                    _label_emb_labels = labels
                    print(f"[CLIP] Loaded {len(labels)} label embeddings from cache ✓")
                    return _label_emb, _label_emb_labels
            except Exception as e:
                print(f"[CLIP] Label cache unreadable ({e}) — rebuilding")

        print(f"[CLIP] Encoding {len(labels)} label embeddings (one time) …")
        feats = []
        # Batch to keep peak memory sane on large vocabularies.
        BATCH = 256
        with torch.no_grad():
            for i in range(0, len(labels), BATCH):
                chunk  = labels[i:i + BATCH]
                tokens = tokenizer(chunk)
                f      = model.encode_text(tokens)
                f      = f / f.norm(dim=-1, keepdim=True)
                feats.append(f)
            _label_emb = torch.cat(feats, dim=0)
        _label_emb_labels = labels
        # Persist so future startups skip the encode entirely.
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            torch.save(_label_emb, cache_file)
            print(f"[CLIP] Label embeddings cached to disk ✓")
        except Exception as e:
            print(f"[CLIP] Could not write label cache ({e}) — continuing")
        return _label_emb, _label_emb_labels

def _invalidate_label_cache():
    """Drop the cached label embeddings (called when CLIP unloads)."""
    global _label_emb, _label_emb_labels
    with _label_emb_lock:
        _label_emb = None
        _label_emb_labels = None

def top_labels(pil_image, top_k=8):
    """Score CANDIDATE_LABELS against an image, return the top-k label strings.

    Uses the cached label embedding matrix, so cost per photo is one image
    encode plus a single matrix multiply — independent of dictionary size.
    """
    import torch
    model, preprocess, _ = get_clip()
    if model is None:
        return []
    text_feat, labels = _get_label_embeddings()
    if text_feat is None:
        return []
    with torch.no_grad():
        img_tensor = preprocess(pil_image).unsqueeze(0)
        img_feat   = model.encode_image(img_tensor)
        img_feat   = img_feat / img_feat.norm(dim=-1, keepdim=True)
        scores     = (img_feat @ text_feat.T)[0]
        k          = min(top_k, len(labels))
        top_idx    = scores.topk(k).indices.tolist()
    return [labels[i] for i in top_idx]

# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    next_run = None
    job = _scheduler.get_job("nightly_tag")
    if job and job.next_run_time:
        next_run = job.next_run_time.strftime("%Y-%m-%dT%H:%M:%S")
    return jsonify({
        "clip_ready":     _clip_ready,
        "clip_loading":   _clip_loading,
        "ocr_ready":      _ocr_ready,
        "ocr_loading":    _ocr_loading,
        "worker_running": _worker_running,
        "queue_size":     _tag_queue.qsize(),
        "auto_tag":       _auto_tag,
        "tagging_now":    _currently_tagging is not None,
        "schedule_hour":  _schedule_hour,
        "schedule_min":   _schedule_min,
        "next_run":       next_run,
        "progress":       _job_snapshot(),
    })

@app.route("/api/settings/autotag", methods=["POST"])
def set_autotag():
    global _auto_tag
    _auto_tag = bool(request.json.get("enabled", True))
    return jsonify({"auto_tag": _auto_tag})

@app.route("/api/settings/runtags", methods=["POST"])
def run_all_tags():
    """Queue every untagged item and start the worker."""
    count = _enqueue_all_untagged()
    _ensure_worker()
    return jsonify({"queued": count})

@app.route("/api/settings/ocr", methods=["GET"])
def get_ocr_settings():
    """Return the OCR preferences shown in the Settings sheet."""
    s = _get_settings()
    return jsonify({
        "ocr_default_on":   s.ocr_default_on,
        "ocr_as_tags":      s.ocr_as_tags,
        "ocr_truncate":     s.ocr_truncate,
        "ocr_truncate_len": s.ocr_truncate_len,
        "model_keepalive_sec": s.model_keepalive_sec,
    })

@app.route("/api/settings/ocr", methods=["POST"])
def save_ocr_settings():
    """Persist OCR preferences. Truncation length is hard-capped at 255."""
    data = request.json or {}
    s = _get_settings()
    if "ocr_default_on"   in data: s.ocr_default_on   = bool(data["ocr_default_on"])
    if "ocr_as_tags"      in data: s.ocr_as_tags      = bool(data["ocr_as_tags"])
    if "ocr_truncate"     in data: s.ocr_truncate     = bool(data["ocr_truncate"])
    if "ocr_truncate_len" in data:
        s.ocr_truncate_len = max(1, min(255, int(data["ocr_truncate_len"])))
    if "model_keepalive_sec" in data:
        # 0 = unload immediately; cap at 1 hour to avoid pinning RAM forever.
        s.model_keepalive_sec = max(0, min(3600, int(data["model_keepalive_sec"])))
    db.session.commit()
    return jsonify({
        "ok": True,
        "ocr_default_on":   s.ocr_default_on,
        "ocr_as_tags":      s.ocr_as_tags,
        "ocr_truncate":     s.ocr_truncate,
        "ocr_truncate_len": s.ocr_truncate_len,
        "model_keepalive_sec": s.model_keepalive_sec,
    })

@app.route("/api/settings/rerun-ocr", methods=["POST"])
def rerun_ocr_all():
    """Re-run OCR across the whole database.

    Used after changing the truncation setting so existing items pick up the
    new rule. Clears ocr_done (so the worker reprocesses) and marks every item
    as OCR-requested. Body: {"scope": "all" | "existing"}
      all      — every item in the library
      existing — only items that already have OCR text (a re-truncate pass)
    """
    scope = (request.json or {}).get("scope", "all")
    q = Item.query
    if scope == "existing":
        q = q.filter(Item.ocr_text != "", Item.ocr_text != None)
    items = q.all()
    for item in items:
        item.ocr_requested = True
        item.ocr_done      = False
    db.session.commit()
    for item in items:
        _tag_queue.put(item.id)
    if items:
        _ensure_worker()
    return jsonify({"ok": True, "queued": len(items)})

# ── Dictionary management ─────────────────────────────────────────────────────
@app.route("/api/dictionaries", methods=["GET"])
def list_dictionaries():
    """Report every dictionary file, its term count, and whether it's enabled."""
    return jsonify({
        "dir":   str(DICT_DIR),
        "files": dictionary_status(),
        "total": len(BASE_CANDIDATE_LABELS),
        "active": len(CANDIDATE_LABELS),
    })

@app.route("/api/dictionaries/reload", methods=["POST"])
def reload_dictionaries():
    """Re-read the dictionaries folder without restarting the server.

    Rebuilds the base vocabulary, re-layers learned words, and drops the cached
    label embeddings so they're recomputed against the new word list.
    """
    global BASE_CANDIDATE_LABELS, _dict_terms, _dict_counts
    _dict_terms, _dict_counts = load_dictionaries()
    BASE_CANDIDATE_LABELS = list(dict.fromkeys(_dict_terms))
    n = _refresh_candidate_labels()
    _invalidate_label_cache()
    return jsonify({"ok": True, "base": len(BASE_CANDIDATE_LABELS), "active": n})

# ── Word frequency analysis (human-in-the-loop vocabulary building) ───────────
# Rather than guessing which words belong in the dictionary, mine them from the
# text already in your library: names you typed and text OCR read off labels.
# Words are counted across items, ranked, and presented for manual review.

_STOPWORDS = {
    "the","a","an","and","or","of","for","with","to","in","on","at","by","from",
    "is","are","was","were","be","this","that","it","its","as","new","used",
    "pcs","pcs.","qty","set","kit","pack","x","mm","cm","in.","inch","inches",
}

def _tokenize_for_vocab(text):
    """Split a string into candidate vocabulary words.

    Lower-cases, strips punctuation, drops stopwords, pure numbers, and very
    short tokens. Deliberately conservative — this feeds a human review screen,
    not an automatic import.
    """
    import re as _re
    if not text:
        return []
    words = _re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", text)
    out = []
    for w in words:
        wl = w.lower().strip("-")
        if len(wl) < 3 or wl in _STOPWORDS:
            continue
        if wl.isdigit():
            continue
        out.append(wl)
    return out

@app.route("/api/vocab/analyze", methods=["GET"])
def analyze_vocab():
    """Count word frequencies across chosen text sources.

    Query params:
      source = "manual" | "ocr" | "both"   (default both)
      min    = minimum occurrences to pre-select (default 2)

    Returns every word with its count and how many distinct items it appeared
    on, sorted most-frequent first, flagged with whether it's already known.
    """
    source  = request.args.get("source", "both")
    min_use = max(1, int(request.args.get("min", 2)))

    items = Item.query.all()
    counts, item_hits = {}, {}
    for it in items:
        texts = []
        # "manual" = names a human typed (label_source user), plus notes.
        if source in ("manual", "both"):
            if (it.label_source or "") == "user" and it.label:
                texts.append(it.label)
            if it.notes:
                texts.append(it.notes)
        if source in ("ocr", "both") and it.ocr_text:
            texts.append(it.ocr_text)

        seen_here = set()
        for t in texts:
            for w in _tokenize_for_vocab(t):
                counts[w] = counts.get(w, 0) + 1
                seen_here.add(w)
        for w in seen_here:
            item_hits[w] = item_hits.get(w, 0) + 1

    known = {l.lower() for l in CANDIDATE_LABELS}
    words = [{
        "word":     w,
        "count":    n,
        "items":    item_hits.get(w, 0),
        "known":    w in known,
        "selected": (n >= min_use) and (w not in known),
    } for w, n in counts.items()]
    words.sort(key=lambda x: (-x["count"], x["word"]))

    return jsonify({
        "source":     source,
        "min":        min_use,
        "scanned":    len(items),
        "unique":     len(words),
        "preselected": sum(1 for w in words if w["selected"]),
        "words":      words,
    })

@app.route("/api/vocab/add-words", methods=["POST"])
def add_vocab_words():
    """Append reviewed words to a dictionary file, then hot-reload.

    Body: {"words": [...], "file": "98_reviewed.txt"}
    Writing to a real file (rather than the learned-words table) means the
    additions are portable, editable by hand, and survive a database reset.
    """
    global BASE_CANDIDATE_LABELS, _dict_terms, _dict_counts
    data  = request.json or {}
    words = [w.strip() for w in data.get("words", []) if w and w.strip()]
    fname = data.get("file", "98_reviewed.txt")
    if not fname.endswith(".txt"):
        fname += ".txt"
    if "/" in fname or "\\" in fname:
        return jsonify({"error": "Invalid filename"}), 400
    if not words:
        return jsonify({"error": "No words supplied"}), 400

    DICT_DIR.mkdir(parents=True, exist_ok=True)
    path = DICT_DIR / fname
    existing = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                existing.add(line.lower())
    else:
        path.write_text(
            "# Reviewed words added from Settings -> Vocabulary Review\n"
            "# One term per line. Edit freely.\n\n", encoding="utf-8")

    added = [w for w in words if w.lower() not in existing]
    if added:
        with path.open("a", encoding="utf-8") as f:
            for w in added:
                f.write(w + "\n")

    # Hot-reload so the new words take effect immediately.
    _dict_terms, _dict_counts = load_dictionaries()
    BASE_CANDIDATE_LABELS = list(dict.fromkeys(_dict_terms))
    n = _refresh_candidate_labels()
    _invalidate_label_cache()
    return jsonify({"ok": True, "added": len(added), "file": fname, "active": n})

@app.route("/api/vocab/export", methods=["POST"])
def export_vocab_words():
    """Return selected words as a downloadable plain-text wordlist."""
    words = [w.strip() for w in (request.json or {}).get("words", []) if w and w.strip()]
    body  = ("# BINventory exported wordlist\n"
             "# One term per line. Drop this file into the dictionaries/ folder to use it.\n\n"
             + "\n".join(words) + "\n")
    from flask import Response
    return Response(body, mimetype="text/plain", headers={
        "Content-Disposition": "attachment; filename=binventory_wordlist.txt"
    })

@app.route("/api/vocab", methods=["GET"])
def list_vocab():
    """Report vocabulary size and the words learned from your renames."""
    learned = LearnedLabel.query.order_by(LearnedLabel.uses.desc()).all()
    return jsonify({
        "base_count":    len(BASE_CANDIDATE_LABELS),
        "learned_count": len(learned),
        "active_count":  len(CANDIDATE_LABELS),
        "learned": [{"id": l.id, "text": l.text, "uses": l.uses} for l in learned],
    })

@app.route("/api/vocab/<int:vid>", methods=["DELETE"])
def delete_vocab(vid):
    """Remove a learned word (e.g. a typo that got recorded)."""
    l = db.session.get(LearnedLabel, vid)
    if not l:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(l)
    db.session.commit()
    _refresh_candidate_labels()
    return jsonify({"ok": True, "active_count": len(CANDIDATE_LABELS)})

@app.route("/api/settings/unloadclip", methods=["POST"])
def unload_clip_route():
    """Manually release CLIP from memory."""
    if _worker_running:
        return jsonify({"error": "Worker is running — wait until tagging finishes"}), 409
    _unload_clip()
    return jsonify({"ok": True})

@app.route("/api/settings/schedule", methods=["POST"])
def set_schedule():
    """Set or clear the nightly auto-tag schedule.
       POST {"hour": 2, "minute": 30}  → run at 02:30 daily
       POST {"hour": null}             → disable schedule
    """
    data   = request.json or {}
    hour   = data.get("hour")
    minute = int(data.get("minute", 0))
    if hour is None:
        _apply_schedule(None, 0)
    else:
        _apply_schedule(int(hour), minute)
    return jsonify({"ok": True, "schedule_hour": _schedule_hour, "schedule_min": _schedule_min})

# Kick off CLIP load when the client first loads the page
@app.route("/api/warmup", methods=["POST"])
def api_warmup():
    get_clip()
    return jsonify({"ok": True})

# ── bins ──────────────────────────────────────────────────────────────────────
@app.route("/api/bins", methods=["GET"])
def list_bins():
    bins = Bin.query.order_by(Bin.created).all()
    result = [_bin_dict(b) for b in bins]

    checked_out_count = Item.query.filter_by(status="checked_out").count()
    if checked_out_count > 0:
        # Synthetic pseudo-bin, pinned to the front. Recognizable by id="checked_out".
        result.insert(0, {
            "id": "checked_out",
            "name": "Checked Out",
            "location": "",
            "color": "#fbbf24",
            "item_count": checked_out_count,
            "created": "",
            "is_virtual": True,
            "wled_enabled": False, "wled_ip": "", "wled_seg_start": 0,
            "wled_seg_len": 5, "wled_color": "#ffffff", "wled_brightness": 128,
            "wled_effect": "solid", "wled_duration": 10,
            "wled_chase_start": 0, "wled_chase_size": 3, "wled_chase_speed": 50,
        })

    return jsonify(result)

@app.route("/api/bins/<bin_id>", methods=["GET"])
def get_bin(bin_id):
    b = db.session.get(Bin, bin_id)
    if not b: return jsonify({"error": "Not found"}), 404
    return jsonify(_bin_dict(b))

@app.route("/api/bins", methods=["POST"])
def create_bin():
    data = request.json
    if not data.get("name","").strip():
        return jsonify({"error": "Name required"}), 400
    if Bin.query.filter_by(name=data["name"].strip()).first():
        return jsonify({"error": "A bin with that name already exists"}), 400
    b = Bin(name=data["name"].strip(),
            location=data.get("location",""),
            color=data.get("color","#6366f1"))
    db.session.add(b)
    db.session.commit()
    return jsonify({"id": b.id, "name": b.name}), 201

@app.route("/api/bins/<bin_id>", methods=["PUT"])
def update_bin(bin_id):
    b = db.session.get(Bin, bin_id)
    if not b: return jsonify({"error": "Not found"}), 404
    data = request.json
    if "name"            in data: b.name            = data["name"].strip()
    if "location"        in data: b.location        = data["location"]
    if "color"           in data: b.color           = data["color"]
    if "wled_enabled"    in data: b.wled_enabled    = bool(data["wled_enabled"])
    if "wled_ip"         in data: b.wled_ip         = data["wled_ip"].strip()
    if "wled_seg_start"  in data: b.wled_seg_start  = int(data["wled_seg_start"])
    if "wled_seg_len"    in data: b.wled_seg_len    = max(1, int(data["wled_seg_len"]))
    if "wled_color"      in data: b.wled_color      = data["wled_color"]
    if "wled_brightness" in data: b.wled_brightness = max(0, min(255, int(data["wled_brightness"])))
    if "wled_effect"     in data: b.wled_effect     = data["wled_effect"]
    if "wled_duration"   in data: b.wled_duration   = max(0, int(data["wled_duration"]))
    if "wled_chase_start" in data: b.wled_chase_start = max(0, int(data["wled_chase_start"]))
    if "wled_chase_size"  in data: b.wled_chase_size  = max(1, int(data["wled_chase_size"]))
    if "wled_chase_speed" in data: b.wled_chase_speed = max(5, int(data["wled_chase_speed"]))
    db.session.commit()
    return jsonify(_bin_dict(b))

@app.route("/api/bins/<bin_id>", methods=["DELETE"])
def delete_bin(bin_id):
    """
    Delete a bin. Requires confirmation and a content-disposition choice.
    POST/DELETE body:
      {
        "confirm": "delete",            # must equal "delete" (case-insensitive)
        "contents": "delete" | "checkout"
      }
    - contents="delete":   permanently delete the bin AND all its items (+ photos)
    - contents="checkout": check every active item out of the bin first (so they
                           survive in the Checked Out list), then delete the bin.
                           Items already consumed are left untouched in the shopping list.
    """
    b = db.session.get(Bin, bin_id)
    if not b: return jsonify({"error": "Not found"}), 404

    data    = request.get_json(silent=True) or {}
    confirm = str(data.get("confirm", "")).strip().lower()
    mode    = data.get("contents", "")

    if confirm != "delete":
        return jsonify({"error": "Confirmation text did not match"}), 400
    if mode not in ("delete", "checkout"):
        return jsonify({"error": "Must choose what to do with contents"}), 400

    if mode == "checkout":
        # Detach active items: check them out, remembering origin = this bin's name.
        # Since the bin is going away, we move them to ANY surviving bin as a holding
        # location but keep status=checked_out and store the original bin name in notes
        # so they remain findable in the Checked Out list.
        other_bin = Bin.query.filter(Bin.id != bin_id).first()
        if not other_bin:
            return jsonify({"error": "Cannot check out — no other bin exists to hold items. Create another bin first or choose delete."}), 400

        moved = 0
        for item in list(b.items):
            if item.status == "consumed":
                # consumed items keep their shopping-list entry; just repoint origin
                item.origin_bin_id = other_bin.id
                continue
            # carry the original bin name forward in notes for reference
            tag = f"[was in {b.name}]"
            if tag not in (item.notes or ""):
                item.notes = (item.notes + "  " if item.notes else "") + tag
            item.bin_id         = other_bin.id
            item.origin_bin_id  = other_bin.id
            item.status         = "checked_out"
            item.checked_out_at = datetime.utcnow()
            moved += 1
        db.session.commit()
        # now safe to delete the (now-empty of active) bin
        db.session.delete(b)
        db.session.commit()
        return jsonify({"ok": True, "checked_out": moved})

    # mode == "delete" — remove bin and every item + photo
    item_count = len(b.items)
    for item in b.items:
        try:
            (UPLOAD_DIR / item.photo_path).unlink(missing_ok=True)
        except Exception:
            pass
    db.session.delete(b)
    db.session.commit()
    return jsonify({"ok": True, "deleted_items": item_count})

# ── items ─────────────────────────────────────────────────────────────────────
@app.route("/api/items", methods=["GET"])
def list_items():
    """
    bin_id=<id>        — items currently in that bin (status=active only)
    bin_id=checked_out  — virtual bin: all checked_out items, regardless of origin
    status=consumed     — shopping list
    (no params)          — all active items (default inventory view)
    """
    bin_id = request.args.get("bin_id")
    status = request.args.get("status")

    q = Item.query

    if status == "consumed":
        q = q.filter_by(status="consumed")
    elif bin_id == "checked_out":
        q = q.filter_by(status="checked_out")
    elif bin_id:
        q = q.filter_by(bin_id=bin_id, status="active")
    else:
        q = q.filter_by(status="active")

    items = q.order_by(Item.created.desc()).all()
    return jsonify([_item_dict(i) for i in items])

@app.route("/api/items", methods=["POST"])
def add_item():
    """Accept a photo (multipart or base64 JSON) + bin_id."""
    # Safely get bin_id regardless of content type (multipart vs JSON)
    if request.content_type and "multipart" in request.content_type:
        json_body = {}
    else:
        json_body = request.get_json(silent=True) or {}
    bin_id = request.form.get("bin_id") or json_body.get("bin_id")
    if not db.session.get(Bin, bin_id):
        return jsonify({"error": "Unknown bin"}), 400

    # --- get image bytes ---
    pil_image = None
    filename  = f"{uuid.uuid4()}.jpg"

    if "photo" in request.files:
        file = request.files["photo"]
        pil_image = _open_upright(file.stream)
    elif request.is_json and request.json.get("photo_b64"):
        raw = base64.b64decode(request.json["photo_b64"])
        pil_image = _open_upright(io.BytesIO(raw))
    else:
        return jsonify({"error": "No photo provided"}), 400

    # resize to max 1200px on longest side to save disk space
    pil_image.thumbnail((1200, 1200), Image.LANCZOS)
    save_path = UPLOAD_DIR / filename
    pil_image.save(save_path, "JPEG", quality=82)

    # --- Should OCR run for this item? ---
    # The Add Item screen sends run_ocr explicitly; if absent we fall back to
    # the user's configured default so the toggle's initial state is honoured.
    raw_ocr = request.form.get("run_ocr", json_body.get("run_ocr", None))
    if raw_ocr is None:
        want_ocr = _get_settings().ocr_default_on
    else:
        want_ocr = str(raw_ocr).lower() in ("1", "true", "yes", "on")

    # A user-typed label wins over anything the models produce, so record its
    # provenance up front.
    typed_label = request.form.get("label","") or json_body.get("label","")

    # --- Save item immediately (no blocking model calls) ---
    item = Item(
        bin_id        = bin_id,
        photo_path    = filename,
        label         = typed_label,
        label_source  = "user" if typed_label else "ml",
        tags          = "",
        embedding     = "",
        notes         = request.form.get("notes","") or json_body.get("notes",""),
        ocr_requested = want_ocr,
        ocr_done      = False,
    )
    db.session.add(item)
    db.session.commit()

    # --- Queue for background processing ---
    if _auto_tag:
        enqueue_item(item.id)

    return jsonify(_item_dict(item)), 201

@app.route("/api/items/<item_id>", methods=["PUT"])
def update_item(item_id):
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    data = request.json
    if "label" in data:
        new_label = (data["label"] or "").strip()
        # A human-typed name is authoritative — record its provenance and teach
        # the vocabulary so similar items can be auto-tagged with it later.
        if new_label and new_label != item.label:
            item.label_source = "user"
            try:
                _learn_label(new_label)
            except Exception as e:
                print(f"[vocab] learn failed: {e}")
        item.label = new_label
    if "notes" in data:
        item.notes = data["notes"]
    if "bin_id" in data:
        if not db.session.get(Bin, data["bin_id"]):
            return jsonify({"error": "Unknown bin"}), 400
        item.bin_id = data["bin_id"]
    db.session.commit()
    return jsonify(_item_dict(item))

@app.route("/api/items/<item_id>", methods=["DELETE"])
def delete_item(item_id):
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    try:
        (UPLOAD_DIR / item.photo_path).unlink(missing_ok=True)
    except Exception:
        pass
    db.session.delete(item)
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/items/<item_id>/retag", methods=["POST"])
def retag_item(item_id):
    """Queue a single item for re-tagging (loads CLIP on demand if needed)."""
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    # clear existing embedding so worker won't skip it
    item.embedding = ""
    item.tags = ""
    db.session.commit()
    # always enqueue and always start the worker regardless of auto_tag setting
    _tag_queue.put(item.id)
    _ensure_worker()
    return jsonify({**_item_dict(item), "queued": True})

# ── checkout / consume / restock ──────────────────────────────────────────────

@app.route("/api/items/<item_id>/checkout", methods=["POST"])
def checkout_item(item_id):
    """Move an item to the virtual Checked Out bin, remembering its origin."""
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    if item.status == "checked_out":
        return jsonify({"error": "Already checked out"}), 400
    if item.status == "consumed":
        return jsonify({"error": "Item is consumed — restock it first"}), 400

    item.origin_bin_id  = item.bin_id
    item.status         = "checked_out"
    item.checked_out_at = datetime.utcnow()
    db.session.commit()
    return jsonify(_item_dict(item))

@app.route("/api/items/<item_id>/return", methods=["POST"])
def return_item(item_id):
    """Return a checked-out item to its original bin."""
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    if item.status != "checked_out":
        return jsonify({"error": "Item is not checked out"}), 400

    # Return to origin bin if it still exists, otherwise leave bin_id unchanged
    if item.origin_bin_id and db.session.get(Bin, item.origin_bin_id):
        item.bin_id = item.origin_bin_id
    item.status         = "active"
    item.origin_bin_id  = ""
    item.checked_out_at = None
    db.session.commit()
    return jsonify(_item_dict(item))

@app.route("/api/items/<item_id>/consume", methods=["POST"])
def consume_item(item_id):
    """Mark an item as used up — moves it to the shopping list."""
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    if item.status == "consumed":
        return jsonify({"error": "Already consumed"}), 400

    # Remember where it lived so "restock to same bin" works later,
    # even if the item was checked out at the time of consumption.
    item.origin_bin_id = item.bin_id if item.status == "active" else (item.origin_bin_id or item.bin_id)
    item.status        = "consumed"
    item.consumed_at   = datetime.utcnow()
    db.session.commit()
    return jsonify(_item_dict(item))

@app.route("/api/items/<item_id>/restock", methods=["POST"])
def restock_item(item_id):
    """
    Restock a consumed item.
    POST {"mode": "same"}
        → reactivate using the same photo/tags/bin, fires WLED find on that bin.
    POST {"mode": "update", "bin_id": "...", "label": "...", "notes": "..."}
        → reactivate into a (possibly different) bin with updated metadata.
        → if a new photo is also uploaded, use /api/items/<id>/restock-photo instead first.
    """
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    if item.status != "consumed":
        return jsonify({"error": "Item is not in the shopping list"}), 400

    data = request.json or {}
    mode = data.get("mode", "same")

    target_bin_id = item.origin_bin_id or item.bin_id
    if mode == "update":
        target_bin_id = data.get("bin_id", target_bin_id)
        if not db.session.get(Bin, target_bin_id):
            return jsonify({"error": "Unknown bin"}), 400
        if "label" in data: item.label = data["label"]
        if "notes" in data: item.notes = data["notes"]

    if not db.session.get(Bin, target_bin_id):
        return jsonify({"error": "Original bin no longer exists — choose a new bin"}), 400

    item.bin_id         = target_bin_id
    item.status         = "active"
    item.origin_bin_id  = ""
    item.consumed_at    = None
    db.session.commit()

    fired_wled = False
    bin_obj = db.session.get(Bin, target_bin_id)
    if bin_obj and bin_obj.wled_enabled and bin_obj.wled_ip:
        try:
            payload = _build_wled_payload(bin_obj, on=True)
            _wled_send(bin_obj.wled_ip, payload)
            fired_wled = True
        except Exception as e:
            print(f"[WLED] restock find error: {e}")

    return jsonify({**_item_dict(item), "wled_fired": fired_wled})

@app.route("/api/items/<item_id>/restock-photo", methods=["POST"])
def restock_item_with_photo(item_id):
    """Restock a consumed item AND replace its photo, then re-queue CLIP tagging."""
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    if item.status != "consumed":
        return jsonify({"error": "Item is not in the shopping list"}), 400

    bin_id = request.form.get("bin_id") or item.origin_bin_id or item.bin_id
    if not db.session.get(Bin, bin_id):
        return jsonify({"error": "Unknown bin"}), 400

    if "photo" not in request.files:
        return jsonify({"error": "No photo provided"}), 400

    file = request.files["photo"]
    pil_image = _open_upright(file.stream)
    pil_image.thumbnail((1200, 1200), Image.LANCZOS)

    # delete old photo, save new one
    try:
        (UPLOAD_DIR / item.photo_path).unlink(missing_ok=True)
    except Exception:
        pass
    filename = f"{uuid.uuid4()}.jpg"
    pil_image.save(UPLOAD_DIR / filename, "JPEG", quality=82)

    item.photo_path     = filename
    item.bin_id          = bin_id
    item.status          = "active"
    item.origin_bin_id   = ""
    item.consumed_at     = None
    item.embedding        = ""   # clear so it gets re-tagged
    item.tags             = ""
    if request.form.get("label"):
        item.label = request.form.get("label")
    if request.form.get("notes"):
        item.notes = request.form.get("notes")
    db.session.commit()

    if _auto_tag:
        enqueue_item(item.id)

    return jsonify(_item_dict(item))

@app.route("/api/items/<item_id>/shopping-delete", methods=["DELETE"])
def shopping_delete_item(item_id):
    """Permanently remove an item from the shopping list (and disk)."""
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    if item.status != "consumed":
        return jsonify({"error": "Item is not in the shopping list"}), 400
    try:
        (UPLOAD_DIR / item.photo_path).unlink(missing_ok=True)
    except Exception:
        pass
    db.session.delete(item)
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/items/<item_id>/rotate", methods=["POST"])
def rotate_item_photo(item_id):
    """Rotate a stored photo by 90° increments and re-queue it for processing.

    A manual fallback for photos that arrived without usable EXIF (some
    in-browser camera captures) or that were saved before the EXIF fix existed.
    Because rotation changes what the models see, we clear the embedding and
    OCR result so the item gets re-tagged against the corrected image.

    Body: {"degrees": 90 | 180 | 270}   (counter-clockwise, PIL convention)
    """
    item = db.session.get(Item, item_id)
    if not item:
        return jsonify({"error": "Not found"}), 404

    degrees = int((request.json or {}).get("degrees", 90)) % 360
    if degrees not in (90, 180, 270):
        return jsonify({"error": "degrees must be 90, 180 or 270"}), 400

    path = UPLOAD_DIR / item.photo_path
    try:
        img = Image.open(path).convert("RGB")
        # expand=True resizes the canvas so a 90/270 turn isn't cropped.
        img = img.rotate(degrees, expand=True)
        img.save(path, "JPEG", quality=82)
    except Exception as e:
        return jsonify({"error": f"Rotate failed: {e}"}), 500

    # The image changed, so previous ML results no longer describe it.
    item.embedding = ""
    item.tags      = ""
    if item.ocr_done:
        item.ocr_done      = False
        item.ocr_requested = True   # keep OCR on if it was on before
    db.session.commit()

    _tag_queue.put(item.id)
    _ensure_worker()
    return jsonify({**_item_dict(item), "rotated": degrees})

@app.route("/api/items/<item_id>/run-ocr", methods=["POST"])
def run_item_ocr(item_id):
    """Queue a single item for an OCR pass (or re-pass)."""
    item = db.session.get(Item, item_id)
    if not item:
        return jsonify({"error": "Not found"}), 404
    item.ocr_requested = True
    item.ocr_done      = False
    db.session.commit()
    _tag_queue.put(item.id)
    _ensure_worker()
    return jsonify({**_item_dict(item), "queued": True})

# ── search ────────────────────────────────────────────────────────────────────
@app.route("/api/search")
def search():
    query   = request.args.get("q","").strip()
    bin_ids = request.args.getlist("bin_id")   # optional filter
    if not query:
        return jsonify({"mode": "none", "results": []})

    # Exclude consumed items from search — they're not in the physical inventory.
    # Checked-out items ARE included, since the user probably still wants to find them.
    all_items = Item.query.filter(Item.status != "consumed")
    if bin_ids:
        all_items = all_items.filter(Item.bin_id.in_(bin_ids))
    all_items = all_items.all()

    # "smart=1" comes from the ✨ button next to the search box: the user is
    # explicitly asking for semantic search, so load CLIP if it isn't resident.
    # Loading takes a few seconds, hence it's opt-in rather than automatic.
    want_smart = request.args.get("smart") in ("1", "true", "yes")
    if want_smart and not _clip_ready:
        ensure_clip_for_query()
    elif _clip_ready:
        # Already warm — extend the window so consecutive searches stay fast.
        _touch_models()

    if _clip_ready:
        # ── Semantic vector search (vectorized with numpy) ────────────────────
        # Rather than looping in pure Python (slow at scale because each item
        # needs a 512-dim dot product), we stack all embeddings into one matrix
        # and let numpy do the whole similarity computation in optimized C.
        import numpy as np

        qemb = np.asarray(embed_text(query), dtype=np.float32)
        qnorm = qemb / (np.linalg.norm(qemb) + 1e-9)

        # Collect embeddings + parallel item list (skip items not yet tagged).
        embs, tagged_items = [], []
        for item in all_items:
            if not item.embedding:
                continue
            embs.append(json.loads(item.embedding))
            tagged_items.append(item)

        if not tagged_items:
            return jsonify({"mode": "semantic", "results": []})

        mat = np.asarray(embs, dtype=np.float32)               # (N, 512)
        mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        scores = mat @ qnorm                                   # (N,) cosine sims

        # Find indices above threshold, sorted best-first, capped at 50.
        # argsort is O(N log N); we only build _item_dict for the survivors,
        # which avoids the per-item origin-bin lookup for the whole table.
        order = np.argsort(-scores)
        out = []
        for idx in order:
            s = float(scores[idx])
            if s < 0.18:        # CLIP cosine threshold for a "reasonable" match
                break           # once below threshold, all rest are too (sorted)
            out.append({**_item_dict(tagged_items[idx]), "score": round(s, 4)})
            if len(out) >= 50:
                break
        return jsonify({"mode": "semantic", "results": out})
    else:
        # ── Keyword fallback (CLIP not loaded) ────────────────────────────────
        ql = query.lower()
        results = []
        for item in all_items:
            haystack = f"{item.label} {item.tags} {item.notes} {item.ocr_text or ''}".lower()
            if ql in haystack:
                results.append(_item_dict(item))
                if len(results) >= 50:
                    break
        return jsonify({"mode": "keyword", "results": results})

# ── WLED API ──────────────────────────────────────────────────────────────────

def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return [int(h[i:i+2], 16) for i in (0, 2, 4)]

def _wled_send(ip, payload, timeout=3):
    """POST a JSON payload to WLED's /json/state endpoint."""
    url = f"http://{ip}/json/state"
    resp = _requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def _wled_get_info(ip, timeout=3):
    """GET /json/info from WLED — returns device info including LED count."""
    url = f"http://{ip}/json/info"
    resp = _requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def _build_wled_payload(b, on=True, override_effect=None):
    """Build a WLED JSON state payload for a bin's *static* config (solid/pulse).

    Note: the chase effect is animated separately by _run_chase() because it
    requires sending many frames over time, which a single static payload
    cannot express.
    """
    rgb = _hex_to_rgb(b.wled_color)
    effect_id = 0   # WLED effect 0 = Solid
    fx_speed  = 128
    fx_int    = 128
    if (override_effect or b.wled_effect) == "pulse":
        effect_id = 45   # WLED effect 45 = Breathe
        fx_speed  = 100
        fx_int    = 200
    seg = {
        "id":   0,
        "start": b.wled_seg_start,
        "stop":  b.wled_seg_start + b.wled_seg_len,
        "on":    on,
        "bri":   b.wled_brightness,
        "col":  [rgb, [0,0,0], [0,0,0]],
        "fx":    effect_id,
        "sx":    fx_speed,
        "ix":    fx_int,
    }
    return {"on": on, "bri": b.wled_brightness, "seg": [seg]}

# Tracks the most recent chase request per IP so a new chase cancels an old one.
_chase_generation = {}
_chase_lock = threading.Lock()

def _run_chase(ip, chase_start, target_start, target_len, comet_size,
               speed_ms, rgb, max_bri, hold_seconds):
    """
    Animate a 'comet' of LEDs travelling from `chase_start` toward the target
    bin zone, growing brighter as it approaches, then hold the target lit.

    Runs in its own thread (fired by wled_find) so the HTTP request returns
    immediately and the animation plays out asynchronously.

    Parameters
    ----------
    ip            : WLED device IP.
    chase_start   : LED index the comet starts from (far end).
    target_start  : first LED of the destination bin zone.
    target_len    : number of LEDs in the destination zone.
    comet_size    : how many LEDs the moving comet spans.
    speed_ms      : delay between animation frames, in milliseconds.
    rgb           : [r,g,b] colour of the chase + final illumination.
    max_bri       : brightness (0-255) the comet reaches at the target.
    hold_seconds  : how long to keep the target lit after the comet arrives.
    """
    # Direction: +1 if the target is "ahead" of the start index, else -1.
    step = 1 if target_start >= chase_start else -1

    # Distance the comet head travels before it lands on the target zone.
    distance = abs(target_start - chase_start)
    if distance == 0:
        distance = 1

    # Claim a generation number; if another chase starts on this IP, ours
    # becomes stale and we abort to avoid two animations fighting.
    with _chase_lock:
        _chase_generation[ip] = _chase_generation.get(ip, 0) + 1
        my_gen = _chase_generation[ip]

    def _is_current():
        with _chase_lock:
            return _chase_generation.get(ip) == my_gen

    try:
        head = chase_start
        for frame in range(distance + 1):
            if not _is_current():
                return  # superseded by a newer chase

            # Comet occupies [head .. head + comet_size) in travel direction.
            seg_lo = min(head, head + step * (comet_size - 1))
            seg_lo = max(0, seg_lo)
            seg_hi = seg_lo + comet_size

            # Brightness ramps from ~20% at the far end up to max at arrival.
            progress = frame / distance               # 0.0 → 1.0
            bri = int(max_bri * (0.2 + 0.8 * progress))

            payload = {
                "on": True,
                "bri": bri,
                "seg": [{
                    "id": 0, "start": seg_lo, "stop": seg_hi,
                    "on": True, "bri": bri,
                    "col": [rgb, [0, 0, 0], [0, 0, 0]],
                    "fx": 0,   # solid — we move the segment ourselves
                }],
            }
            try:
                _wled_send(ip, payload, timeout=2)
            except Exception:
                return  # device went away mid-animation; stop quietly

            head += step
            time.sleep(speed_ms / 1000.0)

        # Arrival: light up the full target zone at max brightness.
        if not _is_current():
            return
        final = {
            "on": True, "bri": max_bri,
            "seg": [{
                "id": 0, "start": target_start, "stop": target_start + target_len,
                "on": True, "bri": max_bri,
                "col": [rgb, [0, 0, 0], [0, 0, 0]], "fx": 0,
            }],
        }
        try:
            _wled_send(ip, final, timeout=2)
        except Exception:
            return

        # Hold, then turn off (unless hold_seconds == 0 meaning "stay on").
        if hold_seconds and hold_seconds > 0:
            for _ in range(int(hold_seconds * 10)):
                if not _is_current():
                    return
                time.sleep(0.1)
            if _is_current():
                try:
                    _wled_send(ip, {"on": False}, timeout=2)
                except Exception:
                    pass
    except Exception as e:
        print(f"[WLED chase] error: {e}")

def _start_chase(b):
    """Kick off a chase animation for bin `b` in a background thread."""
    rgb = _hex_to_rgb(b.wled_color)
    t = threading.Thread(
        target=_run_chase,
        kwargs=dict(
            ip           = b.wled_ip,
            chase_start  = b.wled_chase_start,
            target_start = b.wled_seg_start,
            target_len   = b.wled_seg_len,
            comet_size   = max(1, b.wled_chase_size),
            speed_ms     = max(5, b.wled_chase_speed),
            rgb          = rgb,
            max_bri      = b.wled_brightness,
            hold_seconds = b.wled_duration,
        ),
        daemon=True,
    )
    t.start()

@app.route("/api/wled/settings", methods=["GET"])
def wled_get_settings():
    s = WLEDSettings.query.first()
    if not s:
        return jsonify({"enabled": False, "default_ip": ""})
    return jsonify({"enabled": s.enabled, "default_ip": s.default_ip})

@app.route("/api/wled/settings", methods=["POST"])
def wled_save_settings():
    data = request.json or {}
    s = WLEDSettings.query.first()
    if not s:
        s = WLEDSettings()
        db.session.add(s)
    s.enabled    = bool(data.get("enabled", False))
    s.default_ip = data.get("default_ip", "").strip()
    db.session.commit()
    return jsonify({"ok": True, "enabled": s.enabled, "default_ip": s.default_ip})

@app.route("/api/wled/ping", methods=["POST"])
def wled_ping():
    """Test connectivity and return device info."""
    ip = (request.json or {}).get("ip", "").strip()
    if not ip:
        return jsonify({"error": "No IP provided"}), 400
    try:
        info = _wled_get_info(ip)
        return jsonify({
            "ok":        True,
            "name":      info.get("name", "WLED"),
            "led_count": info.get("leds", {}).get("count", 0),
            "version":   info.get("ver", "?"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/wled/preview", methods=["POST"])
def wled_preview():
    """Send a live preview to WLED without saving — for the jog/setup UI."""
    data = request.json or {}
    ip   = data.get("ip", "").strip()
    if not ip:
        return jsonify({"error": "No IP"}), 400
    rgb = _hex_to_rgb(data.get("color", "#ffffff"))
    bri = max(0, min(255, int(data.get("brightness", 128))))
    start = int(data.get("seg_start", 0))
    length = max(1, int(data.get("seg_len", 5)))
    effect = data.get("effect", "solid")

    # Chase preview: run the animation once using the values currently in the
    # setup sheet, without needing the config to be saved first.
    if effect == "chase":
        try:
            threading.Thread(
                target=_run_chase,
                kwargs=dict(
                    ip           = ip,
                    chase_start  = int(data.get("chase_start", 0)),
                    target_start = start,
                    target_len   = length,
                    comet_size   = max(1, int(data.get("chase_size", 3))),
                    speed_ms     = max(5, int(data.get("chase_speed", 50))),
                    rgb          = rgb,
                    max_bri      = bri,
                    hold_seconds = 3,   # short hold for previews
                ),
                daemon=True,
            ).start()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    effect_id = 45 if effect == "pulse" else 0
    payload = {
        "on":  True,
        "bri": bri,
        "seg": [{
            "id":    0,
            "start": start,
            "stop":  start + length,
            "on":    True,
            "bri":   bri,
            "col":   [rgb, [0,0,0], [0,0,0]],
            "fx":    effect_id,
            "sx":    100,
            "ix":    200,
        }]
    }
    try:
        _wled_send(ip, payload)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/wled/off", methods=["POST"])
def wled_off():
    """Turn off WLED (used after preview / duration expires)."""
    ip = (request.json or {}).get("ip", "").strip()
    if not ip:
        return jsonify({"error": "No IP"}), 400
    try:
        _wled_send(ip, {"on": False})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/wled/find/<bin_id>", methods=["POST"])
def wled_find(bin_id):
    """Illuminate the LEDs for a bin — called by the Find button.

    For solid/pulse we send one static payload. For chase we launch a
    background animation thread and return immediately; the client should
    NOT schedule its own "off" timer in that case (the animation handles it).
    """
    b = db.session.get(Bin, bin_id)
    if not b:
        return jsonify({"error": "Bin not found"}), 404
    if not b.wled_enabled or not b.wled_ip:
        return jsonify({"error": "WLED not configured for this bin"}), 400
    try:
        if b.wled_effect == "chase":
            _start_chase(b)
            # The animation manages its own hold + off, so tell the client
            # not to run a duration timer (chase_managed=True).
            return jsonify({"ok": True, "duration": 0, "ip": b.wled_ip,
                            "chase_managed": True})
        payload = _build_wled_payload(b, on=True)
        _wled_send(b.wled_ip, payload)
        return jsonify({"ok": True, "duration": b.wled_duration, "ip": b.wled_ip})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/wled/find-item/<item_id>", methods=["POST"])
def wled_find_item(item_id):
    """
    Illuminate the LEDs for an item's home bin — resolves correctly whether
    the item is active (bin_id) or checked out (origin_bin_id).
    """
    item = db.session.get(Item, item_id)
    if not item:
        return jsonify({"error": "Item not found"}), 404
    target_bin_id = item.origin_bin_id if item.status == "checked_out" else item.bin_id
    if item.status == "consumed":
        target_bin_id = item.origin_bin_id or item.bin_id
    return wled_find(target_bin_id)

# ── static photo serving ──────────────────────────────────────────────────────
@app.route("/photos/<filename>")
def serve_photo(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# ── single-page app ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    # Send file directly — bypasses Jinja templating which would HTML-encode JS strings
    return send_from_directory(BASE_DIR / "templates", "index.html")

def _item_dict(item):
    b = item.bin
    origin = db.session.get(Bin, item.origin_bin_id) if item.origin_bin_id else None
    return {
        "id":              item.id,
        "bin_id":          item.bin_id,
        "bin_name":        b.name if b else "",
        "bin_color":       b.color if b else "#6366f1",
        "bin_wled":        b.wled_enabled if b else False,
        "photo_url":       f"/photos/{item.photo_path}",
        "label":           item.label,
        "tags":            item.tags,
        "notes":           item.notes,
        "created":         item.created.isoformat(),
        "status":          item.status or "active",
        "origin_bin_id":   item.origin_bin_id or "",
        "origin_bin_name": origin.name if origin else "",
        "origin_bin_wled": origin.wled_enabled if origin else False,
        "checked_out_at":  item.checked_out_at.isoformat() if item.checked_out_at else None,
        "consumed_at":     item.consumed_at.isoformat() if item.consumed_at else None,
        "label_source":    item.label_source or "ml",
        "ocr_text":        item.ocr_text or "",
        "ocr_requested":   bool(item.ocr_requested),
        "ocr_done":        bool(item.ocr_done),
    }

def _bin_dict(b):
    return {
        "id":               b.id,
        "name":             b.name,
        "location":         b.location,
        "color":            b.color,
        "item_count":       len(b.items),
        "created":          b.created.isoformat(),
        "wled_enabled":     b.wled_enabled,
        "wled_ip":          b.wled_ip,
        "wled_seg_start":   b.wled_seg_start,
        "wled_seg_len":     b.wled_seg_len,
        "wled_color":       b.wled_color,
        "wled_brightness":  b.wled_brightness,
        "wled_effect":      b.wled_effect,
        "wled_duration":    b.wled_duration,
        "wled_chase_start": b.wled_chase_start,
        "wled_chase_size":  b.wled_chase_size,
        "wled_chase_speed": b.wled_chase_speed,
    }

def _migrate_db():
    """Add any missing columns to existing tables (safe to run on every start)."""
    import sqlalchemy as sa
    with db.engine.connect() as conn:
        # ── bin table WLED columns ────────────────────────────────────────────
        existing_bin = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(bin)"))}
        bin_migrations = [
            ("wled_enabled",    "BOOLEAN    DEFAULT 0"),
            ("wled_ip",         "VARCHAR(64) DEFAULT ''"),
            ("wled_seg_start",  "INTEGER    DEFAULT 0"),
            ("wled_seg_len",    "INTEGER    DEFAULT 5"),
            ("wled_color",      "VARCHAR(20) DEFAULT '#ffffff'"),
            ("wled_brightness", "INTEGER    DEFAULT 128"),
            ("wled_effect",     "VARCHAR(20) DEFAULT 'solid'"),
            ("wled_duration",   "INTEGER    DEFAULT 10"),
            ("wled_chase_start","INTEGER    DEFAULT 0"),
            ("wled_chase_size", "INTEGER    DEFAULT 3"),
            ("wled_chase_speed","INTEGER    DEFAULT 50"),
        ]
        for col, defn in bin_migrations:
            if col not in existing_bin:
                conn.execute(sa.text(f"ALTER TABLE bin ADD COLUMN {col} {defn}"))
                print(f"[DB] Migrated: bin.{col}")

        # ── item table checkout/consume columns ───────────────────────────────
        existing_item = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(item)"))}
        item_migrations = [
            ("status",         "VARCHAR(20) DEFAULT 'active'"),
            ("origin_bin_id",  "VARCHAR(36) DEFAULT ''"),
            ("checked_out_at", "DATETIME    DEFAULT NULL"),
            ("consumed_at",    "DATETIME    DEFAULT NULL"),
            ("label_source",   "VARCHAR(20) DEFAULT 'ml'"),
            ("ocr_text",       "TEXT       DEFAULT ''"),
            ("ocr_requested",  "BOOLEAN    DEFAULT 0"),
            ("ocr_done",       "BOOLEAN    DEFAULT 0"),
        ]
        # AppSettings gains columns over time too.
        existing_set = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(app_settings)"))}
        for col, defn in [("model_keepalive_sec", "INTEGER DEFAULT 60")]:
            if existing_set and col not in existing_set:
                conn.execute(sa.text(f"ALTER TABLE app_settings ADD COLUMN {col} {defn}"))
                print(f"[DB] Migrated: app_settings.{col}")
        for col, defn in item_migrations:
            if col not in existing_item:
                conn.execute(sa.text(f"ALTER TABLE item ADD COLUMN {col} {defn}"))
                print(f"[DB] Migrated: item.{col}")

        conn.commit()

if __name__ == "__main__":
    with app.app_context():
        db.create_all()      # create any brand-new tables
        _migrate_db()        # add any missing columns to existing tables
        n = _refresh_candidate_labels()   # static dictionary + learned words
        print(f"[vocab] {n} candidate labels active")
    _scheduler.start()
    print("\n🗂  BINventory running at http://0.0.0.0:5000\n")
    print("   CLIP model loads on demand — RAM stays free until tagging starts\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
