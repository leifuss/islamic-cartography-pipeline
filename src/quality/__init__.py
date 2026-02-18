"""
Quality assessment for multi-witness extraction.
"""
from typing import Dict
from .similarity import pairwise_similarities
from .corruption_detector import detect_corruption


def compute_quality_metrics(witnesses: Dict[str, Dict]) -> Dict:
    """
    Compute comprehensive quality score from witness comparison.

    Combines:
    - Agreement between witnesses (similarity)
    - Cleanliness of output (corruption detection)

    Args:
        witnesses: {method_name: extraction_result}

    Returns:
        {
            'score': float (0.0-1.0 overall quality),
            'recommendation': 'auto_accept'|'flag'|'arbitrate'|'review',
            'agreement_score': float,
            'cleanliness_score': float,
            'similarity': {...},
            'corruption': {...}
        }
    """
    # Pairwise similarity between witnesses
    similarity = pairwise_similarities(witnesses)

    # Check each witness for corruption
    corruption_results = {}
    for name, result in witnesses.items():
        if result.get('text'):
            corruption_results[name] = detect_corruption(result['text'])

    # Best case corruption score (if ANY witness looks clean, we're OK)
    min_corruption = min(
        (c['corruption_score'] for c in corruption_results.values()),
        default=1.0
    )

    # Scores
    agreement_score = similarity['average']
    cleanliness_score = 1.0 - min_corruption

    # Overall score: weighted combination
    # Agreement is more important (70%) than cleanliness (30%)
    overall_score = (0.7 * agreement_score) + (0.3 * cleanliness_score)

    # Recommendation based on thresholds (from config.yaml)
    if overall_score >= 0.85:
        recommendation = 'auto_accept'
    elif overall_score >= 0.65:
        recommendation = 'flag'
    elif overall_score >= 0.40:
        recommendation = 'arbitrate'
    else:
        recommendation = 'review'

    return {
        'score': overall_score,
        'recommendation': recommendation,
        'agreement_score': agreement_score,
        'cleanliness_score': cleanliness_score,
        'similarity': similarity,
        'corruption': corruption_results
    }


__all__ = [
    'compute_quality_metrics',
    'pairwise_similarities',
    'detect_corruption'
]
