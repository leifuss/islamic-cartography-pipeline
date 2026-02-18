"""
Docling-based text extraction (primary extractor).
Also returns per-page text so Vision API output can be compared page-by-page.
"""
from pathlib import Path
from typing import Dict
import logging
import re

# Matches ligature fragments (fi, fl, ff, ffi, ffl) followed by 2+ spaces
# and a lowercase letter — artifact of PDFs with unencoded ligature glyphs.
# e.g. "fi  rst" → "first", "fl  at" → "flat", "refl  ection" → "reflection"
_LIGATURE_RE = re.compile(r'(ff?[il]?)\s{2,}(?=[a-z])')


def _fix_ligatures(text: str) -> str:
    return _LIGATURE_RE.sub(r'\1', text)


try:
    from docling.document_converter import DocumentConverter
    DOCLING_AVAILABLE = True
except ImportError:
    DOCLING_AVAILABLE = False
    logging.warning("Docling not installed")


def _make_converter(do_ocr: bool = True) -> 'DocumentConverter':
    """
    Build a DocumentConverter with optional OCR disabled.

    For embedded-font PDFs we can skip OCR entirely — it's faster and avoids
    spurious characters / PIL decompression-bomb errors from high-DPI images.
    Falls back to default converter if the pipeline-options API is unavailable.
    """
    if not do_ocr:
        try:
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import PdfFormatOption
            from docling.datamodel.base_models import InputFormat
            opts = PdfPipelineOptions(do_ocr=False)
            return DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
            )
        except Exception as exc:
            logging.warning(
                f"Could not build no-OCR converter ({exc}). "
                "Falling back to default (OCR enabled)."
            )
    return DocumentConverter()


class DoclingExtractor:

    def __init__(self, do_ocr: bool = True):
        """
        Args:
            do_ocr: Set False for embedded-font PDFs — skips the OCR stage,
                    which is ~3-5× faster and avoids large-image PIL errors.
        """
        if not DOCLING_AVAILABLE:
            raise ImportError("Docling not installed. Run: pip install docling")
        self.do_ocr    = do_ocr
        self.converter = _make_converter(do_ocr=do_ocr)

    def extract(self, file_path: Path) -> Dict:
        """
        Extract text from a PDF or image.

        Returns the standard result dict plus:
          'page_texts'      — {page_no (1-based): text_string}
          'layout_elements' — {page_no (1-based): [{label, text}, …]}
                              label values: text, section_header, title,
                              footnote, caption, list_item, page_header,
                              page_footer, table, picture, formula, …
        """
        try:
            result = self.converter.convert(str(file_path))
            doc    = result.document

            # ── Full document export ──────────────────────────────────────────
            full_text = _fix_ligatures(doc.export_to_markdown())

            # ── Per-page text & layout elements ──────────────────────────────
            # Iterate all text items and bucket them by page provenance.
            page_texts:    Dict[int, list] = {}
            layout_pages:  Dict[int, list] = {}
            # Visual element labels that should be captured even without text
            _VISUAL_LABELS = {'picture', 'figure', 'chart', 'diagram', 'image'}

            try:
                for item, _level in doc.iterate_items():
                    text = getattr(item, 'text', None)
                    # text is a plain str on most items but a method on a few
                    if callable(text):
                        text = text()
                    if not isinstance(text, str):
                        text = ''

                    # Normalise label to a lowercase string (enum or str)
                    raw_label = getattr(item, 'label', None)
                    label_str = (
                        raw_label.value
                        if hasattr(raw_label, 'value')
                        else str(raw_label).split('.')[-1].lower()
                        if raw_label is not None
                        else 'text'
                    )

                    # Skip text-less items unless they are visual elements with a bbox
                    is_visual = label_str in _VISUAL_LABELS
                    if not text and not is_visual:
                        continue

                    for prov in getattr(item, 'prov', []):
                        page_no = getattr(prov, 'page_no', None)
                        if page_no is None:
                            continue

                        bbox = getattr(prov, 'bbox', None)

                        # Visual elements without text still need a bbox to be useful
                        if is_visual and not text and bbox is None:
                            continue

                        if text:
                            page_texts.setdefault(page_no, []).append(text)

                        elem: Dict = {'label': label_str, 'text': _fix_ligatures(text)}
                        # Bounding box — BOTTOMLEFT coord origin (l, t, r, b in PDF pts)
                        if bbox is not None:
                            elem['bbox'] = {
                                'l': round(bbox.l, 2),
                                't': round(bbox.t, 2),
                                'r': round(bbox.r, 2),
                                'b': round(bbox.b, 2),
                            }
                        layout_pages.setdefault(page_no, []).append(elem)
            except Exception as e:
                logging.warning(f"Could not extract per-page text: {e}")

            # ── Page sizes (needed to normalise bbox coords in the reader) ────
            page_sizes: Dict[str, Dict] = {}
            try:
                if hasattr(doc, 'pages'):
                    for pg_no, pg in doc.pages.items():
                        size = getattr(pg, 'size', None)
                        if size:
                            page_sizes[str(pg_no)] = {
                                'w': round(size.width,  2),
                                'h': round(size.height, 2),
                            }
                if page_sizes:
                    layout_pages['_page_sizes'] = page_sizes  # type: ignore[assignment]
            except Exception as e:
                logging.warning(f"Could not capture page sizes: {e}")

            page_texts_str = {k: _fix_ligatures(' '.join(v)) for k, v in page_texts.items()}

            confidence = 0.9 if full_text and len(full_text) > 100 else 0.5
            page_count = getattr(doc, 'num_pages', None) or len(doc.pages) if hasattr(doc, 'pages') else 1

            return {
                'text':            full_text,
                'confidence':      confidence,
                'method':          'docling',
                'do_ocr':          self.do_ocr,
                'metadata': {
                    'page_count': page_count,
                    'format':     file_path.suffix,
                    'success':    True,
                },
                'page_texts':      page_texts_str,   # {1-based page_no: text}
                'layout_elements': layout_pages,     # {1-based page_no: [{label,text,bbox?},…]}
                                                     # also contains '_page_sizes' key
                'error': None,
            }

        except Exception as e:
            logging.error(f"Docling extraction failed for {file_path}: {e}")
            return {
                'text':            None,
                'confidence':      0.0,
                'method':          'docling',
                'do_ocr':          self.do_ocr,
                'metadata':        {'success': False},
                'page_texts':      {},
                'layout_elements': {},
                'error':           str(e),
            }
