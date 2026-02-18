"""
Detect obvious corruption/gibberish in extracted text.
"""
import re
from typing import Dict


def detect_corruption(text: str, language: str = 'arabic') -> Dict:
    """
    Check for signs of corrupt/gibberish OCR output.

    Checks for:
    - Too little actual language content
    - Excessive symbols/artifacts
    - Repeated character sequences (OCR noise)
    - Fragmented words

    Args:
        text: Extracted text to check
        language: Expected language ('arabic' or other)

    Returns:
        {
            'is_corrupt': bool,
            'corruption_score': float (0.0=clean, 1.0=garbage),
            'issues': [list of problems found],
            'metrics': {detailed statistics}
        }
    """
    if not text or len(text.strip()) < 10:
        return {
            'is_corrupt': True,
            'corruption_score': 1.0,
            'issues': ['Text too short or empty'],
            'metrics': {'text_length': len(text) if text else 0}
        }

    issues = []
    metrics = {}

    # Arabic-specific checks
    if language == 'arabic':
        # Count Arabic characters
        arabic_pattern = r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]'
        arabic_chars = len(re.findall(arabic_pattern, text))
        total_chars = len(re.sub(r'\s', '', text))  # Exclude whitespace

        if total_chars > 0:
            arabic_ratio = arabic_chars / total_chars
            metrics['arabic_ratio'] = arabic_ratio

            if arabic_ratio < 0.5:
                issues.append(f'Low Arabic character ratio: {arabic_ratio:.2%}')

        # Check for excessive punctuation/symbols (OCR artifacts)
        symbol_pattern = r'[^\w\s\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]'
        symbol_count = len(re.findall(symbol_pattern, text))
        symbol_ratio = symbol_count / total_chars if total_chars > 0 else 0
        metrics['symbol_ratio'] = symbol_ratio

        if symbol_ratio > 0.3:
            issues.append(f'Excessive symbols/artifacts: {symbol_ratio:.2%}')

    # Check for very short "words" (fragmentation)
    words = text.split()
    if words:
        avg_word_length = sum(len(w) for w in words) / len(words)
        metrics['avg_word_length'] = avg_word_length
        metrics['word_count'] = len(words)

        if avg_word_length < 2:
            issues.append(f'Excessive fragmentation (avg word: {avg_word_length:.1f} chars)')

    # Check for repeated characters (OCR noise)
    repeated_pattern = r'(.)\1{4,}'  # Same char 5+ times
    repeated_matches = re.findall(repeated_pattern, text)
    if repeated_matches:
        issues.append(f'Repeated character sequences: {len(repeated_matches)} instances')
        metrics['repeated_sequences'] = len(repeated_matches)

    # Compute overall corruption score
    # Each issue contributes to score
    corruption_score = min(len(issues) / 4.0, 1.0)  # Cap at 1.0

    return {
        'is_corrupt': corruption_score > 0.5,
        'corruption_score': corruption_score,
        'issues': issues,
        'metrics': metrics
    }


def main():
    """Test corruption detection."""
    # Test clean Arabic text
    clean_text = """
    الخرائط الإسلامية هي وثائق تاريخية مهمة
    تعكس فهم المسلمين للعالم في القرون الوسطى
    """

    # Test corrupted text
    corrupted_text = "ا ل خ ر ا ئ ط ######### @@@@@"

    print("Clean text:")
    result = detect_corruption(clean_text, 'arabic')
    print(f"  Corrupt: {result['is_corrupt']}")
    print(f"  Score: {result['corruption_score']:.2f}")
    print(f"  Issues: {result['issues']}")
    print()

    print("Corrupted text:")
    result = detect_corruption(corrupted_text, 'arabic')
    print(f"  Corrupt: {result['is_corrupt']}")
    print(f"  Score: {result['corruption_score']:.2f}")
    print(f"  Issues: {result['issues']}")


if __name__ == '__main__':
    main()
