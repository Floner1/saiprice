"""Read-side aggregation for the pipeline health dashboard.

Kept out of views.py so the maths is testable without a request, and out of
the management commands so the dashboard does not import a Command class.
"""

import statistics
from decimal import Decimal


def accuracy_metrics(pairs):
    """(mae_vnd, median_ape, n) from an iterable of (predicted, actual).

    Median APE rather than mean: it is the metric already reported for the
    shipped model, and with a log-price target a VND mean is dominated by a
    handful of very large listings. Rows with a null prediction or a null/zero
    actual price are excluded -- the zero check is also what keeps the division
    safe. An empty population returns nulls, never zeros: a zero would read as
    a perfect model rather than as "not measured".
    """
    usable = [(p, a) for p, a in pairs if p is not None and a]
    if not usable:
        return None, None, 0
    errors = [abs(p - a) for p, a in usable]
    ratios = [abs(p - a) / a for p, a in usable]
    mae = (sum(errors) / len(errors)).quantize(Decimal("1"))
    median_ape = statistics.median(ratios).quantize(Decimal("0.0001"))
    return mae, median_ape, len(usable)
