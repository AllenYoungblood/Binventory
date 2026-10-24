"""Reading and writing item embeddings (backlog F1).

An embedding is 512 float32 values stored as raw bytes (exactly 2,048) in
`item.embedding_blob`. The earlier JSON-text column (~6-8 KB per item, parsed
on every search) was backfilled and dropped in phase 4b (migration 7), so this
module is the only place that knows the storage format.
"""
import numpy as np

DIM = 512


def encode(vector):
    """Serialise a float vector to compact float32 bytes."""
    if vector is None:
        return None
    return np.asarray(vector, dtype=np.float32).tobytes()


def decode(blob):
    """Deserialise float32 bytes back to a numpy array."""
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def set_embedding(item, vector):
    """Store an embedding on an item, or clear it with None."""
    item.embedding_blob = encode(vector)


def get_embedding(item):
    """Read an item's embedding as a numpy array, or None."""
    return decode(item.embedding_blob)


def has_embedding(item):
    return bool(item.embedding_blob)


def build_matrix(items):
    """Stack embeddings for a list of items into one L2-normalised matrix.

    Returns (matrix, kept_items). Items without an embedding are skipped, so the
    two are index-aligned — that alignment is what lets a caller map a score back
    to the item that produced it.
    """
    vecs, kept = [], []
    for it in items:
        v = get_embedding(it)
        if v is None or v.size == 0:
            continue
        vecs.append(v)
        kept.append(it)
    if not kept:
        return None, []
    mat = np.vstack(vecs).astype(np.float32)
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    return mat, kept
