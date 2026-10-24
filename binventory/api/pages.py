"""Static page and photo serving."""
import base64
import io
import json

from flask import Blueprint, Response, jsonify, request, send_from_directory
from PIL import Image

from .. import config, events, images, lighting, ml, scheduler, vocabulary
from ..db import db
from ..models import AppSettings, Bin, Item, LearnedLabel, WLEDSettings
from ..services import catalog, thumbnails
from ..services import settings as settings_svc

bp = Blueprint("pages", __name__)


@bp.route("/photos/<filename>")
def serve_photo(filename):
    return send_from_directory(config.UPLOAD_DIR, filename)


@bp.route("/thumbs/<filename>")
def serve_thumb(filename):
    """Grid-sized copy of a photo, built on first request if needed.

    URLs from `thumbnails.url_for` carry the photo's mtime, so a versioned
    request can be cached forever. Falls back to the full photo if a
    thumbnail can't be built (unreadable file), and 404s if there's no photo.
    """
    path = thumbnails.ensure(filename)
    if path is None:
        return send_from_directory(config.UPLOAD_DIR, filename)
    resp = send_from_directory(thumbnails.THUMB_DIR, path.name)
    if request.args.get("v"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp

# ── single-page app ───────────────────────────────────────────────────────────

@bp.route("/")
def index():
    # Send file directly — bypasses Jinja templating which would HTML-encode JS strings
    return send_from_directory(config.BASE_DIR / "templates", "index.html")
