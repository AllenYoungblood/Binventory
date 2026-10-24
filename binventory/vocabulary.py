"""Tag vocabulary: dictionary files on disk plus terms learned from renames.

The active vocabulary is `BASE_CANDIDATE_LABELS` (read from dictionaries/*.txt)
layered with anything in the LearnedLabel table. It powers two things:

  1. the human-readable tag chips CLIP assigns to photos, and
  2. keyword search, which is the ONLY search available when the model is
     unloaded — so vocabulary quality matters more than it first appears.
"""
import re

from .config import DICT_DIR
from .db import db
from .models import LearnedLabel


def load_dictionaries():
    """Read every enabled dictionary file. Returns (terms, per_file_counts)."""
    terms, counts = [], {}
    if not DICT_DIR.exists():
        print(f"[vocab] No dictionaries folder at {DICT_DIR}")
        return terms, counts
    for path in sorted(DICT_DIR.glob("*.txt")):
        try:
            words = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    words.append(line)
            terms.extend(words)
            counts[path.name] = len(words)
        except Exception as e:
            print(f"[vocab] Could not read {path.name}: {e}")
    return terms, counts

def dictionary_status():
    """List every dictionary file with its term count and enabled state."""
    out = []
    if not DICT_DIR.exists():
        return out
    for path in sorted(DICT_DIR.iterdir()):
        if path.suffix not in (".txt", ".off"):
            continue
        enabled = path.name.endswith(".txt")
        try:
            n = sum(1 for l in path.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.strip().startswith("#"))
        except Exception:
            n = 0
        out.append({"file": path.name, "terms": n, "enabled": enabled})
    return out



_STOPWORDS = {
    "the","a","an","and","or","of","for","with","to","in","on","at","by","from",
    "is","are","was","were","be","this","that","it","its","as","new","used",
    "pcs","pcs.","qty","set","kit","pack","x","mm","cm","in.","inch","inches",
}


def _tokenize_for_vocab(text):
    """Split a string into candidate vocabulary words.

    Lower-cases, strips punctuation, drops stopwords, pure numbers, and very
    short tokens. Deliberately conservative — this feeds a human review screen,
    not an automatic import.
    """
    import re as _re
    if not text:
        return []
    words = _re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", text)
    out = []
    for w in words:
        wl = w.lower().strip("-")
        if len(wl) < 3 or wl in _STOPWORDS:
            continue
        if wl.isdigit():
            continue
        out.append(wl)
    return out

# Loaded at import; refreshed with learned terms once the DB is available.
_dict_terms, _dict_counts = load_dictionaries()
BASE_CANDIDATE_LABELS = list(dict.fromkeys(_dict_terms))
CANDIDATE_LABELS = list(BASE_CANDIDATE_LABELS)
print(f"[vocab] Loaded {len(BASE_CANDIDATE_LABELS)} terms from "
      f"{len(_dict_counts)} dictionary file(s)")

def _learn_label(text):
    """Record a user-supplied label as a REVIEW CANDIDATE (backlog D5).

    This used to add the string straight into the active vocabulary. That was
    wrong: the only filter was "3-60 characters and not already known", so
    "DeWalt DCD771C2 20V MAX Brushless" entered whole and polluted tag output
    with strings that will never match another photo.

    Now nothing reaches the dictionary without human confirmation. The term is
    recorded with a use count and surfaces in Vocabulary Review, where the user
    decides. Renames and OCR are both pulls, never pushes.
    """
    text = (text or "").strip()
    if not (2 < len(text) <= 60):
        return False
    if text.lower() in {l.lower() for l in BASE_CANDIDATE_LABELS}:
        return False
    existing = LearnedLabel.query.filter(
        db.func.lower(LearnedLabel.text) == text.lower()
    ).first()
    if existing:
        existing.uses = (existing.uses or 1) + 1
        db.session.commit()
        return False
    db.session.add(LearnedLabel(text=text))
    db.session.commit()
    # Deliberately NOT calling refresh_candidate_labels(): a candidate is not
    # active vocabulary until promoted through review.
    return True


def refresh_candidate_labels():
    """Rebuild the active vocabulary.

    Active vocabulary is the dictionary FILES only. Learned terms are review
    candidates and are excluded until the user promotes them, at which point
    they are written into dictionaries/custom_list.txt and picked up here like
    any other dictionary entry.
    """
    global CANDIDATE_LABELS
    CANDIDATE_LABELS = list(BASE_CANDIDATE_LABELS)
    return len(CANDIDATE_LABELS)


# ── File-level vocabulary edits (backlog E3) ────────────────────────────────
# The dictionary FILES are the active vocabulary, so managing vocabulary means
# editing them. Comments and blank lines are always preserved.

CUSTOM_FILE = "custom_list.txt"
_CUSTOM_HEADER = ("# Custom list — your own terms. Words promoted from Vocabulary Review land here.\n"
                  "# One term per line. Edit freely.\n\n")


def _dictionary_files():
    """Every dictionary file, enabled (.txt) or disabled (.off)."""
    if not DICT_DIR.exists():
        return []
    return sorted(p for p in DICT_DIR.iterdir() if p.suffix in (".txt", ".off"))


def term_sources():
    """Map lower-cased term -> list of file names that contain it."""
    out = {}
    for path in _dictionary_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if t and not t.startswith("#"):
                out.setdefault(t.lower(), []).append(path.name)
    return out


def custom_terms():
    """Terms in the custom list (enabled or not), in file order."""
    for name in (CUSTOM_FILE, CUSTOM_FILE + ".off"):
        path = DICT_DIR / name
        if path.exists():
            return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.strip().startswith("#")]
    return []


def add_terms(words, fname=CUSTOM_FILE):
    """Append words not already in `fname` (created if absent). Returns those added.

    Does not reload — callers reload once after all their edits.
    """
    DICT_DIR.mkdir(parents=True, exist_ok=True)
    path = DICT_DIR / fname
    existing = set()
    if path.exists():
        existing = {l.strip().lower() for l in path.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.strip().startswith("#")}
    else:
        path.write_text(_CUSTOM_HEADER, encoding="utf-8")
    added = []
    for w in words:
        w = (w or "").strip()
        if w and w.lower() not in existing:
            added.append(w)
            existing.add(w.lower())
    if added:
        text = path.read_text(encoding="utf-8")
        sep = "" if text.endswith("\n") or not text else "\n"
        path.write_text(text + sep + "\n".join(added) + "\n", encoding="utf-8")
    return added


def remove_terms(words, files=None):
    """Delete terms (case-insensitive) from dictionary files. Returns {file: n}.

    `files` limits the edit to those file names; default is every file.
    """
    drop = {(w or "").strip().lower() for w in words if w and w.strip()}
    removed = {}
    for path in _dictionary_files():
        if files is not None and path.name not in files:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [l for l in lines
                if l.strip().startswith("#") or l.strip().lower() not in drop]
        n = len(lines) - len(kept)
        if n:
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            removed[path.name] = n
    return removed


def learned_candidates():
    """Terms captured from renames, awaiting review."""
    try:
        return LearnedLabel.query.order_by(LearnedLabel.uses.desc()).all()
    except Exception:
        return []


def reload_from_disk():
    """Re-read the dictionaries folder (Settings -> Reload from disk)."""
    global BASE_CANDIDATE_LABELS, _dict_terms, _dict_counts
    _dict_terms, _dict_counts = load_dictionaries()
    BASE_CANDIDATE_LABELS = list(dict.fromkeys(_dict_terms))
    return refresh_candidate_labels()


def merge_tags(existing, *new_terms):
    """Append terms to a comma-separated tag string, de-duplicated."""
    have = [t.strip() for t in (existing or "").split(",") if t.strip()]
    lower = {t.lower() for t in have}
    for term in new_terms:
        term = (term or "").strip()
        if term and term.lower() not in lower:
            have.append(term)
            lower.add(term.lower())
    return ", ".join(have)
