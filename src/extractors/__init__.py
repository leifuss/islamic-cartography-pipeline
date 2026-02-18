"""
Text extraction witnesses for multi-method comparison.
"""
from .docling_extractor import DoclingExtractor
from .tesseract_extractor import TesseractExtractor
from .vision_extractor import VisionExtractor

__all__ = ['DoclingExtractor', 'TesseractExtractor', 'VisionExtractor']
