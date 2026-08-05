from collections import Counter
from pathlib import Path

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from listings.models import Agent, Listing

REPORT_DIR = "stats_reports"


def _pct(values, p):
    return None if not values else round(float(np.percentile(values, p)))


class Command(BaseCommand):
    help = (
        "Snapshot the market-statistics numbers (price percentiles, per-district "
        "price/sqm, anomaly counts, duplicate groups) that a research writeup "
        "needs. Previously these only ever existed inside a throwaway shell "
        "session, so a past snapshot could never be re-checked. Writes a "
        f"timestamped file to {REPORT_DIR}/, same pattern as scraper_run_reports."
    )

    def handle(self, *args, **options):
        # Windows console defaults to cp1252, which can't print district
        # names like "Quận 7". The file write below is UTF-8 regardless;
        # this only fixes the stdout echo.
        if hasattr(self.stdout._out, "reconfigure"):
            self.stdout._out.reconfigure(encoding="utf-8", errors="replace")

        lines = [f"stats snapshot: {timezone.now().isoformat()}"]

        total = Listing.objects.count()
        active = Listing.objects.filter(is_active=True)
        active_count = active.count()
        lines.append(f"\ntotal listings: {total}")
        lines.append(f"active: {active_count}  inactive: {total - active_count}")

        lines.append("source_site (active): " + str(Counter(active.values_list("source_site", flat=True))))
        lines.append("property_type (active): " + str(Counter(active.values_list("property_type", flat=True))))
        lines.append("listing_intent (active): " + str(Counter(active.values_list("listing_intent", flat=True))))

        # Price/sqm is heavily right-skewed (CLAUDE.md ML target notes), so
        # percentiles are reported, not a mean.
        sale = active.filter(listing_intent="sale", price__isnull=False)
        prices = [float(v) for v in sale.values_list("price", flat=True)]
        pps = [float(v) for v in sale.exclude(price_per_sqm__isnull=True).values_list("price_per_sqm", flat=True)]
        areas = [float(v) for v in sale.exclude(area_sqm__isnull=True).values_list("area_sqm", flat=True)]
        lines.append(f"\nsale listings with price: {len(prices)}")
        if prices:
            lines.append(f"price p10/median/p90 (VND): {_pct(prices, 10):,} / {_pct(prices, 50):,} / {_pct(prices, 90):,}")
        if pps:
            lines.append(f"price_per_sqm median (VND): {_pct(pps, 50):,}")
        if areas:
            lines.append(f"area_sqm median: {_pct(areas, 50)}")

        lines.append("\nper-district (active sale listings, district + price_per_sqm both set):")
        district_pps = {}
        by_district = sale.exclude(district__isnull=True).exclude(price_per_sqm__isnull=True)
        for district, value in by_district.values_list("district", "price_per_sqm"):
            district_pps.setdefault(district, []).append(float(value))
        for district, values in sorted(district_pps.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  {district}: {len(values)} listings, avg {sum(values) / len(values):,.1f} VND/sqm")

        agent_count = Agent.objects.filter(listing__is_active=True).distinct().count()
        lines.append(f"\ndistinct agents (active listings): {agent_count}")

        flagged = active.filter(is_anomaly=True).count()
        images_null = active.filter(images__isnull=True).count()
        lines.append(f"\nanomalies flagged (active): {flagged}")
        lines.append(f"images: null={images_null} populated={active_count - images_null}")

        multi_scraped = Listing.objects.annotate(ph_count=Count("pricehistory")).filter(ph_count__gt=1).count()
        lines.append(f"\nlistings with >1 PriceHistory row (all listings): {multi_scraped}")

        dupe_counter = Counter(active.values_list("title", "area_sqm", "price"))
        dupe_groups = {k: v for k, v in dupe_counter.items() if v > 1}
        lines.append(
            f"\nduplicate title+area+price groups (active): {len(dupe_groups)} groups, "
            f"{sum(dupe_groups.values())} rows"
        )

        text = "\n".join(lines) + "\n"
        self.stdout.write(text)

        out_dir = Path(settings.BASE_DIR) / REPORT_DIR
        out_dir.mkdir(exist_ok=True)
        filename = timezone.now().strftime("%Y-%m-%d_%H%M_stats.txt")
        (out_dir / filename).write_text(text, encoding="utf-8")
        self.stdout.write(f"written to {REPORT_DIR}/{filename}")
