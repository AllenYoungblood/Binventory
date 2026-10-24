"""Bin operations: list, create, edit, nest/unpack, light, and the secure delete.

Shared by the HTTP API and the MCP server. See services/items.py for the
`actor` convention.
"""
from datetime import datetime

from .. import config, events, lighting
from ..db import db
from ..models import Bin, Item
from . import catalog, thumbnails
from .errors import Invalid, NotFound, UNSET

CHECKED_OUT = "checked_out"          # id of the virtual Checked Out bin


def get(bin_id, message="Not found"):
    b = db.session.get(Bin, bin_id) if bin_id else None
    if not b:
        raise NotFound(message)
    return b


def listing(scope="top", query=""):
    """Bin dicts for the Bins tab and pickers.

    scope="top": only top-level bins (a bin whose parent no longer exists counts
    as top-level, so it can never become invisible); "all": every bin, flat.
    query filters by name, location or path. The virtual Checked Out bin is
    pinned first when it has items and there is no query.
    """
    query = (query or "").strip().lower()
    bins = Bin.query.order_by(Bin.created).all()
    if scope == "top" and not query:
        bins = [b for b in bins if not b.parent_bin_id or not db.session.get(Bin, b.parent_bin_id)]
    result = [catalog.bin_dict(b) for b in bins]
    if query:
        result = [d for d in result
                  if query in d["name"].lower()
                  or query in (d["location"] or "").lower()
                  or query in (d["path"] or "").lower()]
    checked_out = Item.query.filter_by(status="checked_out").count()
    if checked_out and not query:
        result.insert(0, {
            "id": CHECKED_OUT, "name": "Checked Out", "location": "", "color": "#fbbf24",
            "item_count": checked_out, "created": "", "is_virtual": True,
            "lighting_enabled": False, "parent_bin_id": "", "parent_bin_name": "",
            "child_count": 0, "total_count": checked_out, "path": "",
        })
    return result


def create(name, location="", color="#6366f1", parent_id=None, actor="user"):
    name = (name or "").strip()
    if not name:
        raise Invalid("Name required")
    if Bin.query.filter_by(name=name).first():
        raise Invalid("A bin with that name already exists")
    if parent_id:
        get(parent_id, "Parent bin not found")
    b = Bin(name=name, location=location or "", color=color or "#6366f1",
            parent_bin_id=parent_id or "")
    db.session.add(b)
    db.session.commit()
    events.emit(events.BIN_CREATED, bin_id=b.id, name=b.name, actor=actor)
    return b


def _check_parent(bin_id, parent_id):
    if parent_id == bin_id:
        raise Invalid("A bin cannot contain itself")
    if not db.session.get(Bin, parent_id):
        raise Invalid("Parent bin not found")
    # The proposed parent must not already be somewhere below this bin.
    if parent_id in catalog.descendant_bin_ids(bin_id):
        raise Invalid("Cannot nest a bin inside one of its own sub-bins")


def update(bin_id, *, name=UNSET, location=UNSET, color=UNSET, parent_id=UNSET, actor="user"):
    """Edit a bin. Lighting is configured separately (services/lighting_config)."""
    b = get(bin_id)
    if parent_id is not UNSET and parent_id:
        # Same guards as nest(); this path is reachable from the edit sheet.
        if parent_id == bin_id or parent_id in catalog.descendant_bin_ids(bin_id):
            raise Invalid("Invalid parent (would create a loop)")
        if not db.session.get(Bin, parent_id):
            raise Invalid("Parent bin not found")
    if name is not UNSET:
        b.name = (name or "").strip()
    if location is not UNSET:
        b.location = location
    if color is not UNSET:
        b.color = color
    if parent_id is not UNSET:
        b.parent_bin_id = parent_id or ""
    db.session.commit()
    events.emit(events.BIN_UPDATED, bin_id=b.id, actor=actor)
    return b


def nest(bin_id, parent_id, actor="user"):
    """Put a bin inside another; an empty parent detaches it (same as unpack)."""
    b = get(bin_id)
    if parent_id:
        _check_parent(bin_id, parent_id)
    b.parent_bin_id = parent_id or ""
    db.session.commit()
    events.emit(events.BIN_UPDATED, bin_id=b.id, actor=actor)
    return b


def unpack(bin_id, actor="user"):
    """Detach a bin from its parent. Nothing physical moves."""
    b = get(bin_id)
    if not b.parent_bin_id:
        raise Invalid("This bin is not nested inside anything")
    b.parent_bin_id = ""
    db.session.commit()
    events.emit(events.BIN_UPDATED, bin_id=b.id, actor=actor)
    return b


def unpack_all(bin_id, deep=False):
    """Detach every bin directly inside this one (deep: at any depth). Returns count."""
    get(bin_id)
    targets = (catalog.descendant_bin_ids(bin_id) if deep
               else [c.id for c in catalog.child_bins(bin_id)])
    freed = 0
    for cid in targets:
        child = db.session.get(Bin, cid)
        if child and child.parent_bin_id:
            child.parent_bin_id = ""
            freed += 1
    db.session.commit()
    return freed


def light(bin_id):
    """Fire the bin's lighting (the Find button). Raises Invalid if it can't."""
    b = get(bin_id, "Bin not found")
    # Through the actuator, which reads the bin's lighting config (layout, 2D
    # zone, linked controller) and picks the right payload shape.
    result = lighting.highlight_bin(b)
    if not result.get("ok"):
        raise Invalid(result.get("error", "Lighting failed"))
    return result


def delete(bin_id, confirm, contents, actor="user"):
    """Delete a bin. `confirm` must be "delete"; `contents` decides the items' fate.

    contents="delete":   delete the bin and its items (+ photos)
    contents="checkout": check every active item out first so it survives in
                         the Checked Out list, then delete the bin
    Either way, shopping-list items survive with their origin cleared (restock
    then asks for a bin, C6), and nested bins move up to this bin's parent.
    Returns {"checked_out": n} or {"deleted_items": n}.
    """
    b = get(bin_id)
    if str(confirm or "").strip().lower() != "delete":
        raise Invalid("Confirmation text did not match")
    if contents not in ("delete", "checkout"):
        raise Invalid("Must choose what to do with contents")

    if contents == "checkout":
        for child in catalog.child_bins(bin_id):
            child.parent_bin_id = b.parent_bin_id or ""
        db.session.commit()
        # bin_id can't be null, so surviving items need a holding bin while
        # they are checked out.
        holding = Bin.query.filter(Bin.id != bin_id).first()
        if not holding:
            raise Invalid("Cannot check out — no other bin exists to hold items. "
                          "Create another bin first or choose delete.")
        moved = 0
        for item in list(b.items):
            if item.status == "consumed":
                # Must leave this bin's `items` collection, or the delete-orphan
                # cascade removes it with the bin. Origin is wiped, not
                # repointed at the arbitrary holding bin.
                item.bin_id, item.origin_bin_id = holding.id, ""
                continue
            tag = f"[was in {b.name}]"
            if tag not in (item.notes or ""):
                item.notes = (item.notes + "  " if item.notes else "") + tag
            item.bin_id = item.origin_bin_id = holding.id
            item.status = "checked_out"
            item.checked_out_at = datetime.utcnow()
            moved += 1
        db.session.commit()
        db.session.delete(b)
        db.session.commit()
        events.emit(events.BIN_DELETED, bin_id=bin_id, actor=actor)
        return {"checked_out": moved}

    # Children re-parent to this bin's own parent (or top level).
    for child in catalog.child_bins(bin_id):
        child.parent_bin_id = b.parent_bin_id or ""
    db.session.commit()

    # C6: items elsewhere still pointing at this bin as their ORIGIN get the
    # pointer wiped, so restock/return can say plainly the bin is gone.
    for it in Item.query.filter(Item.origin_bin_id == bin_id).all():
        if it.bin_id != bin_id:
            it.origin_bin_id = ""
    db.session.commit()

    holding = Bin.query.filter(Bin.id != bin_id).first()
    if not holding and any(i.status == "consumed" for i in b.items):
        raise Invalid("This bin holds shopping-list items and no other bin exists "
                      "to keep them in. Create another bin first.")
    item_count = len(b.items)
    deleted_ids = []
    for item in list(b.items):
        if item.status == "consumed":
            # Shopping-list entries belong to the list, not the bin.
            item.bin_id, item.origin_bin_id = holding.id, ""
            item_count -= 1
            continue
        deleted_ids.append(item.id)
        try:
            (config.UPLOAD_DIR / item.photo_path).unlink(missing_ok=True)
            thumbnails.remove(item.photo_path)
        except Exception:
            pass
    db.session.commit()
    db.session.delete(b)
    db.session.commit()
    for iid in deleted_ids:
        events.emit(events.ITEM_DELETED, item_id=iid, actor=actor)
    events.emit(events.BIN_DELETED, bin_id=bin_id, actor=actor)
    return {"deleted_items": item_count}
