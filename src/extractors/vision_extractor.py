"""
Google Cloud Vision API extraction — used as a diagnostic quality gate.

Samples a spread of pages (first, middle, last) rather than just page 1,
so the comparison against Docling reflects the whole document.
"""
from pathlib import Path
from typing import Dict, List, Optional
import logging
import io

try:
    from google.cloud import vision
    from pdf2image import convert_from_path
    from PIL import Image
    VISION_AVAILABLE = True
except ImportError:
    VISION_AVAILABLE = False
    logging.warning("Google Cloud Vision not available")


def _spread_indices(total: int, n: int = 3) -> List[int]:
    """
    Return n evenly-spread 0-based page indices across [0, total-1].
    Always includes the first and last page.
    e.g. total=40, n=3 → [0, 19, 39]
    """
    if total <= n:
        return list(range(total))
    step = (total - 1) / (n - 1)
    return sorted(set(round(i * step) for i in range(n)))


class VisionExtractor:
    """
    Extract text from a spread of pages using Google Cloud Vision API.
    Used as a quality gate: if Vision agrees with Docling, Tesseract is skipped.
    """

    def __init__(self):
        if not VISION_AVAILABLE:
            raise ImportError("Google Cloud Vision not installed")
        try:
            self.client = vision.ImageAnnotatorClient()
        except Exception as e:
            logging.error(f"Could not initialise Vision client: {e}")
            raise

    def extract(
        self,
        file_path: Path,
        sample_only: bool = True,
        max_pages: int = 1,          # kept for backwards compat; ignored when spread=True
        spread: bool = True,          # use spread sampling by default
        n_pages: int = 3,             # number of spread pages to sample
        page_indices: Optional[List[int]] = None,  # explicit 0-based indices override
    ) -> Dict:
        """
        Extract text from sampled pages via the Vision API.

        Returns a result dict with an extra 'page_texts' key:
            {page_idx (0-based): text_string}
        so the caller can compare against Docling's per-page output.
        """
        try:
            if file_path.suffix.lower() == '.pdf':
                all_images = convert_from_path(
                    str(file_path), dpi=300,
                    poppler_path='/usr/local/bin'
                )
            else:
                all_images = [Image.open(file_path)]

            total = len(all_images)

            # Determine which page indices to process
            if page_indices is not None:
                indices = [i for i in page_indices if 0 <= i < total]
            elif spread and sample_only:
                indices = _spread_indices(total, n_pages)
            elif sample_only:
                indices = list(range(min(max_pages, total)))
            else:
                indices = list(range(total))

            page_texts: Dict[int, str] = {}

            for idx in indices:
                img = all_images[idx]
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                content = buf.getvalue()

                image    = vision.Image(content=content)
                response = self.client.text_detection(image=image)

                if response.error.message:
                    logging.warning(f"Vision API error on page {idx}: {response.error.message}")
                    page_texts[idx] = ''
                elif response.text_annotations:
                    page_texts[idx] = response.text_annotations[0].description
                else:
                    page_texts[idx] = ''

            full_text  = '\n\n'.join(t for t in page_texts.values() if t)
            confidence = 0.9 if full_text else 0.0

            return {
                'text':       full_text,
                'confidence': confidence,
                'method':     'google_vision',
                'metadata': {
                    'page_count':   total,
                    'pages_sampled': indices,
                    'sample_only':  sample_only,
                    'success':      True,
                },
                'page_texts': page_texts,   # {0-based idx: text}
                'error': None,
            }

        except Exception as e:
            logging.error(f"Vision extraction failed for {file_path}: {e}")
            return {
                'text':       None,
                'confidence': 0.0,
                'method':     'google_vision',
                'metadata':   {'success': False},
                'page_texts': {},
                'error':      str(e),
            }
