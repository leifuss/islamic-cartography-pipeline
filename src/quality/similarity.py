"""
Compute similarity between witness texts.
Uses normalized Levenshtein distance to handle Arabic diacritics.
"""
import Levenshtein
import unicodedata
import re
from typing import Dict

# Markdown patterns to strip before comparison
_MD_IMAGE    = re.compile(r'<!--.*?-->', re.DOTALL)   # <!-- image -->
_MD_HEADING  = re.compile(r'^#+\s*', re.MULTILINE)    # ## Heading
_MD_BOLD_IT  = re.compile(r'\*+|_{1,2}')              # **bold** / _italic_
_MD_TABLE    = re.compile(r'\|')                       # table pipes
_MD_HR       = re.compile(r'^-{3,}\s*$', re.MULTILINE) # ---


def strip_markdown(text: str) -> str:
    """Remove Markdown formatting so plain-text and Markdown outputs compare fairly."""
    text = _MD_IMAGE.sub(' ', text)
    text = _MD_HEADING.sub('', text)
    text = _MD_BOLD_IT.sub('', text)
    text = _MD_TABLE.sub(' ', text)
    text = _MD_HR.sub('', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def normalize_arabic_text(text: str) -> str:
    """
    Normalize Arabic text for fair comparison.

    Removes:
    - Diacritics (tashkeel) - OCR often misses these
    - Extra whitespace
    - Common OCR artifacts

    Args:
        text: Raw Arabic text

    Returns:
        Normalized text suitable for comparison
    """
    if not text:
        return ""

    # Remove Arabic diacritics (combining marks)
    # These are often inconsistent between OCR methods
    text = ''.join(
        c for c in unicodedata.normalize('NFD', text)
        if unicodedata.category(c) != 'Mn'
    )

    # Normalize to NFC (composed form)
    text = unicodedata.normalize('NFC', text)

    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()

    return text


def compute_similarity(text1: str, text2: str) -> float:
    """
    Compute normalized similarity between two texts.

    Uses Levenshtein distance normalized by max length.

    Args:
        text1, text2: Texts to compare

    Returns:
        Similarity score 0.0 (completely different) to 1.0 (identical)
    """
    # Strip Markdown formatting then normalize for diacritics
    norm1 = normalize_arabic_text(strip_markdown(text1))
    norm2 = normalize_arabic_text(strip_markdown(text2))

    if not norm1 or not norm2:
        return 0.0

    # Levenshtein ratio (1.0 = identical, 0.0 = completely different)
    return Levenshtein.ratio(norm1, norm2)


def pairwise_similarities(witnesses: Dict[str, Dict]) -> Dict:
    """
    Compute all pairwise similarities between witnesses.

    Args:
        witnesses: Dict of {method_name: extraction_result}
                  where extraction_result has 'text' key

    Returns:
        {
            'pairs': {
                'docling_vs_tesseract': 0.85,
                'docling_vs_vision': 0.82,
                'tesseract_vs_vision': 0.88
            },
            'average': 0.85,
            'max': 0.88,
            'min': 0.82
        }
    """
    # Extract texts from successful extractions
    texts = {
        name: result.get('text', '')
        for name, result in witnesses.items()
        if result.get('text') and result.get('error') is None
    }

    if len(texts) < 2:
        return {
            'pairs': {},
            'average': 0.0,
            'max': 0.0,
            'min': 0.0,
            'witness_count': len(texts)
        }

    # Compute all pairs
    pairs = {}
    methods = list(texts.keys())

    for i, method1 in enumerate(methods):
        for method2 in methods[i+1:]:
            key = f"{method1}_vs_{method2}"
            similarity = compute_similarity(texts[method1], texts[method2])
            pairs[key] = similarity

    # Statistics
    similarities = list(pairs.values())

    return {
        'pairs': pairs,
        'average': sum(similarities) / len(similarities) if similarities else 0.0,
        'max': max(similarities) if similarities else 0.0,
        'min': min(similarities) if similarities else 0.0,
        'witness_count': len(texts)
    }


def main():
    """Test similarity computation."""
    # Test with sample Arabic texts
    text1 = "هذا نص تجريبي"
    text2 = "هذا نَص تَجريبي"  # With diacritics
    text3 = "نص مختلف تماما"

    print(f"Text 1: {text1}")
    print(f"Text 2: {text2}")
    print(f"Text 3: {text3}")
    print()

    print(f"Similarity 1 vs 2 (should be ~1.0): {compute_similarity(text1, text2):.3f}")
    print(f"Similarity 1 vs 3 (should be low): {compute_similarity(text1, text3):.3f}")
    print()

    # Test pairwise
    witnesses = {
        'method_a': {'text': text1, 'error': None},
        'method_b': {'text': text2, 'error': None},
        'method_c': {'text': text3, 'error': None}
    }

    result = pairwise_similarities(witnesses)
    print("Pairwise similarities:")
    for pair, score in result['pairs'].items():
        print(f"  {pair}: {score:.3f}")
    print(f"Average: {result['average']:.3f}")


if __name__ == '__main__':
    main()
