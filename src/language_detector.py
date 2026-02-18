"""
Detect languages present in a document and map to Tesseract language codes.

Strategy (two-pass):
1. Unicode script analysis  – reliable for Arabic/Persian/Greek script
2. langdetect on sampled chunks – distinguishes Latin-script languages
                                  (English, French, Latin)

Supports the corpus: English, French, Arabic, Persian, Ancient Greek, Latin.
"""
import re
import unicodedata
import logging
from typing import Set, List

try:
    from langdetect import detect_langs, LangDetectException
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False
    logging.warning("langdetect not installed – Latin-script detection will be limited")


# ── Unicode block boundaries ─────────────────────────────────────────────────
_ARABIC_BLOCK    = (0x0600, 0x06FF)   # Arabic + Persian share this block
_ARABIC_EXTENDED = (0x0750, 0x077F)
_GREEK_BLOCK     = (0x0370, 0x03FF)
_GREEK_EXTENDED  = (0x1F00, 0x1FFF)

# Persian-specific letters not found in Arabic
_PERSIAN_CHARS = set('پچژگ')          # U+067E, U+0686, U+0698, U+06AF

# Tesseract language codes for languages we support
_LANGDETECT_TO_TESSERACT = {
    'en':  'eng',
    'fr':  'fra',
    'la':  'lat',   # langdetect uses 'la' for Latin
    'ar':  'ara',
    'fa':  'fas',
    'el':  'ell',   # modern Greek (rarely encountered, but handle it)
}

# Languages that Tesseract has installed
_INSTALLED = {'ara', 'ell', 'eng', 'fas', 'fra', 'grc', 'lat'}

# Chunk size and count for langdetect sampling
_CHUNK_CHARS  = 500
_CHUNK_COUNT  = 10
_MIN_PROB     = 0.15   # minimum langdetect probability to count a language


def _has_arabic_script(text: str) -> bool:
    return any(
        _ARABIC_BLOCK[0] <= ord(c) <= _ARABIC_BLOCK[1] or
        _ARABIC_EXTENDED[0] <= ord(c) <= _ARABIC_EXTENDED[1]
        for c in text
    )


def _has_persian(text: str) -> bool:
    """Persian uses Arabic script but has distinctive letters."""
    return any(c in _PERSIAN_CHARS for c in text)


def _has_greek_script(text: str) -> bool:
    return any(
        _GREEK_BLOCK[0] <= ord(c) <= _GREEK_BLOCK[1] or
        _GREEK_EXTENDED[0] <= ord(c) <= _GREEK_EXTENDED[1]
        for c in text
    )


def _has_latin_script(text: str) -> bool:
    return any(
        unicodedata.category(c) in ('Ll', 'Lu') and
        'LATIN' in unicodedata.name(c, '')
        for c in text
    )


def _sample_chunks(text: str, n: int = _CHUNK_COUNT, size: int = _CHUNK_CHARS) -> List[str]:
    """Return up to n evenly-spaced chunks of Latin-only text."""
    # Strip non-Latin so langdetect isn't confused by Arabic/Greek chars
    latin_only = re.sub(r'[^\x00-\x7F\u00C0-\u024F\s]', ' ', text)
    latin_only = re.sub(r'\s+', ' ', latin_only).strip()

    if len(latin_only) < size:
        return [latin_only] if latin_only else []

    step = max(1, (len(latin_only) - size) // n)
    chunks = []
    for i in range(n):
        start = i * step
        chunk = latin_only[start:start + size].strip()
        if len(chunk) > 50:   # too short to be reliable
            chunks.append(chunk)
    return chunks


def detect_languages(text: str) -> Set[str]:
    """
    Return a set of Tesseract language codes detected in *text*.

    Always returns at least {'eng'} as a safe fallback.

    Args:
        text: Extracted text (e.g. from Docling)

    Returns:
        Set of Tesseract codes, e.g. {'eng', 'fra', 'ara'}
    """
    if not text or len(text.strip()) < 20:
        return {'eng'}

    langs: Set[str] = set()

    # ── Pass 1: Unicode script analysis ──────────────────────────────────────
    if _has_arabic_script(text):
        if _has_persian(text):
            langs.add('fas')
            # Persian documents almost always contain Arabic too
            langs.add('ara')
        else:
            langs.add('ara')

    if _has_greek_script(text):
        # Corpus is primarily ancient texts → prefer grc; add ell only if
        # modern Greek letters dominate (heuristic: presence of ά έ ή ί etc.)
        modern_greek_markers = set('άέήίύόώΆΈΉΊΎΌΏ')
        if any(c in modern_greek_markers for c in text):
            langs.add('ell')
        else:
            langs.add('grc')

    # ── Pass 2: langdetect on Latin-script chunks ─────────────────────────────
    if _has_latin_script(text) and LANGDETECT_AVAILABLE:
        chunks = _sample_chunks(text)
        lang_votes: dict = {}

        for chunk in chunks:
            try:
                detections = detect_langs(chunk)
                for d in detections:
                    if d.prob >= _MIN_PROB and d.lang in _LANGDETECT_TO_TESSERACT:
                        tess_code = _LANGDETECT_TO_TESSERACT[d.lang]
                        lang_votes[tess_code] = lang_votes.get(tess_code, 0) + d.prob
            except LangDetectException:
                pass

        # Accept any language that appeared with meaningful cumulative weight
        threshold = 0.5   # total probability across chunks
        for code, weight in lang_votes.items():
            if weight >= threshold:
                langs.add(code)

    elif _has_latin_script(text) and not LANGDETECT_AVAILABLE:
        # Safe fallback: add eng + fra for Latin-script text
        langs.update({'eng', 'fra'})

    # ── Fallback ──────────────────────────────────────────────────────────────
    if not langs:
        langs.add('eng')

    # Filter to only installed languages
    installed = langs & _INSTALLED
    if not installed:
        logging.warning(f"Detected {langs} but none are installed; falling back to eng")
        installed = {'eng'}

    return installed


def langs_to_tesseract_string(langs: Set[str]) -> str:
    """
    Convert a set of Tesseract codes to the '+'-joined string Tesseract expects.

    Order matters for Tesseract: put primary/dominant scripts first.
    Heuristic order: eng > fra > lat > ara > fas > grc > ell
    """
    priority = ['eng', 'fra', 'lat', 'ara', 'fas', 'grc', 'ell']
    ordered  = [l for l in priority if l in langs]
    # Append anything not in priority list
    ordered += sorted(langs - set(priority))
    return '+'.join(ordered)


def detect_and_format(text: str) -> str:
    """Convenience: detect and return a ready-to-use Tesseract lang string."""
    langs = detect_languages(text)
    lang_str = langs_to_tesseract_string(langs)
    logging.info(f"Detected languages: {langs} → Tesseract: '{lang_str}'")
    return lang_str


if __name__ == '__main__':
    # Quick smoke test
    samples = {
        'English only':   'The study of Islamic cartography reveals complex traditions.',
        'French + Eng':   'The concept of géographie arabe et le monde médiéval.',
        'Arabic':         'الخرائط الإسلامية في العصور الوسطى',
        'Persian + Arabic': 'پطلمیوس و جغرافیای اسلامی در قرون وسطا والعرب',
        'Ancient Greek':  'Κλαύδιος Πτολεμαῖος γεωγραφία καὶ κοσμογραφία',
        'Mixed':          'Ptolemy (Claudius Ptolemaeus, Κλαύδιος Πτολεμαῖος) wrote the الجغرافية.',
    }

    for label, text in samples.items():
        result = detect_and_format(text)
        print(f"{label:25s} → {result}")
