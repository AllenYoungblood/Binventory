"""Search endpoints: text/semantic and reverse-image.

Thin adapters over services/search.py.
"""
import base64
import io

from flask import Blueprint, jsonify, request

from .. import auth
from ..services import catalog
from ..services import items as items_svc
from ..services import search as search_svc

bp = Blueprint("search", __name__)


def _results(pairs):
    return [catalog.item_dict(i) if s is None else {**catalog.item_dict(i), "score": s}
            for i, s in pairs]


@bp.route("/api/search/photo", methods=["POST"])
@auth.require("search:read")
def search_by_photo():
    """Reverse image search: find stored items that look like the photo sent.

    Accepts multipart 'photo', or JSON {"photo_b64": "..."}.
    Optional: 'threshold' (default: the image threshold in Settings, 0.55), 'limit' (default 20).
    """
    body = request.get_json(silent=True) or {}
    if "photo" in request.files:
        try:
            pil = items_svc.open_image(request.files["photo"].stream)
        except Exception as e:
            return jsonify({"error": f"Could not read image: {e}"}), 400
    else:
        if not body.get("photo_b64"):
            return jsonify({"error": "No photo provided"}), 400
        try:
            pil = items_svc.open_image(io.BytesIO(base64.b64decode(body["photo_b64"])))
        except Exception as e:
            return jsonify({"error": f"Could not decode image: {e}"}), 400
    try:
        threshold = request.form.get("threshold") or body.get("threshold")
        threshold = float(threshold) if threshold else None
    except Exception:
        threshold = 0.55
    try:
        limit = int(request.form.get("limit") or body.get("limit") or 20)
    except Exception:
        limit = 20

    results, compared, threshold = search_svc.photo(pil, threshold, limit)
    if not compared:
        return jsonify({"mode": "photo", "results": [],
                        "note": "No tagged items to compare against yet."})
    return jsonify({"mode": "photo", "results": _results(results),
                    "threshold": threshold, "compared": compared})


@bp.route("/api/search")
@auth.require("search:read")
def search():
    # "smart=1" comes from the ✨ button: the user is explicitly asking for
    # semantic search, so CLIP loads if needed. Otherwise semantic runs only if
    # the model is already warm.
    smart = request.args.get("smart") in ("1", "true", "yes")
    mode, pairs = search_svc.text(request.args.get("q", ""), request.args.getlist("bin_id"),
                                  mode="semantic" if smart else "warm")
    return jsonify({"mode": mode, "results": _results(pairs)})
