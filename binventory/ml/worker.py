"""Background tagging worker.

Pulls item ids off a queue and runs every registered analyzer that wants them.
The worker owns three things analyzers deliberately do not:

  1. **Model lifecycle** — it loads only the heavy analyzers this batch needs,
     so an OCR-only re-run never pulls CLIP's 1.4 GB into memory.
  2. **Name arbitration** — analyzers propose names with a rank; the worker
     applies the precedence rule (user > ocr > ml) in one place.
  3. **Persistence** — analyzers return results, the worker writes them.

When the queue drains it hands off to the keep-alive reaper rather than
unloading immediately, so tagging then searching doesn't pay the load cost twice.
"""
import json
import queue as _queue_module
import threading
import time

from PIL import Image

from .. import events, vocabulary
from ..config import UPLOAD_DIR
from ..db import db
from ..models import Item
from .runtime import get_app, job_reset, job_tick, touch_models

tag_queue = _queue_module.Queue()
_worker_lock = threading.Lock()
_worker_running = False
_auto_tag = True
_currently_tagging = None


def worker_running():
    return _worker_running


def currently_tagging():
    return _currently_tagging


def get_auto_tag():
    return _auto_tag


def set_auto_tag(value):
    global _auto_tag
    _auto_tag = bool(value)
    return _auto_tag


def ensure_worker():
    """Start the worker thread if it isn't already running."""
    global _worker_running
    with _worker_lock:
        if _worker_running:
            return
        _worker_running = True
    threading.Thread(target=_tag_worker, daemon=True).start()


def _apply_result(item, result, analyzer_id, plugin_id=None):
    """Merge one analyzer's output into the item.

    This is where label-source precedence (backlog C1) is enforced. A proposed
    name only lands if its source ranks >= the stored one's. With the default
    order (user-configurable — see binventory/naming.py):

        CLIP names an item          -> label_source = "ml"
        OCR later reads a part no.  -> outranks "ml", name replaced
        user renames it             -> outranks "ocr", name replaced
        CLIP re-runs                -> "ml" ranks lowest, name left alone

    When a higher-ranked name displaces OCR text, the displaced text is kept as
    a tag so it stays searchable.
    """
    from .. import naming

    changed = False

    if result.remove_tags:
        drop = {t.strip().lower() for t in result.remove_tags if t}
        kept = [t.strip() for t in (item.tags or "").split(",")
                if t.strip() and t.strip().lower() not in drop]
        item.tags = ", ".join(kept)
        changed = True

    if result.tags:
        item.tags = vocabulary.merge_tags(item.tags, *result.tags)
        changed = True

    if result.embedding is not None:
        # float32 BLOB — see services/embeddings_store.
        from ..services import embeddings_store
        embeddings_store.set_embedding(item, result.embedding)
        changed = True

    if plugin_id:
        # A plugin's data goes to its own namespaced store, never onto core
        # columns — otherwise it could set `label` directly and bypass the
        # precedence rule below.
        if result.data:
            from ..services.storage import ExtensionStore
            store = ExtensionStore(plugin_id)
            for key, value in result.data.items():
                store.set_item(item.id, key, value)
    else:
        # Free-form data a core analyzer wants persisted on core fields.
        for key, value in (result.data or {}).items():
            if hasattr(item, key):
                setattr(item, key, value)
                changed = True

    if result.name:
        if naming.may_replace(item.label_source, item.label, result.name_source):
            # Preserve a displaced OCR reading as a tag rather than losing it —
            # unless another OCR read replaces it: a region read (E5) supersedes
            # the whole-photo text outright, name and tag both.
            OCR = ("ocr", "ocr_region")
            if item.label and (item.label_source or "") in OCR and result.name_source not in OCR:
                item.tags = vocabulary.merge_tags(
                    item.tags, " ".join(item.label.replace(",", " ").split()))
            item.label = result.name
            item.label_source = result.name_source
            changed = True

    return changed


def _tag_worker():
    """Drain the queue, running the analyzers each item needs."""
    global _worker_running, _currently_tagging

    from ..analysis import enabled_analyzers
    from ..analysis import get as registry_get
    from ..services.settings import get_settings

    print("[worker] Starting …")
    job_reset(tag_queue.qsize())

    analyzers = enabled_analyzers()
    loaded = set()          # ids of heavy analyzers currently in memory

    if not analyzers:
        print("[worker] No analyzers available — nothing to do")

    while True:
        try:
            item_id = tag_queue.get(timeout=3)
        except _queue_module.Empty:
            break

        started = time.time()
        _currently_tagging = item_id
        try:
            with get_app().app_context():
                item = db.session.get(Item, item_id)
                if not item:
                    continue

                settings = get_settings()
                settings_dict = {
                    "ocr_as_tags":      settings.ocr_as_tags,
                    "ocr_truncate":     settings.ocr_truncate,
                    "ocr_truncate_len": settings.ocr_truncate_len,
                }
                img_path = UPLOAD_DIR / item.photo_path

                from ..analysis.base import AnalysisContext
                wanted = [a for a in analyzers if _safe_wants(a, item)]
                if not wanted:
                    continue

                # Open the photo once and share it across analyzers.
                try:
                    image = Image.open(img_path).convert("RGB")
                except Exception as e:
                    print(f"[worker] cannot open {item_id}: {e}")
                    continue

                ctx = AnalysisContext(image_path=img_path, image=image,
                                      item=item, settings=settings_dict)
                changed = False
                for a in wanted:
                    # A plugin disabled mid-batch has been unregistered; stop
                    # running it rather than waiting for the batch to drain.
                    if registry_get(a.id) is not a:
                        continue
                    # Load heavy analyzers lazily — only if something needs them.
                    if a.cost == "heavy" and a.id not in loaded:
                        try:
                            if not a.load():
                                print(f"[worker] {a.id} unavailable — skipping")
                                continue
                            loaded.add(a.id)
                        except Exception as e:
                            print(f"[worker] {a.id} failed to load: {e}")
                            continue
                    try:
                        result = a.analyze(ctx)
                    except Exception as e:
                        # One broken analyzer must not lose the whole item.
                        print(f"[worker] {a.id} raised on {item_id}: {e}")
                        continue
                    if not result or not result.ok:
                        if result and result.error:
                            print(f"[worker] {a.id}: {result.error}")
                        continue
                    if _apply_result(item, result, a.id, getattr(a, "plugin_id", None)):
                        changed = True
                        print(f"[worker] {a.id} -> {item_id}: "
                              f"{', '.join(result.tags[:4]) if result.tags else '(no tags)'}")

                if changed:
                    db.session.commit()
                    events.emit(events.ITEM_TAGGED, item_id=item.id,
                                analyzers=[a.id for a in wanted])
        except Exception as e:
            print(f"[worker] Error on {item_id}: {e}")
        finally:
            _currently_tagging = None
            job_tick(time.time() - started)
            tag_queue.task_done()

    # Keep models warm briefly rather than unloading immediately.
    with _worker_lock:
        _worker_running = False
    if loaded:
        touch_models()
        print("[worker] Done — models kept warm for the keep-alive window")
    else:
        print("[worker] Done — idle")


def _safe_wants(analyzer, item):
    """`wants()` is analyzer-supplied, so treat a raise as 'no'."""
    from ..analysis.base import AnalysisContext
    try:
        return analyzer.wants(AnalysisContext(image_path=None, image=None, item=item))
    except Exception as e:
        print(f"[worker] {analyzer.id}.wants() failed: {e}")
        return False


def enqueue_item(item_id):
    """Queue an item. Starts the worker if auto-tag is on."""
    tag_queue.put(item_id)
    if _auto_tag:
        ensure_worker()


def enqueue_all_untagged():
    """Queue everything needing CLIP tagging or a requested OCR pass."""
    with get_app().app_context():
        items = Item.query.filter(
            (Item.embedding_blob == None) |
            ((Item.ocr_requested == True) & (Item.ocr_done == False))
        ).all()
        count = len(items)
        for item in items:
            tag_queue.put(item.id)
    if count:
        print(f"[worker] Queued {count} items needing processing")
        ensure_worker()
    return count
