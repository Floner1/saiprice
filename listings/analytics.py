"""Read-side aggregation for the pipeline health dashboard.

Kept out of views.py so the maths is testable without a request, and out of
the management commands so the dashboard does not import a Command class.
"""

import statistics
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from listings.models import ScoringRun, ScrapeRun

# A hard kill (0xC000013A, CLAUDE.md §9) leaves finished_at null with no chance
# to record anything, so elapsed time is the only available evidence. Six hours
# is comfortably longer than any real run and shorter than the daily cadence.
ABORT_AFTER = timedelta(hours=6)


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


def scrapes_per_day(days=30, now=None):
    """One dict per calendar day, oldest first, with no gaps.

    Days with no run are returned as zero-volume rows rather than omitted: a
    gap in the crawl is the signal the chart exists to show, and dropping the
    row hides it. Bucketing is by TruncDate on started_at, which uses the
    project timezone (UTC).
    """
    now = now or timezone.now()
    start = timezone.localdate(now) - timedelta(days=days - 1)
    rows = (
        ScrapeRun.objects.filter(started_at__date__gte=start)
        .annotate(day=TruncDate("started_at"))
        .values("day")
        .annotate(
            seen=Sum("listings_seen"),
            runs=Count("id"),
            finished=Count("id", filter=Q(finished_at__isnull=False)),
            errors=Sum("error_count"),
        )
    )
    by_day = {row["day"]: row for row in rows}
    out = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        row = by_day.get(day)
        if row is None:
            out.append(
                {"day": day, "seen": 0, "runs": 0, "errors": 0, "status": "no run"}
            )
            continue
        if not row["finished"]:
            status = "aborted"
        elif not row["seen"]:
            status = "empty"
        elif row["errors"]:
            status = f"ok, {row['errors']} errors"
        else:
            status = "ok"
        out.append(
            {
                "day": day,
                "seen": row["seen"] or 0,
                "runs": row["runs"],
                "errors": row["errors"] or 0,
                "status": status,
            }
        )
    return out


def with_bar_pct(rows, key):
    """Add a `pct` to each row, scaled so the largest value is 100."""
    top = max((row[key] or 0 for row in rows), default=0)
    for row in rows:
        row["pct"] = round(float(row[key] or 0) / float(top) * 100, 1) if top else 0
    return rows


def run_status(run, now=None):
    """Plain-language status for one ScrapeRun or ScoringRun row.

    getattr with a default of 1 is what lets this serve both models: ScoringRun
    has no listings_seen, so the "empty" branch stays scrape-only.
    """
    now = now or timezone.now()
    if run.finished_at is None:
        return "aborted" if now - run.started_at > ABORT_AFTER else "running"
    if not getattr(run, "listings_seen", 1):
        return "empty"
    if run.error_count:
        return f"ok, {run.error_count} error{'s' if run.error_count != 1 else ''}"
    return "ok"


def accuracy_trend(limit=30):
    """Scoring runs that produced a metric, oldest first for left-to-right reading."""
    runs = list(
        ScoringRun.objects.filter(median_ape__isnull=False).order_by("-started_at")[
            :limit
        ]
    )
    runs.reverse()
    rows = []
    previous = None
    for run in runs:
        rows.append(
            {
                "started_at": run.started_at,
                "median_ape": run.median_ape,
                "median_ape_pct": float(run.median_ape) * 100,
                "n_compared": run.n_compared,
                "fingerprint": run.model_fingerprint,
                "new_model": previous is not None
                and run.model_fingerprint != previous,
            }
        )
        previous = run.model_fingerprint
    return rows


def recent_runs(model, limit=15):
    """Last N runs of either model, newest first, each with a status string."""
    runs = list(model.objects.order_by("-started_at")[:limit])
    return [{"run": run, "status": run_status(run)} for run in runs]
