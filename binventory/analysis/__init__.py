"""Photo analysis as a pluggable pipeline (backlog G1).

Before this, the worker hard-coded "run CLIP, then run OCR". That made adding a
third source of metadata — barcode decoding, a captioning model, colour
extraction — a matter of editing the worker.

Now an analyzer is anything that takes a photo and proposes metadata. CLIP and
OCR are simply the first two implementations, which is the point: the interface
was designed against two things that already exist rather than invented for
hypothetical ones.

IMPORTANT — analyzers PROPOSE, they do not decide.
An analyzer returns a name with a `name_rank`; the worker arbitrates using the
label-source precedence (user > ocr > ml). Keeping that decision in one place is
what stops every future extension from re-implementing naming rules.
"""
from .base import Analyzer, AnalysisContext, AnalysisResult, RANK  # noqa: F401
from .registry import (  # noqa: F401
    register, unregister, get, all_analyzers, enabled_analyzers,
)
from . import clip as _clip_mod       # noqa: F401  (self-registering)
from . import ocr as _ocr_mod         # noqa: F401  (self-registering)
