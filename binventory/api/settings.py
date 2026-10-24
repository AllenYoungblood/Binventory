"""App settings, model status, and processing controls."""
import base64
import io
import json

from flask import Blueprint, Response, jsonify, request, send_from_directory
from PIL import Image

from .. import auth, config, events, images, lighting, ml, scheduler, vocabulary
from ..db import db
from ..models import AppSettings, Bin, Item, LearnedLabel, WLEDSettings
from ..services import catalog
from ..services import settings as settings_svc
from ..services import processing

bp = Blueprint("settings", __name__)


@bp.route("/api/status")
@auth.require("settings:read")
def api_status():
    next_run = None
    job = scheduler.scheduler.get_job("nightly_tag")
    # `next_run_time` is only present on a job attached to a *running* scheduler,
    # and the attribute name differs across APScheduler versions — so read it
    # defensively rather than letting /api/status 500.
    if job is not None:
        nrt = getattr(job, "next_run_time", None)
        if nrt:
            next_run = nrt.strftime("%Y-%m-%dT%H:%M:%S")
    return jsonify({
        "clip_ready":     ml.clip_ready(),
        "clip_loading":   ml.status_snapshot()["clip_loading"],
        "ocr_ready":      ml.ocr_ready(),
        "ocr_loading":    ml.status_snapshot()["ocr_loading"],
        "worker_running": ml.worker_running(),
        "queue_size":     ml.tag_queue.qsize(),
        "auto_tag":       ml.get_auto_tag(),
        "tagging_now":    ml.currently_tagging() is not None,
        "schedule_hour":  scheduler.schedule_hour(),
        "schedule_min":   scheduler.schedule_min(),
        "next_run":       next_run,
        "progress":       ml.job_snapshot(),
    })

@bp.route("/api/settings/autotag", methods=["POST"])
@auth.require("settings:write")
def set_autotag():
    # Auto-tag lives in the ml package; use its setter rather than a global.
    enabled = bool((request.json or {}).get("enabled", True))
    return jsonify({"auto_tag": ml.set_auto_tag(enabled)})

@bp.route("/api/settings/runtags", methods=["POST"])
@auth.require("settings:write")
def run_all_tags():
    """Queue every untagged item and start the worker."""
    # Unified processing (backlog D1).
    n = processing.queue(scope="pending")
    return jsonify({"queued": n})

@bp.route("/api/settings/ocr", methods=["GET"])
@auth.require("settings:read")
def get_ocr_settings():
    """Return the OCR preferences shown in the Settings sheet."""
    s = settings_svc.get_settings()
    return jsonify({
        "ocr_default_on":   s.ocr_default_on,
        "ocr_as_tags":      s.ocr_as_tags,
        "ocr_truncate":     s.ocr_truncate,
        "ocr_truncate_len": s.ocr_truncate_len,
        "model_keepalive_sec": s.model_keepalive_sec,
        "simplified_view":     s.simplified_view,
        "text_threshold":      s.text_threshold if s.text_threshold is not None else 0.18,
        "image_threshold":     s.image_threshold if s.image_threshold is not None else 0.55,
    })

@bp.route("/api/settings/naming", methods=["GET"])
@auth.require("settings:read")
def get_naming():
    """Name priority: which sources may replace a stored item name."""
    from .. import naming
    return jsonify(naming.describe())


@bp.route("/api/settings/naming", methods=["PUT"])
@auth.require("settings:write")
def save_naming():
    """Body: {"order": [lowest … highest]} or {"reset": true}."""
    from .. import naming
    data = request.get_json(silent=True) or {}
    try:
        if data.get("reset"):
            naming.reset_order()
        else:
            naming.set_order(data.get("order"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(naming.describe())


@bp.route("/api/settings/ocr", methods=["POST"])
@auth.require("settings:write")
def save_ocr_settings():
    """Persist OCR preferences. Truncation length is hard-capped at 255."""
    data = request.json or {}
    s = settings_svc.get_settings()
    if "ocr_default_on"   in data: s.ocr_default_on   = bool(data["ocr_default_on"])
    if "ocr_as_tags"      in data: s.ocr_as_tags      = bool(data["ocr_as_tags"])
    if "ocr_truncate"     in data: s.ocr_truncate     = bool(data["ocr_truncate"])
    if "ocr_truncate_len" in data:
        s.ocr_truncate_len = max(1, min(255, int(data["ocr_truncate_len"])))
    if "simplified_view" in data:
        s.simplified_view = bool(data["simplified_view"])
    if "model_keepalive_sec" in data:
        # 0 = unload immediately; cap at 1 hour to avoid pinning RAM forever.
        s.model_keepalive_sec = max(0, min(3600, int(data["model_keepalive_sec"])))
    # Similarity thresholds (C5). Clamped: below ~0.05 everything matches,
    # above ~0.95 nothing does.
    for key in ("text_threshold", "image_threshold"):
        if key in data:
            setattr(s, key, max(0.05, min(0.95, float(data[key]))))
    db.session.commit()
    return jsonify({
        "ok": True,
        "ocr_default_on":   s.ocr_default_on,
        "ocr_as_tags":      s.ocr_as_tags,
        "ocr_truncate":     s.ocr_truncate,
        "ocr_truncate_len": s.ocr_truncate_len,
        "model_keepalive_sec": s.model_keepalive_sec,
        "simplified_view":     s.simplified_view,
        "text_threshold":      s.text_threshold if s.text_threshold is not None else 0.18,
        "image_threshold":     s.image_threshold if s.image_threshold is not None else 0.55,
    })

@bp.route("/api/settings/rerun-ocr", methods=["POST"])
@auth.require("settings:write")
def rerun_ocr_all():
    """Re-run OCR across the whole database.

    Used after changing the truncation setting so existing items pick up the
    new rule. Clears ocr_done (so the worker reprocesses) and marks every item
    as OCR-requested. Body: {"scope": "all" | "existing"}
      all      — every item in the library
      existing — only items that already have OCR text (a re-truncate pass)
    """
    # Unified processing (backlog D1). "existing" limits to items that already
    # have OCR text; "all" re-reads everything.
    scope_arg = (request.json or {}).get("scope", "all")
    if scope_arg == "existing":
        ids = [i.id for i in Item.query.filter(Item.ocr_text != "", Item.ocr_text != None).all()]
        n = 0
        for i in ids:
            n += processing.queue(scope="item", target_id=i, want_ocr=True)
    else:
        n = processing.queue(scope="all", want_ocr=True)
    return jsonify({"ok": True, "queued": n})

# ── Dictionary management ─────────────────────────────────────────────────────

@bp.route("/api/settings/run-all", methods=["POST"])
@auth.require("settings:write")
def run_all_pending():
    """Queue everything that still needs processing — CLIP tagging and any
    OCR that was requested but hasn't run.

    Simplified view collapses the separate "Tag All" / "Re-run OCR" controls
    into this single action, since the distinction isn't useful to someone who
    just wants their backlog processed.
    """
    # Unified processing (backlog D1).
    n = processing.queue(scope="pending")
    return jsonify({"ok": True, "queued": n})

@bp.route("/api/settings/unloadclip", methods=["POST"])
@auth.require("settings:write")
def unload_clip_route():
    """Manually release CLIP from memory."""
    if ml.worker_running():
        return jsonify({"error": "Worker is running — wait until tagging finishes"}), 409
    ml.unload_clip()
    return jsonify({"ok": True})

@bp.route("/api/settings/schedule", methods=["POST"])
@auth.require("settings:write")
def set_schedule():
    """Set or clear the nightly auto-tag schedule.
       POST {"hour": 2, "minute": 30}  → run at 02:30 daily
       POST {"hour": null}             → disable schedule
    """
    data   = request.json or {}
    hour   = data.get("hour")
    minute = int(data.get("minute", 0))
    if hour is None:
        scheduler.apply_schedule(None, 0)
    else:
        scheduler.apply_schedule(int(hour), minute)
    return jsonify({"ok": True, "schedule_hour": scheduler.schedule_hour(), "schedule_min": scheduler.schedule_min()})

# Kick off CLIP load when the client first loads the page

@bp.route("/api/warmup", methods=["POST"])
@auth.require("settings:write")
def api_warmup():
    ml.get_clip()
    return jsonify({"ok": True})

# ── bins ──────────────────────────────────────────────────────────────────────


# ── Unified processing endpoint (phase 5, backlog D1) ────────────────────────
# Replaces five overlapping actions. The older endpoints remain as thin
# redirects so nothing that still calls them breaks.

@bp.route("/api/process", methods=["POST"])
@auth.require("items:write")
def process_items():
    """Queue items for analysis.

    Body: {"scope": "item"|"bin"|"pending"|"all",
           "target_id": "...",      # for item/bin
           "force": false,           # re-run work already done
           "ocr": true|false|null}   # set the per-item OCR flag while queueing
    """
    data = request.json or {}
    try:
        n = processing.queue(
            scope=data.get("scope", "pending"),
            target_id=data.get("target_id"),
            force=bool(data.get("force")),
            want_ocr=data.get("ocr"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "queued": n, "pending": processing.pending_count()})
