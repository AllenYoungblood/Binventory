"""Product-name lookup for barcodes and part numbers — optional, and online.

Off unless the user switches it on. Only the code is sent, never a photo.

Each provider is a separate setting, so a parts bin can use Mouser or
Digi-Key while a pantry uses Open Food Facts. A provider says what it can
answer for:

* "product" — retail barcodes (EAN/UPC) only;
* "any"     — any code text, e.g. a manufacturer or distributor part number.

Enabled providers are tried in PROVIDERS order, specialists first, and the
first hit wins. Each answer is cached per code *and provider* in the plugin's
store, so switching a provider on later isn't blocked by another provider's
cached miss. Misses are retried after a week; network errors aren't cached.
"""
from datetime import datetime, timedelta

import requests

TIMEOUT = 6
MISS_RETRY = timedelta(days=7)
HEADERS = {"User-Agent": "BINventory/1.0 (self-hosted home inventory)"}


class NotConfigured(Exception):
    """The provider is switched on but its key or URL isn't set."""


def _join(brand, title):
    """'Zippo' + 'Genuine Wick' -> 'Zippo Genuine Wick', without doubling."""
    brand, title = (brand or "").strip(), (title or "").strip()
    if not title:
        return None
    if brand and brand.lower() not in title.lower():
        return f"{brand} {title}"
    return title


def _part_name(manufacturer, mpn, description):
    """'Texas Instruments' + 'NE555P' + 'IC OSC SINGLE TIMER' ->
    'Texas Instruments NE555P — IC OSC SINGLE TIMER'."""
    head = _join(manufacturer, mpn) or (manufacturer or "").strip()
    desc = (description or "").strip()
    if head and desc:
        return f"{head} — {desc}"
    return head or desc or None


# ── Providers ────────────────────────────────────────────────────────────────

def _open_facts(host):
    """Open Food Facts and its sister databases share one API."""
    def look(code, setting):
        r = requests.get(f"https://{host}/api/v2/product/{code}.json",
                         params={"fields": "product_name,brands"}, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        j = r.json()
        if j.get("status") != 1:
            return None
        p = j.get("product") or {}
        return _join((p.get("brands") or "").split(",")[0], p.get("product_name"))
    return look


def upcitemdb(code, setting):
    r = requests.get("https://api.upcitemdb.com/prod/trial/lookup",
                     params={"upc": code}, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    items = r.json().get("items") or []
    return _join(items[0].get("brand"), items[0].get("title")) if items else None


def mouser(code, setting):
    key = (setting("mouser_key") or "").strip()
    if not key:
        raise NotConfigured("add a Mouser Search API key")
    r = requests.post("https://api.mouser.com/api/v1/search/partnumber", params={"apiKey": key},
                      json={"SearchByPartRequest": {"mouserPartNumber": code, "partSearchOptions": "Exact"}},
                      headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("Errors"):
        raise RuntimeError(j["Errors"][0].get("Message") or "Mouser returned an error")
    parts = (j.get("SearchResults") or {}).get("Parts") or []
    if not parts:
        return None
    p = parts[0]
    return _part_name(p.get("Manufacturer"), p.get("ManufacturerPartNumber"), p.get("Description"))


_digikey_token = {"value": None, "until": datetime.min, "client": None}


def _digikey_auth(cid, secret):
    t = _digikey_token
    if t["value"] and t["client"] == cid and datetime.utcnow() < t["until"]:
        return t["value"]
    r = requests.post("https://api.digikey.com/v1/oauth2/token", headers=HEADERS, timeout=TIMEOUT,
                      data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
    r.raise_for_status()
    j = r.json()
    t.update(value=j["access_token"], client=cid,
             until=datetime.utcnow() + timedelta(seconds=int(j.get("expires_in", 600)) - 60))
    return t["value"]


def digikey(code, setting):
    cid, secret = (setting("digikey_client_id") or "").strip(), (setting("digikey_client_secret") or "").strip()
    if not (cid and secret):
        raise NotConfigured("add a Digi-Key client ID and secret")
    token = _digikey_auth(cid, secret)
    r = requests.get(f"https://api.digikey.com/products/v4/search/{requests.utils.quote(code, safe='')}/productdetails",
                     headers={**HEADERS, "X-DIGIKEY-Client-Id": cid, "Authorization": f"Bearer {token}"},
                     timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    p = r.json().get("Product") or {}
    return _part_name((p.get("Manufacturer") or {}).get("Name"), p.get("ManufacturerProductNumber"),
                      (p.get("Description") or {}).get("ProductDescription"))


def _dig(data, path):
    """'items.0.title' -> data["items"][0]["title"], or None."""
    for part in [x for x in (path or "").split(".") if x]:
        if isinstance(data, list):
            data = data[int(part)] if part.isdigit() and int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data if isinstance(data, (str, int, float)) else None


def custom(code, setting):
    """Your own service: a URL with {code} in it, answering JSON (name at a
    dotted path) or plain text (the whole body is the name)."""
    url = (setting("custom_url") or "").strip()
    if "{code}" not in url:
        raise NotConfigured("set a lookup URL containing {code}")
    r = requests.get(url.replace("{code}", requests.utils.quote(code, safe="")), headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    path = (setting("custom_field") or "").strip()
    if path:
        value = _dig(r.json(), path)
        return str(value).strip() if value not in (None, "") else None
    text = r.text.strip()
    return text[:200] if text and "<" not in text[:1] else None


class Provider:
    def __init__(self, pid, name, what, accepts, fn):
        self.id, self.name, self.what, self.accepts, self.fn = pid, name, what, accepts, fn

    def takes(self, product_code):
        return self.accepts == "any" or product_code


# Specialists first: a parts database answers only for its own part numbers,
# so trying it before the general ones costs a miss, never a wrong name.
PROVIDERS = [
    Provider("custom", "Your own lookup URL", "anything your service knows", "any", custom),
    Provider("mouser", "Mouser", "electronic parts, by part number", "any", mouser),
    Provider("digikey", "Digi-Key", "electronic parts, by part number", "any", digikey),
    Provider("open_products", "Open Products Facts", "household and general goods", "product",
             _open_facts("world.openproductsfacts.org")),
    Provider("upcitemdb", "UPCitemdb", "general merchandise and tools (100 lookups a day)", "product", upcitemdb),
    Provider("open_food", "Open Food Facts", "food and drink", "product", _open_facts("world.openfoodfacts.org")),
]
BY_ID = {p.id: p for p in PROVIDERS}


def enabled(setting):
    """Providers switched on in settings, in lookup order."""
    return [p for p in PROVIDERS if setting(f"use_{p.id}")]


def _now():
    return datetime.utcnow().isoformat()


def ask(provider, code, store, setting, log=print, fresh=False):
    """One provider: {"name", "source"} on a hit, None on a miss. Raises on
    errors (NotConfigured, network) — callers decide whether to show them."""
    key = f"lookup:{provider.id}"
    cached = store.get("code", code, key)
    if cached and not fresh:
        if cached.get("name"):
            return cached
        if datetime.utcnow() - datetime.fromisoformat(cached["at"]) < MISS_RETRY:
            return None
    name = provider.fn(code, setting)
    if name:
        hit = {"name": name[:200], "source": provider.name, "at": _now()}
        store.set("code", code, key, hit)
        return hit
    store.set("code", code, key, {"name": None, "at": _now()})
    return None


def lookup(code, store, setting, product_code=True, log=print):
    """First hit across the enabled providers that take this kind of code,
    or None. Errors are logged and skipped."""
    for p in enabled(setting):
        if not p.takes(product_code):
            continue
        try:
            hit = ask(p, code, store, setting, log)
        except NotConfigured as e:
            log(f"lookup via {p.name} skipped: {e}")
            continue
        except Exception as e:
            log(f"lookup {code} via {p.name} failed: {e}")
            continue
        if hit:
            return hit
    return None


def lookup_all(code, store, setting, product_code=True, log=print):
    """Every enabled provider's answer, for the lookup page: [{provider,
    what, status: hit|miss|skipped|error, name?, message?}]. Fresh: a person
    asked, so cached misses are tried again."""
    out = []
    for p in enabled(setting):
        row = {"provider": p.name, "id": p.id, "what": p.what}
        if not p.takes(product_code):
            out.append(dict(row, status="skipped", message="only looks up retail barcodes (EAN/UPC)"))
            continue
        try:
            hit = ask(p, code, store, setting, log, fresh=True)
            out.append(dict(row, status="hit", name=hit["name"]) if hit else dict(row, status="miss"))
        except NotConfigured as e:
            out.append(dict(row, status="skipped", message=str(e)))
        except Exception as e:
            log(f"lookup {code} via {p.name} failed: {e}")
            out.append(dict(row, status="error", message=str(e)[:200]))
    return out
