"""Vocabulary and dictionary endpoints."""
import base64
import re
import io
import json

from flask import Blueprint, Response, jsonify, request, send_from_directory
from PIL import Image

from .. import auth, config, events, images, lighting, ml, scheduler, vocabulary
from ..db import db
from ..models import AppSettings, Bin, Item, LearnedLabel, WLEDSettings
from ..services import catalog
from ..services import settings as settings_svc

bp = Blueprint("vocab", __name__)


@bp.route("/api/dictionaries", methods=["GET"])
@auth.require("vocab:read")
def list_dictionaries():
    """Report every dictionary file, its term count, and whether it's enabled."""
    return jsonify({
        "dir":   str(config.DICT_DIR),
        "files": vocabulary.dictionary_status(),
        "total": len(vocabulary.BASE_CANDIDATE_LABELS),
        "active": len(vocabulary.CANDIDATE_LABELS),
    })

@bp.route("/api/dictionaries/reload", methods=["POST"])
@auth.require("vocab:write")
def reload_dictionaries():
    """Re-read the dictionaries folder without restarting the server.

    Rebuilds the base vocabulary, re-layers learned words, and drops the cached
    label embeddings so they're recomputed against the new word list.
    """
    # vocabulary owns its module state; ask it to reload rather than reaching in.
    n = vocabulary.reload_from_disk()
    ml.invalidate_label_cache()
    return jsonify({"ok": True, "base": len(vocabulary.BASE_CANDIDATE_LABELS), "active": n})

# ── Word frequency analysis (human-in-the-loop vocabulary building) ───────────
# Rather than guessing which words belong in the dictionary, mine them from the
# text already in your library: names you typed and text OCR read off labels.
# Words are counted across items, ranked, and presented for manual review.

_STOPWORDS = {
    "the","a","an","and","or","of","for","with","to","in","on","at","by","from",
    "is","are","was","were","be","this","that","it","its","as","new","used",
    "pcs","pcs.","qty","set","kit","pack","x","mm","cm","in.","inch","inches",
}

@bp.route("/api/vocab/analyze", methods=["GET"])
@auth.require("vocab:read")
def analyze_vocab():
    """Count word frequencies across chosen text sources.

    Query params:
      source = "manual" | "ocr" | "both"   (default both)
      min    = minimum occurrences to pre-select (default 2)

    Returns every word with its count and how many distinct items it appeared
    on, sorted most-frequent first, flagged with whether it's already known.
    """
    source  = request.args.get("source", "both")
    min_use = max(1, int(request.args.get("min", 2)))

    items = Item.query.all()
    counts, item_hits = {}, {}
    for it in items:
        texts = []
        # "manual" = names a human typed (label_source user), plus notes.
        if source in ("manual", "both"):
            if (it.label_source or "") == "user" and it.label:
                texts.append(it.label)
            if it.notes:
                texts.append(it.notes)
        if source in ("ocr", "both") and it.ocr_text:
            texts.append(it.ocr_text)

        seen_here = set()
        for t in texts:
            for w in vocabulary._tokenize_for_vocab(t):
                counts[w] = counts.get(w, 0) + 1
                seen_here.add(w)
        for w in seen_here:
            item_hits[w] = item_hits.get(w, 0) + 1

    known = {l.lower() for l in vocabulary.CANDIDATE_LABELS}
    words = [{
        "word":     w,
        "count":    n,
        "items":    item_hits.get(w, 0),
        "known":    w in known,
        "selected": (n >= min_use) and (w not in known),
    } for w, n in counts.items()]
    words.sort(key=lambda x: (-x["count"], x["word"]))

    return jsonify({
        "source":     source,
        "min":        min_use,
        "scanned":    len(items),
        "unique":     len(words),
        "preselected": sum(1 for w in words if w["selected"]),
        "words":      words,
    })

@bp.route("/api/vocab/add-words", methods=["POST"])
@auth.require("vocab:write")
def add_vocab_words():
    """Append reviewed words to a dictionary file, then hot-reload.

    Body: {"words": [...], "file": "custom_list.txt"}  (file optional)
    Writing to a real file (rather than the learned-words table) means the
    additions are portable, editable by hand, and survive a database reset.
    """
    data  = request.json or {}
    words = [w.strip() for w in data.get("words", []) if w and w.strip()]
    # All user-added terms go to custom_list (backlog C3); created if absent.
    fname = data.get("file", "custom_list.txt")
    if not fname.endswith(".txt"):
        fname += ".txt"
    if "/" in fname or "\\" in fname:
        return jsonify({"error": "Invalid filename"}), 400
    if not words:
        return jsonify({"error": "No words supplied"}), 400

    added = vocabulary.add_terms(words, fname)

    # Hot-reload so the new words take effect immediately.
    # vocabulary owns its module state; ask it to reload rather than reaching in.
    n = vocabulary.reload_from_disk()
    ml.invalidate_label_cache()
    return jsonify({"ok": True, "added": len(added), "file": fname, "active": n})

@bp.route("/api/vocab/export", methods=["POST"])
@auth.require("vocab:write")
def export_vocab_words():
    """Return selected words as a downloadable plain-text wordlist."""
    words = [w.strip() for w in (request.json or {}).get("words", []) if w and w.strip()]
    body  = ("# BINventory exported wordlist\n"
             "# One term per line. Drop this file into the dictionaries/ folder to use it.\n\n"
             + "\n".join(words) + "\n")
    from flask import Response
    return Response(body, mimetype="text/plain", headers={
        "Content-Disposition": "attachment; filename=binventory_wordlist.txt"
    })

@bp.route("/api/vocab", methods=["GET"])
@auth.require("vocab:read")
def list_vocab():
    """Report vocabulary size and the words learned from your renames."""
    learned = LearnedLabel.query.order_by(LearnedLabel.uses.desc()).all()
    return jsonify({
        "base_count":    len(vocabulary.BASE_CANDIDATE_LABELS),
        "learned_count": len(learned),
        "active_count":  len(vocabulary.CANDIDATE_LABELS),
        "learned": [{"id": l.id, "text": l.text, "uses": l.uses} for l in learned],
    })

@bp.route("/api/vocab/<int:vid>", methods=["DELETE"])
@auth.require("vocab:write")
def delete_vocab(vid):
    """Remove a learned word (e.g. a typo that got recorded)."""
    l = db.session.get(LearnedLabel, vid)
    if not l:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(l)
    db.session.commit()
    vocabulary.refresh_candidate_labels()
    return jsonify({"ok": True, "active_count": len(vocabulary.CANDIDATE_LABELS)})


# ── Vocabulary management (backlog D6/E3) ──────────────────────────────────
# Everything here edits the dictionary FILES (the active vocabulary) or the
# review queue. Nothing merges or deletes on its own: the UI proposes, the user
# confirms each action.

def _norm(term):
    """Normalised form used to spot duplicates: case, punctuation, plural."""
    t = re.sub(r"[^a-z0-9 ]+", "", (term or "").lower()).strip()
    t = re.sub(r"\s+", " ", t)
    if len(t) > 3 and t.endswith("es"):
        t = t[:-2]
    elif len(t) > 3 and t.endswith("s"):
        t = t[:-1]
    return t


def _tag_usage():
    """lower-cased tag -> number of items carrying it."""
    from collections import Counter
    counts = Counter()
    for (tags,) in db.session.query(Item.tags).all():
        for t in {x.strip().lower() for x in (tags or "").split(",") if x.strip()}:
            counts[t] += 1
    return counts


def _near_pairs(keys, user_keys):
    """Pairs where one term is the other with one letter missing (a typo).

    Only proposed when at least one side is a term the user typed (review queue
    or custom list): the curated lists are spelled correctly, and between two
    real dictionary words a one-letter difference is usually a different word
    (rivet/trivet, towel/trowel, staple/stapler). Terms under 5 characters and
    pairs differing only in digits ("M3 screw" / "M4 screw") are skipped.
    """
    keyset = {k for k in keys if len(k) >= 5}
    pairs = set()
    for k in keyset:
        for i in range(len(k)):
            v = k[:i] + k[i + 1:]
            if (v in keyset and (v in user_keys or k in user_keys)
                    and re.sub(r"\d", "", v) != re.sub(r"\d", "", k)):
                pairs.add(tuple(sorted((v, k))))
    return pairs


@bp.route("/api/vocab/duplicates", methods=["GET"])
@auth.require("vocab:read")
def vocab_duplicates():
    """Propose groups of terms that are probably the same thing.

    kind "exact":   same after folding case, punctuation and simple plurals.
    kind "spacing": same once spaces are ignored (clothes pin / clothespin).
    kind "typo":    one letter apart, where one side is a term the user typed.
    Merging is NEVER automatic; the user picks which term to keep.
    """
    sources = vocabulary.term_sources()
    usage = _tag_usage()
    custom = {t.lower() for t in vocabulary.custom_terms()}
    entries, seen, user_keys = {}, set(), set()
    for term in vocabulary.CANDIDATE_LABELS + vocabulary.custom_terms():
        if term.lower() in seen:
            continue
        seen.add(term.lower())
        key = _norm(term)
        if term.lower() in custom:
            user_keys.add(key)
        entries.setdefault(key, []).append({
            "text": term, "source": "dictionary",
            "files": sources.get(term.lower(), []), "items": usage.get(term.lower(), 0)})
    for l in LearnedLabel.query.all():
        if l.text.lower() in seen:
            continue
        key = _norm(l.text)
        user_keys.add(key)
        entries.setdefault(key, []).append({
            "text": l.text, "source": "learned", "id": l.id, "uses": l.uses,
            "files": [], "items": usage.get(l.text.lower(), 0)})

    groups = [{"kind": "exact", "key": k, "terms": v} for k, v in entries.items() if len(v) > 1]

    # Keys that differ only in spacing, then likely typos — each joined into
    # connected groups (a~b, b~c -> {a,b,c}).
    by_compact = {}
    for k in entries:
        by_compact.setdefault(k.replace(" ", ""), []).append(k)
    spacing_pairs = {tuple(sorted((ks[0], other))) for ks in by_compact.values()
                     if len(ks) > 1 for other in ks[1:]}

    for kind, pairs in (("spacing", spacing_pairs),
                        ("typo", _near_pairs(entries.keys(), user_keys))):
        parent = {}
        def find(x):
            while parent.get(x, x) != x:
                x = parent[x]
            return x
        for a, b in pairs:
            parent[find(a)] = find(b)
        clusters = {}
        for k in set(parent) | set(parent.values()):     # roots too, not just children
            clusters.setdefault(find(k), set()).add(k)
        for keys in clusters.values():
            if len(keys) > 1:
                terms = [t for k in sorted(keys) for t in entries[k]]
                groups.append({"kind": kind, "key": " / ".join(sorted(keys)), "terms": terms})

    order = {"exact": 0, "spacing": 1, "typo": 2}
    groups.sort(key=lambda g: (order[g["kind"]], -len(g["terms"]), g["key"]))
    return jsonify({"groups": groups, "count": len(groups),
                    **{k: sum(g["kind"] == k for g in groups) for k in order}})


@bp.route("/api/vocab/candidates", methods=["GET"])
@auth.require("vocab:read")
def vocab_candidates():
    """Terms captured from manual renames, awaiting review (backlog D5).

    These are NOT in the active vocabulary — they are suggestions.
    """
    rows = vocabulary.learned_candidates()
    known = {t.lower() for t in vocabulary.CANDIDATE_LABELS}
    return jsonify([
        {"id": l.id, "text": l.text, "uses": l.uses,
         "already_known": l.text.lower() in known,
         "added": l.added.isoformat() if l.added else None}
        for l in rows
    ])


@bp.route("/api/vocab/manage", methods=["GET"])
@auth.require("vocab:read")
def vocab_manage():
    """Everything the management screen shows: review queue and custom list."""
    usage = _tag_usage()
    known = {t.lower() for t in vocabulary.CANDIDATE_LABELS}
    return jsonify({
        "candidates": [{"id": l.id, "text": l.text, "uses": l.uses,
                        "already_known": l.text.lower() in known}
                       for l in vocabulary.learned_candidates()],
        "custom": [{"text": t, "items": usage.get(t.lower(), 0)}
                   for t in vocabulary.custom_terms()],
        "active_count": len(vocabulary.CANDIDATE_LABELS),
    })


def _reload():
    n = vocabulary.reload_from_disk()
    ml.invalidate_label_cache()
    return n


@bp.route("/api/vocab/promote", methods=["POST"])
@auth.require("vocab:write")
def vocab_promote():
    """Move review candidates into custom_list.txt (active vocabulary)."""
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    rows = [r for r in (db.session.get(LearnedLabel, int(i)) for i in ids) if r]
    if not rows:
        return jsonify({"error": "Nothing to promote"}), 400
    added = vocabulary.add_terms([r.text for r in rows])
    for r in rows:
        db.session.delete(r)
    db.session.commit()
    return jsonify({"ok": True, "added": added, "active_count": _reload()})


@bp.route("/api/vocab/custom/delete", methods=["POST"])
@auth.require("vocab:write")
def vocab_custom_delete():
    """Remove terms from the custom list. Item tags are left as they are."""
    terms = (request.get_json(silent=True) or {}).get("terms") or []
    if not terms:
        return jsonify({"error": "No terms given"}), 400
    c = vocabulary.CUSTOM_FILE
    removed = vocabulary.remove_terms(terms, files={c, c + ".off"})
    return jsonify({"ok": True, "removed": sum(removed.values()), "active_count": _reload()})


@bp.route("/api/vocab/merge", methods=["POST"])
@auth.require("vocab:write")
def vocab_merge():
    """Merge duplicate terms into one the user chose.

    Body: {"keep": "string light", "remove": ["string lights", ...]}
      * removed terms leave every dictionary file and the review queue;
      * items tagged with a removed term are re-tagged with `keep`;
      * `keep` ends up in the vocabulary (added to custom_list if it was only a
        review candidate, or if removing a case variant took it out too).
    """
    data = request.get_json(silent=True) or {}
    keep = (data.get("keep") or "").strip()
    remove = [t.strip() for t in data.get("remove") or [] if t and t.strip()]
    remove = [t for t in remove if t != keep]
    if not keep or not remove:
        return jsonify({"error": "Choose a term to keep and at least one to merge into it"}), 400

    removed_files = vocabulary.remove_terms(remove)
    drop = {t.lower() for t in remove}
    learned_removed = 0
    for l in LearnedLabel.query.all():
        if l.text.lower() in drop or l.text.lower() == keep.lower():
            db.session.delete(l)
            learned_removed += 1
    if keep.lower() not in vocabulary.term_sources():
        vocabulary.add_terms([keep])

    retagged = 0
    for item in Item.query.filter(Item.tags != "").all():
        tags = [t.strip() for t in (item.tags or "").split(",") if t.strip()]
        if not any(t.lower() in drop for t in tags):
            continue
        tags = [keep if t.lower() in drop else t for t in tags]
        item.tags = vocabulary.merge_tags("", *tags)       # also de-duplicates
        retagged += 1
    db.session.commit()
    return jsonify({"ok": True, "kept": keep,
                    "removed_from_files": sum(removed_files.values()),
                    "learned_removed": learned_removed, "items_retagged": retagged,
                    "active_count": _reload()})
