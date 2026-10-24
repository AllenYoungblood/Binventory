"""Bin and item queries, tree navigation, and serialisation.

The `*_dict` functions are the canonical JSON shape for bins and items. Both the
HTTP API and any future non-HTTP consumer (MCP, CLI) should use them so there is
exactly one definition of what a bin or item looks like on the wire.
"""
from ..config import UPLOAD_DIR
from ..db import db
from ..models import Bin, Item
from . import thumbnails
from .lighting_config import lighting_enabled

def item_dict(item):
    b = item.bin
    origin = db.session.get(Bin, item.origin_bin_id) if item.origin_bin_id else None
    return {
        "id":              item.id,
        "bin_id":          item.bin_id,
        "bin_name":        b.name if b else "",
        "bin_color":       b.color if b else "#6366f1",
        "bin_wled":        lighting_enabled(b) if b else False,
        "photo_url":       f"/photos/{item.photo_path}",
        "thumb_url":       thumbnails.url_for(item.photo_path),
        "label":           item.label,
        "tags":            item.tags,
        "notes":           item.notes,
        "created":         item.created.isoformat(),
        "status":          item.status or "active",
        "origin_bin_id":   item.origin_bin_id or "",
        "origin_bin_name": origin.name if origin else "",
        "origin_bin_wled": lighting_enabled(origin) if origin else False,
        "checked_out_at":  item.checked_out_at.isoformat() if item.checked_out_at else None,
        "consumed_at":     item.consumed_at.isoformat() if item.consumed_at else None,
        "label_source":    item.label_source or "ml",
        "quantity":        item.quantity,
        "ocr_text":        item.ocr_text or "",
        "ocr_requested":   bool(item.ocr_requested),
        "ocr_done":        bool(item.ocr_done),
        "ocr_source":      item.ocr_source or "",
        # Where this item's bin sits in the tree. The UI uses bin_path to show
        # "in <sub-bin>" on cards when viewing a parent bin.
        "bin_path":        bin_path(b) if b else "",
        "bin_parent_id":   (b.parent_bin_id or "") if b else "",
    }

# ── Bin nesting helpers ───────────────────────────────────────────────────────
# Nesting is stored as a simple parent pointer. These helpers walk that pointer
# in both directions. Depth is expected to be shallow (a few levels at most), so
# recursive walks are cheap; every one is cycle-guarded anyway.

def child_bins(bin_id):
    """Direct children of a bin (one level down)."""
    if not bin_id:
        return []
    return Bin.query.filter_by(parent_bin_id=bin_id).order_by(Bin.name).all()

def descendant_bin_ids(bin_id, _seen=None):
    """Every bin nested under `bin_id`, at any depth. Excludes the bin itself.

    The `_seen` set guards against a corrupted parent chain forming a loop,
    which would otherwise recurse forever.
    """
    if _seen is None:
        _seen = set()
    out = []
    for child in child_bins(bin_id):
        if child.id in _seen:
            continue
        _seen.add(child.id)
        out.append(child.id)
        out.extend(descendant_bin_ids(child.id, _seen))
    return out

def ancestor_bin_ids(bin_id, _seen=None):
    """Every bin above `bin_id`, walking up to the root."""
    if _seen is None:
        _seen = set()
    out = []
    cur = db.session.get(Bin, bin_id) if bin_id else None
    while cur and cur.parent_bin_id:
        if cur.parent_bin_id in _seen:
            break
        _seen.add(cur.parent_bin_id)
        out.append(cur.parent_bin_id)
        cur = db.session.get(Bin, cur.parent_bin_id)
    return out

def bin_path(b):
    """Human-readable location path, e.g. 'Garage Shelf / Red Tote'."""
    names = []
    for anc_id in ancestor_bin_ids(b.id):
        anc = db.session.get(Bin, anc_id)
        if anc:
            names.append(anc.name)
    return " / ".join(reversed(names))

def bin_total_count(b):
    """Item count including everything in nested bins."""
    ids = [b.id] + descendant_bin_ids(b.id)
    return Item.query.filter(Item.bin_id.in_(ids), Item.status == "active").count()

def bin_dict(b):
    return {
        "id":               b.id,
        "name":             b.name,
        "location":         b.location,
        "color":            b.color,
        # Active only — consumed (shopping list) and checked-out items keep a
        # bin_id but aren't on the shelf, and the bin view doesn't list them.
        "item_count":       sum(1 for i in b.items if i.status == "active"),
        "created":          b.created.isoformat(),
        # Lighting detail lives in bin_lighting (GET /api/bins/<id>/lighting);
        # the bin only reports whether Find will do anything.
        "lighting_enabled": lighting_enabled(b),
        # ── Nesting ──────────────────────────────────────────────────────
        # item_count      = items directly in this bin
        # total_count     = items here PLUS everything in nested bins
        # child_count     = how many bins are nested directly inside
        # parent_bin_id/name = where this bin lives, if nested
        # path            = readable ancestry, e.g. "Garage Shelf / Red Tote"
        "parent_bin_id":   b.parent_bin_id or "",
        "parent_bin_name": (db.session.get(Bin, b.parent_bin_id).name
                            if b.parent_bin_id and db.session.get(Bin, b.parent_bin_id) else ""),
        "child_count":     len(child_bins(b.id)),
        "total_count":     bin_total_count(b),
        "path":            bin_path(b),
    }
