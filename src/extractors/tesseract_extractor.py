"""
Tesseract OCR extraction with auto-detected language support (Witness B).
"""
from pathlib import Path
from typing import Dict, Optional
import logging

try:
    import pytesseract
    from PIL import Image
    from pdf2image import convert_from_path

    # Tesseract is installed via Homebrew; ensure pytesseract can find it
    import shutil
    _tess_path = shutil.which('tesseract') or '/usr/local/bin/tesseract'
    pytesseract.pytesseract.tesseract_cmd = _tess_path

    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False
    logging.warning("Tesseract dependencies not installed")


class TesseractExtractor:
    """Extract text using Tesseract OCR with auto-detected language support."""

    # Default fallback when no language is provided or detected
    DEFAULT_LANG = 'eng'

    def __init__(self, lang: Optional[str] = None):
        """
        Initialize Tesseract extractor.

        Args:
            lang: Tesseract language string (e.g. 'eng+ara+fra').
                  When None, the default 'eng' is used as a fallback;
                  callers should supply the result of language_detector.detect_and_format()
                  for best results.
        """
        if not TESSERACT_AVAILABLE:
            raise ImportError("Tesseract not available")

        self.default_lang = lang or self.DEFAULT_LANG

        # Verify Tesseract binary is reachable
        try:
            available_langs = pytesseract.get_languages()
            logging.info(f"Tesseract available languages: {available_langs}")
        except Exception as e:
            logging.warning(f"Could not query Tesseract languages: {e}")

    def extract(self, file_path: Path, lang: Optional[str] = None) -> Dict:
        """
        Extract text using Tesseract OCR.

        Args:
            file_path: Path to PDF or image.
            lang:      Tesseract language string for *this document*.
                       Overrides the instance default when provided.
                       Pass the output of language_detector.detect_and_format()
                       after Docling has processed the file.

        Returns:
            {
                'text':       str,
                'confidence': float (0.0-1.0),
                'method':     'tesseract',
                'metadata':   dict,
                'error':      str or None
            }
        """
        active_lang = lang or self.default_lang

        try:
            # Convert PDF to images or load image directly
            if file_path.suffix.lower() == '.pdf':
                images = convert_from_path(str(file_path), dpi=300, poppler_path='/usr/local/bin')
            else:
                images = [Image.open(file_path)]

            texts       = []
            confidences = []

            for img in images:
                text = pytesseract.image_to_string(
                    img,
                    lang=active_lang,
                    config='--psm 3',   # fully automatic page segmentation
                )
                texts.append(text)

                try:
                    data = pytesseract.image_to_data(
                        img,
                        lang=active_lang,
                        output_type=pytesseract.Output.DICT,
                    )
                    confs = [c for c in data['conf'] if c != -1]
                    if confs:
                        confidences.append(sum(confs) / len(confs))
                except Exception as e:
                    logging.warning(f"Could not get Tesseract confidence: {e}")
                    confidences.append(50.0)

            full_text = '\n\n'.join(texts)
            avg_conf  = (sum(confidences) / len(confidences)) if confidences else 0.0

            return {
                'text':       full_text,
                'confidence': avg_conf / 100.0,
                'method':     'tesseract',
                'metadata': {
                    'page_count': len(images),
                    'language':   active_lang,
                    'success':    True,
                },
                'error': None,
            }

        except Exception as e:
            logging.error(f"Tesseract extraction failed for {file_path}: {e}")
            return {
                'text':       None,
                'confidence': 0.0,
                'method':     'tesseract',
                'metadata':   {'success': False, 'language': active_lang},
                'error':      str(e),
            }


def main():
    """Test Tesseract extractor."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python tesseract_extractor.py <file.pdf> [lang]")
        sys.exit(1)

    file_path = Path(sys.argv[1])
    lang      = sys.argv[2] if len(sys.argv) > 2 else None

    extractor = TesseractExtractor(lang=lang)
    result    = extractor.extract(file_path, lang=lang)

    print(f"Method:     {result['method']}")
    print(f"Language:   {result['metadata'].get('language')}")
    print(f"Confidence: {result['confidence']:.2f}")
    print(f"Text length:{len(result['text']) if result['text'] else 0} chars")
    print(f"Error:      {result['error']}")

    if result['text']:
        print("\nFirst 200 chars:")
        print(result['text'][:200])


if __name__ == '__main__':
    main()
