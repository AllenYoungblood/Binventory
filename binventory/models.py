"""Database models.

Design notes worth knowing before changing these:

* `Item.embedding_blob` is CLIP's 512-float fingerprint of the photo, stored as
  raw float32 bytes. It is what makes semantic search work without re-running
  the model.
* `Item.status` drives the whole checkout/consume feature and is indexed because
  every list and search query filters on it.
* `Item.origin_bin_id` remembers where a checked-out or consumed item *belongs*,
  so "Find" and "Restock" still work while it is out of its bin.
* `Bin.parent_bin_id` is a self-reference implementing nesting. Items are never
  moved by nesting — a parent simply also displays its descendants' contents.
"""
import uuid
from datetime import datetime

from .db import db


# ── models ─────────────────────────────────────────────────────────────────────
class Bin(db.Model):
    id        = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name      = db.Column(db.String(100), nullable=False, unique=True)
    location  = db.Column(db.String(200), default="")
    color     = db.Column(db.String(20), default="#6366f1")
    created   = db.Column(db.DateTime, default=datetime.utcnow)
    items     = db.relationship("Item", backref="bin", lazy=True, cascade="all, delete-orphan")
    # Lighting lives in BinLighting (bin_lighting table), not on the bin row.

    # ── Nesting ────────────────────────────────────────────────────────────
    # A bin can live inside another bin (a parts case inside a drawer, a shoebox
    # inside a tote). Empty string / NULL means it is a top-level bin.
    # Items are NOT moved by nesting — each item keeps its own bin_id; the
    # parent simply *displays* everything from its descendants as well.
    parent_bin_id = db.Column(db.String(36), default="", index=True)

class AppSettings(db.Model):
    """Single-row table for app-wide preferences (OCR behaviour, etc.).

    Kept separate from WLEDSettings so each subsystem owns its own config and
    neither has to know about the other.
    """
    id = db.Column(db.Integer, primary_key=True)
    # Default state of the OCR toggle on the Add Item screen.
    ocr_default_on   = db.Column(db.Boolean, default=False)
    # Whether OCR text should also be merged into the item's searchable tags.
    ocr_as_tags      = db.Column(db.Boolean, default=True)
    # Whether to truncate OCR text before storing it as tags.
    ocr_truncate     = db.Column(db.Boolean, default=True)
    # Truncation limit in characters (capped at 255 by the API).
    ocr_truncate_len = db.Column(db.Integer, default=255)
    # Seconds the CLIP/OCR models stay resident after the last search or tag job
    # before being unloaded. Higher = faster repeat queries, more idle RAM.
    model_keepalive_sec = db.Column(db.Integer, default=60)
    # Simplified view hides advanced controls for fast, low-friction operation
    # (intake sessions, or handing the app to someone who just wants to find things).
    simplified_view = db.Column(db.Boolean, default=False)
    # Minimum CLIP cosine similarity for a search hit (backlog C5). Text-to-image
    # scores run far lower than image-to-image, hence two separate thresholds.
    text_threshold  = db.Column(db.Float, default=0.18)
    image_threshold = db.Column(db.Float, default=0.55)

class LearnedLabel(db.Model):
    """Vocabulary the app teaches itself from the user's own corrections.

    Every time you rename an item to something the dictionary didn't contain,
    that word is recorded here and folded into the candidate pool. Over time the
    vocabulary converges on *your* actual belongings rather than a generic list
    someone guessed at up front — which is usually far more effective than
    bolting on an ever-bigger static dictionary.
    """
    id    = db.Column(db.Integer, primary_key=True)
    text  = db.Column(db.String(120), unique=True, nullable=False)
    uses  = db.Column(db.Integer, default=1)      # how often it's been applied
    added = db.Column(db.DateTime, default=datetime.utcnow)

class WLEDSettings(db.Model):
    """Global WLED defaults shared across bins."""
    id              = db.Column(db.Integer, primary_key=True)
    default_ip      = db.Column(db.String(64), default="")
    enabled         = db.Column(db.Boolean, default=False)

class Item(db.Model):
    id          = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    bin_id      = db.Column(db.String(36), db.ForeignKey("bin.id"), nullable=False)
    photo_path  = db.Column(db.String(300), nullable=False)
    label       = db.Column(db.String(200), default="")
    tags        = db.Column(db.Text, default="")   # comma-separated CLIP tags
    # CLIP's 512-float fingerprint of the photo as raw float32 bytes (exactly
    # 2,048). services/embeddings_store owns the format. The legacy JSON text
    # column was dropped in phase 4b (migration 7).
    embedding_blob = db.Column(db.LargeBinary, default=None)
    notes       = db.Column(db.Text, default="")
    created     = db.Column(db.DateTime, default=datetime.utcnow)

    # ── lifecycle status ───────────────────────────────────────────────────
    # "active"      — normal, lives in bin_id
    # "checked_out" — temporarily out, origin_bin_id remembers where it lives
    # "consumed"    — used up, shows in the shopping list
    status         = db.Column(db.String(20), default="active", index=True)
    origin_bin_id  = db.Column(db.String(36), default="")   # bin to return to / find
    checked_out_at = db.Column(db.DateTime, default=None)
    consumed_at    = db.Column(db.DateTime, default=None)

    # How the item's label/name was produced (for provenance & debugging):
    #   "ml"            — CLIP auto-generated the label from the photo
    #   "user"          — a human typed the label
    #   "scanned"       — filled directly from a scanned barcode/QR payload
    #   "scanned_search"— scanned code was looked up via a free API/search
    # Used by the (planned) barcode feature and shown as a small provenance tag.
    label_source   = db.Column(db.String(20), default="ml")

    # ── OCR ────────────────────────────────────────────────────────────────
    # ocr_text      : raw text read off the photo (full, untruncated)
    # ocr_requested : whether the user asked for OCR on this specific item
    #                 (set from the Add Item toggle; drives the worker)
    # ocr_done      : set once OCR has actually run, so re-runs are explicit
    # How many of this thing are in the bin. NULL = uncounted, which is the
    # honest default for anything logged before counting existed. Captured for
    # future features (low-stock alerts, partial consume) but deliberately NOT
    # wired to the checkout/consume lifecycle yet — see backlog C8.
    quantity       = db.Column(db.Integer, default=None, nullable=True)

    ocr_text       = db.Column(db.Text, default="")
    # Where ocr_text came from: "photo" (whole-photo OCR), "region" (a crop the
    # user selected, backlog E5), or "" (none). A region read is never replaced
    # by any automatic or bulk OCR — only by a new region selection. Kept apart
    # from label_source because a later rename changes that to "user".
    ocr_source     = db.Column(db.String(10), default="")
    ocr_requested  = db.Column(db.Boolean, default=False)
    ocr_done       = db.Column(db.Boolean, default=False)


class Code(db.Model):
    """A code that identifies a bin or an item locally.

    `kind` is the symbology or origin: "local" (a short code BINventory
    generated for a printed label), "apriltag" (a tag36h11 id), or a barcode
    type as the decoder reports it ("ean_13", "upc_a", "code_128", "qr", …).
    `source` records how it got here: "generated", "scanned" (a photo of the
    code at Add Item or on the item sheet), "typed", or "photo" (found in the
    item's own photo by the barcode reader).

    The same value may sit on more than one item — two identical products
    share a UPC — but only after the user confirms it (services/codes.py).
    Product data looked up *from* a code is kept elsewhere (the barcode
    plugin's store, keyed by value), so a code stays a local identifier.
    """
    __tablename__ = "code"

    id        = db.Column(db.Integer, primary_key=True)
    value     = db.Column(db.String(200), nullable=False, index=True)
    kind      = db.Column(db.String(20), nullable=False, default="other")
    scope     = db.Column(db.String(10), nullable=False)        # "item" | "bin"
    target_id = db.Column(db.String(36), nullable=False)
    source    = db.Column(db.String(10), nullable=False, default="typed")
    created   = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("value", "scope", "target_id", name="uq_code_target"),
        db.Index("ix_code_target", "scope", "target_id"),
    )


class ExtensionData(db.Model):
    """Namespaced key-value storage for extensions (backlog G4).

    Extensions must NOT add database columns — an unbounded set of third-party
    schemas would make migrations impossible. They get this instead.

    Scoping: (scope, scope_id) identifies what the data is about — usually
    ("item", <item id>) or ("bin", <bin id>) — and `namespace` is the owning
    extension's id, so two extensions can never collide on a key. Uninstalling
    an extension is a single DELETE by namespace.
    """
    __tablename__ = "extension_data"

    id        = db.Column(db.Integer, primary_key=True)
    namespace = db.Column(db.String(64), nullable=False, index=True)
    scope     = db.Column(db.String(32), nullable=False)     # "item" | "bin" | "global"
    scope_id  = db.Column(db.String(36), default="", index=True)
    key       = db.Column(db.String(120), nullable=False)
    value     = db.Column(db.Text, default="")               # JSON-encoded
    updated   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("namespace", "scope", "scope_id", "key",
                            name="uq_extension_data_key"),
    )


class ExtensionSetting(db.Model):
    """Per-extension settings (backlog G5).

    AppSettings is a fixed set of columns, which works for core but cannot serve
    extensions that need their own preferences. This is the open-ended
    equivalent, namespaced the same way as ExtensionData.
    """
    __tablename__ = "extension_setting"

    id        = db.Column(db.Integer, primary_key=True)
    namespace = db.Column(db.String(64), nullable=False, index=True)
    key       = db.Column(db.String(120), nullable=False)
    value     = db.Column(db.Text, default="")               # JSON-encoded
    updated   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("namespace", "key", name="uq_extension_setting_key"),
    )


class BinLighting(db.Model):
    """Per-bin lighting configuration (backlog C4).

    Extracted from the Bin row because only a minority of bins have lighting,
    the field count keeps growing (strip topology, matrix dimensions), and a
    sparse side table keeps Bin readable.

    The legacy `bin.wled_*` columns this replaced were dropped in phase 4b
    (migration 7).
    """
    __tablename__ = "bin_lighting"

    id     = db.Column(db.Integer, primary_key=True)
    bin_id = db.Column(db.String(36), db.ForeignKey("bin.id"), nullable=False,
                       unique=True, index=True)

    enabled    = db.Column(db.Boolean, default=False)

    # Which controller drives this bin. Nullable during migration: bins
    # configured before devices existed keep working off `ip` until they are
    # matched to (or migrated into) a WledDevice row.
    device_id  = db.Column(db.String(36), db.ForeignKey("wled_device.id"),
                           default=None, index=True)
    # Legacy direct address. Still honoured when device_id is unset so nothing
    # breaks; new configuration goes through a device.
    ip         = db.Column(db.String(64), default="")

    # ── Strip topology (backlog B2) ───────────────────────────────────────
    # Read from WLED's /json/info where possible, overridable by hand when
    # detection fails or the device reports something unhelpful.
    led_count   = db.Column(db.Integer, default=0)     # total LEDs on the device
    strip_count = db.Column(db.Integer, default=1)     # number of physical strips
    auto_detect = db.Column(db.Boolean, default=True)  # refresh topology on ping

    # ── Layout: linear strip or 2D matrix (backlog B3/B4) ─────────────────
    # WLED 0.14+ reports matrix dimensions in /json/info -> leds.matrix {w,h}.
    # When the device is configured as a matrix it handles the physical wiring
    # order (serpentine or otherwise) internally, so we address by x/y and let
    # WLED map it — which is why there is no serpentine flag here.
    layout      = db.Column(db.String(16), default="strip")   # "strip" | "matrix"
    matrix_w    = db.Column(db.Integer, default=0)
    matrix_h    = db.Column(db.Integer, default=0)

    # Zone within a linear strip.
    seg_start = db.Column(db.Integer, default=0)
    seg_len   = db.Column(db.Integer, default=5)

    # Zone within a matrix (a rectangle).
    zone_x = db.Column(db.Integer, default=0)
    zone_y = db.Column(db.Integer, default=0)
    zone_w = db.Column(db.Integer, default=1)
    zone_h = db.Column(db.Integer, default=1)

    # ── Appearance ────────────────────────────────────────────────────────
    color      = db.Column(db.String(20), default="#ffffff")
    brightness = db.Column(db.Integer, default=128)
    effect     = db.Column(db.String(20), default="solid")   # solid | pulse
    duration   = db.Column(db.Integer, default=10)           # seconds, 0 = stay on

    updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WledDevice(db.Model):
    """A physical WLED controller (phase 5).

    Previously each bin stored a raw IP string. That duplicated the address
    across every bin on the same controller, so changing a device's IP meant
    editing every bin using it — and there was no way to present a list of
    known devices to pick from.

    A bin references exactly one device (confirmed with the user: a single bin's
    lighting never spans two controllers), so this is a plain foreign key rather
    than a join table.
    """
    __tablename__ = "wled_device"

    id      = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name    = db.Column(db.String(100), nullable=False)     # friendly label
    ip      = db.Column(db.String(64), nullable=False)
    is_default = db.Column(db.Boolean, default=False)

    # Topology, refreshed from /json/info when the device is pinged.
    led_count   = db.Column(db.Integer, default=0)
    layout      = db.Column(db.String(16), default="strip")  # "strip" | "matrix"
    matrix_w    = db.Column(db.Integer, default=0)
    matrix_h    = db.Column(db.Integer, default=0)
    strip_count = db.Column(db.Integer, default=1)

    # Diagnostics — so the picker can show which devices are actually reachable.
    last_seen    = db.Column(db.DateTime, default=None)
    last_status  = db.Column(db.String(200), default="")
    firmware     = db.Column(db.String(40), default="")

    created = db.Column(db.DateTime, default=datetime.utcnow)
