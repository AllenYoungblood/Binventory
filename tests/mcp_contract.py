"""MCP server plugin — contract tests.

    python tests/mcp_contract.py

Builds a fresh, seeded database in a temp dir (never touches your library),
loads the real `plugins/mcp_server`, and drives it two ways:

* in-memory with the SDK's own Client — every tool, resource and prompt,
  permission and "Allow changes" refusals, name priority, elicitation;
* over real Streamable HTTP — localhost mode, the DNS-rebinding guard, LAN mode
  with and without the access token, and that disabling the plugin closes the
  port.

Skips (exit 0) if the optional `mcp` / `uvicorn` packages aren't installed.
WLED and the ML worker are stubbed. Exit code is non-zero if any check fails.
"""
import asyncio, atexit, io, os, shutil, socket, sys, tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
try:
    import mcp, uvicorn  # noqa: F401
except ImportError:
    print("SKIP: install the plugin's optional dependencies to run this:  pip install 'mcp>=2.3,<3' uvicorn")
    sys.exit(0)

PROJECT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="binventory-mcp-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
_UPLOADS = PROJECT / "static" / "uploads"
_THUMBS = _UPLOADS / "thumbs"
_before = set(os.listdir(_UPLOADS)), (set(os.listdir(_THUMBS)) if _THUMBS.exists() else set())
atexit.register(lambda: [(_UPLOADS / f).unlink(missing_ok=True)
                         for f in set(os.listdir(_UPLOADS)) - _before[0] if (_UPLOADS / f).is_file()])
atexit.register(lambda: [(_THUMBS / f).unlink(missing_ok=True)
                         for f in (set(os.listdir(_THUMBS)) if _THUMBS.exists() else set()) - _before[1]])
os.environ["BINVENTORY_DB_DIR"] = str(SCRATCH)
shutil.copytree(PROJECT / "plugins", SCRATCH / "plugins")
os.environ["BINVENTORY_PLUGIN_DIR"] = str(SCRATCH / "plugins")
sys.path.insert(0, str(PROJECT))

from PIL import Image
from binventory import config as _config, vocabulary as _vocab
shutil.copytree(PROJECT / "dictionaries", SCRATCH / "dictionaries")
_config.DICT_DIR = _vocab.DICT_DIR = SCRATCH / "dictionaries"
from binventory import events, lighting, ml, naming, plugins
from binventory.factory import create_app, init_database
from binventory.services import processing
from binventory.services.storage import ExtensionStore

sent = []
lighting._wled_send = lambda ip, payload, *a, **k: sent.append((ip, payload))
lighting._wled_get_info = lambda ip, *a, **k: {"ver": "16.0.1", "leds": {"count": 60}, "seg": [{"id": 0}]}
ml.ensure_worker = lambda: None
processing.ml.ensure_worker = lambda: None
# Never load (or download) CLIP here: auto/semantic modes fall back as if it failed to load.
_clip_loads = []
ml.ensure_clip_for_query = lambda: _clip_loads.append(1) or False

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

PORT = free_port()
app = create_app()
with app.app_context():
    from binventory.db import db
    db.create_all()
    ExtensionStore("mcp_server").settings_set("port", PORT)   # never collide with a real install
init_database(app)
c = app.test_client()
results = []
def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))
def j(r): return r.get_json()

# ── seed ───────────────────────────────────────────────────────────────────
shelf = j(c.post("/api/bins", json={"name": "Garage shelf", "location": "Garage, wall 2"}))["id"]
tote = j(c.post("/api/bins", json={"name": "Red tote"}))["id"]
c.post(f"/api/bins/{tote}/nest", json={"parent_id": shelf})
drawer = j(c.post("/api/bins", json={"name": "Small parts drawer"}))["id"]
def add(bin_id, label, color):
    b = io.BytesIO(); Image.new("RGB", (900, 600), color).save(b, "JPEG"); b.seek(0)
    return j(c.post("/api/items", data={"bin_id": bin_id, "label": label, "photo": (b, "p.jpg")},
                    content_type="multipart/form-data"))["id"]
screws = add(tote, "M3 screws", (120, 120, 120))
typed = add(drawer, "Zip ties", (10, 10, 10))
guessed = add(drawer, "", (200, 0, 0))
from binventory.db import db
from binventory.models import Item
with app.app_context():
    it = db.session.get(Item, guessed); it.label, it.label_source = "cable", "ml"; db.session.commit()
while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()

agent_events = []
for e in (events.ITEM_UPDATED, events.ITEM_CHECKED_OUT, events.ITEM_RESTOCKED):
    events.on(e, lambda event, **kw: agent_events.append((event, kw.get("actor"))))

r = c.post("/api/plugins/mcp_server", json={"enabled": True})
check("plugin enables (opt-in) and loads", j(r).get("status") == "loaded", j(r))
info = plugins.get("mcp_server")
api = info.api
mod = sys.modules["binventory_plugin_mcp_server"]
srv = sys.modules["binventory_plugin_mcp_server.server"]
card = {p["id"]: p for p in j(c.get("/api/plugins"))}["mcp_server"]
check("Settings card shows status, Claude Code command and permissions",
      card["status_rows"][0]["values"][0].startswith("Running") and
      any(rw["type"] == "code" and f"127.0.0.1:{PORT}/mcp" in rw["values"][0] for rw in card["status_rows"])
      and "network:listen" in card["permissions"], card["status_rows"][:3])

from mcp import Client
from mcp.types import ElicitResult

def text(res):
    return " ".join(getattr(x, "text", "") for x in res.content)


async def in_memory():
    server = srv.build(api)
    async with Client(server) as cl:
        tools = {t.name: t for t in (await cl.list_tools()).tools}
        check("tool surface: 19 intent-level tools, none delete",
              len(tools) == 19 and not any("delete" in n or "remove" in n for n in tools), sorted(tools))
        check("read tools are annotated read-only; writes are not; nothing destructive",
              tools["search_items"].annotations.read_only_hint and not tools["move_item"].annotations.read_only_hint
              and not any(t.annotations and t.annotations.destructive_hint for t in tools.values()))
        check("tools describe themselves for the model",
              all(t.description for t in tools.values())
              and "plain words" in str(tools["search_items"].input_schema))

        ov = (await cl.call_tool("inventory_overview", {})).structured_content
        check("inventory_overview counts", ov["items_on_shelves"] == 3 and ov["bins"] == 3
              and ov["named_by_visual_guess_only"] == 1, ov)

        res = (await cl.call_tool("search_items", {"query": "m3 screw"})).structured_content
        hit = res["results"][0] if res["results"] else {}
        check("search finds the item and says where it is (path outermost first + location)",
              res["mode"] == "keyword" and hit.get("id") == screws
              and hit.get("where") == "Garage shelf › Red tote (Garage, wall 2)", res)
        res = (await cl.call_tool("search_items", {"query": "screw", "bin": "Small parts drawer"})).structured_content
        check("search scoped to a bin by name", res["results"] == [], res)
        res = (await cl.call_tool("search_items", {"query": "screw", "bin": "Garage shelf"})).structured_content
        check("search scoped to a parent includes nested bins", [x["id"] for x in res["results"]] == [screws], res)
        check("auto mode tries semantic when keyword finds nothing", _clip_loads, _clip_loads)

        det = (await cl.call_tool("get_item", {"item_id": typed})).structured_content
        check("get_item detail", det["name"] == "Zip ties" and det["named_by"] == "Typed by you"
              and det["photo"].endswith("/photo"), det)
        pic = await cl.call_tool("view_item_photo", {"item_id": typed})
        check("view_item_photo returns the thumbnail image", pic.content[0].type == "image"
              and pic.content[0].mime_type == "image/jpeg", pic.content[0].type)
        r = await cl.call_tool("get_item", {"item_id": "nope"})
        check("unknown item -> readable tool error", r.is_error and "search_items" in text(r), text(r))

        bins_ = (await cl.call_tool("list_bins", {})).structured_content["result"]
        check("list_bins sorted by path", [b["where"] for b in bins_][:2] == ["Garage shelf (Garage, wall 2)",
              "Garage shelf › Red tote (Garage, wall 2)"], [b["where"] for b in bins_])
        gb = (await cl.call_tool("get_bin", {"bin": "garage shelf"})).structured_content
        check("get_bin by case-insensitive name shows nested bins and their items",
              gb["nested"][0]["name"] == "Red tote" and [i["id"] for i in gb["items"]] == [screws], gb)
        r = await cl.call_tool("get_bin", {"bin": "drawr"})
        check("unknown bin name -> suggests close names", r.is_error and "list_bins" in text(r), text(r))
        r = await cl.call_tool("get_bin", {"bin": "drawer"})
        check("partial bin name -> 'did you mean'", r.is_error and "Small parts drawer" in text(r), text(r))

        # Renames and name priority.
        r = await cl.call_tool("rename_item", {"item_id": guessed, "name": "USB-C cable"})
        check("rename over a visual guess lands as 'AI assistant'",
              not r.is_error and r.structured_content["item"]["named_by"] == "AI assistant", text(r))
        r = await cl.call_tool("rename_item", {"item_id": typed, "name": "Cable ties"})
        check("rename can't overwrite a typed name by default — and says why",
              r.is_error and "Name priority" in text(r), text(r))
        with app.app_context():
            naming.set_order(["ml", "ocr", "scanned", "ocr_region", "user", "agent"])
        r = await cl.call_tool("rename_item", {"item_id": typed, "name": "Cable ties"})
        check("...but can once the user ranks the assistant above typed names", not r.is_error, text(r))
        with app.app_context():
            naming.reset_order()

        r = await cl.call_tool("set_item_tags", {"item_id": guessed,
                                                 "tags": ["usb-c", " USB-C ", "charging, cable", ""]})
        check("set_item_tags replaces the list, cleaned and de-duplicated",
              not r.is_error and r.structured_content["item"]["tags"] == ["usb-c", "charging cable"], text(r))
        res = (await cl.call_tool("search_items", {"query": "charging cable", "mode": "keyword"})).structured_content
        check("...and the new tags are searchable", [x["id"] for x in res["results"]] == [guessed], res)
        r = await cl.call_tool("set_item_tags", {"item_id": guessed, "tags": [f"t{n}" for n in range(31)]})
        check("set_item_tags rejects more than 30 tags", r.is_error, text(r))

        r = await cl.call_tool("update_item", {"item_id": screws, "quantity": 40, "notes": "box of 100"})
        check("update_item notes + quantity", r.structured_content["item"]["quantity"] == 40, text(r))
        r = await cl.call_tool("move_item", {"item_id": screws, "bin": "Small parts drawer"})
        check("move_item by bin name", "Small parts drawer" in r.structured_content["item"]["where"], text(r))

        r = await cl.call_tool("checkout_item", {"item_id": screws})
        co = (await cl.call_tool("list_checked_out", {})).structured_content["result"]
        check("checkout -> listed with its home", co and co[0]["home"] == "Small parts drawer", co)
        await cl.call_tool("return_item", {"item_id": screws})
        await cl.call_tool("consume_item", {"item_id": screws})
        sl = (await cl.call_tool("list_shopping_list", {})).structured_content["result"]
        check("consume -> on the shopping list", [x["id"] for x in sl] == [screws], sl)
        r = await cl.call_tool("restock_item", {"item_id": screws})
        check("restock with its bin still there needs no question", not r.is_error
              and r.structured_content["item"]["status"] == "active", text(r))

        b = (await cl.call_tool("create_bin", {"name": "New drawer unit", "inside": "Garage shelf",
                                              "location": "Garage, wall 2"})).structured_content
        check("create_bin nested by name", b["where"].startswith("Garage shelf › New drawer unit"), b)

        res = (await cl.call_tool("process_items", {})).structured_content
        check("process_items queues pending work", res["ok"] and "Queued" in res["message"], res)
        while not ml.tag_queue.empty(): ml.tag_queue.get(); ml.tag_queue.task_done()

        r = await cl.call_tool("light_bin", {"bin": "Red tote"})
        check("light_bin without lights -> clear error", r.is_error and "Couldn't light" in text(r), text(r))
        dev = j(c.post("/api/wled/devices", json={"name": "Shelf strip", "ip": "10.9.9.9"}))["id"]
        c.put(f"/api/bins/{drawer}/lighting", json={"device_id": dev, "enabled": True, "seg_start": 4, "seg_len": 3})
        sent.clear()
        r = await cl.call_tool("light_bin", {"item_id": screws})
        check("light_bin by item lights its bin", not r.is_error and sent and sent[-1][0] == "10.9.9.9", (text(r), sent))

        # Restock when the home bin is gone: elicitation asks which bin.
        await cl.call_tool("consume_item", {"item_id": typed})
        c.delete(f"/api/bins/{drawer}", json={"confirm": "delete", "contents": "checkout"})
    asked = []
    async def choose(ctx, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={"bin": "New drawer unit"})
    async with Client(server, elicitation_callback=choose) as cl:
        r = await cl.call_tool("restock_item", {"item_id": typed})
        check("restock with its bin deleted asks which bin (elicitation), then restocks",
              asked and "no longer exists" in asked[0] and not r.is_error
              and "New drawer unit" in r.structured_content["item"]["where"], (asked, text(r)))
        await cl.call_tool("consume_item", {"item_id": typed})
    async def decline(ctx, params):
        return ElicitResult(action="decline")
    with app.app_context():
        it = db.session.get(Item, typed); it.origin_bin_id = ""; db.session.commit()
    async with Client(server, elicitation_callback=decline) as cl:
        r = await cl.call_tool("restock_item", {"item_id": typed})
        check("declining the question leaves it on the shopping list",
              r.structured_content["ok"] is False and r.structured_content["item"] is None, text(r))

        # Allow changes off.
        c.post("/api/plugins/mcp_server/settings", json={"allow_changes": False})
        r = await cl.call_tool("rename_item", {"item_id": guessed, "name": "Lead"})
        r2 = await cl.call_tool("search_items", {"query": "cable"})
        check("Allow changes off: writes refused with where to switch it on, reads still work",
              r.is_error and "Allow changes" in text(r) and not r2.is_error, text(r))
        c.post("/api/plugins/mcp_server/settings", json={"allow_changes": True})

        res = await cl.read_resource("binventory://bins")
        check("bins resource", "Garage shelf" in res.contents[0].text)
        res = await cl.read_resource(f"binventory://items/{guessed}/photo")
        check("item photo resource is a JPEG", res.contents[0].mime_type == "image/jpeg")
        prompts = [p.name for p in (await cl.list_prompts()).prompts]
        check("prompts offered", sorted(prompts) == ["find", "tidy_names"], prompts)

    check("agent writes carry actor='agent' on core events",
          ("item.updated", "agent") in agent_events and ("item.checked_out", "agent") in agent_events, agent_events[:6])
    with app.app_context():
        log = api.store.get("global", "", "activity", [])
    check("activity log records what the assistant changed",
          any("USB-C cable" in a["text"] for a in log) and any(a["text"].startswith("Moved") for a in log), log[:4])


async def over_http():
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    url = f"http://127.0.0.1:{PORT}/mcp"
    async with Client(url) as cl:
        r = await cl.call_tool("search_items", {"query": "cable"})
        check("HTTP (localhost mode): tool call works, protocol 2026-07-28",
              not r.is_error and cl.protocol_version == "2026-07-28", cl.protocol_version)
    r = httpx2.post(url, headers={"Host": "evil.example:80"}, json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    check("DNS-rebinding guard rejects a foreign Host header", r.status_code in (400, 403, 421), r.status_code)

    # LAN mode needs a token first.
    c.post("/api/plugins/mcp_server/settings", json={"lan_access": True})
    st = {p["id"]: p for p in j(c.get("/api/plugins"))}["mcp_server"]["status_rows"][0]["values"][0]
    check("LAN mode without a token refuses to start and says what to do", "access token" in st, st)
    r = j(c.post("/api/plugins/mcp_server/actions/create_token", json={}))
    token = r["reveal"]["value"]
    with app.app_context():
        stored = api.store.settings_get("token")
    check("token revealed once, stored only as a hash", token.startswith("binv_") and token not in str(stored)
          and stored["hint"] == token[-4:], stored)
    scheme = "https" if mod._state["tls"] else "http"
    lan_url = f"{scheme}://127.0.0.1:{PORT}/mcp"
    r = httpx2.post(lan_url, verify=False, json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    check(f"LAN mode ({scheme}): no token -> 401", r.status_code == 401, r.status_code)
    try:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, verify=False) as h:
            async with Client(streamable_http_client(lan_url, http_client=h)) as cl:
                r = await cl.call_tool("inventory_overview", {})
                ok = not r.is_error
    except Exception as e:
        ok = f"{type(e).__name__}: {e}"
    check("LAN mode: valid token -> tools work", ok is True, ok)
    c.post("/api/plugins/mcp_server/actions/revoke_token", json={})
    st = {p["id"]: p for p in j(c.get("/api/plugins"))}["mcp_server"]["status_rows"][0]["values"][0]
    check("revoking the token stops LAN access", "access token" in st, st)
    c.post("/api/plugins/mcp_server/settings", json={"lan_access": False})

    c.post("/api/plugins/mcp_server", json={"enabled": False})
    try:
        httpx2.post(url, json={}, timeout=2)
        closed = False
    except Exception:
        closed = True
    check("disabling the plugin closes the port", closed)


asyncio.run(in_memory())
asyncio.run(over_http())

ok = sum(1 for r in results if r[0])
for passed, name, detail in results:
    print(("PASS " if passed else "FAIL ") + name + ("" if passed else f"   -> {detail}"))
print(f"\n{ok}/{len(results)} passed")
sys.exit(0 if ok == len(results) else 1)
