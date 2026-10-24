"""Text recognition, as an analyzer.

Reads printed labels and part numbers off the photo. Its name proposal outranks
CLIP's, because a printed part number beats a generic visual guess like
"electronic" — but it still loses to a name the user typed.
"""
import re

from ..ml import runtime
from .base import Analyzer, AnalysisResult
from .registry import register


def ocr_tag(text, settings):
    """The tag copy of an OCR read.

    Tags are stored comma-separated, so commas inside the read are flattened —
    otherwise the text splits into fragments, de-duplication never matches it,
    and every re-run appends another copy.
    """
    snippet = re.sub(r"\s+", " ", (text or "").replace(",", " ")).strip()
    if settings.get("ocr_truncate", True):
        limit = max(1, min(255, settings.get("ocr_truncate_len", 255) or 255))
        snippet = snippet[:limit].strip()
    return snippet


class OcrAnalyzer(Analyzer):
    id = "ocr"
    label = "Text recognition (OCR)"
    cost = "heavy"

    def available(self):
        try:
            import easyocr  # noqa: F401
            return True
        except Exception:
            return False

    def wants(self, ctx):
        # OCR is opt-in per item — the Add Item toggle sets ocr_requested.
        return bool(ctx.item.ocr_requested) and not ctx.item.ocr_done

    def load(self):
        return runtime.load_ocr()

    def unload(self):
        runtime.unload_ocr()

    def analyze(self, ctx):
        # A region the user explicitly selected (E5) is never replaced by an
        # automatic or bulk whole-photo read. Discard it entirely — it would
        # only repeat the region's text, more verbosely — but mark OCR done so
        # the item isn't retried forever.
        if (ctx.item.ocr_source or "") == "region":
            return AnalysisResult(data={"ocr_done": True})
        try:
            text = runtime.run_ocr(ctx.image_path)
        except Exception as e:
            return AnalysisResult.failed(f"OCR failed: {e}")

        # Record the raw read even when nothing legible was found, so `ocr_done`
        # gets set and the item isn't retried forever.
        result = AnalysisResult(data={"ocr_text": text, "ocr_done": True,
                                      "ocr_source": "photo" if text else ""})
        if not text:
            return result

        return apply_text(result, text, ctx.item, ctx.settings or {}, "ocr")


def apply_text(result, text, item, settings, name_source):
    """Fill `result` with the name and tag for an OCR read.

    Shared by whole-photo OCR and region OCR (E5) so both handle the tag the
    same way: the previous read's tag is replaced, never accumulated.
    """
    result.name = text[:200]
    result.name_source = name_source
    # Optionally index the text as searchable tags, truncated per settings.
    # The RAW text is always kept on the item regardless (backlog C7) — only
    # the tag copy is truncated.
    if settings.get("ocr_as_tags", True):
        result.tags = [ocr_tag(text, settings)]
        previous = ocr_tag(item.ocr_text, settings)
        if previous and previous != result.tags[0]:
            result.remove_tags = [previous]
    return result


register(OcrAnalyzer())
