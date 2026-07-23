from django.core.management.base import BaseCommand

from listings.models import Listing

LOW_PHOTOS_THRESHOLD = 3
STALE_LISTING_THRESHOLD_DAYS = 90


class Command(BaseCommand):
    help = (
        "Recompute anomaly flags over active listings (CLAUDE.md §12). "
        "low_photos + stale_listing; price_gap waits on the ML model."
    )

    def handle(self, *args, **options):
        scored = flagged = 0
        # §12: each rule scopes its own population from is_active=True.
        # anomaly_reason holds one key per rule that ran; a listing no rule
        # covers is left untouched, not written with an empty dict.
        for listing in Listing.objects.filter(is_active=True):
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
            if not reason:
                continue
            listing.is_anomaly = any(rule["triggered"] for rule in reason.values())
            listing.anomaly_reason = reason
            # update_fields: a scoring write must never touch last_seen_at
            listing.save(update_fields=["is_anomaly", "anomaly_reason"])
            scored += 1
            flagged += listing.is_anomaly
        self.stdout.write(f"scored={scored} flagged={flagged}")
