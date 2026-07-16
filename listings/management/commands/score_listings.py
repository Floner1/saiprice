from django.core.management.base import BaseCommand

from listings.models import Listing

LOW_PHOTOS_THRESHOLD = 3


class Command(BaseCommand):
    help = (
        "Recompute anomaly flags over active enriched listings (CLAUDE.md §12). "
        "low_photos only until the ML model exists."
    )

    def handle(self, *args, **options):
        scored = flagged = 0
        # images IS NULL means the LDP was never visited (scrape_listings),
        # not zero photos -- those rows are skipped, not flagged.
        for listing in Listing.objects.filter(is_active=True, images__isnull=False):
            count = len(listing.images)
            triggered = count < LOW_PHOTOS_THRESHOLD
            listing.is_anomaly = triggered
            listing.anomaly_reason = {
                "low_photos": {"triggered": triggered, "value": count}
            }
            # update_fields: a scoring write must never touch last_seen_at
            listing.save(update_fields=["is_anomaly", "anomaly_reason"])
            scored += 1
            flagged += triggered
        self.stdout.write(f"scored={scored} flagged={flagged}")
