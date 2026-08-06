import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from listings.models import Listing, ScrapeRun
from listings.scraping.client import fetch
from listings.scraping.sites import alonhadat
from listings.upsert import upsert

logger = logging.getLogger(__name__)

# Codes that describe a page rather than a malfunction, so they must not
# inflate error_count. ldp_404/ldp_soft_gone mean the LDP is not real content;
# ldp_no_anchor/unmapped_breadcrumb are CLAUDE.md §8 nullable-field parse
# failures, which §8 states explicitly are neither a skip nor an error.
NON_ERROR_CODES = {"ldp_404", "ldp_soft_gone", "ldp_no_anchor", "unmapped_breadcrumb"}


def record(run, code):
    """Bump one status code on the run, and error_count unless it's benign.

    Each failure increments exactly one key, so status_counts values always
    sum to the number of failures.
    """
    counts = run.status_counts or {}
    counts[code] = counts.get(code, 0) + 1
    run.status_counts = counts
    if code not in NON_ERROR_CODES:
        run.error_count += 1
    logger.info(f"{run.source_site}: {code}")


def sweep_delistings(run):
    # Same blocked-crawl floor as the old batdongsan crawler, scoped per
    # source: a run that saw under half of its own source's prior run looks
    # broken, and sweeping after it would mass-delist the active table.
    prior = (
        ScrapeRun.objects.filter(source_site=run.source_site, finished_at__isnull=False)
        .exclude(pk=run.pk)
        .order_by("-started_at")
        .first()
    )
    if prior and run.listings_seen < prior.listings_seen / 2:
        logger.warning(
            f"skipping delist sweep: listings_seen={run.listings_seen} is under "
            f"half of prior run's {prior.listings_seen} -- crawl looks broken"
        )
        return
    Listing.objects.filter(
        source_site=run.source_site,
        is_active=True,
        last_seen_at__lt=run.started_at,
    ).update(is_active=False, delisted_at=run.finished_at)


def check_posted_date_nulls(run):
    # A renamed date label/attribute parses to null with no error (§8's
    # nullable-field path), so error_count stays clean while §7's full
    # overwrite nulls posted_date across the whole source. Warn on the run
    # where the null rate first jumps past 80%; stay quiet while the prior
    # run was already that broken (chronic, e.g. a source that never
    # exposes dates), so a break warns once instead of every run forever.
    if not run.listings_seen:
        return
    rate = run.posted_date_nulls / run.listings_seen
    if rate < 0.8:
        return
    prior = (
        ScrapeRun.objects.filter(source_site=run.source_site, finished_at__isnull=False)
        .exclude(pk=run.pk)
        .order_by("-started_at")
        .first()
    )
    if prior and prior.listings_seen and prior.posted_date_nulls / prior.listings_seen >= 0.8:
        return
    logger.warning(
        f"posted_date null rate {rate:.0%} ({run.posted_date_nulls}/{run.listings_seen}) "
        f"for {run.source_site} -- date parsing looks broken (label/markup change?)"
    )


class Command(BaseCommand):
    help = (
        "SRP crawl of the alonhadat HCMC tracked scope with plain requests "
        "(no browser, CLAUDE.md §6), upsert per §7. One run = one source."
    )

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, choices=["alonhadat"])
        parser.add_argument(
            "--pages",
            type=int,
            default=None,
            help="cap pages per category root; a capped run is a partial crawl, "
            "so it skips the delisting sweep",
        )
        # ponytail: default 20 because the exact request count that trips
        # alonhadat's robot-verify wall is unknown (~30 sequential LDP
        # fetches triggered it once); raise only after a run proves safe.
        parser.add_argument(
            "--max-ldp-visits",
            type=int,
            default=20,
            help="cap actual LDP fetches per run; items past the cap keep "
            "their SRP fields, skip enrichment this run, and stay eligible "
            "next run (null images)",
        )
        parser.add_argument(
            "--no-ldp-enrich",
            action="store_true",
            help="SRP-only crawl: no LDP fetches at all; takes precedence "
            "over --max-ldp-visits",
        )

    # class default so direct _enrich_from_ldp calls (tests) work without
    # handle(); handle() overwrites it per run from the CLI flags
    ldp_budget = 20

    def _enrich_from_ldp(self, item, run):
        # SRP category is provisional (vip cards can be cross-category), so
        # property_type/listing_intent are only ever written from the LDP
        # breadcrumb. LDP is visited once, on first insert; images IS NULL
        # marks a failed/never-done visit and gets retried next pass.
        row = (
            Listing.objects.filter(
                source_site=item.source_site, source_id=item.source_id
            )
            .values_list("pk", "images")
            .first()
        )
        if row:
            item.fields.pop("property_type", None)
            item.fields.pop("listing_intent", None)
            if row[1] is not None:
                return
        # budget gate sits here, not around the call: the pop above must run
        # for every existing row even when no LDP fetch is allowed, or upsert
        # would overwrite LDP-confirmed types with the provisional SRP
        # category. Skipping is not an error; the row self-heals next run.
        if self.ldp_budget <= 0:
            return
        self.ldp_budget -= 1
        response, error = fetch(item.fields["url"])
        if error:
            # http_404 -> ldp_404, bot_challenge -> ldp_bot_challenge, etc.
            code = "ldp_404" if error == "http_404" else f"ldp_{error}"
            self.stderr.write(f"ldp {error} for {item.fields['url']}")
            record(run, code)
            return
        extras = alonhadat.parse_ldp_extras(response.text)
        if extras["soft_gone"]:
            # A placeholder shell, not a listing: leave images null so the row
            # stays retry-eligible instead of parsing as a zero-photo LDP. No
            # delisting -- the SRP listed it this run, and upsert() reactivates
            # unconditionally moments later anyway.
            self.stderr.write(f"soft-gone placeholder at {item.fields['url']}")
            record(run, "ldp_soft_gone")
            return
        if extras["images"] is None:
            self.stderr.write(
                f"no article.property anchor on {item.fields['url']}; "
                "images left null for retry (markup change?)"
            )
            record(run, "ldp_no_anchor")
        item.fields["images"] = extras["images"]
        if extras["property_type"]:
            item.fields["property_type"] = extras["property_type"]
            item.fields["listing_intent"] = extras["listing_intent"]
        else:
            self.stderr.write(
                f"unmapped breadcrumb category on {item.fields['url']}, "
                "property_type left as-is"
            )
            record(run, "unmapped_breadcrumb")

    def handle(self, *args, **options):
        run = ScrapeRun.objects.create(
            source_site=options["source"], started_at=timezone.now()
        )
        self.ldp_budget = 0 if options["no_ldp_enrich"] else options["max_ldp_visits"]
        max_visits = self.ldp_budget
        seen = set()
        duplicates = 0
        # try/finally, not try/except: an unexpected exception must still
        # surface, but the run's counters must survive it. Before this, `run`
        # was saved only on the last line, so anything raising mid-loop
        # discarded the whole run's numbers rather than just the tail.
        try:
            for root, (property_type, listing_intent) in alonhadat.CATEGORY_ROOTS.items():
                page = 1
                while options["pages"] is None or page <= options["pages"]:
                    response, error = fetch(alonhadat.page_url(root, page))
                    if error:
                        record(run, f"srp_{error}")
                        break
                    parsed, skips = alonhadat.parse_srp(
                        response.text, property_type, listing_intent
                    )
                    for ref, field in skips:
                        self.stderr.write(
                            f"skipped {ref}: required field missing: {field}"
                        )
                        run.skipped += 1
                        record(run, "required_field_missing")
                    # out-of-range trang-N pages re-serve earlier content, so a
                    # page with no unseen ids means the category is exhausted
                    new = [p for p in parsed if p.source_id not in seen]
                    duplicates += len(parsed) - len(new)
                    if not new:
                        break
                    for item in new:
                        seen.add(item.source_id)
                        run.listings_seen += 1
                        if item.fields.get("posted_date") is None:
                            run.posted_date_nulls += 1
                        try:
                            self._enrich_from_ldp(item, run)
                            if upsert(item):
                                run.inserted += 1
                            else:
                                run.updated += 1
                        except Exception as exc:
                            self.stderr.write(f"error {item.fields['url']}: {exc}")
                            record(run, "upsert_exception")
                    page += 1
            # set before the sweep: sweep_delistings stamps delisted_at from it
            run.finished_at = timezone.now()
            if options["pages"] is None:
                sweep_delistings(run)
        except BaseException:
            # A partial crawl must not sweep, so the sweep above sits inside
            # the try. run_aborted only covers Python-level aborts; a hard kill
            # (0xC000013A) leaves finished_at null, which the reader side reads
            # as aborted instead.
            record(run, "run_aborted")
            raise
        finally:
            run.finished_at = run.finished_at or timezone.now()
            check_posted_date_nulls(run)
            run.save()
        self.stdout.write(
            f"seen={run.listings_seen} inserted={run.inserted} "
            f"updated={run.updated} skipped={run.skipped} "
            f"duplicates={duplicates} errors={run.error_count} "
            f"ldp_visits={max_visits - self.ldp_budget} "
            f"status={run.status_counts or '{}'}"
        )
