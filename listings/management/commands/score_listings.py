import hashlib
import logging
from decimal import Decimal

import pandas as pd
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from listings.analytics import accuracy_metrics
from listings.ml.dataset import MAX_AREA_SQM, MAX_BEDROOMS
from listings.ml.predict import MODEL_PATH, load, predict
from listings.models import Listing, ScoringRun

logger = logging.getLogger(__name__)

LOW_PHOTOS_THRESHOLD = 3
STALE_LISTING_THRESHOLD_DAYS = 90


def record(statuses, code):
    statuses[code] = statuses.get(code, 0) + 1
    logger.error(f"score_listings: {code}")


def model_fingerprint():
    """First 12 hex of sha256(model.pkl), so a retrain is visible on the chart.

    None rather than a raise if the file is unreadable -- the same run already
    records model_load_failed, and a missing fingerprint must not be a second
    way for scoring to die.
    """
    try:
        return hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def _predictions(statuses):
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
    # load() is called explicitly, before predict() calls it internally, purely
    # so a missing/corrupt pickle is distinguishable from a bad input row. It
    # is @cache'd, so the second call inside predict() is free.
    try:
        load()
    except Exception:
        record(statuses, "model_load_failed")
        return {}
    frame = pd.DataFrame(rows)
    # area_sqm arrives as Decimal, which makes the column object-dtyped;
    # sklearn rejects that.
    frame["area_sqm"] = frame["area_sqm"].astype(float)
    try:
        values = predict(frame)
    except Exception:
        # A model failure must not take down low_photos/stale_listing, neither
        # of which needs the model. Empty dict, run continues.
        record(statuses, "inference_failed")
        return {}
    return {
        row_id: Decimal(float(value)).quantize(Decimal("1"))
        for row_id, value in zip(frame["id"], values)
    }


class Command(BaseCommand):
    help = (
        "Score active listings: ML predicted_price (CLAUDE.md §13) plus the "
        "§12 anomaly rules. low_photos + stale_listing; price_gap not built."
    )

    def handle(self, *args, **options):
        # One timestamp for the whole run, so every row it touches agrees.
        now = timezone.now()
        run = ScoringRun.objects.create(
            started_at=now, model_fingerprint=model_fingerprint()
        )
        statuses = {}
        scored = flagged = 0
        try:
            predictions = _predictions(statuses)
            # An empty dict means either "no row qualified" or "the model
            # failed". Only the first justifies clearing stored predictions:
            # nulling the whole table because model.pkl was unreadable would do
            # more damage than the crash this handler replaced.
            model_ran = not statuses.keys() & {"model_load_failed", "inference_failed"}
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
                elif model_ran and listing.predicted_price is not None:
                    # §12 calls predicted_price current-state output. A row that
                    # left the population (area re-parsed past the cap,
                    # posted_date nulled by a markup change) must not keep the
                    # last run's number as though the model still stands behind it.
                    listing.predicted_price = listing.predicted_at = None
                    fields += ["predicted_price", "predicted_at"]

                reason = {}
                # images IS NULL means the LDP was never visited
                # (scrape_listings), not zero photos -- low_photos skips those.
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
                    listing.is_anomaly = any(
                        rule["triggered"] for rule in reason.values()
                    )
                    listing.anomaly_reason = reason
                    listing.anomaly_scored_at = now
                    fields += ["is_anomaly", "anomaly_reason", "anomaly_scored_at"]

                if fields:
                    try:
                        listing.save(update_fields=fields)
                    except Exception as exc:
                        # One unwritable row must not abandon the rest of the
                        # table half-scored.
                        self.stderr.write(
                            f"save failed for listing {listing.pk}: {exc}"
                        )
                        record(statuses, "listing_save_failed")
                        continue
                    # counted after the write, so a row that failed to save is
                    # not reported as scored
                    if reason:
                        scored += 1
                        flagged += listing.is_anomaly

            run.predicted = len(predictions)
            run.scored = scored
            run.flagged = flagged
            run.mae_vnd, run.median_ape, run.n_compared = accuracy_metrics(
                Listing.objects.filter(
                    is_active=True,
                    listing_intent="sale",
                    predicted_price__isnull=False,
                    price__isnull=False,
                ).values_list("predicted_price", "price")
            )
        except BaseException:
            record(statuses, "run_aborted")
            raise
        finally:
            run.finished_at = timezone.now()
            run.status_counts = statuses or None
            run.error_count = sum(statuses.values())
            run.save()
        self.stdout.write(
            f"predicted={run.predicted} scored={run.scored} flagged={run.flagged} "
            f"compared={run.n_compared} median_ape={run.median_ape} "
            f"status={run.status_counts or '{}'}"
        )
