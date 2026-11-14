from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from listings.district_analytics import MARKET_SOURCES, district_stats
from listings.models import DistrictSnapshot, Listing

# The date is the market's calendar day, not the server's. TIME_ZONE is UTC,
# so a run after 17:00 UTC would otherwise file under the previous day.
HCMC = ZoneInfo("Asia/Ho_Chi_Minh")


def _whole_vnd(value):
    return None if value is None else round(value)


def take_snapshot(source, snapshot_date, as_of):
    """Replace one source's rows for one date. Returns the number written."""
    rows = [
        DistrictSnapshot(
            snapshot_date=snapshot_date,
            source_site=source,
            district=s["district"],
            property_type=s["property_type"],
            listing_count=s["listing_count"],
            distinct_count=s["distinct_count"],
            median_price_per_sqm=_whole_vnd(s["median_price_per_sqm"]),
            median_price=_whole_vnd(s["median_price"]),
            p25_price_per_sqm=_whole_vnd(s["p25_price_per_sqm"]),
            p75_price_per_sqm=_whole_vnd(s["p75_price_per_sqm"]),
        )
        for s in district_stats(Listing.objects.filter(source_site=source), as_of)
    ]
    with transaction.atomic():
        DistrictSnapshot.objects.filter(
            snapshot_date=snapshot_date, source_site=source
        ).delete()
        DistrictSnapshot.objects.bulk_create(rows)
    return len(rows)


class Command(BaseCommand):
    help = "Store today's median asking price per m2 by district for each market source."

    def handle(self, *args, **options):
        as_of = timezone.now()
        snapshot_date = timezone.localdate(as_of, timezone=HCMC)
        for source in MARKET_SOURCES:
            written = take_snapshot(source, snapshot_date, as_of)
            self.stdout.write(f"{source}: {written} rows for {snapshot_date}")
