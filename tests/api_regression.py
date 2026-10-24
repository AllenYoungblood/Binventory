"""API regression against a COPY of the live database, or a seeded fresh one.

    python tests/api_regression.py                          # live copy if present
    BINVENTORY_TEST_FRESH=1 python tests/api_regression.py  # always fresh + seeded

With a local binventory.db, it and the dictionaries are copied to a temp dir and
the app is pointed at the copies, so real data is never touched and pending
migrations run against real rows. Without one (a fresh clone, CI), a new
database is built through the normal startup path and seeded with synthetic
bins, items and a controller. WLED network calls, the ML worker and the OCR
engine are stubbed. Test photos written to static/uploads are removed on exit,
even if the run crashes. Exit code is non-zero if any check fails.
"""
import atexit, io, os, shutil, sys, tempfile
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # cp1252 consoles
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="binventory-test-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
# Test photos land in the real static/uploads (the folder isn't configurable).
# Remove any file created during the run on exit — even if the run crashes.
_UPLOADS = PROJECT / "static" / "uploads"
_THUMBS = _UPLOADS / "thumbs"
_uploads_before = set(os.listdir(_UPLOADS))
_thumbs_before = set(os.listdir(_THUMBS)) if _THUMBS.exists() else set()
atexit.register(lambda: [(_UPLOADS / f).unlink(missing_ok=True)
                         for f in set(os.listdir(_UPLOADS)) - _uploads_before if (_UPLOADS / f).is_file()])
atexit.register(lambda: [(_THUMBS / f).unlink(missing_ok=True)
                         for f in (set(os.listdir(_THUMBS)) if _THUMBS.exists() else set()) - _thumbs_before])
FRESH = os.environ.get("BINVENTORY_TEST_FRESH") == "1" or not (PROJECT / "binventory.db").exists()
if not FRESH:
    shutil.copy(PROJECT / "binventory.db", SCRATCH / "binventory.db")
print(f"[test] {'fresh seeded database' if FRESH else 'copy of the live database'} in {SCRATCH}")
os.environ["BINVENTORY_DB_DIR"] = str(SCRATCH)

# Plugins: the real ones plus deliberately broken fixtures, from a scratch dir.
_PLUGINS = SCRATCH / "plugins"
shutil.copytree(PROJECT / "plugins", _PLUGINS)
_GOOD = 'name="{n}"\nversion="0.1"\napi_version=1\ndefault_enabled=true\npermissions=["items:read","items:write"]\n'
for _pid, _toml, _init in [
    ("bad_manifest", 'id="Bad Manifest"\napi_version=1\n', "def register(api): pass\n"),
    ("old_api",      'id="old_api"\nname="Old"\napi_version=0\n', "def register(api): pass\n"),
    ("needs_dep",    'id="needs_dep"\n' + _GOOD.format(n="Needs dep") + 'requires=["nonexistent-pkg"]\nimports=["nonexistent_pkg_zz"]\n',
                     "def register(api): pass\n"),
    # Registers an analyzer, then raises: the analyzer must be rolled back.
    ("broken",       'id="broken"\n' + _GOOD.format(n="Broken"),
                     "from binventory.analysis import Analyzer\n"
                     "class A(Analyzer):\n    id = 'broken_an'\n    def analyze(self, ctx): pass\n"
                     "def register(api):\n    api.add_analyzer(A())\n    raise RuntimeError('boom')\n"),
    ("sneaky",       'id="sneaky"\n' + _GOOD.format(n="Sneaky"),
                     "def register(api):\n    api.add_action('undeclared', lambda p: {})\n"),    # Exercises the service / status / settings hooks the MCP plugin relies on.
    ("svc_demo",     'id="svc_demo"\n' + _GOOD.format(n="Service demo")
                     + '[[settings]]\nkey="port"\ntype="number"\ndefault=1\n',
                     "import binventory\n"
                     "LOG = binventory.__dict__.setdefault('_svc_log', [])\n"
                     "def register(api):\n"
                     "    api.add_service(lambda: LOG.append('start'), lambda: LOG.append('stop'), 'demo')\n"
                     "    api.set_status(lambda: [{'label': 'State', 'value': 'running'},\n"
                     "                            {'label': 'Command', 'value': 'connect here', 'type': 'code'},\n"
                     "                            {'label': 'Bad', 'value': 'javascript:x', 'type': 'link'}])\n"
                     "    api.on_settings_changed(lambda ch: LOG.append(('changed', sorted(ch))))\n"
                     "    with api.app_context():\n"
                     "        from binventory.models import Item\n"
                     "        LOG.append(('items', Item.query.count() >= 0))\n"),
    # Plugin routes, a page and links (the label maker relies on them).
    ("web_demo",     'id="web_demo"\nname="Web demo"\nversion="0.1"\napi_version=1\ndefault_enabled=true\n'
                     'permissions=["items:read","web:routes"]\n[page]\ntitle="Demo page"\npath="/"\n'
                     '[[links]]\nwhere="item"\nlabel="Demo"\npath="/?item={id}"\n',
                     "from binventory.services.errors import NotFound\n"
                     "def register(api):\n"
                     "    api.add_route('/', lambda: 'demo page')\n"
                     "    api.add_route('/item/<item_id>', lambda item_id: f'item {item_id}')\n"
                     "    def boom():\n        raise NotFound('No such widget')\n"
                     "    api.add_route('/missing', boom)\n"
                     "    api.add_route('/echo', lambda: 'posted', methods=('POST',))\n"),
    ("web_nope",     'id="web_nope"\n' + _GOOD.format(n="Page without permission") + '[page]\ntitle="x"\n',
                     "def register(api): pass\n"),
    ("route_noperm", 'id="route_noperm"\n' + _GOOD.format(n="Route without permission"),
                     "def register(api):\n    api.add_route('/x', lambda: 'x')\n"),
]:
    (_PLUGINS / _pid).mkdir()
    (_PLUGINS / _pid / "plugin.toml").write_text(_toml, encoding="utf-8")
    (_PLUGINS / _pid / "__init__.py").write_text(_init, encoding="utf-8")
os.environ["BINVENTORY_PLUGIN_DIR"] = str(_PLUGINS)
sys.path.insert(0, str(PROJECT))

from PIL import Image
from binventory import config as _config, vocabulary as _vocab
# Vocabulary edits write dictionary FILES, whose folder is fixed rather than
# configurable — so point the app at a copy, like the DB.
shutil.copytree(PROJECT / "dictionaries", SCRATCH / "dictionaries")
_config.DICT_DIR = _vocab.DICT_DIR = SCRATCH / "dictionaries"
from binventory import lighting, ml
from binventory.factory import create_app, init_database

sent = []
lighting._wled_send = lambda ip, payload, *a, **k: sent.append((ip, payload))
lighting._wled_get_info = lambda ip, *a, **k: {
    "ver": "16.0.1", "name": "Stub", "leds": {"count": 256, "matrix": {"w": 32, "h": 8}},
    "seg": [{"id": 0}]}
ml.ensure_worker = lambda: None
from binventory.services import processing
processing.ml.ensure_worker = lambda: None

app = create_app()
init_database(app)
c = app.test_client()
results = []

def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))

def j(r):
    try: return r.get_json()
    except Exception: return None

if FRESH:
    # Enough shape for the checks below that assume a lived-in library: several
    # bins, more than one page of items with photos, and a registered controller.
    _seed_bins = [j(c.post("/api/bins", json={"name": f"Seed bin {n}", "location": "Shelf A"}))["id"]
                  for n in range(1, 6)]
    for n in range(14):
        _b = io.BytesIO(); Image.new("RGB", (48, 48), (n * 15, 90, 160)).save(_b, "JPEG"); _b.seek(0)
        c.post("/api/items", data={"bin_id": _seed_bins[n % 5], "label": f"Seed item {n}", "photo": (_b, "s.jpg")},
               content_type="multipart/form-data")
    c.post("/api/wled/devices", json={"name": "Seed controller", "ip": "10.0.0.50"})
    while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()

# --- bins / items basics -----------------------------------------------------
r = c.get("/api/bins?scope=all"); bins = j(r)
check("list bins", r.status_code == 200 and len(bins) >= 5, f"{len(bins)} bins")
r = c.post("/api/bins", json={"name": "ZZ regression bin", "location": "test"})
bin_id = j(r)["id"]; check("create bin", r.status_code == 201)
r = c.post("/api/bins", json={"name": "ZZ second bin"}); bin2 = j(r)["id"]

buf = io.BytesIO(); Image.new("RGB", (64, 32), (200, 30, 30)).save(buf, "JPEG"); buf.seek(0)
r = c.post("/api/items", data={"bin_id": bin_id, "label": "", "notes": "", "run_ocr": "false",
                               "photo": (buf, "t.jpg")}, content_type="multipart/form-data")
check("create item (multipart)", r.status_code == 201, r.status_code)
item = j(r); iid = item["id"]
r = c.get(f"/api/items/{iid}"); check("GET single item (new route)", r.status_code == 200 and j(r)["id"] == iid)
check("GET missing item 404", c.get("/api/items/nope").status_code == 404)

# --- D1 unified processing ----------------------------------------------------
for scope, extra in [("item", {"target_id": iid}), ("bin", {"target_id": bin_id}), ("pending", {}), ("all", {})]:
    r = c.post("/api/process", json={"scope": scope, **extra})
    check(f"D1 /api/process scope={scope}", r.status_code == 200, j(r))
check("D1 bad scope rejected", c.post("/api/process", json={"scope": "bogus"}).status_code == 400)
for path in [f"/api/items/{iid}/retag", f"/api/items/{iid}/run-ocr",
             "/api/settings/runtags", "/api/settings/run-all"]:
    r = c.post(path, json={})
    check(f"D1 legacy {path.split('/')[-1]} delegates", r.status_code == 200, r.status_code)
r = c.post("/api/settings/rerun-ocr", json={"scope": "existing"})
check("D1 legacy rerun-ocr delegates", r.status_code == 200, r.status_code)
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()

# --- D3 rotate ------------------------------------------------------------------
r = c.post(f"/api/items/{iid}/rotate", json={"degrees": 90})
w, h = Image.open(PROJECT / "static" / "uploads" / Path(j(r)["photo_url"]).name.split("?")[0]).size
check("D3 rotate saved photo 90°", r.status_code == 200 and (w, h) == (32, 64), f"{w}x{h}")
# Rotating must clear the embedding so CLIP re-tags the new orientation. It used
# to clear only the legacy JSON column, which CLIP never looked at.
from binventory.db import db as _db
from binventory.models import Item as _Item
from binventory.services import embeddings_store as _es
with app.app_context():
    _it = _db.session.get(_Item, iid); _es.set_embedding(_it, [0.1] * 512); _db.session.commit()
c.post(f"/api/items/{iid}/rotate", json={"degrees": 270})
with app.app_context():
    check("4b rotate clears the embedding (CLIP will re-tag)", not _es.has_embedding(_db.session.get(_Item, iid)))
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()

# --- D5 rename -> review candidate only ----------------------------------------
before = j(c.get("/api/vocab"))["active_count"]
r = c.put(f"/api/items/{iid}", json={"label": "ZZ Regression Widget 9000"})
after = j(c.get("/api/vocab"))
check("D5 rename sets label_source=user", j(c.get(f"/api/items/{iid}"))["label_source"] == "user")
check("D5 rename does not grow active vocab", after["active_count"] == before, f"{before}->{after['active_count']}")
check("D5 rename becomes review candidate",
      any(l["text"] == "ZZ Regression Widget 9000" for l in after["learned"]))

# --- D6 vocab endpoints ----------------------------------------------------------
for p in ["/api/vocab/duplicates", "/api/vocab/candidates", "/api/vocab/analyze?source=names&min=1"]:
    check(f"D6 {p.split('?')[0]}", c.get(p).status_code == 200)

# --- D4 restock: consume -> restock same / to other bin / dangling origin ------
check("consume", c.post(f"/api/items/{iid}/consume").status_code == 200)
src_before = j(c.get(f"/api/items/{iid}"))
c.post(f"/api/items/{iid}/restock", json={"bin_id": bin_id, "label": src_before["label"], "notes": src_before["notes"]})
src_after = j(c.get(f"/api/items/{iid}"))
check("D4 untouched pre-filled form keeps label_source", src_after["label_source"] == src_before["label_source"] and src_after["status"] == "active", (src_before["label_source"], src_after["label_source"]))
buf2 = io.BytesIO(); Image.new("RGB", (40, 40), (0, 0, 200)).save(buf2, "JPEG"); buf2.seek(0)
c.post(f"/api/items/{iid}/consume")
old_photo = j(c.get(f"/api/items/{iid}"))["photo_url"]
r = c.post(f"/api/items/{iid}/restock", data={"bin_id": bin_id, "label": src_before["label"], "notes": "", "photo": (buf2, "n.jpg")}, content_type="multipart/form-data")
check("D4 single /restock endpoint accepts a new photo", r.status_code == 200 and j(c.get(f"/api/items/{iid}"))["photo_url"] != old_photo, r.status_code)
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()
c.post(f"/api/items/{iid}/consume")
r = c.post(f"/api/items/{iid}/restock", json={})
check("D4 restock unchanged -> origin bin", r.status_code == 200 and j(c.get(f"/api/items/{iid}"))["bin_id"] == bin_id)
c.post(f"/api/items/{iid}/consume")
r = c.post(f"/api/items/{iid}/restock-photo", json={"bin_id": bin2, "notes": "moved"})
it = j(c.get(f"/api/items/{iid}"))
check("D4 legacy restock-photo path, new bin + notes", r.status_code == 200 and it["bin_id"] == bin2 and it["notes"] == "moved")
c.post(f"/api/items/{iid}/consume")
c.delete(f"/api/bins/{bin2}", json={"confirm": "delete", "contents": "checkout"})
it = j(c.get(f"/api/items/{iid}"))
check("bin delete (checkout mode) keeps shopping-list item", it.get("status") == "consumed", it)
check("  ...origin cleared, not repointed at holding bin", it.get("origin_bin_id") == "" and it.get("origin_bin_name") == "", it)
r = c.post(f"/api/items/{iid}/restock", json={})
check("C6 restock with deleted origin -> 409 needs_bin", r.status_code == 409 and j(r).get("needs_bin"), (r.status_code, j(r)))
check("C6 restock into chosen bin", c.post(f"/api/items/{iid}/restock", json={"bin_id": bin_id}).status_code == 200)
r = c.post("/api/bins", json={"name": "ZZ third bin"}); bin3 = j(r)["id"]
c.put(f"/api/items/{iid}", json={"bin_id": bin3}); c.post(f"/api/items/{iid}/consume")
bins_now = {b["id"]: b for b in j(c.get("/api/bins?scope=all"))}
check("bin item_count excludes consumed items", bins_now[bin3]["item_count"] == 0, bins_now[bin3]["item_count"])
r = c.delete(f"/api/bins/{bin3}", json={"confirm": "delete", "contents": "delete"})
it = j(c.get(f"/api/items/{iid}"))
check("bin delete (delete mode) keeps shopping-list item", it.get("status") == "consumed" and j(r)["deleted_items"] == 0, (it, j(r)))
check("C6 delete mode -> 409 then chosen bin",
      c.post(f"/api/items/{iid}/restock", json={}).status_code == 409 and
      c.post(f"/api/items/{iid}/restock", json={"bin_id": bin_id}).status_code == 200)

# --- checkout / return ----------------------------------------------------------
check("checkout", c.post(f"/api/items/{iid}/checkout").status_code == 200)
check("return", c.post(f"/api/items/{iid}/return").status_code == 200)

# --- WLED registry + matrix ------------------------------------------------------
devs = j(c.get("/api/wled/devices"))
check("registry lists migrated device", len(devs) >= 1)
r = c.post("/api/wled/devices", json={"name": "ZZ test ctrl", "ip": "10.9.9.9"}); d2 = j(r)["id"]
check("add device", r.status_code in (200, 201))
check("add duplicate IP returns existing", j(c.post("/api/wled/devices", json={"ip": "10.9.9.9"}))["id"] == d2)
r = c.post(f"/api/wled/devices/{d2}/probe", json={})
check("probe detects matrix", j(r)["layout"] == "matrix" and j(r)["matrix_w"] == 32)
r = c.put(f"/api/wled/devices/{d2}", json={"name": "ZZ renamed", "ip": "10.9.9.10"})
check("rename + re-address", j(r)["name"] == "ZZ renamed" and j(r)["ip"] == "10.9.9.10")
r = c.put(f"/api/bins/{bin_id}/lighting", json={"device_id": d2, "enabled": True,
                                                "zone_x": 3, "zone_y": 1, "zone_w": 4, "zone_h": 2})
cfg = j(r)
check("link bin -> adopts matrix layout", cfg["layout"] == "matrix" and cfg["ip"] == "10.9.9.10")
sent.clear(); r = c.post(f"/api/wled/find/{bin_id}")
seg = sent[-1][1]["seg"][0] if sent else {}
check("Find sends 2D matrix payload", r.status_code == 200 and seg.get("startY") == 1 and seg.get("stopY") == 3
      and seg.get("start") == 3 and seg.get("stop") == 7, seg)
sent.clear(); c.post("/api/wled/preview", json={"device_id": d2, "layout": "matrix",
                                              "zone_x": 0, "zone_y": 0, "zone_w": 2, "zone_h": 2})
check("preview via device_id uses device IP + matrix", sent and sent[-1][0] == "10.9.9.10" and "startY" in sent[-1][1]["seg"][0])
sent.clear(); c.post("/api/wled/preview", json={"ip": "10.1.1.1", "seg_start": 5, "seg_len": 3})
check("strip preview has no Y", sent and "startY" not in sent[-1][1]["seg"][0] and sent[-1][1]["seg"][0]["stop"] == 8)
check("make default", j(c.put(f"/api/wled/devices/{d2}", json={"is_default": True}))["is_default"])
r = c.delete(f"/api/wled/devices/{d2}")
check("delete device unlinks bins, keeps IP", j(r)["bins_unlinked"] == 1
      and j(c.get(f"/api/bins/{bin_id}/lighting"))["ip"] == "10.9.9.10")
devs_after = j(c.get("/api/wled/devices"))
check("a default device remains after deleting the default",
      any(d["is_default"] for d in devs_after), [d["is_default"] for d in devs_after])

# --- phase 4b ---------------------------------------------------------------------
import sqlite3 as _sq
_conn = _sq.connect(str(SCRATCH / "binventory.db"))
_cols = lambda t: {r[1] for r in _conn.execute(f"pragma table_info({t})")}
check("4b schema is at least v7", _conn.execute("select max(version) from schema_version").fetchone()[0] >= 7)
check("4b bin.wled_* columns dropped", not any(x.startswith("wled_") for x in _cols("bin")), _cols("bin"))
check("4b bin_lighting.chase_* dropped", not any(x.startswith("chase_") for x in _cols("bin_lighting")))
check("4b item.embedding (JSON) dropped", "embedding" not in _cols("item") and "embedding_blob" in _cols("item"))
_conn.close()
bl = j(c.get("/api/bins?scope=all"))
check("4b bin JSON has lighting_enabled, no wled_* keys",
      all("lighting_enabled" in b and not any(k.startswith("wled_") for k in b) for b in bl))
r = c.put(f"/api/bins/{bin_id}/lighting", json={"effect": "chase"})
check("4b chase effect rejected -> solid", j(r)["effect"] == "solid" and "chase_start" not in j(r), j(r))
sent.clear(); c.post("/api/wled/preview", json={"ip": "10.1.1.1", "effect": "chase"})
check("4b chase preview sends a plain solid payload", sent and sent[-1][1]["seg"][0]["fx"] == 0, sent[-1:] )
dicts = j(c.get("/api/dictionaries"))
dict_files = sorted(d["file"] for d in (dicts.get("files", dicts) if isinstance(dicts, dict) else dicts))
check("4b dictionaries are the three merged lists",
      dict_files == ["base_list.txt", "custom_list.txt", "electronics_shop_list.txt"], dict_files)
# Active vocabulary = the unique terms in the enabled files (the live count
# changes as the user merges duplicates, so don't pin a number).
_file_terms = {l.strip().lower() for f in _vocab.DICT_DIR.glob("*.txt")
               for l in f.read_text(encoding="utf-8").splitlines() if l.strip() and not l.strip().startswith("#")}
check("4b active vocabulary == unique terms in the three files",
      j(c.get("/api/vocab"))["active_count"] == len(_file_terms), (j(c.get("/api/vocab"))["active_count"], len(_file_terms)))

# --- phase 6: C5 configurable thresholds ------------------------------------------
st = j(c.get("/api/settings/ocr"))
check("C5 thresholds default 0.18 / 0.55", st["text_threshold"] == 0.18 and st["image_threshold"] == 0.55, st)
r = j(c.post("/api/settings/ocr", json={"text_threshold": 0.3, "image_threshold": 2}))
check("C5 thresholds saved and clamped", r["text_threshold"] == 0.3 and r["image_threshold"] == 0.95, r)
c.post("/api/settings/ocr", json={"text_threshold": 0.18, "image_threshold": 0.55})

# --- phase 6: E5 region OCR ----------------------------------------------------------
# The OCR engine is stubbed; everything around it (endpoint, precedence, tag
# replacement, the analyzer the worker runs) is real. A PIL image means a region
# crop; a path means a whole-photo read.
from binventory.ml import runtime as _rt
from binventory.analysis.ocr import OcrAnalyzer
from binventory.analysis.base import AnalysisContext
from binventory.ml.worker import _apply_result
WHOLE = "Made in China, Shenzhen Widget Co. SG90 SERVO 9g"
REGION_TEXT = ["SG90 SERVO"]
_rt.load_ocr = lambda: True
ml.load_ocr = lambda: True
_fake = lambda img: REGION_TEXT[0] if hasattr(img, "convert") else WHOLE
_rt.run_ocr = _fake; ml.run_ocr = _fake

def whole_photo_ocr():
    """What the worker does for a queued OCR job, run inline."""
    with app.app_context():
        it = _db.session.get(_Item, iid)
        res = OcrAnalyzer().analyze(AnalysisContext(
            image_path=PROJECT / "static" / "uploads" / it.photo_path, image=None, item=it,
            settings={"ocr_as_tags": True, "ocr_truncate": True, "ocr_truncate_len": 255}))
        _apply_result(it, res, "ocr"); _db.session.commit()

def get(): return j(c.get(f"/api/items/{iid}"))
def tags(): return [t.strip() for t in get()["tags"].split(",") if t.strip()]

c.put(f"/api/items/{iid}", json={"label": ""})
with app.app_context():
    _it = _db.session.get(_Item, iid); _it.label_source = "ml"; _it.label = "servo"; _db.session.commit()
whole_photo_ocr()
it = get()
check("E5 baseline: whole-photo OCR names the item", it["label_source"] == "ocr" and it["ocr_source"] == "photo", it["label_source"])
whole_tag = "Made in China Shenzhen Widget Co. SG90 SERVO 9g"
check("E5 baseline: whole-photo OCR tag present (commas flattened)", whole_tag in tags(), tags())

check("E5 box too small -> 400", c.post(f"/api/items/{iid}/ocr-region", json={"x": .1, "y": .1, "w": .01, "h": .5}).status_code == 400)
check("E5 missing fields -> 400", c.post(f"/api/items/{iid}/ocr-region", json={"x": .1}).status_code == 400)

r = c.post(f"/api/items/{iid}/ocr-region", json={"x": .2, "y": .2, "w": .5, "h": .3})
it = get()
check("E5 region read wins the name", r.status_code == 200 and it["label"] == "SG90 SERVO" and it["label_source"] == "ocr_region", (r.status_code, it["label"], it["label_source"]))
check("E5 region replaces ocr_text and marks source", it["ocr_text"] == "SG90 SERVO" and it["ocr_source"] == "region", it["ocr_text"])
check("E5 region replaces the whole-photo tag (not kept)", "SG90 SERVO" in tags() and whole_tag not in tags() and WHOLE not in tags(), tags())

snap = get()
whole_photo_ocr()
check("E5 later whole-photo OCR changes nothing", get()["label"] == snap["label"] and get()["ocr_text"] == snap["ocr_text"] and get()["tags"] == snap["tags"])
for path, body in [("/api/settings/rerun-ocr", {"scope": "all"}), ("/api/settings/rerun-ocr", {"scope": "existing"}),
                   ("/api/process", {"scope": "all", "force": True}), ("/api/settings/run-all", {}),
                   ("/api/settings/runtags", {}), (f"/api/items/{iid}/run-ocr", {})]:
    c.post(path, json=body); whole_photo_ocr()
    now = get()
    check(f"E5 bulk {path} {body.get('scope', '')} keeps region name + text",
          now["label"] == "SG90 SERVO" and now["label_source"] == "ocr_region" and now["ocr_text"] == "SG90 SERVO", (now["label"], now["label_source"]))
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()

REGION_TEXT[0] = ""
r = c.post(f"/api/items/{iid}/ocr-region", json={"x": 0, "y": 0, "w": .5, "h": .5})
check("E5 empty region read changes nothing", j(r)["region_text"] == "" and get()["label"] == "SG90 SERVO")

c.put(f"/api/items/{iid}", json={"label": "Micro servo"})
REGION_TEXT[0] = "MG996R"
c.post(f"/api/items/{iid}/ocr-region", json={"x": 0, "y": 0, "w": .5, "h": .5})
it = get()
check("E5 typed name outranks a region read", it["label"] == "Micro servo" and it["label_source"] == "user", it["label"])
check("E5 ...but region text still replaces the OCR text", it["ocr_text"] == "MG996R" and "MG996R" in tags() and "SG90 SERVO" not in tags(), tags())
whole_photo_ocr()
check("E5 after a rename, whole-photo OCR still can't replace region text", get()["ocr_text"] == "MG996R")
REGION_TEXT[0] = "SG90 SERVO"

# --- phase 6: E3 vocabulary management ---------------------------------------------
# Self-contained: seeds its own terms into the COPIED dictionaries rather than
# relying on whatever is in the live vocabulary or review queue.
REAL_DICTS = {p.name: p.read_bytes() for p in (PROJECT / "dictionaries").iterdir()}
_vocab.add_terms(["zz gizmo", "zz gizmos", "zz gadget pin", "zz gadgetpin", "zz sprocket"])
c.post("/api/dictionaries/reload")
m = j(c.get("/api/vocab/manage"))
start = m["active_count"]
cp = next((x for x in m["candidates"] if x["text"] == "ZZ Regression Widget 9000"), None)
check("E3 manage lists review candidates", cp is not None, [x["text"] for x in m["candidates"]])
r = c.post("/api/vocab/promote", json={"ids": [cp["id"]]})
m = j(c.get("/api/vocab/manage"))
check("E3 promote -> custom list, active vocab +1, leaves review queue",
      any(x["text"] == cp["text"] for x in m["custom"]) and m["active_count"] == start + 1
      and not any(x["text"] == cp["text"] for x in m["candidates"]), (j(r), m["active_count"]))
r = c.post("/api/vocab/custom/delete", json={"terms": [cp["text"].lower()]})
m = j(c.get("/api/vocab/manage"))
check("E3 remove from custom list (case-insensitive)",
      m["active_count"] == start and not any(x["text"] == cp["text"] for x in m["custom"]), j(r))

d = j(c.get("/api/vocab/duplicates"))
kinds = {(g["kind"], tuple(sorted(t["text"] for t in g["terms"]))) for g in d["groups"]}
check("E3 exact duplicate found", ("exact", ("zz gizmo", "zz gizmos")) in kinds, kinds)
check("E3 spacing duplicate found", ("spacing", ("zz gadget pin", "zz gadgetpin")) in kinds, kinds)
check("E3 no dictionary-vs-dictionary typo noise",
      all(any(t["source"] == "learned" or "custom_list.txt" in t["files"] for t in g["terms"])
          for g in d["groups"] if g["kind"] == "typo"))
c.put(f"/api/items/{iid}", json={"label": "zz sprockt"})           # a typo lands in the review queue
d = j(c.get("/api/vocab/duplicates"))
check("E3 typo in a typed name is proposed against the vocabulary",
      any(g["kind"] == "typo" and {"zz sprockt", "zz sprocket"} <= {t["text"] for t in g["terms"]} for g in d["groups"]),
      [g for g in d["groups"] if g["kind"] == "typo"])

with app.app_context():
    _it = _db.session.get(_Item, iid); _it.tags = "zz gizmos, lamp, ZZ Gizmos"; _db.session.commit()
check("E3 merge needs keep + remove", c.post("/api/vocab/merge", json={"keep": "x"}).status_code == 400)
before = j(c.get("/api/vocab/manage"))["active_count"]
r = j(c.post("/api/vocab/merge", json={"keep": "zz gizmo", "remove": ["zz gizmos"]}))
check("E3 merge removes term from files and re-tags items",
      r["items_retagged"] == 1 and r["removed_from_files"] == 1 and r["active_count"] == before - 1, r)
check("E3 re-tagged item has kept term once, dropped term gone", tags() == ["zz gizmo", "lamp"], tags())
check("E3 merged-away term no longer in any dictionary file", "zz gizmos" not in _vocab.term_sources())
r = j(c.post("/api/vocab/merge", json={"keep": "zz sprocket", "remove": ["zz sprockt"]}))
check("E3 merging a review-queue typo clears it from the queue", r["learned_removed"] == 1 and
      not any(x["text"] == "zz sprockt" for x in j(c.get("/api/vocab/manage"))["candidates"]), r)
check("E3 test never touched the real dictionaries folder",
      REAL_DICTS == {p.name: p.read_bytes() for p in (PROJECT / "dictionaries").iterdir()})

# --- phase 6: F2 pagination --------------------------------------------------------
full = j(c.get("/api/items"))
check("F2 unpaged response is still a bare list", isinstance(full, list) and len(full) > 10, type(full))
p1 = j(c.get("/api/items?limit=10"))
p2 = j(c.get("/api/items?limit=10&offset=10"))
check("F2 paged shape + total", set(p1) >= {"items", "total", "has_more"} and p1["total"] == len(full)
      and len(p1["items"]) == 10 and p1["has_more"], {k: p1[k] for k in ("total", "has_more")})
check("F2 pages don't overlap and follow the unpaged order",
      [i["id"] for i in p1["items"] + p2["items"]] == [i["id"] for i in full[:20]])
last = j(c.get(f"/api/items?limit=10&offset={len(full) - 3}"))
check("F2 last page: has_more false", len(last["items"]) == 3 and not last["has_more"])
check("F2 bad limit -> 400", c.get("/api/items?limit=abc").status_code == 400)
# Parent-bin grouping must hold across pages: own items first, then by sub-bin.
r = c.post("/api/bins", json={"name": "ZZ parent"}); par = j(r)["id"]
r = c.post("/api/bins", json={"name": "ZZ child A"}); ca = j(r)["id"]
check("F2 setup: nest child bin", c.post(f"/api/bins/{ca}/nest", json={"parent_id": par}).status_code == 200)
made = []
for k, target in enumerate([ca, par, ca, par, ca]):
    b = io.BytesIO(); Image.new("RGB", (8, 8), (k * 40, 0, 0)).save(b, "JPEG"); b.seek(0)
    made.append(j(c.post("/api/items", data={"bin_id": target, "label": f"pg{k}", "photo": (b, "p.jpg")},
                         content_type="multipart/form-data"))["id"])
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()
pg = j(c.get(f"/api/items?bin_id={par}&limit=2"))["items"] + j(c.get(f"/api/items?bin_id={par}&limit=2&offset=2"))["items"] \
   + j(c.get(f"/api/items?bin_id={par}&limit=2&offset=4"))["items"]
check("F2 parent-bin view: own items first, then sub-bin, across pages",
      [i["bin_id"] for i in pg] == [par, par, ca, ca, ca], [i["bin_name"] for i in pg])
for mid in made:
    ph = PROJECT / "static" / "uploads" / Path(j(c.get(f"/api/items/{mid}"))["photo_url"]).name
    c.delete(f"/api/items/{mid}")
    if ph.exists(): ph.unlink()

# --- search -----------------------------------------------------------------------
check("keyword search", c.get("/api/search?q=bag").status_code == 200)
check("status", c.get("/api/status").status_code == 200)

# --- cleanup: remove test item photo ------------------------------------------------
left = j(c.get(f"/api/items/{iid}"))
check("test item still exists at end", "photo_url" in left, left)
if "photo_url" in left:
    photo = PROJECT / "static" / "uploads" / Path(left["photo_url"]).name.split("?")[0]
    c.delete(f"/api/items/{iid}")
    if photo.exists(): photo.unlink()

# --- phase 6: A5 maintenance.py ---------------------------------------------------
# Runs against the temp DB copy and a scratch photos folder, never static/uploads.
import contextlib, sqlite3
import maintenance
_mdb = SCRATCH / "binventory.db"
_mup = SCRATCH / "uploads"; _mup.mkdir()
_mc = sqlite3.connect(str(_mdb))
(_ia, ref_a), (_ib, ref_b) = _mc.execute(
    "SELECT id, photo_path FROM item WHERE photo_path != '' ORDER BY id LIMIT 2").fetchall()
_mc.execute("UPDATE item SET embedding_blob = ? WHERE id = ?", (b"\0" * 2048, _ia)); _mc.commit()
_items_before = _mc.execute("SELECT COUNT(*) FROM item").fetchone()[0]
_mc.close()
_ex = Image.Exif(); _ex[274] = 6                        # "rotate 90° CW to view"
Image.new("RGB", (60, 30), (9, 9, 9)).save(_mup / ref_a, "JPEG", exif=_ex)
Image.new("RGB", (20, 20), (9, 9, 9)).save(_mup / ref_b, "JPEG")
Image.new("RGB", (20, 20), (9, 9, 9)).save(_mup / "zz-orphan.jpg", "JPEG")
(_mup / ".gitkeep").write_text("")
_quiet = contextlib.redirect_stdout(io.StringIO())

with _quiet:
    _info = maintenance.cmd_schema(_mdb)
check("A5 schema reports current == latest", _info["current"] == _info["latest"] and not _info["pending"], _info)
with contextlib.redirect_stdout(io.StringIO()):
    _orph, _miss, _mv = maintenance.cmd_orphans(_mdb, _mup, apply=False, quarantine_dir=SCRATCH / "q")
check("A5 orphans finds the stray file, ignores .gitkeep", _orph == ["zz-orphan.jpg"], _orph)
check("A5 orphans reports items with missing photos",
      _miss and {n for _, _, n in _miss}.isdisjoint({ref_a, ref_b}), len(_miss))
check("A5 orphans dry run moves nothing", (_mup / "zz-orphan.jpg").exists() and _mv is None)
with contextlib.redirect_stdout(io.StringIO()):
    _orph, _, _mv = maintenance.cmd_orphans(_mdb, _mup, apply=True, quarantine_dir=SCRATCH / "q")
check("A5 orphans --apply moves (not deletes) the stray file",
      not (_mup / "zz-orphan.jpg").exists() and (_mv / "zz-orphan.jpg").exists()
      and (_mup / ref_a).exists() and (_mup / ref_b).exists())
with contextlib.redirect_stdout(io.StringIO()):
    _fx, _ = maintenance.cmd_fix_orientation(_mdb, _mup, apply=False)
check("A5 fix-orientation dry run finds only the EXIF-rotated photo, changes nothing",
      _fx == [ref_a] and Image.open(_mup / ref_a).size == (60, 30), _fx)
with contextlib.redirect_stdout(io.StringIO()):
    _fx, _cleared = maintenance.cmd_fix_orientation(_mdb, _mup, apply=True)
_mc = sqlite3.connect(str(_mdb))
check("A5 fix-orientation --apply rewrites upright and clears the embedding",
      Image.open(_mup / ref_a).size == (30, 60) and _cleared >= 1
      and _mc.execute("SELECT embedding_blob FROM item WHERE id = ?", (_ia,)).fetchone()[0] is None,
      (Image.open(_mup / ref_a).size, _cleared))
_mc.close()
with contextlib.redirect_stdout(io.StringIO()):
    _b, _a = maintenance.cmd_vacuum(_mdb, apply=True)
_mc = sqlite3.connect(str(_mdb))
check("A5 vacuum shrinks or keeps size, data and schema intact",
      _a["size"] <= _b["size"] and _a["free"] == 0
      and _mc.execute("SELECT COUNT(*) FROM item").fetchone()[0] == _items_before
      and _mc.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == _info["latest"],
      (_b, _a))
_mc.close()
with contextlib.redirect_stdout(io.StringIO()):
    check("A5 CLI entry point runs (schema)", maintenance.main(["schema"]) == 0)
check("A5 CLI points at the configured DB (the temp copy here)",
      maintenance._db_path() == SCRATCH / "binventory.db", maintenance._db_path())

# --- services layer: routes that were thinned in the MCP prep -----------------------
from binventory import events as _ev
_seen = []
_offs = [_ev.on(e, lambda event, **kw: _seen.append((event, kw)))
         for e in (_ev.ITEM_UPDATED, _ev.BIN_DELETED, _ev.BIN_UPDATED)]
_pa = j(c.post("/api/bins", json={"name": "ZZ sv parent"}))["id"]
_ch = j(c.post("/api/bins", json={"name": "ZZ sv child"}))["id"]
r = c.put(f"/api/bins/{_ch}", json={"name": "ZZ sv child 2", "parent_bin_id": _pa})
check("SV update bin: rename + nest via edit sheet", r.status_code == 200 and j(r)["name"] == "ZZ sv child 2"
      and j(r)["parent_bin_id"] == _pa, j(r))
check("SV update bin: loop refused", c.put(f"/api/bins/{_pa}", json={"parent_bin_id": _ch}).status_code == 400)
check("SV bin.updated emitted", any(e == _ev.BIN_UPDATED and kw.get("bin_id") == _ch for e, kw in _seen))
check("SV unpack", c.post(f"/api/bins/{_ch}/unpack").status_code == 200
      and c.post(f"/api/bins/{_ch}/unpack").status_code == 400)
c.post(f"/api/bins/{_ch}/nest", json={"parent_id": _pa})
check("SV unpack-all", j(c.post(f"/api/bins/{_pa}/unpack-all", json={}))["unpacked"] == 1)
check("SV duplicate bin name refused", c.post("/api/bins", json={"name": "ZZ sv parent"}).status_code == 400)
_sb = io.BytesIO(); Image.new("RGB", (30, 30), (9, 9, 90)).save(_sb, "JPEG"); _sb.seek(0)
_si = j(c.post("/api/items", data={"bin_id": _ch, "label": "sv item", "photo": (_sb, "s.jpg")},
               content_type="multipart/form-data"))["id"]
c.put(f"/api/items/{_si}", json={"notes": "sv note", "quantity": 3})
_upd = [kw for e, kw in _seen if e == _ev.ITEM_UPDATED and kw.get("item_id") == _si]
check("SV item.updated emitted with changes and actor",
      _upd and _upd[-1]["actor"] == "user" and set(_upd[-1]["changes"]) == {"notes", "quantity"}, _upd)
check("SV update to unknown bin refused, nothing changed",
      c.put(f"/api/items/{_si}", json={"bin_id": "nope", "notes": "x"}).status_code == 400
      and j(c.get(f"/api/items/{_si}"))["notes"] == "sv note")
sent.clear()
check("SV find-item resolves the home bin (no lighting configured -> 400)",
      c.post(f"/api/wled/find-item/{_si}").status_code == 400 and c.post("/api/wled/find-item/nope").status_code == 404)
check("SV shopping-delete refuses an active item", c.delete(f"/api/items/{_si}/shopping-delete").status_code == 400)
c.post(f"/api/items/{_si}/consume")
check("SV shopping-delete removes a consumed item", c.delete(f"/api/items/{_si}/shopping-delete").status_code == 200
      and c.get(f"/api/items/{_si}").status_code == 404)
r = c.delete(f"/api/bins/{_ch}", json={"confirm": "delete", "contents": "checkout"})
check("SV checkout-mode bin delete now emits bin.deleted",
      r.status_code == 200 and any(e == _ev.BIN_DELETED and kw.get("bin_id") == _ch for e, kw in _seen))
c.delete(f"/api/bins/{_pa}", json={"confirm": "delete", "contents": "delete"})
for _o in _offs: _o()
_hb = io.BytesIO(); Image.new("RGB", (30, 30), (1, 90, 9)).save(_hb, "JPEG"); _hb.seek(0)
_hi = j(c.post("/api/items", data={"bin_id": bin_id, "label": "zz quokka bracket", "photo": (_hb, "q.jpg")},
               content_type="multipart/form-data"))["id"]
_real = (ml.clip_ready, ml.touch_models, ml.embed_text)
from binventory.services import search as _ssvc
_ssvc.ml.clip_ready, _ssvc.ml.touch_models, _ssvc.ml.embed_text = (lambda: True), (lambda: None), (lambda q: [1.0] + [0.0] * 511)
_hr = j(c.get("/api/search?q=quokka"))
_ssvc.ml.clip_ready, _ssvc.ml.touch_models, _ssvc.ml.embed_text = _real
check("SV warm semantic search still finds a not-yet-analysed item by its exact words",
      _hr["mode"] == "semantic" and any(r["id"] == _hi for r in _hr["results"]), _hr)
c.delete(f"/api/items/{_hi}")
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()
check("SV keyword search mode reported", j(c.get("/api/search?q=zzznothing"))["mode"] in ("keyword", "semantic"))

# --- keyword ranking + tag replacement --------------------------------------------
from binventory.services import items as _isvc
_rk = {}
for _key, _label in (("inside", "zz bundle of abcqxvoltdef"), ("ocr", "zz something"),
                     ("tag", "zz other thing"), ("name", "zz qxvolt stick")):
    _rb = io.BytesIO(); Image.new("RGB", (30, 30), (9, 9, 90)).save(_rb, "JPEG"); _rb.seek(0)
    _rk[_key] = j(c.post("/api/items", data={"bin_id": bin_id, "label": _label, "photo": (_rb, "r.jpg")},
                         content_type="multipart/form-data"))["id"]
with app.app_context():
    _isvc.update(_rk["tag"], tags=["qxvolt", "meter"])
    _db.session.get(_Item, _rk["ocr"]).ocr_text = "QXVOLT 3000 made in"; _db.session.commit()
    check("RK clean_tags strips commas, trims, de-duplicates",
          _isvc.clean_tags(["a, b", " A b ", "", "c" * 90]) == "a b, " + "c" * 60)
_order = [r["id"] for r in j(c.get("/api/search?q=qxvolt"))["results"] if r["id"] in _rk.values()]
check("RK keyword ranking: name > tag > OCR text > inside another word",
      _order == [_rk["name"], _rk["tag"], _rk["ocr"], _rk["inside"]], _order)
_order = [r["id"] for r in j(c.get("/api/search?q=qxv"))["results"] if r["id"] in _rk.values()]
check("RK a word prefix ranks as a match (search-as-you-type)", _order[:1] == [_rk["name"]], _order)
check("RK multi-word queries match words in any field, any order",
      [r["id"] for r in j(c.get("/api/search?q=meter%20qxvolt"))["results"]] == [_rk["tag"]])
_ssvc.ml.clip_ready, _ssvc.ml.touch_models = (lambda: True), (lambda: None)
_ssvc.ml.embed_text = lambda q: [1.0] + [0.0] * 511
_hr = j(c.get("/api/search?q=qxvolt"))
_ssvc.ml.clip_ready, _ssvc.ml.touch_models, _ssvc.ml.embed_text = _real
check("RK with CLIP warm, a name match still comes first",
      _hr["mode"] == "semantic" and _hr["results"][0]["id"] == _rk["name"], _hr)
for _v in _rk.values():
    c.delete(f"/api/items/{_v}")
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()

# --- configurable name priority -----------------------------------------------------
from binventory import naming as _naming
from binventory.analysis import AnalysisResult as _ARn
from binventory.ml import worker as _worker
_nd = j(c.get("/api/settings/naming"))
check("NP default order: assistant directly below typed, typed highest",
      [o["key"] for o in _nd["order"]] == ["ml", "ocr", "scanned", "ocr_region", "agent", "user"] and _nd["is_default"], _nd)
check("NP partial / unknown order rejected",
      c.put("/api/settings/naming", json={"order": ["ml", "user"]}).status_code == 400
      and c.put("/api/settings/naming", json={"order": ["ml", "ocr", "scanned", "ocr_region", "agent", "boss"]}).status_code == 400)
_nb = io.BytesIO(); Image.new("RGB", (40, 40), (1, 2, 3)).save(_nb, "JPEG"); _nb.seek(0)
_ni = j(c.post("/api/items", data={"bin_id": bin_id, "label": "My typed name", "photo": (_nb, "n.jpg")},
               content_type="multipart/form-data"))["id"]
def _propose(name, source):
    with app.app_context():
        it = _db.session.get(_Item, _ni)
        _worker._apply_result(it, _ARn(name=name, name_source=source), "test"); _db.session.commit()
        return it.label, it.label_source
check("NP default: assistant can't overwrite a typed name", _propose("Agent name", "agent") == ("My typed name", "user"))
with app.app_context():
    _it = _db.session.get(_Item, _ni); _it.label, _it.label_source = "region text", "ocr_region"; _db.session.commit()
check("NP default: assistant beats region OCR", _propose("Agent name", "agent") == ("Agent name", "agent"))
check("NP scanned_search ranks with scanned", _naming.rank("scanned_search") == _naming.rank("scanned"))
r = c.put("/api/settings/naming", json={"order": ["ocr", "scanned", "ocr_region", "user", "agent", "ml"]})
check("NP custom order saved", r.status_code == 200 and not j(r)["is_default"] and j(r)["order"][-1]["key"] == "ml")
with app.app_context():
    _it = _db.session.get(_Item, _ni); _it.label, _it.label_source = "My typed name", "user"; _db.session.commit()
check("NP custom order: CLIP placed first now overwrites a typed name", _propose("clip guess", "ml") == ("clip guess", "ml"))
check("NP custom order: assistant above typed", _naming.rank("agent") > _naming.rank("user"))
_naming.invalidate()
with app.app_context():
    check("NP order persists (re-read from the database)", _naming.get_order()[-1] == "ml")
r = c.put("/api/settings/naming", json={"reset": True})
check("NP reset restores the default", j(r)["is_default"] and _naming.get_order() == _naming.DEFAULT_ORDER)
c.delete(f"/api/items/{_ni}")
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()

# --- thumbnails -----------------------------------------------------------------------
from binventory.services import thumbnails as _th
def _photo_name(url): return Path(url.split("?")[0]).name
def _size(path):
    with Image.open(path) as im: return im.size      # closed: Windows can't replace an open file
def _get(url):
    r = c.get(url); data = r.data; r.close(); return r, data
_tb = io.BytesIO(); Image.new("RGB", (1600, 1200), (30, 140, 60)).save(_tb, "JPEG"); _tb.seek(0)
_ti = j(c.post("/api/items", data={"bin_id": bin_id, "label": "zz thumb", "photo": (_tb, "t.jpg")},
               content_type="multipart/form-data"))
_tname = _photo_name(_ti["photo_url"])
check("TH item JSON carries a versioned thumb_url",
      _ti["thumb_url"].startswith(f"/thumbs/{_tname}?v="), _ti.get("thumb_url"))
check("TH thumbnail built at upload, long edge 512",
      _th.thumb_path(_tname).exists() and max(_size(_th.thumb_path(_tname))) == 512,
      _size(_th.thumb_path(_tname)) if _th.thumb_path(_tname).exists() else "missing")
r, _data = _get(_ti["thumb_url"])
check("TH versioned thumb served as cacheable JPEG, smaller than the photo",
      r.status_code == 200 and r.mimetype == "image/jpeg" and "immutable" in r.headers.get("Cache-Control", "")
      and len(_data) < (_UPLOADS / _tname).stat().st_size, (r.status_code, r.headers.get("Cache-Control")))
check("TH unversioned thumb URL is not marked immutable",
      "immutable" not in _get(f"/thumbs/{_tname}")[0].headers.get("Cache-Control", ""))
r = c.post(f"/api/items/{_ti['id']}/rotate", json={"degrees": 90})
_rot = j(c.get(f"/api/items/{_ti['id']}"))
check("TH rotate rebuilds the thumbnail and changes its URL",
      _size(_th.thumb_path(_tname)) == (384, 512) and _rot["thumb_url"] != _ti["thumb_url"],
      _size(_th.thumb_path(_tname)))
_th.thumb_path(_tname).unlink()
check("TH missing thumbnail rebuilt on first request (old photos need no migration)",
      _get(_rot["thumb_url"])[0].status_code == 200 and _th.thumb_path(_tname).exists())
check("TH no photo -> 404", _get("/thumbs/zz-does-not-exist.jpg")[0].status_code == 404)
check("TH path traversal refused", _th.ensure("../binventory.db") is None and _th.ensure("..\\x.jpg") is None
      and _get("/thumbs/..%2Fbinventory.db")[0].status_code == 404)
c.post(f"/api/items/{_ti['id']}/consume")
_tb2 = io.BytesIO(); Image.new("RGB", (900, 900), (200, 200, 0)).save(_tb2, "JPEG"); _tb2.seek(0)
c.post(f"/api/items/{_ti['id']}/restock", data={"bin_id": bin_id, "label": "zz thumb", "notes": "",
       "photo": (_tb2, "n.jpg")}, content_type="multipart/form-data")
_new = _photo_name(j(c.get(f"/api/items/{_ti['id']}"))["photo_url"])
check("TH restock photo: new thumbnail built, old one removed",
      _new != _tname and _th.thumb_path(_new).exists() and not _th.thumb_path(_tname).exists())
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()

# The maintenance command scans a whole folder and deletes stray thumbnails, so
# it runs against a scratch copy of the photos folder — never the real one,
# whose thumbnails a fresh-DB run would otherwise see as strays.
_real_dirs = (_th.UPLOAD_DIR, _th.THUMB_DIR)
_th.UPLOAD_DIR = SCRATCH / "th_uploads"; _th.THUMB_DIR = _th.UPLOAD_DIR / "thumbs"
_th.THUMB_DIR.mkdir(parents=True)
with app.app_context():
    for _row in _Item.query.all():
        if _row.photo_path and (_UPLOADS / _row.photo_path).is_file():
            shutil.copy(_UPLOADS / _row.photo_path, _th.UPLOAD_DIR / _row.photo_path)
(_th.THUMB_DIR / "zz-stray.jpg").write_bytes(b"x")
with contextlib.redirect_stdout(io.StringIO()):
    _build, _stray, _ = maintenance.cmd_thumbnails(SCRATCH / "binventory.db", apply=False)
check("TH maintenance dry run: finds the missing thumb and the stray, changes nothing",
      _new in _build and "zz-stray.jpg" in _stray and (_th.THUMB_DIR / "zz-stray.jpg").exists()
      and not _th.thumb_path(_new).exists(), (len(_build), _stray))
with contextlib.redirect_stdout(io.StringIO()):
    _, _, _built = maintenance.cmd_thumbnails(SCRATCH / "binventory.db", apply=True)
check("TH maintenance --apply builds missing, deletes stray",
      _th.thumb_path(_new).exists() and not (_th.THUMB_DIR / "zz-stray.jpg").exists() and _built >= 1)
_th.UPLOAD_DIR, _th.THUMB_DIR = _real_dirs
check("TH maintenance test never touched the real thumbnails folder",
      _thumbs_before <= set(os.listdir(_THUMBS)), len(_thumbs_before))
with contextlib.redirect_stdout(io.StringIO()):
    _orph2, _, _ = maintenance.cmd_orphans(SCRATCH / "binventory.db", _UPLOADS, apply=False, quarantine_dir=SCRATCH / "q2")
check("TH thumbs folder is never reported as an orphan photo", "thumbs" not in _orph2, _orph2[:5])
c.delete(f"/api/items/{_ti['id']}")
check("TH item delete removes its thumbnail", not _th.thumb_path(_new).exists())
(_UPLOADS / _new).unlink(missing_ok=True)

# --- phase 7: plugin loader + barcode plugin ----------------------------------------
import cv2, numpy as np
from binventory import plugins as _plugins, analysis as _analysis
from binventory.analysis import AnalysisContext as _Ctx
from binventory.ml import worker as _worker
from binventory.models import ExtensionData as _ED

_pl = {p["id"]: p for p in j(c.get("/api/plugins"))}
check("P7 barcode plugin loaded", _pl.get("barcode_reader", {}).get("status") == "loaded", _pl.get("barcode_reader"))
check("P7 malformed manifest reported, not fatal", _pl.get("bad_manifest", {}).get("status") == "invalid", _pl.get("bad_manifest"))
check("P7 wrong api_version refused", _pl.get("old_api", {}).get("status") == "incompatible")
check("P7 missing dependency reported with install hint",
      _pl.get("needs_dep", {}).get("status") == "missing_deps" and "pip install nonexistent-pkg" in _pl["needs_dep"]["error"],
      _pl.get("needs_dep"))
check("P7 register() raising -> error, its analyzer rolled back",
      _pl.get("broken", {}).get("status") == "error" and _analysis.get("broken_an") is None, _pl.get("broken"))
check("P7 undeclared action refused", _pl.get("sneaky", {}).get("status") == "error" and "not declared" in _pl["sneaky"]["error"])
check("P7 can't enable a plugin with missing deps",
      c.post("/api/plugins/needs_dep", json={"enabled": True}).status_code == 400)
check("P7 permissions + settings exposed for review",
      "network:external" in _pl["barcode_reader"]["permissions"]
      and _pl["barcode_reader"]["settings"][0]["key"] == "lookup_enabled"
      and _pl["barcode_reader"]["settings"][0]["value"] is False)

# Synthetic codes: an EAN-13 drawn from its bit pattern, and QR codes via OpenCV.
_L = ["0001101","0011001","0010011","0111101","0100011","0110001","0101111","0111011","0110111","0001011"]
_G = ["0100111","0110011","0011011","0100001","0011101","0111001","0000101","0010001","0001001","0010111"]
_R = ["1110010","1100110","1101100","1000010","1011100","1001110","1010000","1000100","1001000","1110100"]
_P = ["LLLLLL","LLGLGG","LLGGLG","LLGGGL","LGLLGG","LGGLLG","LGGGLL","LGLGLG","LGLGGL","LGGLGL"]
def _ean13(code):
    d = [int(x) for x in code]
    bits = "101" + "".join((_L if p == "L" else _G)[d[1 + i]] for i, p in enumerate(_P[d[0]])) \
         + "01010" + "".join(_R[x] for x in d[7:]) + "101"
    img = np.full((280, len(bits) * 4 + 80), 255, np.uint8)
    for i, b in enumerate(bits):
        if b == "1": img[40:240, 40 + i * 4: 44 + i * 4] = 0
    img = cv2.copyMakeBorder(img, 300, 300, 300, 300, cv2.BORDER_CONSTANT, value=200)
    return Image.fromarray(img).convert("RGB")
def _qr(text):
    img = cv2.resize(cv2.QRCodeEncoder.create().encode(text), None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    return Image.fromarray(cv2.copyMakeBorder(img, 60, 60, 60, 60, cv2.BORDER_CONSTANT, value=255)).convert("RGB")

_bc = _analysis.get("barcode")
_bmod = sys.modules["binventory_plugin_barcode_reader"]
def _new_item(label="", source=""):
    b = io.BytesIO(); Image.new("RGB", (40, 40), (5, 5, 5)).save(b, "JPEG"); b.seek(0)
    iid_ = j(c.post("/api/items", data={"bin_id": bin_id, "label": "", "photo": (b, "b.jpg")},
                    content_type="multipart/form-data"))["id"]
    with app.app_context():
        it = _db.session.get(_Item, iid_); it.label, it.label_source = label, source; _db.session.commit()
    return iid_
def _scan(iid_, image):
    with app.app_context():
        it = _db.session.get(_Item, iid_)
        wants = _bc.wants(_Ctx(image_path=None, image=None, item=it))
        res = _bc.analyze(_Ctx(image_path=None, image=image, item=it))
        _worker._apply_result(it, res, _bc.id, _bc.plugin_id); _db.session.commit()
        return wants, res, dict(label=it.label, source=it.label_source, tags=it.tags)

_b1 = _new_item("Heinz Baked Beans jumble", "ocr")
_w, _res, _it = _scan(_b1, _ean13("5000112637922"))
check("P7 EAN-13 decoded -> searchable tag, no name without lookup",
      _w and "5000112637922" in _it["tags"] and _it["label"] == "Heinz Baked Beans jumble", _it)
check("P7 plugin data lands in its store, not on the item",
      j(c.get(f"/api/items/{_b1}/plugin-sections"))[0]["rows"][0]["values"] == ["5000112637922 (EAN-13)"],
      j(c.get(f"/api/items/{_b1}/plugin-sections")))
with app.app_context():
    check("P7 scanned once: wants() is False afterwards",
          not _bc.wants(_Ctx(image_path=None, image=None, item=_db.session.get(_Item, _b1))))
check("P7 keyword search finds the item by its barcode number",
      any(i["id"] == _b1 for i in j(c.get("/api/search?q=5000112637922"))["results"])
      if isinstance(j(c.get("/api/search?q=5000112637922")), dict)
      else any(i["id"] == _b1 for i in j(c.get("/api/search?q=5000112637922"))))

# Lookup on, providers stubbed (no network in tests).
_calls = []
_real_providers = _bmod.lookup.PROVIDERS
_bmod.lookup.PROVIDERS = [_bmod.lookup.Provider("open_food", "StubDB", "test", "product",
                                                lambda code, setting: _calls.append(code) or "Stub Brand Beans")]
check("P7 settings: toggle saved", c.post("/api/plugins/barcode_reader/settings",
                                          json={"lookup_enabled": True}).status_code == 200)
check("P7 settings: undeclared key rejected", c.post("/api/plugins/barcode_reader/settings",
                                                   json={"evil": 1}).status_code == 400)
_b2 = _new_item("OCR jumble", "ocr")
_, _, _it = _scan(_b2, _ean13("5000112637922"))
check("P7 lookup hit renames over OCR (scanned_search), OCR kept as tag",
      _it["label"] == "Stub Brand Beans" and _it["source"] == "scanned_search" and "OCR jumble" in _it["tags"], _it)
_b3 = _new_item("My typed name", "user")
_, _, _it = _scan(_b3, _ean13("5000112637922"))
check("P7 lookup never overrides a typed name; answer came from cache",
      _it["label"] == "My typed name" and _calls == ["5000112637922"], (_it, _calls))
_b4 = _new_item("Region read", "ocr_region")
_, _, _it = _scan(_b4, _qr("Spare fuses 5A"))
check("P7 QR text loses to region OCR", _it["label"] == "Region read", _it)
_b5 = _new_item("clip guess", "ml")
_, _, _it = _scan(_b5, _qr("Spare fuses 5A"))
check("P7 QR text names an ML-named item (scanned)", _it["label"] == "Spare fuses 5A" and _it["source"] == "scanned", _it)
_b6 = _new_item("clip guess", "ml")
_, _, _it = _scan(_b6, _qr("https://example.com/manual?id=7"))
_secs = j(c.get(f"/api/items/{_b6}/plugin-sections"))
check("P7 QR link shown as a link, not used as name",
      _it["label"] == "clip guess" and {"label": "Link", "type": "link", "values": ["https://example.com/manual?id=7"]} in _secs[0]["rows"],
      (_it, _secs))

# A plugin analyzer can't write core columns through `data`.
from binventory.analysis import AnalysisResult as _AR
with app.app_context():
    _it = _db.session.get(_Item, _b5)
    _worker._apply_result(_it, _AR(data={"label": "HIJACKED", "label_source": "user"}), "barcode", "barcode_reader")
    _db.session.commit()
    check("P7 plugin `data` can't set core fields",
          _db.session.get(_Item, _b5).label == "Spare fuses 5A"
          and _ED.query.filter_by(namespace="barcode_reader", scope_id=_b5, key="label").count() == 1)

# Actions.
r = c.post("/api/plugins/barcode_reader/actions/scan_existing", json={})
_n_unscanned = j(r)["queued"]
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()
r2 = c.post("/api/plugins/barcode_reader/actions/rescan_all", json={})
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()
with app.app_context():
    _total = _Item.query.count()
check("P7 scan_existing queues only unscanned items; rescan_all queues all",
      r.status_code == 200 and 0 < _n_unscanned < _total and j(r2)["queued"] == _total, (_n_unscanned, j(r2), _total))
check("P7 unknown action 404", c.post("/api/plugins/barcode_reader/actions/nope", json={}).status_code == 404)

# Disable / enable, live and persisted.
r = c.post("/api/plugins/barcode_reader", json={"enabled": False})
check("P7 disable unregisters the analyzer immediately",
      j(r)["status"] == "disabled" and _analysis.get("barcode") is None)
check("P7 disabled plugin: actions and item sections stop",
      c.post("/api/plugins/barcode_reader/actions/scan_existing", json={}).status_code == 400
      and j(c.get(f"/api/items/{_b1}/plugin-sections")) == [])
with app.app_context():
    _plugins.load_all(app)            # a restart
check("P7 disabled state survives a restart", _plugins.get("barcode_reader").status == "disabled")
r = c.post("/api/plugins/barcode_reader", json={"enabled": True})
check("P7 re-enable registers the analyzer again",
      j(r)["status"] == "loaded" and _analysis.get("barcode") is not None and _analysis.get("barcode").plugin_id == "barcode_reader")

# Plugin API additions for long-running services (MCP prep).
import binventory as _pkg
_log = _pkg.__dict__.setdefault("_svc_log", [])
_sv = {p["id"]: p for p in j(c.get("/api/plugins"))}.get("svc_demo", {})
check("PA service started at load, app context usable in register()",
      _sv.get("status") == "loaded" and "start" in _log and ("items", True) in _log, (_sv.get("status"), _log))
check("PA status rows exposed; code type kept; non-http link dropped",
      _sv.get("status_rows", [{}])[0].get("values") == ["running"]
      and _sv["status_rows"][1]["type"] == "code" and _sv["status_rows"][2]["values"] == [], _sv.get("status_rows"))
c.post("/api/plugins/svc_demo/settings", json={"port": 7})
check("PA settings listener told what changed", ("changed", ["port"]) in _log, _log)
_n = len(_log)
c.post("/api/plugins/svc_demo", json={"enabled": False})
check("PA disabling a plugin stops its service", _log[_n:] == ["stop"], _log[_n:])
_n = len(_log)
c.post("/api/plugins/svc_demo", json={"enabled": True})
check("PA re-enabling starts it again", "start" in _log[_n:], _log[_n:])
_n = len(_log)
with app.app_context():
    _plugins.load_all(app)
check("PA a reload stops the old service before starting the new one",
      _log[_n:_n + 1] == ["stop"] and "start" in _log[_n + 1:], _log[_n:])

# --- plugin routes, pages and links ------------------------------------------
_pl = {p["id"]: p for p in j(c.get("/api/plugins"))}
check("PR a plugin with web:routes exposes its page and links",
      _pl["web_demo"]["page"] == {"title": "Demo page", "path": "/"}
      and _pl["web_demo"]["links"][0]["where"] == "item", _pl["web_demo"])
r = c.get("/p/web_demo/")
check("PR plugin page served under /p/<id>/", r.status_code == 200 and r.data == b"demo page", r.status_code)
check("PR route variables arrive as arguments", c.get("/p/web_demo/item/abc").data == b"item abc")
check("PR wrong method -> 405, unknown path -> 404",
      c.get("/p/web_demo/echo").status_code == 405 and c.get("/p/web_demo/nope").status_code == 404)
r = c.get("/p/web_demo/missing")
check("PR a service error in a plugin view becomes the usual JSON error",
      r.status_code == 404 and j(r)["error"] == "No such widget", (r.status_code, r.data[:80]))
c.post("/api/plugins/web_demo", json={"enabled": False})
_pl = {p["id"]: p for p in j(c.get("/api/plugins"))}
check("PR disabling a plugin removes its routes and page at once",
      c.get("/p/web_demo/").status_code == 404 and _pl["web_demo"]["page"] is None)
c.post("/api/plugins/web_demo", json={"enabled": True})
check("PR re-enabling brings them back", c.get("/p/web_demo/").status_code == 200)
check("PR [page] without web:routes is a broken manifest",
      _pl["web_nope"]["status"] == "invalid" and "web:routes" in _pl["web_nope"]["error"], _pl["web_nope"])
check("PR add_route without web:routes refuses to load",
      _pl["route_noperm"]["status"] == "error" and "web:routes" in _pl["route_noperm"]["error"], _pl["route_noperm"])

# --- codes: local ids, conflicts, scanning ------------------------------------
import re as _re
from binventory import scanning as _scan
from binventory.services import codes as _codes
from binventory.models import Code as _Code


def _new_item(label, **extra):
    buf = io.BytesIO(); Image.new("RGB", (30, 30), (40, 80, 120)).save(buf, "JPEG"); buf.seek(0)
    return c.post("/api/items", data={"bin_id": bin_id, "label": label, "photo": (buf, "c.jpg"), **extra},
                  content_type="multipart/form-data")


def _photo_of(png_bytes):
    """A label 'photographed': on a white sheet, grey, slightly soft."""
    from PIL import ImageFilter
    lab = Image.open(io.BytesIO(png_bytes)).convert("L")
    sheet = Image.new("L", (lab.width + 300, lab.height + 300), 255)
    sheet.paste(lab, (150, 150))
    return sheet.filter(ImageFilter.GaussianBlur(0.6))


def _scan_photo(img):
    buf = io.BytesIO(); img.convert("RGB").save(buf, "JPEG", quality=92); buf.seek(0)
    return j(c.post("/api/scan", data={"photo": (buf, "s.jpg")}, content_type="multipart/form-data"))


_EAN = "4006381333931"                     # a valid EAN-13 not in the real library
_ca, _cb, _cc = (j(_new_item(n))["id"] for n in ("zz code A", "zz code B", "zz code C"))
_photos = [j(c.get(f"/api/items/{x}"))["photo_url"] for x in (_ca, _cb, _cc)]
r = c.post("/api/codes", json={"scope": "item", "id": _ca, "value": _EAN, "kind": "ean_13"})
check("CD add a barcode to an item", r.status_code == 201 and j(r)["source"] == "typed", j(r))
check("CD the item lists it", [x["value"] for x in j(c.get(f"/api/codes?scope=item&id={_ca}"))] == [_EAN])
r = c.post("/api/codes", json={"scope": "item", "id": _cb, "value": _EAN})
check("CD the same code on another item -> 409 naming where it already is",
      r.status_code == 409 and j(r)["conflicts"][0]["id"] == _ca and "zz code A" in j(r)["error"], j(r))
r = c.post("/api/codes", json={"scope": "item", "id": _cb, "value": _EAN, "force": True})
check("CD ...and allowed once the user confirms (force)", r.status_code == 201)
check("CD adding a code the item already has is a no-op",
      c.post("/api/codes", json={"scope": "item", "id": _ca, "value": _EAN}).status_code == 201
      and len(j(c.get(f"/api/codes?scope=item&id={_ca}"))) == 1)
_hits = [m["id"] for m in j(c.post("/api/scan", json={"value": _EAN}))["matches"]]
check("CD scanning a shared code returns every item that has it", sorted(_hits) == sorted([_ca, _cb]), _hits)
c.post("/api/codes", json={"scope": "item", "id": _cc, "value": "012345678905", "kind": "upc_a"})
check("CD a UPC-A matches its EAN-13 spelling (leading zero)",
      [m["id"] for m in j(c.post("/api/scan", json={"value": "0012345678905"}))["matches"]] == [_cc])
_res = [x["id"] for x in j(c.get(f"/api/search?q={_EAN}"))["results"]]
check("CD typing a barcode into Search finds its items first", sorted(_res[:2]) == sorted([_ca, _cb]), _res[:3])

with app.app_context():
    _n_before = _Item.query.count()
r = _new_item("zz dup", barcode=_EAN, barcode_kind="ean_13")
with app.app_context():
    _n_after = _Item.query.count()
check("CD Add Item with a barcode already in use -> 409 and nothing saved",
      r.status_code == 409 and j(r)["conflicts"] and _n_after == _n_before, (r.status_code, _n_before, _n_after))
r = _new_item("zz dup", barcode=_EAN, barcode_kind="ean_13", force_barcode="1")
_cd = j(r)["id"]
_photos.append(j(r)["photo_url"])
_cdc = j(c.get(f"/api/codes?scope=item&id={_cd}"))
check("CD ...saved with the barcode attached once confirmed",
      r.status_code == 201 and _cdc[0]["value"] == _EAN and _cdc[0]["source"] == "scanned", _cdc)
r = _new_item("zz fresh code", barcode="ZZ-LOCAL-1")
_ce = j(r)["id"]
_photos.append(j(r)["photo_url"])
check("CD Add Item with a new barcode needs no question",
      r.status_code == 201 and j(c.get(f"/api/codes?scope=item&id={_ce}"))[0]["value"] == "ZZ-LOCAL-1")

with app.app_context():
    _lc = _codes.ensure_local("item", _ca).value
    _lc2 = _codes.ensure_local("item", _ca).value
check("CD a label code is generated once and then stable",
      _lc == _lc2 and _re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{6}", _lc), _lc)
r = c.get(f"/c/{_lc}")
check("CD /c/<code> opens the item", r.status_code == 302 and r.headers["Location"].endswith(f"/#open=item:{_ca}"),
      r.headers.get("Location"))
r = c.get(f"/C/{_lc.lower()}")
check("CD upper-case /C/ and lower-case codes work too (QR labels print upper case)",
      r.status_code == 302 and f"open=item:{_ca}" in r.headers["Location"], r.headers.get("Location"))
check("CD an unknown code opens the Scan tab with it", c.get("/c/ZZZZZZ").headers["Location"].endswith("/#scan=ZZZZZZ"))
check("CD a scanned label URL resolves like its code",
      [m["id"] for m in j(c.post("/api/scan", json={"value": f"HTTPS://10.0.0.9:5000/C/{_lc}"}))["matches"]] == [_ca])

# --- the label maker ----------------------------------------------------------
_lb = j(c.post("/api/bins", json={"name": "zz Label bin", "location": "Garage"}))["id"]
_lb2 = j(c.post("/api/bins", json={"name": "zz Label bin 2"}))["id"]
c.put(f"/api/items/{_cc}", json={"bin_id": _lb})
_info = j(c.get("/p/labels/info"))
check("LB info: six sizes, QR and AprilTag (Code 128 off by default), a read-distance row per size, QR by default",
      len(_info["sizes"]) == 6 and _info["styles"] == ["QR code", "AprilTag"]
      and len(_info["distances"]["rows"]) == 6 and _info["defaults"]["style"] == "QR code"
      and {"1 × 1 in", "4 × 6 in"} <= {s["key"] for s in _info["sizes"]}, _info.get("defaults"))
_d11 = _info["distances"]["rows"][0]["styles"]
check("LB read distance ranks AprilTag > QR > Code 128 for the same label",
      _d11["AprilTag"]["inches"] > _d11["QR code"]["inches"] > _d11["Code 128"]["inches"], _d11)
check("LB QR links swap localhost for a network address",
      "localhost" not in _info["base_url"] and "localhost" in _info["base_url_note"], _info["base_url"])
check("LB the page is served", c.get("/p/labels/").status_code == 200 and b"Label maker" in c.get("/p/labels/").data)
_sizes_ok = []
for _s in _info["sizes"]:
    r = c.post("/p/labels/preview", json={"scope": "bin", "id": _lb, "size": _s["key"]})
    im = Image.open(io.BytesIO(r.data))
    _sizes_ok.append(r.status_code == 200 and abs(im.width - _s["w"] * 300) <= 1 and abs(im.height - _s["h"] * 300) <= 1)
check("LB every size previews at 300 dpi, exactly the label's size", all(_sizes_ok), _sizes_ok)

r = c.post("/p/labels/preview", json={"scope": "bin", "id": _lb, "size": "1⅛ × 3½ in", "style": "QR code"})
with app.app_context():
    _blc = _codes.ensure_local("bin", _lb).value
_got = _scan.decode(_photo_of(r.data))
check("LB a QR bin label decodes to its upper-case /C/<code> URL",
      any(t.endswith(f"/C/{_blc}") and t.startswith(("HTTP://", "HTTPS://")) for t, k in _got if k == "qr"), _got)
_sr = _scan_photo(_photo_of(r.data))
check("LB scanning a photo of the printed bin label finds the bin",
      [m["id"] for m in _sr["matches"]] == [_lb], _sr)
r = c.post("/p/labels/preview", json={"scope": "bin", "id": _lb, "size": "4 × 6 in", "style": "AprilTag"})
_sr = _scan_photo(_photo_of(r.data).resize((900, 1350)))
with app.app_context():
    _tag = _codes.ensure_apriltag(_lb).value
    _tag2 = _codes.ensure_apriltag(_lb2).value
check("LB an AprilTag bin label is found from a photo", [m["id"] for m in _sr["matches"]] == [_lb], _sr)
check("LB bins get distinct AprilTag ids", _tag != _tag2, (_tag, _tag2))
r = c.post("/p/labels/preview", json={"scope": "item", "id": _cc, "size": "1 × 1 in", "style": "AprilTag"})
check("LB items fall back to QR when the style is AprilTag",
      any(k == "qr" for _, k in _scan.decode(_photo_of(r.data))))
_lm = sys.modules["binventory_plugin_labels"]
with app.test_request_context(base_url="https://10.0.0.9:5000"):
    _sp = _lm.build_spec("item", _cc, "QR code", {"use_barcode": True}, "https://x")
    _sp2 = _lm.build_spec("item", _cc, "QR code", {}, "https://x")
check("LB 'use the item's own barcode' prints the UPC as EAN-13; off, it prints the label QR",
      _sp.symbol == ("ean13", "012345678905") and _sp2.symbol[0] == "qr", (_sp.symbol, _sp2.symbol))
check("LB home-network addresses win over a VPN's 10.x for QR links",
      sorted(["10.111.18.2", "192.168.0.212", "172.20.1.5"], key=_lm._rank) == ["192.168.0.212", "172.20.1.5", "10.111.18.2"])
_rend = _lm.render
check("LB Code 128 is the right length (quiet zones, start, data, check, stop)",
      len(_rend.code128_modules("K7Q2MD")) == 10 + 11 * (1 + 6 + 1) + 13 + 10)
try:
    _rend.ean13_modules("4006381333932"); _bad = False
except ValueError:
    _bad = True
check("LB an EAN-13 with a bad check digit is refused", _bad)
r = c.post("/p/labels/pdf", json={"targets": [{"scope": "bin", "id": _lb}, {"scope": "bin", "id": _lb2}],
                                  "size": "1⅛ × 3½ in", "copies": 2})
_pages = _re.findall(rb"/Type /Page\b", r.data)
_box = _re.findall(rb"/MediaBox \[ 0 0 ([\d.]+) ([\d.]+) \]", r.data)
check("LB PDF: a page per label and copy, each page the label's size",
      r.status_code == 200 and r.mimetype == "application/pdf" and len(_pages) == 4
      and all(abs(float(w) - 252) < 1 and abs(float(h) - 81) < 1 for w, h in _box), (len(_pages), _box[:1]))
check("LB PDF with nothing picked -> 400",
      c.post("/p/labels/pdf", json={"targets": []}).status_code == 400)
_tg = j(c.get(f"/p/labels/targets?scope=item&ids={_cc}"))
check("LB an item link (?item=) resolves its bin and its barcode",
      _tg[0]["bin_id"] == _lb and _tg[0]["barcode"] == "012345678905", _tg)

# --- label text, reprints, new codes (LT) ---------------------------------------
from PIL import ImageDraw
from binventory.services.storage import ExtensionStore as _XS
check("LT info lists five text sizes, Medium by default",
      _info["text_sizes"] == ["Smallest", "Small", "Medium", "Large", "Largest"]
      and _info["defaults"]["text_size"] == "Medium", _info.get("text_sizes"))
check("LT an unknown text size -> 400",
      c.post("/p/labels/preview", json={"scope": "bin", "id": _lb, "text_size": "Huge"}).status_code == 400)
_lspec = _rend.Spec(("qr", "HTTPS://X/C/ABC123"), "Knives & Flashlights", "Garage / Shelf E-2", "ABC123")

def _ink(img, x0=0, x1=None, y0=0):
    a = np.array(img.convert("L")) < 128
    a = a[y0:, x0:x1]
    rows, cols = np.where(a)
    return rows.min() + y0, rows.max() + y0, cols.min() + x0, cols.max() + x0

_wide = _rend.render(_lspec, "1⅛ × 3½ in")
_qr_right = _ink(_wide, 0, 340)[3]
_t = _ink(_wide, _qr_right + 30)
check("LT beside a QR, the text block is centred on the code's middle",
      abs((_t[0] + _t[1]) / 2 - _wide.height / 2) < _wide.height * 0.08, (_t, _wide.height))
_tall = _rend.render(_rend.Spec(("qr", "HTTPS://X/C/ABC123"), "Knives & Flashlights", "Garage", "ABC123",
                                contents=["One", "Two"]), "4 × 6 in")
_qr_bottom = int(_tall.height * 0.5) + 30
_t = _ink(_tall, 0, None, _qr_bottom)
check("LT under a QR (4×6), the text is centred on the code's axis",
      abs((_t[2] + _t[3]) / 2 - _tall.width / 2) < 12, (_t, _tall.width))
_d = ImageDraw.Draw(Image.new("L", (10, 10)))
_lines = _rend._fit_lines(_d, "Knives & Flashlights", _rend._font(80), 300, 2)
check("LT long words are never split; a word that can't fit is cut with an ellipsis",
      all(l in ("Knives", "&", "Knives &") or l.endswith("…") for l in _lines) and "Flashlights" not in _lines, _lines)
_small = _rend.render(_lspec, "1¼ × 2¼ in", text_scale=0.7)
_big = _rend.render(_lspec, "1¼ × 2¼ in", text_scale=1.45)
_ink_s = (np.array(_small.convert("L"))[:, 400:] < 128).sum()
_ink_b = (np.array(_big.convert("L"))[:, 400:] < 128).sum()
check("LT the text size scales the lettering", _ink_b > _ink_s * 1.2, (_ink_s, _ink_b))

r = c.post("/p/labels/pdf", json={"targets": [{"scope": "bin", "id": _lb}], "size": "4 × 6 in",
                                  "style": "AprilTag", "text_size": "Large", "options": {"contents": False}})
_row = next(b for b in j(c.get("/p/labels/targets?scope=bin")) if b["id"] == _lb)
check("LT printing remembers how, for a reprint (size, style, text size, options)",
      r.status_code == 200 and _row["last"]["size"] == "4 × 6 in" and _row["last"]["style"] == "AprilTag"
      and _row["last"]["text_size"] == "Large" and _row["last"]["options"] == {"contents": False}
      and _row["code"] == _blc, _row)
_lnk = [l for l in j(c.get("/api/plugins"))["plugins"] if l["id"] == "labels"][0]["links"] \
    if isinstance(j(c.get("/api/plugins")), dict) else [l for l in j(c.get("/api/plugins")) if l["id"] == "labels"][0]["links"]
check("LT bins and items both get a Reprint label link",
      {("bin", "/?bin={id}&reprint=1"), ("item", "/?item={id}&reprint=1")} <= {(l["where"], l["path"]) for l in _lnk}, _lnk)
r = c.post("/p/labels/new-code", json={"scope": "bin", "id": _lb})
_nc = j(r)
check("LT a new code replaces the old one; the old label stops resolving",
      r.status_code == 200 and _nc["old"] == _blc and _nc["code"] != _blc
      and c.get(f"/c/{_blc}").headers["Location"].endswith(f"/#scan={_blc}")
      and c.get(f"/c/{_nc['code']}").headers["Location"].endswith(f"/#open=bin:{_lb}"), _nc)
check("LT new code for an unknown bin -> 404",
      c.post("/p/labels/new-code", json={"scope": "bin", "id": "nope"}).status_code == 404)
_pls = j(c.get("/api/plugins"))
_lset = {f["key"]: f for f in next(p for p in _pls if p["id"] == "labels")["settings"]}
check("LT Code 128 sits behind its own switch, off, which carries the scanner warning",
      _lset["allow_code128"]["value"] is False and "scanners only" not in _lset["code_style"].get("help", "")
      and "phone cameras" in _lset["allow_code128"]["help"]
      and _lset["code_style"]["option_requires"] == {"Code 128": "allow_code128"}, _lset["allow_code128"])
r = c.post("/p/labels/preview", json={"scope": "bin", "id": _lb, "style": "Code 128"})
check("LT with the switch off, a Code 128 label is refused, pointing at the setting",
      r.status_code == 400 and "Code 128 barcodes" in j(r)["error"], r.data[:200])
c.post("/api/plugins/labels/settings", json={"allow_code128": True, "code_style": "Code 128"})
_info128 = j(c.get("/p/labels/info"))
r = c.post("/p/labels/preview", json={"scope": "bin", "id": _lb, "style": "Code 128"})
check("LT switched on, Code 128 is offered and prints",
      _info128["styles"] == ["QR code", "AprilTag", "Code 128"] and _info128["defaults"]["style"] == "Code 128"
      and r.status_code == 200, _info128["styles"])
c.post("/api/plugins/labels/settings", json={"allow_code128": False})
check("LT switched off again, a saved Code 128 default falls back to QR",
      j(c.get("/p/labels/info"))["defaults"]["style"] == "QR code")
c.post("/api/plugins/labels/settings", json={"code_style": "QR code"})

# --- icons (IC) -------------------------------------------------------------------
import re as _re
_sprite = (PROJECT / "static/icons/lucide.svg").read_text(encoding="utf-8")
_have = set(_re.findall(r'<symbol id="([a-z0-9-]+)"', _sprite))
_ui = [PROJECT / "templates/index.html", *sorted((PROJECT / "static/js").glob("*.js")),
       *sorted(PROJECT.glob("plugins/*/web/*.html")), *sorted(PROJECT.glob("plugins/*/plugin.toml"))]
_used, _emoji = set(), []
_EMOJI = _re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u25A0-\u25FF\u2194-\u21FF]")
for _f in _ui:
    _t = _f.read_text(encoding="utf-8")
    _used |= set(_re.findall(r"lucide\.svg#([a-z0-9-]+)", _t))
    _used |= set(_re.findall(r"\b(?:icon|setIconText)\([^,()]*?'([a-z0-9-]+)'", _t))
    _used |= set(_re.findall(r'^icon\s*=\s*"([a-z0-9-]+)"', _t, flags=_re.M))
    _emoji += [f"{_f.name}: {ch}" for ch in _EMOJI.findall(_t)]
check("IC the UI has no emoji left (icons come from the Lucide sprite)", not _emoji, _emoji[:10])
check("IC every icon the UI names is in the sprite", _used and _used <= _have, sorted(_used - _have))
check("IC the icons ship with the app, licence included (no CDN)",
      (PROJECT / "static/icons/LICENSE-lucide.txt").exists() and "cdn" not in _sprite.lower()
      and c.get("/static/icons/lucide.svg").status_code == 200)
_lnk2 = [l for p in j(c.get("/api/plugins")) for l in p.get("links", [])]
check("IC plugin buttons name an icon instead of an emoji",
      {(l["label"], l["icon"]) for l in _lnk2} >= {("Label", "tag"), ("Reprint label", "printer"),
                                                    ("Print label", "tag"), ("Look up product", "search")}, _lnk2)

# --- product lookup sources (PL) ------------------------------------------------
_lk = _bmod.lookup
_seen = []
def _stub(name):
    return lambda code, setting: _seen.append((name, code)) or (f"{name} answer" if name == "mouser" else None)
_lk.PROVIDERS = [_lk.Provider(pid, pid.title(), "test", acc, _stub(pid)) for pid, acc in
                 (("custom", "any"), ("mouser", "any"), ("open_products", "product"), ("open_food", "product"))]
c.post("/api/plugins/barcode_reader/settings", json={"use_mouser": True, "use_custom": False})
_pls = j(c.get("/api/plugins"))
_br = next(p for p in _pls if p["id"] == "barcode_reader")
check("PL lookup sources are separate settings; parts databases are off by default",
      {f["key"]: f["value"] for f in _br["settings"] if f["key"].startswith("use_")}
      == {"use_open_food": True, "use_open_products": True, "use_upcitemdb": True,
          "use_mouser": True, "use_digikey": False, "use_custom": False}, _br["settings"])
c.post("/api/plugins/barcode_reader/settings", json={"mouser_key": "secret-key-123"})
_pls = j(c.get("/api/plugins"))
_mk = next(f for f in next(p for p in _pls if p["id"] == "barcode_reader")["settings"] if f["key"] == "mouser_key")
check("PL an API key is stored but never sent back to the browser",
      _mk["value"] == "" and _mk["is_set"] is True and b"secret-key-123" not in c.get("/api/plugins").data, _mk)
_info_pl = j(c.get("/p/barcode_reader/info"))
check("PL the lookup page lists the sources switched on, in lookup order",
      [s["id"] for s in _info_pl["sources"]] == ["mouser", "open_products", "open_food"] and _info_pl["enabled"], _info_pl)
_seen.clear()
_lr = j(c.post("/p/barcode_reader/lookup", json={"code": "NE555P"}))
check("PL a part number goes to part sources only; retail-only sources are skipped",
      [r["status"] for r in _lr["results"]] == ["hit", "skipped", "skipped"]
      and _seen == [("mouser", "NE555P")] and _lr["product_code"] is False, _lr)
_seen.clear()
_lr = j(c.post("/p/barcode_reader/lookup", json={"code": "5000112637922"}))
check("PL a retail barcode goes to every source switched on",
      [s for s, _ in _seen] == ["mouser", "open_products", "open_food"] and _lr["product_code"] is True, _seen)
_seen.clear()
with app.app_context():
    _hit = _lk.ask(_lk.PROVIDERS[1], "NE555P", _XS("barcode_reader"), lambda k: None)
check("PL a hit is cached per code and source", _hit and _hit["name"] == "mouser answer" and _seen == [], (_hit, _seen))
_lk.PROVIDERS = [_lk.Provider("mouser", "Mouser", "parts", "any", _real_providers[1].fn)]
c.post("/api/plugins/barcode_reader/settings", json={"mouser_key": ""})
_lr = j(c.post("/p/barcode_reader/lookup", json={"code": "LM317T"}))
check("PL a source without its key is skipped with a reason, no request made",
      _lr["results"][0]["status"] == "skipped" and "key" in _lr["results"][0]["message"], _lr)
_lk.PROVIDERS = _real_providers
check("PL a dotted path finds the name in your own service's answer",
      _lk._dig({"items": [{"title": "Hex key set"}]}, "items.0.title") == "Hex key set"
      and _lk._dig({"items": []}, "items.0.title") is None)
check("PL part names read 'Maker PartNo — description'",
      _lk._part_name("Texas Instruments", "NE555P", "IC OSC SINGLE TIMER") == "Texas Instruments NE555P — IC OSC SINGLE TIMER")
_pb = io.BytesIO(); Image.new("RGB", (40, 40), (5, 5, 5)).save(_pb, "JPEG"); _pb.seek(0)
_pi = j(c.post("/api/items", data={"bin_id": _lb2, "label": "", "photo": (_pb, "p.jpg")},
               content_type="multipart/form-data"))["id"]
r = c.post("/p/barcode_reader/use", json={"item": _pi, "name": "Texas Instruments NE555P", "source": "Mouser"})
with app.app_context():
    _pit = _db.session.get(_Item, _pi)
    _pl_label, _pl_src = _pit.label, _pit.label_source
check("PL 'Use name' renames the item as the user would", r.status_code == 200
      and _pl_label == "Texas Instruments NE555P" and _pl_src == "user", (_pl_label, _pl_src))
c.post("/api/plugins/barcode_reader/settings", json={"lookup_enabled": False})
r = c.post("/p/barcode_reader/lookup", json={"code": "NE555P"})
check("PL with online lookup off, the page refuses and nothing is sent",
      r.status_code == 400 and "off" in j(r)["error"])
c.delete(f"/api/items/{_pi}")

with app.app_context():
    _left = _Code.query.filter(_Code.target_id.in_([_ca, _lb])).count()
c.delete(f"/api/items/{_ca}")
c.delete(f"/api/bins/{_lb}", json={"confirm": "delete", "contents": "checkout"})
with app.app_context():
    _gone = _Code.query.filter(_Code.target_id.in_([_ca, _lb])).count()
check("CD deleting an item or bin removes its codes", _left >= 3 and _gone == 0, (_left, _gone))
from binventory.services.storage import ExtensionStore as _XS
with app.app_context():
    _bstore = _XS("barcode_reader")
    _bstore.set_item(_cb, "codes", ["9780201379624 (EAN-13)"])
    _bstore.set("global", "", "codes_backfilled", False)
c.post("/api/plugins/barcode_reader", json={"enabled": False})
c.post("/api/plugins/barcode_reader", json={"enabled": True})
check("CD barcodes found in earlier photos are backfilled as item codes, once",
      "9780201379624" in [x["value"] for x in j(c.get(f"/api/codes?scope=item&id={_cb}"))]
      and j(c.get(f"/api/codes?scope=item&id={_cb}"))[-1]["source"] == "photo")
_cid = j(c.get(f"/api/codes?scope=item&id={_ce}"))[0]["id"]
check("CD a code can be removed", c.delete(f"/api/codes/{_cid}").status_code == 200
      and j(c.get(f"/api/codes?scope=item&id={_ce}")) == [])
for _x in (_cb, _cc, _cd, _ce):
    c.delete(f"/api/items/{_x}")
c.delete(f"/api/bins/{_lb2}", json={"confirm": "delete", "contents": "delete"})
for _p in _photos:
    (PROJECT / "static" / "uploads" / Path(_p).name.split("?")[0]).unlink(missing_ok=True)
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()

# Deleting an item deletes its plugin data.
for _x in (_b1, _b2, _b3, _b4, _b5, _b6):
    _ph = PROJECT / "static" / "uploads" / Path(j(c.get(f"/api/items/{_x}"))["photo_url"]).name.split("?")[0]
    c.delete(f"/api/items/{_x}")
    _ph.unlink(missing_ok=True)
with app.app_context():
    check("P7 item delete purges its plugin data",
          _ED.query.filter(_ED.scope_id.in_([_b1, _b2, _b3, _b4, _b5, _b6])).count() == 0)

ok = sum(1 for r in results if r[0])
for passed, name, detail in results:
    print(("PASS " if passed else "FAIL ") + name + ("" if passed else f"   -> {detail}"))
print(f"\n{ok}/{len(results)} passed")

sys.exit(0 if ok == len(results) else 1)
