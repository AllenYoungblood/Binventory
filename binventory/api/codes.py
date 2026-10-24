"""Codes and scanning: what a barcode or label points at.

    GET    /api/codes?scope=item&id=<id>   codes on a bin or item
    POST   /api/codes                      {scope, id, value, kind?, source?, force?}
    DELETE /api/codes/<code id>
    POST   /api/scan                       a photo (multipart "photo") or {"value": …}
    GET    /c/<code>                       a printed label: opens the bin or item

Adding a value another bin or item already has answers 409 with `conflicts`;
the UI asks, then resends with force=true.
"""
from flask import Blueprint, jsonify, redirect, request

from .. import auth, scanning
from ..services import codes as codes_svc
from ..services import items as items_svc
from ..services.errors import Invalid

bp = Blueprint("codes", __name__)


def code_dict(c):
    return {"id": c.id, "value": c.value, "kind": c.kind, "source": c.source,
            "scope": c.scope, "target_id": c.target_id,
            "created": c.created.isoformat() if c.created else None}


@bp.route("/api/codes")
@auth.require("items:read")
def list_codes():
    rows = codes_svc.for_target(request.args.get("scope", "item"), request.args.get("id", ""))
    return jsonify([code_dict(c) for c in rows])


@bp.route("/api/codes", methods=["POST"])
@auth.require("items:write")
def add_code():
    b = request.get_json(silent=True) or {}
    c = codes_svc.add(b.get("scope", "item"), b.get("id", ""), b.get("value", ""),
                      kind=b.get("kind") or "other", source=b.get("source") or "typed",
                      force=bool(b.get("force")))
    return jsonify(code_dict(c)), 201


@bp.route("/api/codes/<int:code_id>", methods=["DELETE"])
@auth.require("items:write")
def delete_code(code_id):
    codes_svc.remove(code_id)
    return jsonify({"ok": True})


@bp.route("/api/scan", methods=["POST"])
@auth.require("items:read")
def scan():
    """Decode a photo (or take a typed/scanner-typed value) and say what it opens.

    {"decoded": [{"value", "kind"}], "matches": [describe(...)]} — `matches`
    covers every decoded value, so a photo of a shelf can return several bins.
    """
    if "photo" in request.files:
        image = items_svc.open_image(request.files["photo"].stream)
        decoded = [{"value": t, "kind": k} for t, k in scanning.decode(image)]
    else:
        value = ((request.get_json(silent=True) or {}).get("value") or "").strip()
        if not value:
            raise Invalid("Nothing to look up — send a photo or a value")
        decoded = [{"value": value, "kind": "typed"}]
    matches, seen = [], set()
    for d in decoded:
        for m in codes_svc.resolve(d["value"]):
            if (m["scope"], m["id"]) not in seen:
                seen.add((m["scope"], m["id"]))
                matches.append(dict(m, scanned=d["value"]))
    return jsonify({"decoded": decoded, "matches": matches})


@bp.route("/c/<code>")
@bp.route("/C/<code>")          # labels print the URL in upper case (smaller QR)
def open_label(code):
    """What a phone's camera app opens from a QR label. The app reads the
    hash on load and opens the bin or item (or says the code is unknown)."""
    found = codes_svc.resolve(code)
    if len(found) == 1:
        return redirect(f"/#open={found[0]['scope']}:{found[0]['id']}")
    return redirect(f"/#scan={code}")
