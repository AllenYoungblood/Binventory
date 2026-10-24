"""Bin endpoints: CRUD, nesting, and the secure delete flow.

Thin adapters over services/bins.py.
"""
from flask import Blueprint, jsonify, request

from .. import auth
from ..services import bins as bins_svc
from ..services import catalog
from ..services.errors import UNSET

bp = Blueprint("bins", __name__)


@bp.route("/api/bins", methods=["GET"])
@auth.require("bins:read")
def list_bins():
    """List bins.

    ?scope=top   (default) — only top-level bins; nested ones are shown inside
                             their parent's detail view instead
    ?scope=all             — every bin, flat. Used by pickers and search.
    ?q=<text>              — filter by name / location / path
    """
    return jsonify(bins_svc.listing(request.args.get("scope", "top"), request.args.get("q")))


@bp.route("/api/bins/<bin_id>", methods=["GET"])
@auth.require("bins:read")
def get_bin(bin_id):
    return jsonify(catalog.bin_dict(bins_svc.get(bin_id)))


# ── Nesting operations ────────────────────────────────────────────────────────

@bp.route("/api/bins/<bin_id>/nest", methods=["POST"])
@auth.require("bins:write")
def nest_bin(bin_id):
    """Put a bin inside another bin.

    Body: {"parent_id": "<bin id>"}  — or null/"" to detach (same as unpack).
    Refuses moves that would create a loop.
    """
    parent_id = (request.get_json(silent=True) or {}).get("parent_id") or ""
    return jsonify(catalog.bin_dict(bins_svc.nest(bin_id, parent_id)))


@bp.route("/api/bins/<bin_id>/unpack", methods=["POST"])
@auth.require("bins:write")
def unpack_bin(bin_id):
    """Remove this bin from its parent — it becomes top-level again."""
    return jsonify(catalog.bin_dict(bins_svc.unpack(bin_id)))


@bp.route("/api/bins/<bin_id>/unpack-all", methods=["POST"])
@auth.require("bins:write")
def unpack_all_bins(bin_id):
    """Detach every bin nested directly inside this one ({"deep": true}: at any depth)."""
    deep = bool((request.get_json(silent=True) or {}).get("deep", False))
    return jsonify({"ok": True, "unpacked": bins_svc.unpack_all(bin_id, deep)})


@bp.route("/api/bins/<bin_id>/children", methods=["GET"])
@auth.require("bins:read")
def bin_children(bin_id):
    """Bins nested directly inside this one (for the bin detail view)."""
    return jsonify([catalog.bin_dict(c) for c in catalog.child_bins(bin_id)])


@bp.route("/api/bins", methods=["POST"])
@auth.require("bins:write")
def create_bin():
    data = request.get_json(silent=True) or {}
    b = bins_svc.create(data.get("name", ""), data.get("location", ""),
                        data.get("color", "#6366f1"))
    return jsonify({"id": b.id, "name": b.name}), 201


@bp.route("/api/bins/<bin_id>", methods=["PUT"])
@auth.require("bins:write")
def update_bin(bin_id):
    data = request.get_json(silent=True) or {}
    # Lighting is configured through PUT /api/bins/<id>/lighting, not here.
    b = bins_svc.update(
        bin_id,
        name=data["name"] if "name" in data else UNSET,
        location=data["location"] if "location" in data else UNSET,
        color=data["color"] if "color" in data else UNSET,
        parent_id=data["parent_bin_id"] if "parent_bin_id" in data else UNSET,
    )
    return jsonify(catalog.bin_dict(b))


@bp.route("/api/bins/<bin_id>", methods=["DELETE"])
@auth.require("bins:write")
def delete_bin(bin_id):
    """Delete a bin. Body: {"confirm": "delete", "contents": "delete" | "checkout"}.

    See services.bins.delete for what happens to the contents.
    """
    data = request.get_json(silent=True) or {}
    result = bins_svc.delete(bin_id, data.get("confirm", ""), data.get("contents", ""))
    return jsonify({"ok": True, **result})
