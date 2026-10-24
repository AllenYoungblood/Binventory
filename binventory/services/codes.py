"""Codes that identify bins and items — the local half of barcodes.

A code is a *local identifier*: scanning it opens that bin or item. Where the
value came from doesn't matter to resolution:

* a product barcode already on the item (scanned at Add Item, typed, or found
  in the item's photo by the barcode reader);
* a short code BINventory generated for a printed label ("local");
* an AprilTag id assigned to a bin ("apriltag").

Product data looked up *from* a barcode (a name from an online database) is a
separate concern kept by the barcode plugin, keyed by the code's value. That
split is deliberate: codes work offline as identifiers today, and a later bulk
lookup can enrich every code in the inventory without touching this table.

Conflicts: the same value on two things is allowed — two identical products
share a UPC — but only when the caller passes force=True after asking the
user. A first attempt raises Conflict listing where the value already is.
"""
import re
import secrets

from .. import events
from ..db import db
from ..models import Bin, Code, Item
from . import catalog
from .errors import Conflict, Invalid, NotFound

SCOPES = ("item", "bin")
SOURCES = ("generated", "scanned", "typed", "photo")
MAX_LEN = 200

# Crockford base32: no I, L, O, U — nothing to misread on a printed label.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LOCAL_LEN = 6
# Our own label URLs: https://<host>/c/<code>. Matched on any host, so a label
# keeps working after the server's address changes.
_LABEL_URL = re.compile(r"^https?://[^/\s]+/c/([0-9A-Z]{4,12})/?$", re.IGNORECASE)


def label_url(base, code):
    """The URL printed in a QR label. Upper case on purpose: QR's alphanumeric
    mode covers upper-case letters, digits and ':/.', so the whole URL encodes
    smaller — a version-2 symbol instead of version 3 for a typical address."""
    return f"{base.rstrip('/')}/c/{code}".upper()


def _clean(value):
    value = " ".join(str(value or "").split())
    if not value:
        raise Invalid("A code can't be empty")
    if len(value) > MAX_LEN:
        raise Invalid(f"A code can be at most {MAX_LEN} characters")
    return value


def _variants(value):
    """Spellings of the same code: a scanned label URL is its local code; a
    UPC-A is the EAN-13 with a leading zero (scanners report either)."""
    m = _LABEL_URL.match(value)
    if m:
        return {m.group(1).upper()}
    out = {value, value.upper()}
    if value.isdigit():
        if len(value) == 12:
            out.add("0" + value)
        elif len(value) == 13 and value.startswith("0"):
            out.add(value[1:])
    return out


def _target(scope, target_id):
    if scope not in SCOPES:
        raise Invalid(f"scope must be one of {', '.join(SCOPES)}")
    obj = db.session.get(Item if scope == "item" else Bin, target_id or "")
    if obj is None:
        raise NotFound(f"Unknown {scope}")
    return obj


def describe(code):
    """What a code points at, shaped for the Scan tab and conflict prompts."""
    base = {"code_id": code.id, "value": code.value, "kind": code.kind,
            "source": code.source, "scope": code.scope, "id": code.target_id}
    if code.scope == "bin":
        b = db.session.get(Bin, code.target_id)
        if b is None:
            return None
        path = catalog.bin_path(b)
        return dict(base, name=b.name, where=(f"{path} / {b.name}" if path else b.name)
                    + (f" ({b.location})" if b.location else ""))
    item = db.session.get(Item, code.target_id)
    if item is None:
        return None
    d = catalog.item_dict(item)
    where = d["bin_name"] if d["status"] == "active" else (
        f"checked out from {d['origin_bin_name']}" if d["status"] == "checked_out"
        else "on the shopping list")
    return dict(base, name=item.label or "(unnamed item)", where=where, status=d["status"],
                thumb_url=d["thumb_url"], bin_id=d["bin_id"], origin_bin_id=d["origin_bin_id"])


def lookup(value):
    """Every code row matching `value` in any of its spellings."""
    return Code.query.filter(Code.value.in_(_variants(_clean(value)))).all()


def resolve(value):
    """What scanning `value` should open: [describe(code), …], best first."""
    rows = [describe(c) for c in lookup(value)]
    # Bins first (a box label), then items; duplicates of one target collapse.
    seen, out = set(), []
    for r in sorted((r for r in rows if r), key=lambda r: r["scope"] != "bin"):
        if (r["scope"], r["id"]) not in seen:
            seen.add((r["scope"], r["id"]))
            out.append(r)
    return out


def for_target(scope, target_id):
    return (Code.query.filter_by(scope=scope, target_id=target_id)
            .order_by(Code.created, Code.id).all())


def conflicts(value, scope=None, target_id=None):
    """Other bins/items that already carry `value`."""
    return [d for d in resolve(value) if not (d["scope"] == scope and d["id"] == target_id)]


def add(scope, target_id, value, kind="other", source="typed", force=False, actor="user"):
    """Attach a code. Raises Conflict (with `conflicts`) if another bin or item
    already has it, unless force=True — the user said yes."""
    _target(scope, target_id)
    value = _clean(value)
    m = _LABEL_URL.match(value)
    if m:                                      # someone scanned one of our labels
        value, kind = m.group(1).upper(), "local"
    kind = (kind or "other").lower()[:20]
    if source not in SOURCES:
        raise Invalid(f"source must be one of {', '.join(SOURCES)}")
    existing = Code.query.filter(Code.value.in_(_variants(value)), Code.scope == scope,
                                 Code.target_id == target_id).first()
    if existing:
        return existing
    others = conflicts(value, scope, target_id)
    if others and not force:
        first = others[0]
        more = f" and {len(others) - 1} more" if len(others) > 1 else ""
        raise Conflict(f"{value} is already on {first['scope']} '{first['name']}' "
                       f"({first['where']}){more}.", conflicts=others)
    code = Code(value=value, kind=kind, scope=scope, target_id=target_id, source=source)
    db.session.add(code)
    db.session.commit()
    if scope == "item":
        events.emit(events.ITEM_UPDATED, item_id=target_id,
                    changes={"codes": (None, value)}, actor=actor)
    return code


def remove(code_id, actor="user"):
    code = db.session.get(Code, code_id)
    if code is None:
        raise NotFound("Unknown code")
    scope, target, value = code.scope, code.target_id, code.value
    db.session.delete(code)
    db.session.commit()
    if scope == "item":
        events.emit(events.ITEM_UPDATED, item_id=target, changes={"codes": (value, None)}, actor=actor)


def ensure_local(scope, target_id):
    """The target's generated label code, created on first use."""
    _target(scope, target_id)
    code = Code.query.filter_by(scope=scope, target_id=target_id, kind="local").first()
    if code:
        return code
    for _ in range(20):
        value = "".join(secrets.choice(_ALPHABET) for _ in range(LOCAL_LEN))
        if not Code.query.filter_by(value=value).first():
            break
    code = Code(value=value, kind="local", scope=scope, target_id=target_id, source="generated")
    db.session.add(code)
    db.session.commit()
    return code


def ensure_apriltag(bin_id):
    """The bin's AprilTag id (tag36h11), the lowest free one on first use.

    Only bins get tags: the family has 587 ids, enough for every box on a
    wall but not for every item."""
    from ..scanning import APRILTAG_COUNT
    _target("bin", bin_id)
    code = Code.query.filter_by(scope="bin", target_id=bin_id, kind="apriltag").first()
    if code:
        return code
    used = {int(c.value) for c in Code.query.filter_by(kind="apriltag") if c.value.isdigit()}
    free = next((n for n in range(APRILTAG_COUNT) if n not in used), None)
    if free is None:
        raise Conflict(f"All {APRILTAG_COUNT} AprilTag ids are in use.")
    code = Code(value=str(free), kind="apriltag", scope="bin", target_id=bin_id, source="generated")
    db.session.add(code)
    db.session.commit()
    return code


def register_found(item_id, found):
    """Codes the barcode reader found in an item's own photo: attach the
    barcodes nobody else has. Conflicts are skipped silently — there's no one
    to ask in a background job; the user can still add one on the item sheet."""
    added = []
    for text, kind in found:
        if kind in ("qr", "apriltag"):
            continue                   # QR text is content, not an id; tags are labels
        try:
            add("item", item_id, text, kind=kind, source="photo")
            added.append(text)
        except (Conflict, Invalid, NotFound):
            continue
    return added


# ── Housekeeping ─────────────────────────────────────────────────────────────

def _purge(scope, target_id):
    Code.query.filter_by(scope=scope, target_id=target_id).delete()
    db.session.commit()


def _on_item_deleted(event, item_id=None, **_):
    if item_id:
        _purge("item", item_id)


def _on_bin_deleted(event, bin_id=None, **_):
    if bin_id:
        _purge("bin", bin_id)


events.on(events.ITEM_DELETED, _on_item_deleted)
events.on(events.BIN_DELETED, _on_bin_deleted)
