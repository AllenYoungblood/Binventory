"""CLIP visual tagging, as an analyzer.

The heavy lifting still lives in `ml/` — this is a thin adapter that presents
CLIP through the common interface so the worker doesn't special-case it.
"""
import json

from .. import vocabulary
from ..ml import embeddings, runtime
from .base import Analyzer, AnalysisResult
from .registry import register


class ClipAnalyzer(Analyzer):
    id = "clip"
    label = "Visual tagging (CLIP)"
    cost = "heavy"

    def available(self):
        try:
            import open_clip  # noqa: F401
            return True
        except Exception:
            return False

    def wants(self, ctx):
        # Only worth running if the item has no embedding yet. Re-tagging clears
        # the embedding first, which is what makes that path work.
        from ..services.embeddings_store import has_embedding
        return not has_embedding(ctx.item)

    def load(self):
        return runtime.load_clip()

    def unload(self):
        runtime.unload_clip()

    def analyze(self, ctx):
        try:
            labels = embeddings.top_labels(ctx.image)
            emb = embeddings.embed_image(ctx.image)
        except Exception as e:
            return AnalysisResult.failed(f"CLIP analysis failed: {e}")

        return AnalysisResult(
            tags=labels,
            # CLIP's best guess is only a fallback name — rank "ml" loses to
            # anything the user typed or OCR read.
            name=labels[0] if labels else None,
            name_source="ml",
            embedding=emb,
        )


register(ClipAnalyzer())
