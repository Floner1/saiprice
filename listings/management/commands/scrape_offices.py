from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from listings.models import OfficeBuilding, OfficeRentHistory
from listings.scraping.client import fetch
from listings.scraping.parsers import RequiredFieldMissing
from listings.scraping.sites import maisonoffice


def save_building(fields):
    """Upsert one building; True if it was new.

    A rent history row goes in on first sight and whenever the stored range
    differs, compared before the overwrite -- the same order as upsert().
    """
    now = timezone.now()
    existing = OfficeBuilding.objects.filter(source_id=fields["source_id"]).first()
    building, created = OfficeBuilding.objects.update_or_create(
        source_id=fields["source_id"],
        defaults={
            **{k: v for k, v in fields.items() if k != "source_id"},
            "last_seen_at": now,
            "is_active": True,
            "delisted_at": None,
        },
    )
    rent = (fields["rent_min_usd"], fields["rent_max_usd"])
    if created or rent != (existing.rent_min_usd, existing.rent_max_usd):
        OfficeRentHistory.objects.create(
            building=building, rent_min_usd=rent[0], rent_max_usd=rent[1], observed_at=now
        )
    return created


class Command(BaseCommand):
    help = (
        "Office buildings from maisonoffice.vn (rent quotes in USD/m2/month), "
        "discovered through the sitemap. Separate from the residential pipeline."
    )

    def add_arguments(self, parser):
        parser.add_argument("--districts", help="comma-separated URL slugs, e.g. quan-1,quan-3")
        parser.add_argument("--per-district", type=int, help="cap buildings per district")
        parser.add_argument("--dry-run", action="store_true", help="print parsed fields, write nothing")

    def _sitemap(self, url):
        # A missing sitemap aborts: discovery has to be complete or not at all.
        response, error = fetch(url)
        if error:
            raise CommandError(f"sitemap {url}: {error}")
        return maisonoffice.parse_sitemap(response.content)

    def handle(self, *args, **options):
        wanted = set(options["districts"].split(",")) if options["districts"] else None
        cap = options["per_district"]
        entries = {}
        for sitemap_url, _ in self._sitemap(maisonoffice.SITEMAP_INDEX):
            if maisonoffice.HCM_SITEMAP.search(sitemap_url):
                for loc, lastmod in self._sitemap(sitemap_url):
                    # 13 live URLs appear in two sitemap files
                    entries.setdefault(loc, lastmod)

        taken = Counter()
        counts = Counter()
        for url, lastmod in entries.items():
            slug = maisonoffice.district_slug(url)
            if slug is None or (wanted and slug not in wanted):
                continue
            if cap and taken[slug] >= cap:
                continue
            taken[slug] += 1
            response, error = fetch(url)
            if error:
                counts[f"page_{error}"] += 1
                self.stderr.write(f"{error} for {url}")
                continue
            try:
                fields, notes = maisonoffice.parse_building(response.text, url)
            except RequiredFieldMissing as exc:
                counts["required_field_missing"] += 1
                self.stderr.write(f"skipped {url}: {exc}")
                continue
            try:
                fields["source_modified_at"] = datetime.fromisoformat(lastmod) if lastmod else None
            except ValueError:
                # A bad date costs the date, not the run.
                fields["source_modified_at"] = None
                counts["bad_lastmod"] += 1
                self.stderr.write(f"unparseable lastmod {lastmod!r} for {url}")
            counts["parsed"] += 1
            counts.update(notes)
            self.stdout.write(self._line(fields, notes))
            if not options["dry_run"]:
                # Same as the residential crawl: a row that won't save is
                # counted and logged, and the run carries on.
                try:
                    counts["inserted" if save_building(fields) else "updated"] += 1
                except Exception as exc:
                    counts["save_exception"] += 1
                    self.stderr.write(f"error saving {url}: {exc}")
        self.stdout.write(f"summary: {dict(counts)}")

    def _line(self, fields, notes):
        parts = [
            fields["source_id"],
            fields["district"] or "-",
            fields["grade"] or "-",
            f"rent {fields['rent_min_usd']}-{fields['rent_max_usd']}",
            f"fee {fields['service_fee_usd']}",
            f"floor {fields['typical_floor_sqm']}",
            fields["ward"] or "-",
            fields["name"],
        ]
        if notes:
            parts.append(f"notes={notes}")
        return " | ".join(parts)
