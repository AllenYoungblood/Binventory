"""Item operations: create, edit, move, lifecycle, photo changes, delete.

The HTTP API, the MCP server and any other caller go through these functions,
so validation, events and side effects (thumbnails, re-analysis, lighting)
happen identically whoever asks. Functions return model objects; callers
serialise with `catalog.item_dict`.

`actor` records who made a change ("user" for the app's own UI, "agent" for the
AI assistant) and travels on every event, so subscribers such as the activity
log can tell them apart.
"""
import uuid
from datetime import datetime

from PIL import Image

from .. import config, events, images, lighting, ml, naming, vocabulary
from ..db import db
from ..models import Bin, Item
from . import catalog, thumbnails
from . import settings as settings_svc
from .errors import Conflict, Failed, Invalid, NotFound, UNSET

MAX_EDGE = 1200          # stored photos are downscaled to this on the long side


# ── lookups ──────────────────────────────────────────────────────────────────

def get(item_id):
    item = db.session.get(Item, item_id) if item_id else None
    if not item:
        raise NotFound("Not found")
    return item


def _bin(bin_id, message="Unknown bin"):
    b = db.session.get(Bin, bin_id) if bin_id else None
    if not b:
        raise Invalid(message)
    return b


def query(bin_id=None, status=None, direct=False):
    """The query behind every item list, in display order.

    bin_id=<id>          items in that bin AND every bin nested under it
                         (direct=True: only the bin itself), active only
    bin_id="checked_out" the virtual Checked Out bin
    status="consumed"    the shopping list
    neither              all active items
    """
    q = Item.query
    order = [Item.created.desc()]
    if status == "consumed":
        q = q.filter_by(status="consumed")
    elif bin_id == "checked_out":
        q = q.filter_by(status="checked_out")
    elif bin_id:
        ids = [bin_id] if direct else [bin_id] + catalog.descendant_bin_ids(bin_id)
        q = q.filter(Item.bin_id.in_(ids), Item.status == "active")
        if len(ids) > 1:
            # Parent-bin view: the bin's own items first, then grouped by
            # sub-bin — ordered here so it holds across pages.
            q = q.join(Bin, Item.bin_id == Bin.id)
            order = [db.case((Item.bin_id == bin_id, 0), else_=1), Bin.name, Item.created.desc()]
    else:
        q = q.filter_by(status="active")
    return q.order_by(*order)


def page(q, limit, offset=0):
    """(items, total) for one page. limit is clamped to 1–500."""
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    total = q.order_by(None).count()
    return q.offset(offset).limit(limit).all(), total, offset, limit


# ── photos ───────────────────────────────────────────────────────────────────

def _store_photo(pil_image):
    """Downscale and save a new photo; returns its filename. Builds the thumbnail."""
    filename = f"{uuid.uuid4()}.jpg"
    pil_image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    pil_image.save(config.UPLOAD_DIR / filename, "JPEG", quality=82)
    thumbnails.ensure(filename)       # so the item's first appearance in a grid is fast
    return filename


def _discard_photo(filename):
    if not filename:
        return
    try:
        (config.UPLOAD_DIR / filename).unlink(missing_ok=True)
        thumbnails.remove(filename)
    except Exception:
        pass


def open_image(stream):
    """EXIF-corrected PIL image from a file-like object."""
    return images.open_upright(stream)


# ── create / edit ────────────────────────────────────────────────────────────

def create(bin_id, image, label="", notes="", want_ocr=None, actor="user",
           barcode=None, barcode_kind="other", barcode_source="scanned", force_barcode=False):
    """Save a photographed item and queue it for analysis. Never blocks on a model.

    `barcode` attaches a code (scanned or typed at Add Item) as the item's
    local identifier; analysis runs exactly as without it. If another bin or
    item already has that code, Conflict is raised *before* anything is
    saved, so the user can decide and resend with force_barcode=True.
    """
    from . import codes as codes_svc
    _bin(bin_id)
    if image is None:
        raise Invalid("No photo provided")
    barcode = (barcode or "").strip()
    if barcode and not force_barcode:
        others = codes_svc.conflicts(barcode)
        if others:
            o = others[0]
            raise Conflict(f"{barcode} is already on {o['scope']} '{o['name']}' ({o['where']}).",
                           conflicts=others)
    filename = _store_photo(image)
    if want_ocr is None:
        want_ocr = settings_svc.get_settings().ocr_default_on
    label = (label or "").strip()
    item = Item(
        bin_id=bin_id, photo_path=filename, label=label,
        # A name given at creation is recorded with its provenance up front.
        label_source=("user" if actor == "user" else actor) if label else "ml",
        tags="", notes=notes or "", ocr_requested=bool(want_ocr), ocr_done=False,
    )
    db.session.add(item)
    db.session.commit()
    if barcode:
        codes_svc.add("item", item.id, barcode, kind=barcode_kind, source=barcode_source,
                      force=True, actor=actor)
    if ml.get_auto_tag():
        ml.enqueue_item(item.id)
    events.emit(events.ITEM_CREATED, item_id=item.id, bin_id=item.bin_id, actor=actor)
    return item


MAX_TAGS, MAX_TAG_LEN = 30, 60


def clean_tags(tags):
    """A tag list as stored: commas stripped (they're the separator), trimmed,
    de-duplicated case-insensitively, capped in count and length."""
    if isinstance(tags, str):
        tags = tags.split(",")
    terms = [" ".join(str(t).replace(",", " ").split())[:MAX_TAG_LEN] for t in tags or []]
    return vocabulary.merge_tags("", *terms[:MAX_TAGS])


def update(item_id, *, label=UNSET, notes=UNSET, quantity=UNSET, bin_id=UNSET, tags=UNSET,
           actor="user"):
    """Edit an item. Only the fields passed are changed.

    A rename by the user in the app always applies and becomes a vocabulary
    review candidate (D5). A rename by anyone else (the assistant) is a
    *proposal*: it applies only if its source ranks at least as high as the
    current name's in Settings → Name priority, else Conflict.

    `tags` replaces the whole tag list. Analysis merges its own tags back in
    only when the item is analysed again (forced re-analysis, or a rotate).
    """
    item = get(item_id)
    if bin_id is not UNSET:
        _bin(bin_id)                       # validate before changing anything

    changes = {}
    if label is not UNSET:
        new_label = (label or "").strip()
        if actor == "user":
            if new_label and new_label != item.label:
                item.label_source = "user"
                try:
                    vocabulary._learn_label(new_label)
                except Exception as e:
                    print(f"[vocab] learn failed: {e}")
            if new_label != item.label:
                changes["label"] = (item.label, new_label)
            item.label = new_label
        elif new_label and new_label != item.label:
            if not naming.may_replace(item.label_source, item.label, actor):
                raise Conflict(
                    f"Not renamed: the current name ({item.label!r}) came from "
                    f"'{naming.label_for(item.label_source)}', "
                    f"which ranks above '{naming.SOURCES.get(actor, actor)}' in Settings → Name priority.",
                    current_source=item.label_source)
            changes["label"] = (item.label, new_label)
            item.label, item.label_source = new_label, actor
        elif not new_label:
            raise Invalid("A name can't be empty")

    if notes is not UNSET and (notes or "") != (item.notes or ""):
        changes["notes"] = (item.notes, notes or "")
        item.notes = notes or ""
    if quantity is not UNSET:
        # Empty string or None means "uncounted" rather than zero.
        q = None if quantity in (None, "") else max(0, int(quantity))
        if q != item.quantity:
            changes["quantity"] = (item.quantity, q)
        item.quantity = q
    if bin_id is not UNSET and bin_id != item.bin_id:
        changes["bin_id"] = (item.bin_id, bin_id)
        item.bin_id = bin_id
    if tags is not UNSET:
        new_tags = clean_tags(tags)
        if new_tags != (item.tags or ""):
            changes["tags"] = (item.tags, new_tags)
        item.tags = new_tags
    db.session.commit()
    if changes:
        events.emit(events.ITEM_UPDATED, item_id=item.id, changes=changes, actor=actor)
    return item


def move(item_id, bin_id, actor="user"):
    return update(item_id, bin_id=bin_id, actor=actor)


def delete(item_id, actor="user"):
    item = get(item_id)
    _discard_photo(item.photo_path)
    db.session.delete(item)
    db.session.commit()
    events.emit(events.ITEM_DELETED, item_id=item_id, actor=actor)


# ── lifecycle ────────────────────────────────────────────────────────────────

def checkout(item_id, actor="user"):
    """Move an item to the virtual Checked Out bin, remembering its origin."""
    item = get(item_id)
    if item.status == "checked_out":
        raise Invalid("Already checked out")
    if item.status == "consumed":
        raise Invalid("Item is consumed — restock it first")
    item.origin_bin_id  = item.bin_id
    item.status         = "checked_out"
    item.checked_out_at = datetime.utcnow()
    db.session.commit()
    events.emit(events.ITEM_CHECKED_OUT, item_id=item.id, origin_bin_id=item.origin_bin_id, actor=actor)
    return item


def return_(item_id, actor="user"):
    """Return a checked-out item to its original bin (if that still exists)."""
    item = get(item_id)
    if item.status != "checked_out":
        raise Invalid("Item is not checked out")
    if item.origin_bin_id and db.session.get(Bin, item.origin_bin_id):
        item.bin_id = item.origin_bin_id
    item.status         = "active"
    item.origin_bin_id  = ""
    item.checked_out_at = None
    db.session.commit()
    events.emit(events.ITEM_RETURNED, item_id=item.id, bin_id=item.bin_id, actor=actor)
    return item


def consume(item_id, actor="user"):
    """Mark an item as used up — moves it to the shopping list."""
    item = get(item_id)
    if item.status == "consumed":
        raise Invalid("Already consumed")
    # Remember where it lived so "restock to same bin" works later, even if the
    # item was checked out at the time.
    item.origin_bin_id = item.bin_id if item.status == "active" else (item.origin_bin_id or item.bin_id)
    item.status        = "consumed"
    item.consumed_at   = datetime.utcnow()
    db.session.commit()
    events.emit(events.ITEM_CONSUMED, item_id=item.id, origin_bin_id=item.origin_bin_id, actor=actor)
    return item


def restock(item_id, bin_id=None, label=None, notes=None, quantity=None, photo=None, actor="user"):
    """Return a consumed item to the shelf (backlog D4).

    Change nothing → back to its original bin. bin/label/notes/quantity are
    applied only where they differ from what's stored (the UI form is pre-filled
    and always submits everything). A photo replaces the image and re-queues
    analysis. Lights the destination bin. Returns (item, lit, photo_replaced).

    Raises Conflict(needs_bin=True) when no bin was given and the item's
    original bin has since been deleted (C6).
    """
    item = get(item_id)
    if item.status != "consumed":
        raise Invalid("Item is not in the shopping list")

    # Explicit bin wins, else where it came from. No fallback to item.bin_id —
    # after its bin is deleted that's an arbitrary holding bin.
    target_bin_id = (bin_id or "").strip() or item.origin_bin_id
    if not target_bin_id or not db.session.get(Bin, target_bin_id):
        raise Conflict("The bin this item came from no longer exists. "
                       "Choose a bin to restock it into.", needs_bin=True)

    new_label = (label or "").strip()
    if new_label and new_label != (item.label or ""):
        if actor != "user" and not naming.may_replace(item.label_source, item.label, actor):
            raise Conflict(f"Not renamed: the current name ({item.label!r}) ranks above "
                           f"'{naming.SOURCES.get(actor, actor)}' in Settings → Name priority.")
        item.label = new_label
        item.label_source = "user" if actor == "user" else actor
    if notes is not None and notes != (item.notes or ""):
        item.notes = notes
    if quantity not in (None, ""):
        try:
            item.quantity = max(0, int(quantity))
        except (TypeError, ValueError):
            pass

    replaced = False
    if photo is not None:
        _discard_photo(item.photo_path)
        item.photo_path = _store_photo(photo)
        # A new image invalidates whatever the models concluded about the old one.
        item.embedding_blob = None
        item.tags = ""
        item.ocr_done = False
        replaced = True

    item.bin_id = target_bin_id
    item.status = "active"
    item.origin_bin_id = ""
    item.consumed_at = None
    db.session.commit()

    if replaced:
        ml.enqueue_item(item.id)

    # Light the destination so the user can walk over and put it away.
    lit = bool(lighting.highlight_bin(db.session.get(Bin, target_bin_id)).get("ok"))
    events.emit(events.ITEM_RESTOCKED, item_id=item.id, bin_id=target_bin_id, actor=actor)
    return item, lit, replaced


def remove_from_shopping_list(item_id, actor="user"):
    """Permanently delete an item that is on the shopping list."""
    item = get(item_id)
    if item.status != "consumed":
        raise Invalid("Item is not in the shopping list")
    delete(item_id, actor=actor)


def rotate(item_id, degrees):
    """Rotate a stored photo by 90° steps (counter-clockwise) and re-queue it.

    Rotation changes what the models see, so the embedding, tags and OCR result
    are cleared and the item is analysed again against the corrected image.
    """
    item = get(item_id)
    degrees = int(degrees) % 360
    if degrees not in (90, 180, 270):
        raise Invalid("degrees must be 90, 180 or 270")
    path = config.UPLOAD_DIR / item.photo_path
    try:
        img = Image.open(path).convert("RGB")
        img = img.rotate(degrees, expand=True)    # expand: a 90° turn isn't cropped
        img.save(path, "JPEG", quality=82)
        thumbnails.ensure(item.photo_path)        # now older than the photo, so rebuilt
    except Exception as e:
        raise Failed(f"Rotate failed: {e}")
    item.embedding_blob = None
    item.tags = ""
    if item.ocr_done:
        item.ocr_done = False
        item.ocr_requested = True                 # keep OCR on if it was on before
    db.session.commit()
    ml.tag_queue.put(item.id)
    ml.ensure_worker()
    return item


# ── where things are ─────────────────────────────────────────────────────────

def home_bin_id(item):
    """The bin an item belongs in: its origin while checked out or consumed."""
    if item.status == "checked_out":
        return item.origin_bin_id
    if item.status == "consumed":
        return item.origin_bin_id or item.bin_id
    return item.bin_id
