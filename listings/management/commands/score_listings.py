from decimal import Decimal

import pandas as pd
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from listings.ml.dataset import MAX_AREA_SQM, MAX_BEDROOMS
from listings.ml.predict import predict
from listings.models import Listing

LOW_PHOTOS_THRESHOLD = 3
STALE_LISTING_THRESHOLD_DAYS = 90


def _predictions():
    """{listing id: predicted whole VND} for the rows the model can score.

    The filters are the model's requirements, not a policy choice.
    `features.listing_age` has no null fallback, so a null posted_date reaches
    sklearn as NaN and raises; the fit is sale-only because rent prices are
    monthly totals (ml/dataset.py). A null district needs no filter --
    `district_median_price` falls back to the train-wide median.

    The two domain caps are imported from `dataset`, not restated, so scoring
    can never outgrow the set the model was fit on. Past them a random forest
    does not extrapolate, it returns its top leaf: measured on live data, a
    19,884 m2 / 100-bedroom building listed at 999 ty scored 31.2 ty, and 39 of
    802 rows were in that state. Null is the honest answer for those.
    """
    rows = list(
        Listing.objects.filter(
            is_active=True,
            listing_intent="sale",
            area_sqm__gt=0,
            posted_date__isnull=False,
        )
        .exclude(Q(area_sqm__gt=MAX_AREA_SQM) | Q(bedrooms__gt=MAX_BEDROOMS))
        .values("id", "district", "property_type", "area_sqm", "posted_date")
    )
    # Also keeps an empty run from loading the 7.5 MB pickle for nothing.
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    # area_sqm arrives as Decimal, which makes the column object-dtyped;
    # sklearn rejects that.
    frame["area_sqm"] = frame["area_sqm"].astype(float)
    return {
        row_id: Decimal(float(value)).quantize(Decimal("1"))
        for row_id, value in zip(frame["id"], predict(frame))
    }


class Command(BaseCommand):
    help = (
        "Score active listings: ML predicted_price (CLAUDE.md §13) plus the "
        "§12 anomaly rules. low_photos + stale_listing; price_gap not built."
    )

    def handle(self, *args, **options):
        # One timestamp for the whole run, so every row it touches agrees.
        now = timezone.now()
        predictions = _predictions()
        scored = flagged = 0
        # §12: each rule scopes its own population from is_active=True.
        # anomaly_reason holds one key per rule that ran; a listing no rule
        # covers is left untouched, not written with an empty dict.
        for listing in Listing.objects.filter(is_active=True):
            # update_fields is built per row, never a fixed list: a scoring
            # write must not touch last_seen_at, and §12 forbids stamping
            # predicted_at on a row the model didn't actually price.
            fields = []
            if listing.pk in predictions:
                listing.predicted_price = predictions[listing.pk]
                listing.predicted_at = now
                fields += ["predicted_price", "predicted_at"]
            elif listing.predicted_price is not None:
                # §12 calls predicted_price current-state output. A row that
                # left the population (area re-parsed past the cap, posted_date
                # nulled by a markup change) must not keep the last run's
                # number as though the model still stands behind it.
                listing.predicted_price = listing.predicted_at = None
                fields += ["predicted_price", "predicted_at"]

            reason = {}
            # images IS NULL means the LDP was never visited (scrape_listings),
            # not zero photos -- low_photos skips those rows.
            if listing.images is not None:
                count = len(listing.images)
                reason["low_photos"] = {
                    "triggered": count < LOW_PHOTOS_THRESHOLD,
                    "value": count,
                }
            days = listing.days_on_market
            if days is not None:
                reason["stale_listing"] = {
                    "triggered": days > STALE_LISTING_THRESHOLD_DAYS,
                    "value": days,
                }
            if reason:
                listing.is_anomaly = any(rule["triggered"] for rule in reason.values())
                listing.anomaly_reason = reason
                listing.anomaly_scored_at = now
                fields += ["is_anomaly", "anomaly_reason", "anomaly_scored_at"]
                scored += 1
                flagged += listing.is_anomaly

            if fields:
                listing.save(update_fields=fields)
        self.stdout.write(
            f"predicted={len(predictions)} scored={scored} flagged={flagged}"
        )
