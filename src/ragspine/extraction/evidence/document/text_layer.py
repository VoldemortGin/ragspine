"""Text-layer quality of one page: detection only, nothing here repairs a page.

A page whose words are painted as vector outlines has no spans to index, and a font
without ToUnicode yields spans whose text is not what the page shows. Both are named per
page so a later OCR stage knows where to look; this module never guesses the words.
"""

import unicodedata
from collections.abc import Sequence

from ragspine.extraction.evidence.document.models import TextLayerDiagnostic, TextLayerStatus

# A page with no non-space character at all that paints at least this many vector paths
# (``page.get_drawings()``, which excludes text glyphs) is ``outlined_text``: only OCR can
# tell whether its shapes are words. On the real outlined AIA deck a divider page's one-line
# heading is 13-25 paths, so no count above one is safe. A false positive costs one OCR run
# whose empty result is harmless; a miss loses the page's words. A path-free page is blank.
TEXTLESS_OUTLINED_MIN_DRAWINGS = 1
# A page that carries some text is outlined text only when it paints at least this many
# paths; fewer is a divider, a logo or an ornament beside real words.
OUTLINED_TEXT_MIN_DRAWINGS = 50
# ...and it is outlined text only when it paints at least this many paths per character
# its text layer carries. Outlined body text is one path per glyph (hundreds against a
# handful of live characters); a vector chart labels its bars (on the real AIA sample no
# page with 50+ paths exceeds 0.28 per character), so a dense chart with real labels stays ``ok``.
OUTLINED_TEXT_DRAWINGS_PER_CHAR = 10
# A page is garbled once undecodable characters reach this share of its non-space text.
GARBLED_PAGE_MIN_RATIO = 0.05

# Private use (glyph codes of a font without ToUnicode), unassigned and lone surrogates.
_UNDECODABLE_CATEGORIES = frozenset({"Co", "Cn", "Cs"})


def _undecodable(char: str) -> bool:
    category = unicodedata.category(char)
    return (
        char == "\ufffd"
        or category in _UNDECODABLE_CATEGORIES
        or (category == "Cc" and not char.isspace())
    )


def is_garbled_text(text: str) -> bool:
    """One undecodable character is enough: such a span cannot be quoted verbatim."""
    return any(_undecodable(char) for char in text)


def diagnose_text_layer(texts: Sequence[str], *, drawing_count: int) -> TextLayerDiagnostic:
    """Classify a page from every span text it observed, including garbled ones."""
    chars = [char for text in texts for char in text if not char.isspace()]
    garbled_chars = sum(_undecodable(char) for char in chars)
    if (not chars and drawing_count >= TEXTLESS_OUTLINED_MIN_DRAWINGS) or (
        drawing_count >= OUTLINED_TEXT_MIN_DRAWINGS
        and drawing_count >= len(chars) * OUTLINED_TEXT_DRAWINGS_PER_CHAR
    ):
        status = TextLayerStatus.OUTLINED_TEXT
    elif chars and garbled_chars / len(chars) >= GARBLED_PAGE_MIN_RATIO:
        status = TextLayerStatus.GARBLED
    else:
        status = TextLayerStatus.OK
    return TextLayerDiagnostic(
        status,
        span_count=len(texts),
        char_count=len(chars),
        garbled_char_count=garbled_chars,
        garbled_span_count=sum(is_garbled_text(text) for text in texts),
        drawing_count=drawing_count,
    )
