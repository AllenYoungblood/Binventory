"""Item endpoints: CRUD, lifecycle (checkout/consume/restock), OCR and retag.

Thin adapters over services/items.py — parse the request, call the service,
shape the response. Service errors become HTTP statuses in api.register.
"""
import base64
import io

from flask import Blueprint, jsonify, request

from .. import auth
from ..services import catalog, processing
from ..services import items as items_svc
from ..services.errors import Invalid, UNSET

bp = Blueprint("items", __name__)


def _body():
    """Form fields for multipart requests, else the JSON body (never raises)."""
    if request.content_type and "multipart" in request.content_type:
        return request.form
    return request.get_json(silent=True) or {}


def _photo_from_request():
    if "photo" in request.files:
        return items_svc.open_image(request.files["photo"].stream)
    body = request.get_json(silent=True) or {}
    if body.get("photo_b64"):
        return items_svc.open_image(io.BytesIO(base64.b64decode(body["photo_b64"])))
    return None


@bp.route("/api/items", methods=["GET"])
@auth.require("items:read")
def list_items():
    """
    bin_id=<id>        — items currently in that bin (status=active only)
    bin_id=checked_out  — virtual bin: all checked_out items, regardless of origin
    status=consumed     — shopping list
    (no params)          — all active items (default inventory view)

    Pagination (backlog F2): pass `limit` (1-500) and optional `offset` to get
    {"items", "total", "offset", "limit", "has_more"} instead of a bare list.
    Without `limit` the response is the full list, as before, so callers that
    don't page are unaffected.
    """
    q = items_svc.query(bin_id=request.args.get("bin_id"), status=request.args.get("status"),
                        direct=request.args.get("direct") in ("1", "true"))
    limit = request.args.get("limit")
    if limit is None:
        return jsonify([catalog.item_dict(i) for i in q.all()])
    try:
        page, total, offset, limit = items_svc.page(q, limit, request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400
    return jsonify({"items": [catalog.item_dict(i) for i in page], "total": total,
                    "offset": offset, "limit": limit,
                    "has_more": offset + len(page) < total})


@bp.route("/api/items", methods=["POST"])
@auth.require("items:write")
def add_item():
    """Accept a photo (multipart or base64 JSON) + bin_id."""
    body = _body()
    bin_id = body.get("bin_id")
    items_svc._bin(bin_id)                       # "Unknown bin" before reading the photo
    image = _photo_from_request()
    if image is None:
        raise Invalid("No photo provided")
    # The Add Item screen sends run_ocr explicitly; if absent the configured
    # default applies.
    raw_ocr = body.get("run_ocr")
    want_ocr = None if raw_ocr is None else str(raw_ocr).lower() in ("1", "true", "yes", "on")
    item = items_svc.create(bin_id, image, label=body.get("label", ""),
                            notes=body.get("notes", ""), want_ocr=want_ocr,
                            barcode=body.get("barcode") or None,
                            barcode_kind=body.get("barcode_kind") or "other",
                            barcode_source=body.get("barcode_source") or "scanned",
                            force_barcode=str(body.get("force_barcode", "")).lower() in ("1", "true"))
    return jsonify(catalog.item_dict(item)), 201


@bp.route("/api/items/<item_id>", methods=["GET"])
@auth.require("items:read")
def get_item(item_id):
    """One item, fresh — the detail sheet reads this rather than a grid snapshot."""
    return jsonify(catalog.item_dict(items_svc.get(item_id)))


@bp.route("/api/items/<item_id>", methods=["PUT"])
@auth.require("items:write")
def update_item(item_id):
    data = request.get_json(silent=True) or {}
    item = items_svc.update(
        item_id,
        label=data.get("label") if "label" in data else UNSET,
        notes=data.get("notes") if "notes" in data else UNSET,
        quantity=data.get("quantity") if "quantity" in data else UNSET,
        bin_id=data.get("bin_id") if "bin_id" in data else UNSET,
    )
    return jsonify(catalog.item_dict(item))


@bp.route("/api/items/<item_id>", methods=["DELETE"])
@auth.require("items:write")
def delete_item(item_id):
    items_svc.delete(item_id)
    return jsonify({"ok": True})


@bp.route("/api/items/<item_id>/retag", methods=["POST"])
@auth.require("items:write")
def retag_item(item_id):
    """Queue a single item for re-tagging (loads CLIP on demand if needed)."""
    # Delegates to the unified processing service (backlog D1). Kept as its
    # own route so existing clients keep working.
    item = items_svc.get(item_id)
    processing.queue(scope="item", target_id=item_id, force=True)
    return jsonify({**catalog.item_dict(item), "queued": True})


# ── checkout / consume / restock ──────────────────────────────────────────────

@bp.route("/api/items/<item_id>/checkout", methods=["POST"])
@auth.require("items:write")
def checkout_item(item_id):
    """Move an item to the virtual Checked Out bin, remembering its origin."""
    return jsonify(catalog.item_dict(items_svc.checkout(item_id)))


@bp.route("/api/items/<item_id>/return", methods=["POST"])
@auth.require("items:write")
def return_item(item_id):
    """Return a checked-out item to its original bin."""
    return jsonify(catalog.item_dict(items_svc.return_(item_id)))


@bp.route("/api/items/<item_id>/consume", methods=["POST"])
@auth.require("items:write")
def consume_item(item_id):
    """Mark an item as used up — moves it to the shopping list."""
    return jsonify(catalog.item_dict(items_svc.consume(item_id)))


@bp.route("/api/items/<item_id>/restock", methods=["POST"])
@bp.route("/api/items/<item_id>/restock-photo", methods=["POST"])
@auth.require("items:write")
def restock_item(item_id):
    """Return a consumed item to the shelf (backlog D4).

    One endpoint for what used to be two (JSON, or multipart with a new photo);
    both legacy paths are still routed here. See services.items.restock.
    """
    data = _body()
    multipart = bool(request.content_type and "multipart" in request.content_type)
    photo = (items_svc.open_image(request.files["photo"].stream)
             if multipart and "photo" in request.files else None)
    items_svc.get(item_id)                       # 404 before reading anything else
    item, lit, replaced = items_svc.restock(
        item_id, bin_id=data.get("bin_id"), label=data.get("label"),
        notes=data.get("notes"), quantity=data.get("quantity"), photo=photo)
    return jsonify({**catalog.item_dict(item), "wled_fired": lit, "photo_replaced": replaced})


@bp.route("/api/items/<item_id>/shopping-delete", methods=["DELETE"])
@auth.require("items:write")
def shopping_delete_item(item_id):
    """Permanently remove an item from the shopping list (and disk)."""
    items_svc.remove_from_shopping_list(item_id)
    return jsonify({"ok": True})


@bp.route("/api/items/<item_id>/rotate", methods=["POST"])
@auth.require("items:write")
def rotate_item_photo(item_id):
    """Rotate a stored photo by 90° increments and re-queue it for processing.

    Body: {"degrees": 90 | 180 | 270}   (counter-clockwise, PIL convention)
    """
    degrees = (request.get_json(silent=True) or {}).get("degrees", 90)
    item = items_svc.rotate(item_id, degrees)
    return jsonify({**catalog.item_dict(item), "rotated": int(degrees) % 360})


@bp.route("/api/items/<item_id>/run-ocr", methods=["POST"])
@auth.require("items:write")
def run_item_ocr(item_id):
    """Queue a single item for an OCR pass (or re-pass)."""
    item = items_svc.get(item_id)
    processing.queue(scope="item", target_id=item_id, want_ocr=True)
    return jsonify({**catalog.item_dict(item), "queued": True})


@bp.route("/api/items/<item_id>/ocr-region", methods=["POST"])
@auth.require("items:write")
def ocr_item_region(item_id):
    """OCR a user-selected area of the photo (backlog E5).

    Body: {"x", "y", "w", "h"} as fractions (0-1) of the stored photo. Runs
    synchronously and returns the updated item plus the text read.
    """
    from ..services import region_ocr
    item = items_svc.get(item_id)
    try:
        box = region_ocr.parse_box(request.get_json(silent=True) or {})
        text = region_ocr.read_region(item, box)
    except region_ocr.RegionError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({**catalog.item_dict(item), "region_text": text})
