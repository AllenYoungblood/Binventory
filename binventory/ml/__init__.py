"""On-device machine learning: CLIP tagging, OCR, and the background worker.

MODEL LIFECYCLE — the thing to understand before changing anything here.

CLIP is ~1.4 GB and OCR ~0.7 GB, which is too much to keep resident on a home
server. So models are loaded on demand, used, then unloaded:

    request -> _touch_models() extends an expiry timestamp
    worker  -> loads only the models this batch actually needs
    reaper  -> unloads once nothing has touched them for the keep-alive window

Loading is expensive twice over: the weights, *and* re-encoding the entire tag
dictionary. That is why the keep-alive window exists — without it, two searches
in a row would pay the cost twice.
"""
from .runtime import (                      # noqa: F401
    load_clip, unload_clip, get_clip, clip_ready,
    load_ocr, unload_ocr, run_ocr, ocr_ready,
    ensure_clip_for_query, touch_models, status_snapshot,
    job_snapshot, bind_app,
)
from .embeddings import embed_image, embed_text, top_labels, invalidate_label_cache  # noqa: F401
from .worker import (                       # noqa: F401
    enqueue_item, ensure_worker, enqueue_all_untagged, tag_queue,
    worker_running, currently_tagging,
    get_auto_tag, set_auto_tag,
)
