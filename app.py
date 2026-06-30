import os
import json
import uuid
import math
import threading
import gc
import requests as _requests
from datetime import datetime
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from PIL import Image
import io
import base64

# ── paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{BASE_DIR}/binventory.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB

db = SQLAlchemy(app)

# ── models ─────────────────────────────────────────────────────────────────────
class Bin(db.Model):
    id        = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name      = db.Column(db.String(100), nullable=False, unique=True)
    location  = db.Column(db.String(200), default="")
    color     = db.Column(db.String(20), default="#6366f1")
    created   = db.Column(db.DateTime, default=datetime.utcnow)
    items     = db.relationship("Item", backref="bin", lazy=True, cascade="all, delete-orphan")
    # WLED integration
    wled_enabled    = db.Column(db.Boolean, default=False)
    wled_ip         = db.Column(db.String(64), default="")
    wled_seg_start  = db.Column(db.Integer, default=0)    # first LED index
    wled_seg_len    = db.Column(db.Integer, default=5)    # number of LEDs
    wled_color      = db.Column(db.String(20), default="#ffffff")
    wled_brightness = db.Column(db.Integer, default=128)  # 0-255
    wled_effect     = db.Column(db.String(20), default="solid")  # solid | pulse
    wled_duration   = db.Column(db.Integer, default=10)   # seconds, 0=forever

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
    embedding   = db.Column(db.Text, default="")   # JSON float array
    notes       = db.Column(db.Text, default="")
    created     = db.Column(db.DateTime, default=datetime.utcnow)

# ── CLIP state ────────────────────────────────────────────────────────────────
_clip_model       = None
_clip_preprocess  = None
_clip_tokenizer   = None
_clip_lock        = threading.Lock()
_clip_loading     = False
_clip_ready       = False

# ── Tag queue & worker ────────────────────────────────────────────────────────
import queue as _queue_module
_tag_queue         = _queue_module.Queue()
_worker_lock       = threading.Lock()
_worker_running    = False
_auto_tag          = True
_currently_tagging = None

# ── Scheduler ─────────────────────────────────────────────────────────────────
_scheduler     = BackgroundScheduler(daemon=True)
_schedule_hour = None    # None = disabled
_schedule_min  = 0

def _load_clip():
    """Load the CLIP model into memory. Blocks until ready."""
    global _clip_model, _clip_preprocess, _clip_tokenizer, _clip_ready, _clip_loading
    with _clip_lock:
        if _clip_ready:
            return True
        if _clip_loading:
            return False
        _clip_loading = True
    try:
        import open_clip
        print("[CLIP] Loading ViT-B-32 …")
        model, _, preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="openai"
        )
        tokenizer = open_clip.get_tokenizer("ViT-B-32")
        model.eval()
        with _clip_lock:
            _clip_model      = model
            _clip_preprocess = preprocess
            _clip_tokenizer  = tokenizer
            _clip_ready      = True
            _clip_loading    = False
        print("[CLIP] Loaded ✓")
        return True
    except Exception as e:
        print(f"[CLIP] Load error: {e}")
        with _clip_lock:
            _clip_loading = False
        return False

def _unload_clip():
    """Release CLIP from memory and run garbage collection."""
    global _clip_model, _clip_preprocess, _clip_tokenizer, _clip_ready
    with _clip_lock:
        if not _clip_ready:
            return
        _clip_model      = None
        _clip_preprocess = None
        _clip_tokenizer  = None
        _clip_ready      = False
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass
    print("[CLIP] Unloaded — RAM freed")

def get_clip():
    """Return (model, preprocess, tokenizer) if loaded, else (None, None, None)."""
    with _clip_lock:
        if _clip_ready:
            return _clip_model, _clip_preprocess, _clip_tokenizer
    return None, None, None

def _ensure_worker():
    """Start the background tagging worker thread if not already running."""
    global _worker_running
    with _worker_lock:
        if _worker_running:
            return
        _worker_running = True
    threading.Thread(target=_tag_worker, daemon=True).start()

def _tag_worker():
    """
    Background thread:
      1. Load CLIP
      2. Drain the queue
      3. Unload CLIP to free RAM
    """
    global _worker_running, _currently_tagging

    print("[CLIP worker] Starting …")

    # Load model (synchronous inside this thread)
    if not _load_clip():
        print("[CLIP worker] Could not load model — aborting")
        with _worker_lock:
            _worker_running = False
        return

    print("[CLIP worker] Processing queue …")
    while True:
        try:
            item_id = _tag_queue.get(timeout=3)
        except _queue_module.Empty:
            break   # queue drained

        _currently_tagging = item_id
        try:
            with app.app_context():
                item = db.session.get(Item, item_id)
                if item and not item.embedding:
                    pil    = Image.open(UPLOAD_DIR / item.photo_path).convert("RGB")
                    labels = top_labels(pil)
                    emb    = embed_image(pil)
                    item.tags      = ", ".join(labels)
                    item.embedding = json.dumps(emb) if emb else ""
                    if not item.label and labels:
                        item.label = labels[0]
                    db.session.commit()
                    print(f"[CLIP worker] Tagged {item_id}: {item.tags[:60]}")
        except Exception as e:
            print(f"[CLIP worker] Error on {item_id}: {e}")
        finally:
            _currently_tagging = None
            _tag_queue.task_done()

    # Done — unload model
    _unload_clip()
    with _worker_lock:
        _worker_running = False
    print("[CLIP worker] Done — idle")

def enqueue_item(item_id):
    """Add item to tag queue. Starts the worker if auto-tag is on."""
    _tag_queue.put(item_id)
    if _auto_tag:
        _ensure_worker()

def _enqueue_all_untagged():
    """Queue every item that has no embedding. Called by scheduler or API."""
    with app.app_context():
        items = Item.query.filter(
            (Item.embedding == "") | (Item.embedding == None)
        ).all()
        count = len(items)
        for item in items:
            _tag_queue.put(item.id)
    if count:
        print(f"[Scheduler] Queued {count} untagged items")
        _ensure_worker()
    return count

def _apply_schedule(hour, minute):
    """Set or clear the nightly tagging schedule."""
    global _schedule_hour, _schedule_min
    _scheduler.remove_all_jobs()
    if hour is None:
        _schedule_hour = None
        print("[Scheduler] Schedule cleared")
    else:
        _schedule_hour = hour
        _schedule_min  = minute
        _scheduler.add_job(
            _enqueue_all_untagged,
            CronTrigger(hour=hour, minute=minute),
            id="nightly_tag",
            replace_existing=True,
        )
        print(f"[Scheduler] Set to run daily at {hour:02d}:{minute:02d}")

# ══════════════════════════════════════════════════════════════════════════════
# CLIP CANDIDATE LABEL POOL
# ──────────────────────────────────────────────────────────────────────────────
# These labels are used to generate the visible tag chips on each item.
# They do NOT affect semantic search (which uses full image embeddings).
#
# HOW TO CUSTOMIZE:
#   • To DISABLE a category: comment out its _LABELS list AND its entry in
#     CANDIDATE_LABELS at the bottom of this section.
#   • To ADD words: add them to the relevant category list, or put them in
#     CUSTOM_LABELS at the bottom.
#   • More labels = slightly slower tagging but better tag accuracy.
#     500–1500 labels is a comfortable range for CPU use.
# ══════════════════════════════════════════════════════════════════════════════

# ── HAND TOOLS (100 words) ────────────────────────────────────────────────────
HAND_TOOLS_LABELS = [
    "screwdriver", "Phillips screwdriver", "flathead screwdriver", "hammer",
    "claw hammer", "mallet", "rubber mallet", "wrench", "adjustable wrench",
    "combination wrench", "socket wrench", "torque wrench", "pliers",
    "needle-nose pliers", "locking pliers", "channel-lock pliers", "wire stripper",
    "wire cutter", "bolt cutter", "tin snips", "utility knife", "box cutter",
    "exacto knife", "chisel", "wood chisel", "cold chisel", "gouge",
    "hand saw", "hacksaw", "coping saw", "keyhole saw", "jab saw",
    "tape measure", "measuring tape", "ruler", "level", "torpedo level",
    "speed square", "combination square", "framing square", "protractor",
    "Allen key", "hex key", "Allen wrench", "Torx key", "spanner",
    "nut driver", "ratchet", "socket", "extension bar", "breaker bar",
    "pry bar", "crowbar", "wrecking bar", "cat's paw", "nail puller",
    "staple gun", "brad nailer", "punch", "center punch", "awl",
    "putty knife", "scraper", "floor scraper", "paint scraper", "trowel",
    "margin trowel", "notched trowel", "grout float", "caulk gun", "glue gun",
    "clamp", "C-clamp", "bar clamp", "spring clamp", "pipe clamp",
    "vise grip", "bench vise", "woodworking clamp", "strap clamp",
    "file", "rasp", "wood rasp", "metal file", "needle file",
    "sandpaper block", "sanding block", "hand plane", "block plane", "spokeshave",
    "marking gauge", "bevel gauge", "chalk line", "plumb bob", "compass",
    "nail set", "drift punch", "pin punch", "scribe", "stud finder",
    "voltage tester", "multimeter", "continuity tester",
]

# ── POWER TOOLS & EQUIPMENT (100 words) ───────────────────────────────────────
POWER_TOOLS_LABELS = [
    "power drill", "cordless drill", "hammer drill", "impact driver",
    "rotary tool", "Dremel", "oscillating tool", "multi-tool",
    "circular saw", "miter saw", "chop saw", "table saw", "band saw",
    "jigsaw", "reciprocating saw", "Sawzall", "scroll saw", "track saw",
    "angle grinder", "bench grinder", "die grinder", "belt sander",
    "orbital sander", "random orbit sander", "detail sander", "disc sander",
    "router", "palm router", "plunge router", "router table",
    "nail gun", "framing nailer", "finish nailer", "brad nailer", "pin nailer",
    "stapler", "air compressor", "air hose", "pneumatic tool", "impact wrench",
    "heat gun", "soldering iron", "soldering station", "wire welder", "MIG welder",
    "TIG welder", "plasma cutter", "torch", "propane torch", "MAP gas torch",
    "laser level", "laser measure", "laser distance meter",
    "tile saw", "wet saw", "concrete grinder", "jackhammer", "rotary hammer",
    "chain saw", "pole saw", "hedge trimmer", "leaf blower", "string trimmer",
    "pressure washer", "shop vacuum", "wet-dry vac", "dust collector",
    "drill press", "lathe", "wood lathe", "metal lathe",
    "power supply", "battery charger", "battery pack", "tool battery",
    "extension cord", "power strip", "GFCI outlet", "work light", "trouble light",
    "generator", "inverter", "surge protector",
    "pipe threader", "pipe cutter", "conduit bender", "fish tape",
    "cable puller", "wire puller", "knockout punch", "hole saw",
    "step drill bit", "spade bit", "Forstner bit", "masonry bit",
    "drill bit set", "router bit", "saw blade", "grinding wheel", "cutting disc",
]

# ── PARTS & HARDWARE — FASTENERS (100 words) ──────────────────────────────────
FASTENERS_LABELS = [
    "screw", "wood screw", "machine screw", "sheet metal screw", "self-tapping screw",
    "drywall screw", "deck screw", "lag screw", "lag bolt", "set screw",
    "bolt", "hex bolt", "carriage bolt", "eye bolt", "U-bolt", "J-bolt",
    "anchor bolt", "stud bolt", "shoulder bolt", "thumb screw",
    "nut", "hex nut", "nylon lock nut", "wing nut", "cap nut", "flange nut",
    "coupling nut", "T-nut", "square nut", "acorn nut",
    "washer", "flat washer", "lock washer", "split washer", "fender washer",
    "finishing washer", "sealing washer", "copper washer",
    "nail", "common nail", "finish nail", "brad nail", "roofing nail",
    "ring shank nail", "spiral nail", "duplex nail", "joist hanger nail",
    "staple", "brad", "pin", "cotter pin", "clevis pin", "roll pin", "dowel pin",
    "rivet", "pop rivet", "blind rivet", "solid rivet", "drive rivet",
    "anchor", "wall anchor", "toggle bolt", "Molly bolt", "concrete anchor",
    "wedge anchor", "sleeve anchor", "drop-in anchor", "lead anchor",
    "threaded insert", "barrel nut", "sex bolt", "Chicago screw",
    "hanger bolt", "dowel screw", "drive screw",
    "clip", "retaining clip", "snap ring", "E-clip", "circlip",
    "spring pin", "hairpin cotter", "lynch pin", "hitch pin",
    "zip tie", "cable tie", "wire loom", "wire clip", "wire staple",
    "hook and loop", "velcro strip", "adhesive anchor", "double-sided tape",
    "screw cup", "screw cover cap", "plastic anchor", "drywall anchor",
    "concrete screw", "Tapcon", "masonry screw",
]

# ── PARTS & HARDWARE — PLUMBING & ELECTRICAL (100 words) ─────────────────────
PLUMBING_ELECTRICAL_LABELS = [
    "pipe", "PVC pipe", "copper pipe", "galvanized pipe", "ABS pipe",
    "PEX tubing", "flexible hose", "garden hose", "drain hose",
    "elbow fitting", "90-degree elbow", "45-degree elbow", "street elbow",
    "tee fitting", "reducing tee", "cross fitting", "coupling", "union",
    "reducer", "bushing", "adapter", "cap", "plug", "nipple", "close nipple",
    "valve", "ball valve", "gate valve", "check valve", "shutoff valve",
    "pressure relief valve", "solenoid valve", "needle valve",
    "faucet", "faucet cartridge", "faucet stem", "O-ring", "washer",
    "p-trap", "trap arm", "drain assembly", "flange", "wax ring",
    "pipe wrench", "plumber's tape", "Teflon tape", "pipe dope", "flux",
    "solder", "pipe sealant", "compression fitting", "flare fitting",
    "wire", "electrical wire", "romex", "conduit", "EMT conduit",
    "wire nut", "wire connector", "butt connector", "ring terminal",
    "spade terminal", "push-in connector", "lever nut",
    "electrical box", "junction box", "outlet box", "switch box",
    "outlet", "receptacle", "switch", "GFCI outlet", "AFCI breaker",
    "circuit breaker", "fuse", "fuse holder", "panel", "bus bar",
    "ground wire", "neutral wire", "hot wire", "electrical tape",
    "heat shrink tubing", "cable clamp", "conduit fitting", "pull box",
    "wire fish tape", "grommet", "strain relief", "cable entry",
    "light switch", "dimmer switch", "timer switch", "motion sensor",
    "outlet cover", "switch plate", "blank cover", "cable staple",
]

# ── ADHESIVES, SEALANTS & FINISHING (100 words) ───────────────────────────────
ADHESIVES_FINISHING_LABELS = [
    "super glue", "cyanoacrylate", "epoxy", "two-part epoxy", "epoxy putty",
    "construction adhesive", "liquid nails", "wood glue", "PVA glue",
    "contact cement", "rubber cement", "spray adhesive", "hot glue stick",
    "caulk", "silicone caulk", "acrylic caulk", "latex caulk", "paintable caulk",
    "weatherstripping", "foam tape", "butyl tape", "flashing tape",
    "duct tape", "masking tape", "painter's tape", "gaffer tape", "electrical tape",
    "double-sided tape", "carpet tape", "mounting tape", "foam mounting tape",
    "paint", "latex paint", "oil paint", "primer", "spray paint", "aerosol paint",
    "paint can", "paint tray", "paint roller", "paint brush", "foam brush",
    "chip brush", "paint edger", "drop cloth", "plastic sheeting",
    "sandpaper", "sanding disc", "sanding belt", "steel wool", "scotch-brite pad",
    "wood filler", "wood putty", "spackling compound", "joint compound",
    "patching plaster", "concrete patch", "hydraulic cement", "grout",
    "tile adhesive", "mortar", "thinset", "mastic",
    "stain", "wood stain", "gel stain", "deck stain", "fence stain",
    "varnish", "polyurethane", "lacquer", "shellac", "wax", "paste wax",
    "mineral spirits", "paint thinner", "acetone", "denatured alcohol",
    "stripper", "paint remover", "rust remover", "chemical stripper",
    "wire brush", "paint roller cover", "paint roller frame", "extension pole",
    "caulk remover", "grout saw", "grout remover",
]

# ── KITCHEN (200 words) ───────────────────────────────────────────────────────
KITCHEN_LABELS = [
    "plate", "dinner plate", "salad plate", "side plate", "bowl", "mixing bowl",
    "soup bowl", "cereal bowl", "serving bowl", "cup", "mug", "coffee mug",
    "tea cup", "glass", "drinking glass", "wine glass", "champagne flute",
    "tumbler", "mason jar", "water bottle", "thermos", "travel mug",
    "pot", "saucepan", "stockpot", "Dutch oven", "pressure cooker",
    "slow cooker", "pan", "frying pan", "skillet", "cast iron skillet",
    "wok", "saute pan", "griddle", "grill pan", "baking sheet",
    "cookie sheet", "baking pan", "cake pan", "muffin tin", "loaf pan",
    "pie dish", "casserole dish", "roasting pan", "broiler pan",
    "knife", "chef's knife", "paring knife", "bread knife", "serrated knife",
    "boning knife", "fillet knife", "cleaver", "steak knife", "butter knife",
    "fork", "dinner fork", "salad fork", "serving fork", "carving fork",
    "spoon", "tablespoon", "teaspoon", "serving spoon", "ladle",
    "slotted spoon", "wooden spoon", "spatula", "rubber spatula",
    "fish spatula", "tongs", "whisk", "hand mixer", "stand mixer",
    "can opener", "bottle opener", "corkscrew", "peeler", "grater",
    "zester", "mandoline", "colander", "strainer", "sieve", "funnel",
    "cutting board", "chopping block", "rolling pin", "pastry brush",
    "pastry cutter", "cookie cutter", "bench scraper", "kitchen shears",
    "kitchen timer", "meat thermometer", "candy thermometer",
    "measuring cup", "measuring spoon", "kitchen scale", "food scale",
    "blender", "immersion blender", "food processor", "toaster", "toaster oven",
    "microwave", "air fryer", "instant pot", "rice cooker", "waffle maker",
    "panini press", "electric kettle", "coffee maker", "espresso machine",
    "French press", "pour-over dripper", "coffee grinder", "juicer",
    "ice cream maker", "bread machine", "dehydrator", "sous vide",
    "plate rack", "dish rack", "drying rack", "dish towel", "oven mitt",
    "pot holder", "apron", "kitchen towel", "paper towel holder",
    "spice rack", "spice jar", "salt shaker", "pepper grinder",
    "oil bottle", "vinegar bottle", "condiment bottle", "squeeze bottle",
    "food container", "Tupperware", "storage container", "lunch box",
    "zip-lock bag", "plastic wrap", "aluminum foil", "parchment paper",
    "wax paper", "food storage bag", "vacuum sealer bag",
    "napkin holder", "paper towel", "plastic utensil", "chopstick",
    "place mat", "trivet", "pot rack", "knife block", "utensil holder",
    "drawer organizer", "cabinet organizer", "lazy Susan",
]

# ── BATHROOM & PERSONAL CARE (200 words) ──────────────────────────────────────
BATHROOM_LABELS = [
    "toothbrush", "electric toothbrush", "toothbrush head", "toothpaste",
    "dental floss", "floss pick", "mouthwash", "tongue scraper",
    "shampoo", "conditioner", "hair mask", "dry shampoo", "hair oil",
    "body wash", "soap bar", "liquid soap", "hand soap", "face wash",
    "facial cleanser", "toner", "serum", "moisturizer", "face cream",
    "eye cream", "sunscreen", "SPF lotion", "body lotion", "body butter",
    "exfoliator", "face scrub", "body scrub", "loofa", "bath sponge",
    "washcloth", "bath towel", "hand towel", "face towel", "bath mat",
    "shower curtain", "shower liner", "shower hook", "shower caddy",
    "toilet paper", "tissue box", "paper towel", "cotton ball", "cotton swab",
    "Q-tip", "cotton pad", "makeup remover pad", "makeup wipe",
    "razor", "safety razor", "cartridge razor", "electric razor", "shaver",
    "shaving cream", "shaving gel", "aftershave", "shaving brush",
    "deodorant", "antiperspirant", "body spray", "perfume", "cologne",
    "makeup bag", "cosmetic bag", "lipstick", "lip gloss", "lip balm",
    "mascara", "eyeliner", "eyeshadow", "foundation", "concealer",
    "blush", "bronzer", "highlighter", "setting powder", "setting spray",
    "makeup brush", "foundation brush", "blending sponge", "beauty blender",
    "eyelash curler", "tweezers", "nail clippers", "nail file", "nail buffer",
    "nail polish", "nail polish remover", "cuticle pusher", "cuticle oil",
    "hair brush", "comb", "wide-tooth comb", "detangling brush",
    "hair dryer", "blow dryer", "diffuser attachment", "hair straightener",
    "flat iron", "curling iron", "curling wand", "hair roller",
    "bobby pin", "hair clip", "hair tie", "scrunchie", "headband",
    "hair spray", "hair gel", "pomade", "hair wax", "mousse",
    "medicine cabinet", "first aid kit", "bandage", "adhesive bandage",
    "gauze", "medical tape", "antiseptic", "hydrogen peroxide", "alcohol wipe",
    "antibiotic ointment", "pain reliever", "ibuprofen", "aspirin",
    "cold medicine", "allergy medicine", "antacid", "vitamins", "supplements",
    "pill bottle", "prescription bottle", "medicine organizer", "pill case",
    "thermometer", "blood pressure cuff", "pulse oximeter", "heating pad",
    "ice pack", "ace bandage", "compression wrap", "medical glove",
    "toilet brush", "toilet plunger", "bathroom cleaner", "tile cleaner",
    "grout brush", "bathroom scale", "bath bomb", "bath salts", "bubble bath",
]

# ── OFFICE & STATIONERY (200 words) ───────────────────────────────────────────
OFFICE_LABELS = [
    "pen", "ballpoint pen", "gel pen", "rollerball pen", "fountain pen",
    "felt-tip pen", "marker", "permanent marker", "dry-erase marker",
    "highlighter", "pencil", "mechanical pencil", "colored pencil",
    "pencil sharpener", "eraser", "correction fluid", "white-out",
    "correction tape", "tape dispenser", "scotch tape", "packing tape",
    "notebook", "spiral notebook", "composition notebook", "legal pad",
    "sticky note", "Post-it note", "index card", "flash card",
    "folder", "manila folder", "hanging folder", "pocket folder",
    "binder", "three-ring binder", "binder clip", "paper clip",
    "stapler", "staple remover", "staple", "hole punch", "three-hole punch",
    "scissors", "letter opener", "ruler", "straightedge", "protractor",
    "compass", "triangle", "T-square", "set square",
    "paper", "printer paper", "copy paper", "graph paper", "cardstock",
    "envelope", "mailing envelope", "padded envelope", "bubble mailer",
    "label", "mailing label", "address label", "file label",
    "rubber band", "binder ring", "book ring", "fastener",
    "calculator", "adding machine", "desk organizer", "pencil cup",
    "inbox tray", "paper tray", "file sorter", "desktop organizer",
    "calendar", "planner", "day planner", "wall calendar", "desk calendar",
    "sticky tape", "washi tape", "masking tape", "label maker",
    "laminator", "laminating pouch", "shredder", "paper shredder",
    "printer", "inkjet printer", "laser printer", "printer ink", "toner cartridge",
    "scanner", "fax machine", "copier", "multifunction printer",
    "computer", "laptop", "desktop computer", "tablet", "iPad",
    "keyboard", "mouse", "trackpad", "mousepad", "monitor", "screen",
    "webcam", "microphone", "headset", "speaker", "USB hub",
    "hard drive", "external hard drive", "USB drive", "flash drive",
    "SD card", "memory card", "cable", "USB cable", "HDMI cable",
    "ethernet cable", "power cable", "charging cable", "adapter",
    "phone", "smartphone", "cell phone", "charger", "wireless charger",
    "power bank", "battery pack", "laptop bag", "briefcase", "backpack",
    "whiteboard", "corkboard", "bulletin board", "pushpin", "thumbtack",
    "dry-erase board", "easel", "presentation board",
    "stamp", "ink pad", "date stamp", "rubber stamp", "embosser",
    "book", "textbook", "manual", "reference book", "dictionary",
    "magazine", "journal", "catalog", "brochure", "pamphlet",
    "document", "report", "contract", "form", "receipt", "invoice",
    "business card", "ID badge", "badge holder", "lanyard",
    "map", "atlas", "poster", "chart", "diagram",
]

# ── BEDROOM & CLOTHING (200 words) ────────────────────────────────────────────
BEDROOM_CLOTHING_LABELS = [
    "shirt", "T-shirt", "polo shirt", "button-down shirt", "dress shirt",
    "blouse", "tank top", "camisole", "undershirt", "base layer",
    "pants", "jeans", "trousers", "chinos", "leggings", "sweatpants",
    "shorts", "cargo shorts", "athletic shorts", "swim trunks", "swimsuit",
    "dress", "skirt", "maxi skirt", "mini skirt", "wrap skirt",
    "jacket", "coat", "blazer", "sport coat", "windbreaker",
    "hoodie", "sweatshirt", "cardigan", "sweater", "pullover", "fleece",
    "vest", "down vest", "puffer vest", "rain jacket", "raincoat",
    "shoe", "sneaker", "running shoe", "walking shoe", "dress shoe",
    "boot", "ankle boot", "work boot", "rain boot", "hiking boot",
    "sandal", "flip flop", "slip-on", "loafer", "moccasin",
    "sock", "ankle sock", "crew sock", "knee sock", "compression sock",
    "underwear", "briefs", "boxers", "bra", "sports bra",
    "belt", "leather belt", "suspenders", "tie", "necktie", "bow tie",
    "scarf", "hat", "baseball cap", "beanie", "winter hat", "sun hat",
    "glove", "winter glove", "work glove", "rubber glove", "gardening glove",
    "mitten", "watch", "sunglasses", "reading glasses", "glasses case",
    "wallet", "purse", "handbag", "tote bag", "clutch", "backpack",
    "luggage", "suitcase", "duffel bag", "carry-on bag", "garment bag",
    "pillow", "throw pillow", "body pillow", "pillow case", "pillow cover",
    "blanket", "throw blanket", "comforter", "duvet", "duvet cover",
    "sheet", "bed sheet", "fitted sheet", "flat sheet", "mattress pad",
    "mattress cover", "mattress protector", "bed skirt", "quilt",
    "towel", "bath towel", "beach towel", "hand towel", "washcloth",
    "robe", "bathrobe", "pajamas", "nightgown", "sleep shirt",
    "hanger", "clothes hanger", "velvet hanger", "pants hanger", "skirt hanger",
    "laundry bag", "laundry basket", "hamper", "clothes pin",
    "iron", "clothes iron", "steamer", "ironing board",
    "sewing kit", "needle", "thread", "button", "zipper", "safety pin",
    "lint roller", "fabric shaver", "clothes brush", "shoe horn",
    "shoe rack", "boot tray", "cedar shoe tree", "shoe box",
    "storage box", "under-bed storage", "vacuum storage bag",
    "jewelry box", "necklace", "bracelet", "ring", "earring", "brooch",
]

# ── LIVING ROOM & ENTERTAINMENT (200 words) ───────────────────────────────────
LIVING_ROOM_LABELS = [
    "remote control", "TV remote", "universal remote", "streaming remote",
    "television", "TV", "monitor", "projector", "projection screen",
    "cable box", "streaming device", "Apple TV", "Roku", "Fire Stick",
    "game console", "PlayStation", "Xbox", "Nintendo Switch", "controller",
    "gaming controller", "joystick", "headset", "gaming headset",
    "speaker", "Bluetooth speaker", "bookshelf speaker", "subwoofer",
    "soundbar", "home theater", "amplifier", "receiver", "turntable",
    "record player", "vinyl record", "CD player", "CD", "DVD", "Blu-ray",
    "headphones", "earbuds", "earphones", "wireless headphones", "AirPods",
    "book", "novel", "comic book", "graphic novel", "coffee table book",
    "magazine", "newspaper", "e-reader", "Kindle",
    "board game", "card game", "playing cards", "poker chips", "dice",
    "puzzle", "jigsaw puzzle", "chess set", "checkers", "dominoes",
    "Scrabble", "Monopoly", "game piece", "game token",
    "picture frame", "photo frame", "canvas print", "art print", "poster",
    "vase", "flower pot", "plant pot", "succulent", "artificial plant",
    "candle", "pillar candle", "tea light", "candle holder", "lantern",
    "statue", "figurine", "sculpture", "decorative bowl", "decorative tray",
    "clock", "wall clock", "mantel clock", "alarm clock", "desk clock",
    "throw pillow", "accent pillow", "decorative pillow", "cushion",
    "blanket", "throw", "knit blanket", "fleece blanket",
    "lamp", "table lamp", "floor lamp", "desk lamp", "reading lamp",
    "light bulb", "LED bulb", "smart bulb", "dimmer", "light switch",
    "extension cord", "power strip", "surge protector",
    "coaster", "drink coaster", "coffee table coaster",
    "ashtray", "incense holder", "diffuser", "essential oil",
    "photo album", "scrapbook", "memory box", "keepsake box",
    "umbrella", "rain umbrella", "compact umbrella", "umbrella stand",
    "coat rack", "hat rack", "key holder", "mail organizer",
    "tissue box", "tissue holder", "napkin holder",
    "air purifier", "humidifier", "dehumidifier", "fan", "space heater",
    "fireplace tool", "fire poker", "log holder", "fire screen",
    "window treatment", "curtain rod", "curtain", "blind", "shade",
]

# ── KIDS & TOYS (200 words) ───────────────────────────────────────────────────
KIDS_TOYS_LABELS = [
    "toy", "action figure", "doll", "Barbie", "stuffed animal", "plush toy",
    "teddy bear", "puppet", "marionette", "finger puppet",
    "building blocks", "LEGO", "LEGO brick", "LEGO set", "Duplo",
    "Lincoln Logs", "K'Nex", "Mega Bloks", "magnetic tiles",
    "toy car", "die-cast car", "Hot Wheels", "toy truck", "toy train",
    "train set", "track piece", "remote control car", "RC car",
    "toy airplane", "toy boat", "toy helicopter", "toy robot",
    "ball", "basketball", "football", "soccer ball", "tennis ball",
    "baseball", "softball", "volleyball", "dodgeball", "bouncy ball",
    "bike", "bicycle", "tricycle", "scooter", "skateboard", "roller skate",
    "jump rope", "hula hoop", "frisbee", "yo-yo", "kite",
    "puzzle", "wooden puzzle", "floor puzzle", "shape sorter", "peg puzzle",
    "board game", "card game", "matching game", "memory game",
    "art supply", "crayon", "marker", "colored pencil", "watercolor",
    "paint set", "finger paint", "chalk", "sidewalk chalk",
    "coloring book", "activity book", "sticker book", "sticker sheet",
    "craft kit", "bead kit", "friendship bracelet kit", "jewelry kit",
    "play-doh", "modeling clay", "kinetic sand", "slime", "putty",
    "science kit", "chemistry set", "telescope", "microscope", "magnet kit",
    "instrument", "toy piano", "xylophone", "toy guitar", "drum",
    "recorder", "harmonica", "toy microphone", "karaoke machine",
    "dress-up costume", "Halloween costume", "superhero costume",
    "play kitchen", "toy food", "toy dishes", "tea set", "tool set",
    "doctor kit", "stethoscope", "play cash register",
    "doll house", "doll furniture", "doll clothes", "doll accessories",
    "stuffed dinosaur", "stuffed unicorn", "stuffed elephant",
    "squirt gun", "water gun", "nerf gun", "foam dart",
    "game controller", "handheld game", "video game cartridge",
    "bath toy", "rubber duck", "bath boat", "bath crayons",
    "sandbox toy", "bucket", "shovel", "rake", "mold",
    "chalk board", "magnetic drawing board", "Etch A Sketch",
    "Play-Doh mold", "cookie cutter set", "stamp set",
    "backpack", "lunch box", "pencil case", "school supply",
    "flashcard", "learning toy", "alphabet toy", "number toy",
]

# ── SEASONAL & HOLIDAY (200 words) ────────────────────────────────────────────
SEASONAL_HOLIDAY_LABELS = [
    "Christmas ornament", "glass ornament", "ball ornament", "icicle ornament",
    "tree topper", "star topper", "angel topper", "tree skirt",
    "Christmas lights", "string lights", "icicle lights", "LED lights",
    "Christmas stocking", "stocking hanger", "advent calendar",
    "nativity scene", "figurine", "wreath", "door wreath", "garland",
    "Christmas tree", "artificial tree", "tree stand", "tree bag",
    "tinsel", "ribbon", "bow", "gift bow", "curling ribbon",
    "wrapping paper", "gift wrap", "tissue paper", "gift bag", "gift box",
    "gift tag", "gift card", "Christmas card", "holiday card",
    "Santa Claus", "snowman decoration", "reindeer decoration",
    "nutcracker", "snow globe", "music box", "holiday figurine",
    "Halloween decoration", "jack-o-lantern", "pumpkin", "carved pumpkin",
    "skull decoration", "ghost decoration", "witch hat", "candy bucket",
    "Easter basket", "Easter egg", "plastic Easter egg", "egg decorating kit",
    "Easter grass", "bunny decoration", "spring wreath",
    "Valentine decoration", "heart decoration", "Valentine card",
    "Fourth of July decoration", "patriotic flag", "American flag",
    "Thanksgiving decoration", "fall wreath", "harvest decoration",
    "cornucopia", "fall leaves", "pumpkin decoration", "scarecrow",
    "Hanukkah menorah", "dreidel", "Hanukkah decoration",
    "Kwanzaa decoration", "kinara",
    "party decoration", "balloon", "party banner", "party streamer",
    "confetti", "party hat", "party favor", "noisemaker", "pinata",
    "birthday candle", "cake topper", "birthday decoration",
    "tablecloth", "table runner", "centerpiece", "place card",
    "outdoor decoration", "yard stake", "inflatable decoration",
    "light-up decoration", "projection light", "laser light",
    "extension cord", "outdoor extension cord", "timer", "light timer",
    "storage box", "ornament box", "decoration storage", "tote",
    "wreath storage", "tree storage bag", "light reel",
    "seasonal towel", "holiday dish", "seasonal mug", "holiday plate",
    "candle", "holiday candle", "scented candle", "taper candle", "pillar candle",
]

# ── GARDEN & OUTDOOR (200 words) ──────────────────────────────────────────────
GARDEN_OUTDOOR_LABELS = [
    "trowel", "hand trowel", "transplanting trowel", "bulb planter",
    "garden fork", "hand fork", "cultivator", "weeder", "hoe",
    "rake", "leaf rake", "garden rake", "bow rake", "thatch rake",
    "shovel", "spade", "round-point shovel", "square shovel", "edger",
    "pruning shears", "pruner", "loppers", "hedge shears", "hedge trimmer",
    "hand saw", "pruning saw", "pole pruner", "bow saw",
    "garden hose", "soaker hose", "hose nozzle", "spray nozzle", "hose reel",
    "sprinkler", "oscillating sprinkler", "impact sprinkler", "drip system",
    "watering can", "garden cart", "wheelbarrow", "garden kneeler",
    "knee pad", "gardening glove", "rubber glove", "leather glove",
    "plant pot", "terra cotta pot", "plastic pot", "ceramic pot", "hanging basket",
    "window box", "raised bed", "planter box", "grow bag",
    "seed packet", "seed tray", "seed starter", "seedling tray",
    "potting soil", "garden soil", "compost", "mulch", "peat moss",
    "fertilizer", "plant food", "slow-release fertilizer", "liquid fertilizer",
    "pesticide", "herbicide", "insecticide", "fungicide", "weed killer",
    "plant stake", "garden stake", "bamboo stake", "tomato cage",
    "trellis", "garden netting", "row cover", "frost cloth",
    "garden twine", "plant tie", "wire tie", "clip",
    "bird feeder", "bird bath", "bird house", "suet cage",
    "wind chime", "garden ornament", "garden gnome", "stepping stone",
    "landscape fabric", "ground cloth", "weed barrier", "edging",
    "garden border", "lawn edging", "plastic edging", "metal edging",
    "lawnmower", "push mower", "riding mower", "reel mower", "robotic mower",
    "weed whacker", "string trimmer", "edger", "blower", "leaf blower",
    "cultivator", "rototiller", "aerator", "dethatcher", "roller",
    "irrigation timer", "water timer", "hose timer", "drip timer",
    "outdoor furniture", "garden chair", "patio chair", "Adirondack chair",
    "garden bench", "picnic table", "patio umbrella", "outdoor cushion",
    "BBQ grill", "gas grill", "charcoal grill", "smoker", "grill brush",
    "grill tongs", "charcoal", "propane tank", "fire pit", "patio heater",
    "outdoor light", "solar light", "pathway light", "string light",
    "extension cord", "outdoor extension cord", "rain gauge", "thermometer",
    "compost bin", "compost tumbler", "worm bin",
]

# ── CLEANING & LAUNDRY (200 words) ────────────────────────────────────────────
CLEANING_LAUNDRY_LABELS = [
    "mop", "spin mop", "string mop", "flat mop", "steam mop", "mop bucket",
    "broom", "push broom", "angle broom", "corn broom", "dustpan",
    "vacuum cleaner", "upright vacuum", "canister vacuum", "robot vacuum",
    "handheld vacuum", "stick vacuum", "vacuum bag", "vacuum filter",
    "scrub brush", "toilet brush", "grout brush", "tile brush",
    "sponge", "scrubbing sponge", "dish sponge", "cellulose sponge",
    "cloth", "microfiber cloth", "cleaning cloth", "dust cloth", "mop cloth",
    "cleaning glove", "rubber glove", "latex glove", "nitrile glove",
    "spray bottle", "trigger sprayer", "pump sprayer", "mist bottle",
    "bucket", "mop bucket", "cleaning bucket", "pail",
    "all-purpose cleaner", "multi-surface cleaner", "spray cleaner",
    "disinfectant", "disinfecting wipe", "bleach", "chlorine bleach",
    "bathroom cleaner", "toilet bowl cleaner", "tile cleaner", "tub cleaner",
    "glass cleaner", "window cleaner", "streak-free cleaner",
    "kitchen cleaner", "degreaser", "oven cleaner", "stovetop cleaner",
    "dish soap", "dish detergent", "dishwasher pod", "dishwasher tablet",
    "dishwasher powder", "dish liquid", "hand dish soap",
    "laundry detergent", "liquid detergent", "powder detergent", "laundry pod",
    "laundry pack", "fabric softener", "dryer sheet", "static guard",
    "stain remover", "stain stick", "OxiClean", "color-safe bleach",
    "laundry bag", "mesh laundry bag", "delicate bag", "lingerie bag",
    "washing machine", "dryer", "washer-dryer combo", "laundry basket",
    "hamper", "clothespin", "drying rack", "clothes horse", "laundry line",
    "iron", "steam iron", "travel iron", "steamer", "garment steamer",
    "ironing board", "ironing board cover", "sleeve board",
    "lint roller", "lint brush", "pet hair remover",
    "trash bag", "garbage bag", "compost bag", "recycling bag",
    "trash can", "waste basket", "recycling bin", "compost bin",
    "paper towel", "paper towel roll", "cleaning wipe", "disinfecting wipe",
    "disposable glove", "face mask", "N95 mask", "respirator mask",
    "duster", "feather duster", "microfiber duster", "telescoping duster",
    "squeegee", "window squeegee", "shower squeegee", "floor squeegee",
    "drain cleaner", "drain snake", "pipe cleaner", "drain strainer",
    "air freshener", "odor eliminator", "deodorizer", "carpet deodorizer",
    "furniture polish", "wood cleaner", "leather cleaner", "stainless steel cleaner",
]

# ── SPORTS & RECREATION (200 words) ───────────────────────────────────────────
SPORTS_RECREATION_LABELS = [
    "basketball", "basketball hoop", "basketball net", "basketball pump",
    "football", "football helmet", "shoulder pads", "football cleat",
    "soccer ball", "soccer cleat", "shin guard", "soccer goal", "net",
    "baseball", "baseball bat", "aluminum bat", "wood bat", "batting glove",
    "baseball glove", "catcher's mitt", "fielder's glove", "batting helmet",
    "tennis ball", "tennis racket", "tennis racquet", "racket strings",
    "golf ball", "golf club", "iron", "wood", "putter", "golf bag", "golf tee",
    "volleyball", "volleyball net", "kneepads",
    "ping pong ball", "ping pong paddle", "table tennis paddle",
    "badminton birdie", "shuttlecock", "badminton racket", "badminton net",
    "bicycle", "road bike", "mountain bike", "helmet", "bike lock",
    "bike pump", "bike light", "bike rack", "bike bag", "cycling glove",
    "skateboard", "longboard", "scooter", "rollerblade", "roller skate",
    "skate helmet", "skate pad", "wrist guard",
    "running shoe", "trail shoe", "hiking boot", "hiking shoe",
    "hiking backpack", "trekking pole", "trail map", "compass", "headlamp",
    "water bottle", "hydration pack", "Camelbak", "sport bottle",
    "yoga mat", "exercise mat", "foam roller", "resistance band",
    "jump rope", "kettlebell", "dumbbell", "barbell", "weight plate",
    "pull-up bar", "push-up handle", "ab wheel", "balance board",
    "treadmill", "stationary bike", "rowing machine", "elliptical",
    "weight bench", "squat rack", "power rack",
    "boxing glove", "punching bag", "speed bag", "jump rope", "wraps",
    "swim goggles", "swim cap", "fins", "kickboard", "pull buoy",
    "snorkel", "mask", "wetsuit", "surfboard", "paddleboard", "kayak paddle",
    "fishing rod", "fishing reel", "tackle box", "fishing lure", "fishing line",
    "fishing hook", "bobber", "sinker", "net", "fishing vest",
    "camping tent", "sleeping bag", "sleeping pad", "camp stove", "camp fuel",
    "lantern", "headlamp", "flashlight", "multi-tool", "pocket knife",
    "cooler", "ice chest", "bear canister", "water filter",
    "climbing harness", "carabiner", "climbing rope", "chalk bag",
    "hunting vest", "camouflage", "game call", "binoculars", "spotting scope",
    "archery bow", "arrow", "quiver", "target",
]

# ── AUTOMOTIVE & GARAGE (200 words) ───────────────────────────────────────────
AUTOMOTIVE_LABELS = [
    "motor oil", "engine oil", "oil filter", "oil drain pan", "funnel",
    "antifreeze", "coolant", "radiator fluid", "brake fluid", "power steering fluid",
    "transmission fluid", "differential fluid", "gear oil",
    "windshield washer fluid", "de-icer", "rain repellent",
    "car wax", "car polish", "detailing spray", "clay bar", "buffer pad",
    "car wash soap", "wash mitt", "chamois", "microfiber towel",
    "tire pressure gauge", "air chuck", "inflator", "portable compressor",
    "jack", "floor jack", "bottle jack", "scissor jack", "jack stand",
    "lug wrench", "breaker bar", "torque wrench", "impact wrench",
    "jumper cable", "jump starter", "battery charger", "battery tester",
    "OBD scanner", "code reader", "scan tool",
    "spark plug", "spark plug socket", "ignition coil", "distributor cap",
    "air filter", "cabin air filter", "fuel filter",
    "brake pad", "brake rotor", "brake caliper", "brake line", "brake hose",
    "wiper blade", "wiper arm", "headlight bulb", "tail light bulb",
    "fuse", "fuse puller", "fuse box", "relay", "fuse holder",
    "serpentine belt", "timing belt", "drive belt", "belt tensioner",
    "hose clamp", "radiator hose", "heater hose", "coolant hose",
    "gasket", "head gasket", "valve cover gasket", "exhaust gasket",
    "exhaust pipe", "muffler", "catalytic converter", "heat shield",
    "tie rod", "ball joint", "control arm", "sway bar link",
    "shock absorber", "strut", "coil spring", "leaf spring",
    "wheel bearing", "hub bearing", "axle", "CV joint", "CV boot",
    "tire", "wheel", "rim", "hubcap", "center cap",
    "tire iron", "bead breaker", "wheel weight", "valve stem",
    "funnel", "turkey baster", "oil catch", "drain plug",
    "creeper", "mechanic creeper", "work glove", "shop rag",
    "car cover", "seat cover", "floor mat", "trunk liner",
    "touch-up paint", "body filler", "primer", "clear coat",
    "WD-40", "penetrating oil", "anti-seize", "threadlocker", "Loctite",
    "degreaser", "brake cleaner", "carburetor cleaner", "parts washer",
    "tow strap", "recovery strap", "D-ring shackle", "chain", "hook",
    "parking sensor", "backup camera", "dash cam",
]

# ══════════════════════════════════════════════════════════════════════════════
# CUSTOM LABELS — Add your own words here (no limit)
# These are always included regardless of which categories are enabled above.
#
# Examples:
#   "my specific label", "another label", "brand name item",
# ══════════════════════════════════════════════════════════════════════════════
CUSTOM_LABELS = [
    # Add your own words here:
    # "vintage camera", "sewing machine", "microscope", "telescope",
]

# ══════════════════════════════════════════════════════════════════════════════
# ACTIVE LABEL POOL
# Comment out any line below to disable that entire category.
# Restart the server after making changes.
# ══════════════════════════════════════════════════════════════════════════════
CANDIDATE_LABELS = list(dict.fromkeys([   # dict.fromkeys deduplicates while preserving order
    *HAND_TOOLS_LABELS,           # ~100 words — hand tools
    *POWER_TOOLS_LABELS,          # ~100 words — power tools & equipment
    *FASTENERS_LABELS,            # ~100 words — screws, bolts, nuts, anchors
    *PLUMBING_ELECTRICAL_LABELS,  # ~100 words — pipe fittings, wire, outlets
    *ADHESIVES_FINISHING_LABELS,  # ~100 words — glues, paints, caulks
    *KITCHEN_LABELS,              # ~200 words — cookware, appliances, utensils
    *BATHROOM_LABELS,             # ~200 words — toiletries, medicine, personal care
    *OFFICE_LABELS,               # ~200 words — stationery, electronics, supplies
    *BEDROOM_CLOTHING_LABELS,     # ~200 words — clothes, linens, accessories
    *LIVING_ROOM_LABELS,          # ~200 words — entertainment, décor, media
    *KIDS_TOYS_LABELS,            # ~200 words — toys, games, craft supplies
    *SEASONAL_HOLIDAY_LABELS,     # ~200 words — holiday decorations, party supplies
    *GARDEN_OUTDOOR_LABELS,       # ~200 words — garden tools, plants, outdoor gear
    *CLEANING_LAUNDRY_LABELS,     # ~200 words — cleaning products, laundry
    *SPORTS_RECREATION_LABELS,    # ~200 words — sports equipment, fitness, camping
    *AUTOMOTIVE_LABELS,           # ~200 words — car parts, fluids, tools
    *CUSTOM_LABELS,               # your own custom words
]))

def embed_image(pil_image):
    """Return a float list embedding for a PIL image, or None if CLIP not ready."""
    import torch
    model, preprocess, _ = get_clip()
    if model is None:
        return None
    with torch.no_grad():
        tensor = preprocess(pil_image).unsqueeze(0)
        feat   = model.encode_image(tensor)
        feat   = feat / feat.norm(dim=-1, keepdim=True)
    return feat[0].tolist()

def embed_text(text):
    """Return a float list embedding for a text query."""
    import torch
    model, _, tokenizer = get_clip()
    if model is None:
        return None
    with torch.no_grad():
        tokens = tokenizer([text])
        feat   = model.encode_text(tokens)
        feat   = feat / feat.norm(dim=-1, keepdim=True)
    return feat[0].tolist()

def top_labels(pil_image, top_k=8):
    """Score CANDIDATE_LABELS against image, return top-k label strings."""
    import torch
    model, preprocess, tokenizer = get_clip()
    if model is None:
        return []
    with torch.no_grad():
        img_tensor    = preprocess(pil_image).unsqueeze(0)
        img_feat      = model.encode_image(img_tensor)
        img_feat      = img_feat / img_feat.norm(dim=-1, keepdim=True)
        text_tokens   = tokenizer(CANDIDATE_LABELS)
        text_feat     = model.encode_text(text_tokens)
        text_feat     = text_feat / text_feat.norm(dim=-1, keepdim=True)
        scores        = (img_feat @ text_feat.T)[0]
        top_idx       = scores.topk(top_k).indices.tolist()
    return [CANDIDATE_LABELS[i] for i in top_idx]

def cosine(a, b):
    dot = sum(x*y for x,y in zip(a,b))
    na  = math.sqrt(sum(x*x for x in a))
    nb  = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb + 1e-9)

# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    next_run = None
    job = _scheduler.get_job("nightly_tag")
    if job and job.next_run_time:
        next_run = job.next_run_time.strftime("%Y-%m-%dT%H:%M:%S")
    return jsonify({
        "clip_ready":     _clip_ready,
        "clip_loading":   _clip_loading,
        "worker_running": _worker_running,
        "queue_size":     _tag_queue.qsize(),
        "auto_tag":       _auto_tag,
        "tagging_now":    _currently_tagging is not None,
        "schedule_hour":  _schedule_hour,
        "schedule_min":   _schedule_min,
        "next_run":       next_run,
    })

@app.route("/api/settings/autotag", methods=["POST"])
def set_autotag():
    global _auto_tag
    _auto_tag = bool(request.json.get("enabled", True))
    return jsonify({"auto_tag": _auto_tag})

@app.route("/api/settings/runtags", methods=["POST"])
def run_all_tags():
    """Queue every untagged item and start the worker."""
    count = _enqueue_all_untagged()
    _ensure_worker()
    return jsonify({"queued": count})

@app.route("/api/settings/unloadclip", methods=["POST"])
def unload_clip_route():
    """Manually release CLIP from memory."""
    if _worker_running:
        return jsonify({"error": "Worker is running — wait until tagging finishes"}), 409
    _unload_clip()
    return jsonify({"ok": True})

@app.route("/api/settings/schedule", methods=["POST"])
def set_schedule():
    """Set or clear the nightly auto-tag schedule.
       POST {"hour": 2, "minute": 30}  → run at 02:30 daily
       POST {"hour": null}             → disable schedule
    """
    data   = request.json or {}
    hour   = data.get("hour")
    minute = int(data.get("minute", 0))
    if hour is None:
        _apply_schedule(None, 0)
    else:
        _apply_schedule(int(hour), minute)
    return jsonify({"ok": True, "schedule_hour": _schedule_hour, "schedule_min": _schedule_min})

# Kick off CLIP load when the client first loads the page
@app.route("/api/warmup", methods=["POST"])
def api_warmup():
    get_clip()
    return jsonify({"ok": True})

# ── bins ──────────────────────────────────────────────────────────────────────
@app.route("/api/bins", methods=["GET"])
def list_bins():
    bins = Bin.query.order_by(Bin.created).all()
    return jsonify([_bin_dict(b) for b in bins])

@app.route("/api/bins/<bin_id>", methods=["GET"])
def get_bin(bin_id):
    b = db.session.get(Bin, bin_id)
    if not b: return jsonify({"error": "Not found"}), 404
    return jsonify(_bin_dict(b))

@app.route("/api/bins", methods=["POST"])
def create_bin():
    data = request.json
    if not data.get("name","").strip():
        return jsonify({"error": "Name required"}), 400
    if Bin.query.filter_by(name=data["name"].strip()).first():
        return jsonify({"error": "A bin with that name already exists"}), 400
    b = Bin(name=data["name"].strip(),
            location=data.get("location",""),
            color=data.get("color","#6366f1"))
    db.session.add(b)
    db.session.commit()
    return jsonify({"id": b.id, "name": b.name}), 201

@app.route("/api/bins/<bin_id>", methods=["PUT"])
def update_bin(bin_id):
    b = db.session.get(Bin, bin_id)
    if not b: return jsonify({"error": "Not found"}), 404
    data = request.json
    if "name"            in data: b.name            = data["name"].strip()
    if "location"        in data: b.location        = data["location"]
    if "color"           in data: b.color           = data["color"]
    if "wled_enabled"    in data: b.wled_enabled    = bool(data["wled_enabled"])
    if "wled_ip"         in data: b.wled_ip         = data["wled_ip"].strip()
    if "wled_seg_start"  in data: b.wled_seg_start  = int(data["wled_seg_start"])
    if "wled_seg_len"    in data: b.wled_seg_len    = max(1, int(data["wled_seg_len"]))
    if "wled_color"      in data: b.wled_color      = data["wled_color"]
    if "wled_brightness" in data: b.wled_brightness = max(0, min(255, int(data["wled_brightness"])))
    if "wled_effect"     in data: b.wled_effect     = data["wled_effect"]
    if "wled_duration"   in data: b.wled_duration   = max(0, int(data["wled_duration"]))
    db.session.commit()
    return jsonify(_bin_dict(b))

@app.route("/api/bins/<bin_id>", methods=["DELETE"])
def delete_bin(bin_id):
    b = db.session.get(Bin, bin_id)
    if not b: return jsonify({"error": "Not found"}), 404
    # delete photos from disk
    for item in b.items:
        try:
            (UPLOAD_DIR / item.photo_path).unlink(missing_ok=True)
        except Exception:
            pass
    db.session.delete(b)
    db.session.commit()
    return jsonify({"ok": True})

# ── items ─────────────────────────────────────────────────────────────────────
@app.route("/api/items", methods=["GET"])
def list_items():
    bin_id = request.args.get("bin_id")
    q = Item.query
    if bin_id:
        q = q.filter_by(bin_id=bin_id)
    items = q.order_by(Item.created.desc()).all()
    return jsonify([_item_dict(i) for i in items])

@app.route("/api/items", methods=["POST"])
def add_item():
    """Accept a photo (multipart or base64 JSON) + bin_id."""
    # Safely get bin_id regardless of content type (multipart vs JSON)
    if request.content_type and "multipart" in request.content_type:
        json_body = {}
    else:
        json_body = request.get_json(silent=True) or {}
    bin_id = request.form.get("bin_id") or json_body.get("bin_id")
    if not db.session.get(Bin, bin_id):
        return jsonify({"error": "Unknown bin"}), 400

    # --- get image bytes ---
    pil_image = None
    filename  = f"{uuid.uuid4()}.jpg"

    if "photo" in request.files:
        file = request.files["photo"]
        pil_image = Image.open(file.stream).convert("RGB")
    elif request.is_json and request.json.get("photo_b64"):
        raw = base64.b64decode(request.json["photo_b64"])
        pil_image = Image.open(io.BytesIO(raw)).convert("RGB")
    else:
        return jsonify({"error": "No photo provided"}), 400

    # resize to max 1200px on longest side to save disk space
    pil_image.thumbnail((1200, 1200), Image.LANCZOS)
    save_path = UPLOAD_DIR / filename
    pil_image.save(save_path, "JPEG", quality=82)

    # --- Save item immediately (no blocking CLIP call) ---
    item = Item(
        bin_id     = bin_id,
        photo_path = filename,
        label      = request.form.get("label","") or json_body.get("label",""),
        tags       = "",
        embedding  = "",
        notes      = request.form.get("notes","") or json_body.get("notes",""),
    )
    db.session.add(item)
    db.session.commit()

    # --- Queue for background tagging ---
    if _auto_tag:
        enqueue_item(item.id)

    return jsonify(_item_dict(item)), 201

@app.route("/api/items/<item_id>", methods=["PUT"])
def update_item(item_id):
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    data = request.json
    if "label" in data:
        item.label = data["label"]
    if "notes" in data:
        item.notes = data["notes"]
    if "bin_id" in data:
        if not db.session.get(Bin, data["bin_id"]):
            return jsonify({"error": "Unknown bin"}), 400
        item.bin_id = data["bin_id"]
    db.session.commit()
    return jsonify(_item_dict(item))

@app.route("/api/items/<item_id>", methods=["DELETE"])
def delete_item(item_id):
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    try:
        (UPLOAD_DIR / item.photo_path).unlink(missing_ok=True)
    except Exception:
        pass
    db.session.delete(item)
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/items/<item_id>/retag", methods=["POST"])
def retag_item(item_id):
    """Queue a single item for re-tagging (loads CLIP on demand if needed)."""
    item = db.session.get(Item, item_id)
    if not item: return jsonify({"error": "Not found"}), 404
    # clear existing embedding so worker won't skip it
    item.embedding = ""
    item.tags = ""
    db.session.commit()
    # always enqueue and always start the worker regardless of auto_tag setting
    _tag_queue.put(item.id)
    _ensure_worker()
    return jsonify({**_item_dict(item), "queued": True})

# ── search ────────────────────────────────────────────────────────────────────
@app.route("/api/search")
def search():
    query   = request.args.get("q","").strip()
    bin_ids = request.args.getlist("bin_id")   # optional filter
    if not query:
        return jsonify([])

    all_items = Item.query
    if bin_ids:
        all_items = all_items.filter(Item.bin_id.in_(bin_ids))
    all_items = all_items.all()

    results = []

    if _clip_ready:
        # semantic vector search
        qemb = embed_text(query)
        for item in all_items:
            if not item.embedding:
                continue
            emb  = json.loads(item.embedding)
            score = cosine(qemb, emb)
            results.append((_item_dict(item), score))
        results.sort(key=lambda x: x[1], reverse=True)
        # return top 50, threshold 0.18 (CLIP cosine for reasonable matches)
        results = [r for r in results if r[1] >= 0.18][:50]
        return jsonify([{**r[0], "score": round(r[1],4)} for r in results])
    else:
        # fallback: keyword match on label + tags + notes
        ql = query.lower()
        for item in all_items:
            haystack = f"{item.label} {item.tags} {item.notes}".lower()
            if ql in haystack:
                results.append(_item_dict(item))
        return jsonify(results[:50])

# ── WLED API ──────────────────────────────────────────────────────────────────

def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return [int(h[i:i+2], 16) for i in (0, 2, 4)]

def _wled_send(ip, payload, timeout=3):
    """POST a JSON payload to WLED's /json/state endpoint."""
    url = f"http://{ip}/json/state"
    resp = _requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def _wled_get_info(ip, timeout=3):
    """GET /json/info from WLED — returns device info including LED count."""
    url = f"http://{ip}/json/info"
    resp = _requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def _build_wled_payload(b, on=True, override_effect=None):
    """Build WLED JSON state payload for a bin's config."""
    rgb = _hex_to_rgb(b.wled_color)
    effect_id = 0   # solid
    fx_speed  = 128
    fx_int    = 128
    if (override_effect or b.wled_effect) == "pulse":
        effect_id = 45   # WLED effect 45 = Breathe
        fx_speed  = 100
        fx_int    = 200
    seg = {
        "id":   0,
        "start": b.wled_seg_start,
        "stop":  b.wled_seg_start + b.wled_seg_len,
        "on":    on,
        "bri":   b.wled_brightness,
        "col":  [rgb, [0,0,0], [0,0,0]],
        "fx":    effect_id,
        "sx":    fx_speed,
        "ix":    fx_int,
    }
    return {"on": on, "bri": b.wled_brightness, "seg": [seg]}

@app.route("/api/wled/settings", methods=["GET"])
def wled_get_settings():
    s = WLEDSettings.query.first()
    if not s:
        return jsonify({"enabled": False, "default_ip": ""})
    return jsonify({"enabled": s.enabled, "default_ip": s.default_ip})

@app.route("/api/wled/settings", methods=["POST"])
def wled_save_settings():
    data = request.json or {}
    s = WLEDSettings.query.first()
    if not s:
        s = WLEDSettings()
        db.session.add(s)
    s.enabled    = bool(data.get("enabled", False))
    s.default_ip = data.get("default_ip", "").strip()
    db.session.commit()
    return jsonify({"ok": True, "enabled": s.enabled, "default_ip": s.default_ip})

@app.route("/api/wled/ping", methods=["POST"])
def wled_ping():
    """Test connectivity and return device info."""
    ip = (request.json or {}).get("ip", "").strip()
    if not ip:
        return jsonify({"error": "No IP provided"}), 400
    try:
        info = _wled_get_info(ip)
        return jsonify({
            "ok":        True,
            "name":      info.get("name", "WLED"),
            "led_count": info.get("leds", {}).get("count", 0),
            "version":   info.get("ver", "?"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/wled/preview", methods=["POST"])
def wled_preview():
    """Send a live preview to WLED without saving — for the jog/setup UI."""
    data = request.json or {}
    ip   = data.get("ip", "").strip()
    if not ip:
        return jsonify({"error": "No IP"}), 400
    rgb = _hex_to_rgb(data.get("color", "#ffffff"))
    bri = max(0, min(255, int(data.get("brightness", 128))))
    start = int(data.get("seg_start", 0))
    length = max(1, int(data.get("seg_len", 5)))
    effect = data.get("effect", "solid")
    effect_id = 45 if effect == "pulse" else 0
    payload = {
        "on":  True,
        "bri": bri,
        "seg": [{
            "id":    0,
            "start": start,
            "stop":  start + length,
            "on":    True,
            "bri":   bri,
            "col":   [rgb, [0,0,0], [0,0,0]],
            "fx":    effect_id,
            "sx":    100,
            "ix":    200,
        }]
    }
    try:
        _wled_send(ip, payload)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/wled/off", methods=["POST"])
def wled_off():
    """Turn off WLED (used after preview / duration expires)."""
    ip = (request.json or {}).get("ip", "").strip()
    if not ip:
        return jsonify({"error": "No IP"}), 400
    try:
        _wled_send(ip, {"on": False})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/wled/find/<bin_id>", methods=["POST"])
def wled_find(bin_id):
    """Illuminate the LEDs for a bin — called by the Find button."""
    b = db.session.get(Bin, bin_id)
    if not b:
        return jsonify({"error": "Bin not found"}), 404
    if not b.wled_enabled or not b.wled_ip:
        return jsonify({"error": "WLED not configured for this bin"}), 400
    try:
        payload = _build_wled_payload(b, on=True)
        _wled_send(b.wled_ip, payload)
        return jsonify({"ok": True, "duration": b.wled_duration, "ip": b.wled_ip})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

# ── static photo serving ──────────────────────────────────────────────────────
@app.route("/photos/<filename>")
def serve_photo(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# ── single-page app ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    # Send file directly — bypasses Jinja templating which would HTML-encode JS strings
    return send_from_directory(BASE_DIR / "templates", "index.html")

def _item_dict(item):
    b = item.bin
    return {
        "id":              item.id,
        "bin_id":          item.bin_id,
        "bin_name":        b.name if b else "",
        "bin_color":       b.color if b else "#6366f1",
        "bin_wled":        b.wled_enabled if b else False,
        "photo_url":       f"/photos/{item.photo_path}",
        "label":           item.label,
        "tags":            item.tags,
        "notes":           item.notes,
        "created":         item.created.isoformat(),
    }

def _bin_dict(b):
    return {
        "id":               b.id,
        "name":             b.name,
        "location":         b.location,
        "color":            b.color,
        "item_count":       len(b.items),
        "created":          b.created.isoformat(),
        "wled_enabled":     b.wled_enabled,
        "wled_ip":          b.wled_ip,
        "wled_seg_start":   b.wled_seg_start,
        "wled_seg_len":     b.wled_seg_len,
        "wled_color":       b.wled_color,
        "wled_brightness":  b.wled_brightness,
        "wled_effect":      b.wled_effect,
        "wled_duration":    b.wled_duration,
    }

def _migrate_db():
    """Add any missing columns to existing tables (safe to run on every start)."""
    import sqlalchemy as sa
    with db.engine.connect() as conn:
        # ── bin table WLED columns ────────────────────────────────────────────
        existing = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(bin)"))}
        migrations = [
            ("wled_enabled",    "BOOLEAN    DEFAULT 0"),
            ("wled_ip",         "VARCHAR(64) DEFAULT ''"),
            ("wled_seg_start",  "INTEGER    DEFAULT 0"),
            ("wled_seg_len",    "INTEGER    DEFAULT 5"),
            ("wled_color",      "VARCHAR(20) DEFAULT '#ffffff'"),
            ("wled_brightness", "INTEGER    DEFAULT 128"),
            ("wled_effect",     "VARCHAR(20) DEFAULT 'solid'"),
            ("wled_duration",   "INTEGER    DEFAULT 10"),
        ]
        for col, defn in migrations:
            if col not in existing:
                conn.execute(sa.text(f"ALTER TABLE bin ADD COLUMN {col} {defn}"))
                print(f"[DB] Migrated: bin.{col}")
        conn.commit()

if __name__ == "__main__":
    with app.app_context():
        db.create_all()      # create any brand-new tables
        _migrate_db()        # add any missing columns to existing tables
    _scheduler.start()
    print("\n🗂  BINventory running at http://0.0.0.0:5000\n")
    print("   CLIP model loads on demand — RAM stays free until tagging starts\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
