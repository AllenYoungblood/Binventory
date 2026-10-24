"""The analyzer contract."""
from dataclasses import dataclass, field

# Name-source precedence (backlog C1) — the DEFAULT order, kept for reference
# and for plugins that import it. The active order is user-configurable and
# lives in binventory/naming.py; the worker arbitrates with `naming.rank()`.
# A proposed name only wins if its rank is at least as high as whatever is
# already stored, so OCR can overwrite a CLIP guess but never a name the user
# typed (unless the user reorders them).
RANK = {
    "": 0,
    "none": 0,
    "ml": 1,
    "ocr": 2,
    # A decoded barcode/QR (phase 7) is exact where whole-photo OCR is noisy, so
    # it outranks OCR — otherwise a later OCR re-run would put the jumble back.
    "scanned": 3,
    "scanned_search": 3,
    # OCR of a region the user deliberately selected (backlog E5): beats any
    # automatic read, loses only to a name the user typed.
    "ocr_region": 4,
    # The AI assistant via the MCP server: beats every automatic guess, but by
    # default never a name the user typed.
    "agent": 5,
    "user": 6,
}


@dataclass
class AnalysisContext:
    """What an analyzer is given.

    `image` is already EXIF-corrected, so analyzers never have to think about
    orientation. `item` is a live model instance but should be treated as
    read-only — return changes instead of writing them, so the worker stays in
    control of ordering and precedence.
    """
    image_path: object
    image: object
    item: object
    settings: dict = field(default_factory=dict)


@dataclass
class AnalysisResult:
    """What an analyzer returns.

    Every field is optional; an analyzer contributes only what it knows.
    """
    tags: list = field(default_factory=list)
    name: str = None
    name_source: str = "ml"          # keyed into RANK above
    data: dict = field(default_factory=dict)   # free-form, stored per-extension
    remove_tags: list = field(default_factory=list)  # superseded tags to drop first
    embedding: list = None
    ok: bool = True
    error: str = None

    @classmethod
    def empty(cls):
        return cls()

    @classmethod
    def failed(cls, error):
        return cls(ok=False, error=error)


class Analyzer:
    """Base class for anything that derives metadata from a photo.

    Subclasses set `id`, optionally `cost`, and implement `analyze()`.
    """

    id = "analyzer"
    label = "Analyzer"
    # "heavy" analyzers participate in the model load/unload lifecycle; "light"
    # ones (barcode decoding, colour extraction) are assumed cheap enough to run
    # without any lifecycle management at all.
    cost = "light"

    def available(self):
        """Can this analyzer run at all (dependencies installed, etc.)?"""
        return True

    def wants(self, ctx):
        """Should it run for THIS item? Lets an analyzer opt out per item —
        OCR uses this to honour the per-item toggle."""
        return True

    def load(self):
        """Prepare heavy resources. Called once per batch, only if `wants`."""
        return True

    def unload(self):
        """Release heavy resources. Called when the batch drains."""

    def analyze(self, ctx):
        raise NotImplementedError

    def __repr__(self):
        return f"<Analyzer {self.id}>"
