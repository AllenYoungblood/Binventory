"""Finding items: keyword, semantic (CLIP text→image) and photo (image→image).

Results are lists of (item, score) — score is None for keyword matches.

Modes for `text()`:
    "keyword"   word match over name, tags, notes, OCR text, ranked so name
                matches lead; no model
    "semantic"  CLIP; loads the model if it isn't resident (~20 s the first time)
    "warm"      semantic if CLIP is already loaded, else keyword — what the
                app's search box does unless ✨ is pressed
    "auto"      "warm", and if that finds nothing by keyword, load CLIP and try
                semantic — for callers that can wait (the AI assistant)
"""
import re

import numpy as np

from .. import ml
from ..models import Item
from . import embeddings_store as emb_store
from . import settings as settings_svc
from .errors import Failed, Invalid, Unavailable

MODES = ("keyword", "semantic", "warm", "auto")


def _candidates(bin_ids=None):
    # Consumed items aren't in the physical inventory; checked-out ones are
    # still worth finding.
    q = Item.query.filter(Item.status != "consumed")
    if bin_ids:
        q = q.filter(Item.bin_id.in_(list(bin_ids)))
    return q.all()


# Where a word is found matters more than whether it is: the name is what the
# user is looking for, OCR text is noisy. Weights per field…
_FIELD_WEIGHT = (("label", 8), ("tags", 3), ("notes", 3), ("ocr_text", 1))
# …times how well the word matched: a whole word, the start of a word (typing
# "zig" for "zigbee"), or merely inside one ("ram" inside "macrame").
_WHOLE, _PREFIX, _INSIDE = 3, 2, 0.25     # inside a name (2) < a whole word in OCR (3)
_WORD = re.compile(r"[\w#+.-]+")


def _match(word, text):
    best = 0
    for w in _WORD.findall(text):
        w = w.strip(".-")
        if w == word:
            return _WHOLE
        if w.startswith(word):
            best = max(best, _PREFIX)
        elif word in w:
            best = max(best, _INSIDE)
    if not best and word in text:          # spans punctuation, e.g. "m2-m4"
        best = _INSIDE
    return best


def _keyword_score(query, words, item):
    """0 means no match. Every query word must be found somewhere, unless the
    query as a whole appears verbatim (the old substring behaviour)."""
    fields = [(getattr(item, f) or "").lower() for f, _ in _FIELD_WEIGHT]
    score = 0
    for word in words:
        best = max(_match(word, text) * weight for text, (_, weight) in zip(fields, _FIELD_WEIGHT))
        if not best:
            return 1 if any(query in t for t in fields) else 0
        score += best
    label = fields[0]
    if label == query:
        score += 50                        # the exact name
    elif label.startswith(query):
        score += 10
    elif len(words) > 1 and query in label:
        score += 5                         # the words in order
    return score


def _keyword(query, items, limit):
    """Items matching every word of `query`, best first: name matches lead,
    then tags and notes, then text read from the photo."""
    ql = " ".join(query.lower().split())
    words = [w.strip(".-") for w in _WORD.findall(ql)] or [ql]
    scored = [(s, item) for item in items if (s := _keyword_score(ql, words, item))]
    scored.sort(key=lambda p: (-p[0], len(p[1].label or ""), (p[1].label or "").lower()))
    return [(item, None) for _, item in scored[:limit]]


def _strong_name_hit(query, item):
    words = [w.strip(".-") for w in _WORD.findall(query.lower())]
    label = (item.label or "").lower()
    return bool(words) and all(_match(w, label) >= _PREFIX for w in words)


def _ranked(mat, items, qvec, threshold, limit):
    """Cosine scores in one vectorised pass, best first, cut at threshold."""
    scores = mat @ qvec
    out = []
    for idx in np.argsort(-scores):
        s = float(scores[idx])
        if s < threshold:
            break               # sorted, so everything after is worse too
        out.append((items[idx], round(s, 4)))
        if len(out) >= limit:
            break
    return out


def _semantic(query, items, limit):
    qemb = np.asarray(ml.embed_text(query), dtype=np.float32)
    qemb /= (np.linalg.norm(qemb) + 1e-9)
    mat, tagged = emb_store.build_matrix(items)
    if not tagged:
        return []
    threshold = settings_svc.get_settings().text_threshold or 0.18   # C5
    return _ranked(mat, tagged, qemb, threshold, limit)


def text(query, bin_ids=None, mode="warm", limit=50):
    """Returns (mode_used, [(item, score), ...]).

    An item carrying the query as one of its codes (a barcode typed or
    scanned into the search box) always comes first."""
    query = (query or "").strip()
    if mode not in MODES:
        raise Invalid(f"mode must be one of {', '.join(MODES)}")
    if not query:
        return "none", []
    items = _candidates(bin_ids)
    used, found = _text(query, items, mode, limit)
    coded = _code_hits(query, items)
    if coded:
        ids = {i.id for i in coded}
        found = [(i, None) for i in coded] + [(i, s) for i, s in found if i.id not in ids]
    return used, found[:limit]


def _code_hits(query, items):
    if len(query) > 200 or " " in query:
        return []
    from . import codes as codes_svc
    allowed = {i.id: i for i in items}
    ids = dict.fromkeys(c.target_id for c in codes_svc.lookup(query)
                        if c.scope == "item" and c.target_id in allowed)
    return [allowed[i] for i in ids]


def _text(query, items, mode, limit):

    if mode == "keyword":
        return "keyword", _keyword(query, items, limit)
    if mode == "semantic" and not ml.clip_ready():
        ml.ensure_clip_for_query()
    elif ml.clip_ready():
        ml.touch_models()             # extend the keep-alive window

    if ml.clip_ready():
        return "semantic", _with_keyword_hits(_semantic(query, items, limit), query, items, limit)
    found = _keyword(query, items, limit)
    if mode == "auto" and not found and ml.ensure_clip_for_query():
        return "semantic", _semantic(query, items, limit)
    return "keyword", found


def _with_keyword_hits(semantic, query, items, limit):
    """Semantic results merged with keyword matches.

    Items whose *name* contains every word of the query come first: someone
    typing "ram" wants the RAM stick before whatever CLIP thinks looks like
    one. Then the semantic results, then the remaining keyword matches.
    Without the keyword half, a just-added, not-yet-analysed item (no
    embedding yet) was unfindable — even by its exact name — whenever CLIP
    happened to be warm. Keyword hits carry no score.
    """
    keyword = _keyword(query, items, limit)
    named = [(i, s) for i, s in keyword if _strong_name_hit(query, i)]
    first = {i.id for i, _ in named}
    sem = [(i, s) for i, s in semantic if i.id not in first]
    seen = first | {i.id for i, _ in sem}
    rest = [(i, s) for i, s in keyword if i.id not in seen]
    return (named + sem + rest)[:limit]


def photo(image, threshold=None, limit=20):
    """Items that look like `image`. Returns (results, compared, threshold).

    Always needs CLIP (there's no keyword fallback for a picture). Image-to-
    image scores run much higher than text-to-image, hence the stricter default.
    """
    from PIL import Image as _Image
    image.thumbnail((640, 640), _Image.LANCZOS)   # CLIP works at 224 px
    if not ml.ensure_clip_for_query():
        raise Unavailable("Could not load the ML model")
    qemb = ml.embed_image(image)
    if not qemb:
        raise Failed("Could not analyze the photo")
    if threshold is None:
        threshold = settings_svc.get_settings().image_threshold or 0.55
    q = np.asarray(qemb, dtype=np.float32)
    q /= (np.linalg.norm(q) + 1e-9)
    mat, keep = emb_store.build_matrix(_candidates())
    if not keep:
        return [], 0, threshold
    return _ranked(mat, keep, q, threshold, limit), len(keep), threshold
