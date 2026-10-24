"""Queueing items for analysis (phase 5, backlog D1).

Five endpoints used to do variations of the same thing — retag one item, OCR one
item, tag everything, re-run OCR everywhere, process pending. They differed only
in *which* items to queue and whether to force work that was already done.

That distinction is now two parameters. The worker already decides per item
which analyzers want it, so the CLIP-vs-OCR split never needed to exist in the
API at all.
"""
from ..db import db
from ..models import Item
from .. import ml

# Scopes a caller can ask for.
SCOPES = ("item", "bin", "pending", "all")


def _needs_work(query):
    """Items missing an embedding, or with OCR requested but not yet run.

    Must match embeddings_store.has_embedding(), which CLIP's wants() uses —
    when the two disagreed, "Tag All" queued items the worker then skipped.
    """
    return query.filter(
        (Item.embedding_blob == None) |
        ((Item.ocr_requested == True) & (Item.ocr_done == False))
    )


def queue(scope="pending", target_id=None, force=False, want_ocr=None):
    """Queue items for analysis.

    scope     "item"    a single item (target_id required)
              "bin"     everything in a bin, including nested bins
              "pending" anything not yet processed  (default)
              "all"     the entire library
    force     re-run even if already processed — clears prior results so the
              analyzers' `wants()` returns True again
    want_ocr  True/False to set the per-item OCR flag while queueing; None
              leaves each item's existing preference alone

    Returns the number of items queued.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")

    if scope == "item":
        if not target_id:
            raise ValueError("target_id is required for scope='item'")
        items = [db.session.get(Item, target_id)]
        items = [i for i in items if i]
    elif scope == "bin":
        if not target_id:
            raise ValueError("target_id is required for scope='bin'")
        from .catalog import descendant_bin_ids
        ids = [target_id] + descendant_bin_ids(target_id)
        items = Item.query.filter(Item.bin_id.in_(ids)).all()
    elif scope == "all":
        items = Item.query.all()
    else:                                   # pending
        items = _needs_work(Item.query).all()

    for item in items:
        if want_ocr is not None:
            item.ocr_requested = bool(want_ocr)
            if want_ocr:
                item.ocr_done = False       # ask for a fresh read
        if force:
            # Clearing results is what makes the analyzers want the item again;
            # the worker's precedence rules then decide what may overwrite what.
            item.embedding_blob = None
            item.ocr_done = False
    if items:
        db.session.commit()

    for item in items:
        ml.tag_queue.put(item.id)
    if items:
        ml.ensure_worker()
    return len(items)


def pending_count():
    return _needs_work(Item.query).count()
