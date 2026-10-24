"""CLIP embeddings and the cached label-embedding matrix.

WHY THE CACHE MATTERS: `top_labels()` originally re-encoded every dictionary
term through CLIP's text encoder for EVERY photo. Text encoding is a full
transformer pass per term, so with thousands of terms that was ~99% of tagging
time, repeated per photo. Encoding once into a cached matrix reduced the
per-photo cost to a single matrix multiply — about 0.25 ms at 2,300 terms.

The cache is keyed by a hash of the vocabulary, so editing the dictionary
invalidates it automatically, and is persisted to disk so restarts skip the
encode entirely.
"""
import json
import threading

from .. import vocabulary
from ..config import DB_DIR
from .runtime import get_clip

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

# ── Cached label embeddings ───────────────────────────────────────────────────
# The label vocabulary never changes while the server runs, so its CLIP text
# embeddings are computed ONCE and reused for every photo.
#
# WHY THIS MATTERS ENORMOUSLY: encoding text is a full transformer forward pass
# per label. Without caching, tagging one photo meant 1 image encode + N text
# encodes (N = every word in the dictionary). With ~2,300 labels that made the
# text side ~99% of the work, and it repeated for every single photo.
#
# With the cache, per-photo cost becomes: 1 image encode + one (1 x 512) @
# (512 x N) matrix multiply. That matmul is microseconds even at N = 20,000,
# so the dictionary can grow almost without limit at no per-photo cost. The
# only price is a one-time encode when the model loads (batched below).
_label_emb        = None   # torch tensor, shape (N, 512), L2-normalised
_label_emb_labels = None   # the exact label list the cache was built from
_label_emb_lock   = threading.Lock()


def _label_cache_path(labels):
    """Path for the on-disk label-embedding cache.

    The filename embeds a hash of the exact vocabulary, so changing, adding or
    removing any label produces a different filename and the stale cache is
    simply ignored (no manual invalidation needed).
    """
    import hashlib
    h = hashlib.sha256("\u0000".join(labels).encode("utf-8")).hexdigest()[:16]
    return DB_DIR / "label_cache" / f"labels_{h}.pt"

def _get_label_embeddings():
    """Return cached (embeddings, labels), building them on first use.

    Rebuilds automatically if vocabulary.CANDIDATE_LABELS changed (e.g. user-taught words
    were added at runtime) or if CLIP was unloaded and reloaded.
    """
    global _label_emb, _label_emb_labels
    import torch
    model, _, tokenizer = get_clip()
    if model is None:
        return None, None

    with _label_emb_lock:
        # Cache is valid only if it was built from the current vocabulary.
        if _label_emb is not None and _label_emb_labels == vocabulary.CANDIDATE_LABELS:
            return _label_emb, _label_emb_labels

        labels = list(vocabulary.CANDIDATE_LABELS)

        # Try the on-disk cache first. Keyed by a hash of the vocabulary, so
        # editing the dictionary automatically invalidates it. This turns the
        # one-time encode into a near-instant file read on every later startup.
        cache_file = _label_cache_path(labels)
        if cache_file.exists():
            try:
                data = torch.load(cache_file, map_location="cpu")
                if data.shape[0] == len(labels):
                    _label_emb        = data
                    _label_emb_labels = labels
                    print(f"[CLIP] Loaded {len(labels)} label embeddings from cache ✓")
                    return _label_emb, _label_emb_labels
            except Exception as e:
                print(f"[CLIP] Label cache unreadable ({e}) — rebuilding")

        print(f"[CLIP] Encoding {len(labels)} label embeddings (one time) …")
        feats = []
        # Batch to keep peak memory sane on large vocabularies.
        BATCH = 256
        with torch.no_grad():
            for i in range(0, len(labels), BATCH):
                chunk  = labels[i:i + BATCH]
                tokens = tokenizer(chunk)
                f      = model.encode_text(tokens)
                f      = f / f.norm(dim=-1, keepdim=True)
                feats.append(f)
            _label_emb = torch.cat(feats, dim=0)
        _label_emb_labels = labels
        # Persist so future startups skip the encode entirely.
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            torch.save(_label_emb, cache_file)
            print(f"[CLIP] Label embeddings cached to disk ✓")
        except Exception as e:
            print(f"[CLIP] Could not write label cache ({e}) — continuing")
        return _label_emb, _label_emb_labels

def invalidate_label_cache():
    """Drop the cached label embeddings (called when CLIP unloads)."""
    global _label_emb, _label_emb_labels
    with _label_emb_lock:
        _label_emb = None
        _label_emb_labels = None

def top_labels(pil_image, top_k=8):
    """Score vocabulary.CANDIDATE_LABELS against an image, return the top-k label strings.

    Uses the cached label embedding matrix, so cost per photo is one image
    encode plus a single matrix multiply — independent of dictionary size.
    """
    import torch
    model, preprocess, _ = get_clip()
    if model is None:
        return []
    text_feat, labels = _get_label_embeddings()
    if text_feat is None:
        return []
    with torch.no_grad():
        img_tensor = preprocess(pil_image).unsqueeze(0)
        img_feat   = model.encode_image(img_tensor)
        img_feat   = img_feat / img_feat.norm(dim=-1, keepdim=True)
        scores     = (img_feat @ text_feat.T)[0]
        k          = min(top_k, len(labels))
        top_idx    = scores.topk(k).indices.tolist()
    return [labels[i] for i in top_idx]
