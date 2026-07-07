import logging
import random
import time

from django.core.management.base import BaseCommand
from django.utils import timezone
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from listings.models import Listing, ScrapeRun
from listings.scraping.parsers import (
    RequiredFieldMissing,
    parse_ldp,
    parse_srp,
    parse_srp_total_pages,
)
from listings.upsert import upsert

logger = logging.getLogger(__name__)

BASE_URL = "https://batdongsan.com.vn"
# ponytail: launch scope is the two confirmed roots only (2026-07-07).
# The other six tracked-scope slugs were cut -- apartment-sale alone is
# 809 pages, so the original "hundreds to low thousands" sizing was wrong.
# Re-add roots one at a time once a two-root run is proven stable.
SRP_ROOTS = [
    "/ban-can-ho-chung-cu-tp-hcm",
    "/ban-nha-rieng-tp-hcm",
]
LISTING_LINK_SELECTOR = "a.js__product-link-for-product-id"
LDP_TITLE_SELECTOR = "h1.re__pr-title"


def fetch_page(page, url, selector):
    # Playwright analog of the old client.fetch: same 3 attempts and 2s/4s/8s
    # backoff per CLAUDE.md §8, catching Playwright's Error (TimeoutError
    # subclasses it). The selector wait rides out Cloudflare's vanilla JS
    # challenge; no stealth of any kind. Serves LDPs too since 2026-07-07:
    # plain requests now gets 403 on LDPs while this headed page gets 200
    # from the same IP in the same minute.
    # Returns (status, html). (404, None) is a delisting signal for LDPs;
    # (None, None) means gave up after 3 attempts.
    for attempt in range(3):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=10_000)
            if response is not None and response.status == 404:
                time.sleep(random.uniform(1, 3))
                return 404, None
            if response is not None and response.status == 429:
                retry_after = response.headers.get("retry-after")
                time.sleep(float(retry_after) if retry_after else 2**attempt * 2)
                continue
            if response is not None and response.status >= 500:
                time.sleep(2**attempt * 2)
                continue
            page.wait_for_selector(selector, timeout=10_000)
        except PlaywrightError:
            time.sleep(2**attempt * 2)
            continue
        time.sleep(random.uniform(1, 3))
        return 200, page.content()

    logger.error(f"gave up on {url} after 3 attempts")
    return None, None


def crawl_srps(page, run):
    ldp_urls = []
    seen = set()
    for root in SRP_ROOTS:
        _, first_html = fetch_page(page, f"{BASE_URL}{root}", LISTING_LINK_SELECTOR)
        if first_html is None:
            run.error_count += 1
            continue
        total_pages = parse_srp_total_pages(first_html)
        for number in range(1, total_pages + 1):
            if number == 1:
                html = first_html
            else:
                _, html = fetch_page(
                    page, f"{BASE_URL}{root}/p{number}", LISTING_LINK_SELECTOR
                )
                if html is None:
                    run.error_count += 1
                    continue
            for url in parse_srp(html):
                if url not in seen:
                    seen.add(url)
                    ldp_urls.append(url)
    return ldp_urls


def process_listing(page, url, run):
    status, html = fetch_page(page, url, LDP_TITLE_SELECTOR)
    if status == 404:
        Listing.objects.filter(source_site="batdongsan", url=url).update(
            is_active=False, delisted_at=timezone.now()
        )
        return
    if html is None:
        run.error_count += 1
        return
    try:
        parsed = parse_ldp(html, url)
    except RequiredFieldMissing as exc:
        logger.warning(f"skipped {url}: {exc}")
        run.skipped += 1
        return
    if parsed.expired:
        Listing.objects.filter(
            source_site=parsed.source_site, source_id=parsed.source_id
        ).update(is_active=False, delisted_at=timezone.now())
        return
    if upsert(parsed):
        run.inserted += 1
    else:
        run.updated += 1


def sweep_delistings(run):
    # A Cloudflare-blocked crawl sees a fraction of the real scope; sweeping
    # after one would mass-delist the active table. Floor: skip the sweep when
    # this run saw under half of what the prior finished run saw. First run
    # (no prior) sweeps unconditionally.
    # ponytail: baseline is the single prior finished run, per spec. Two
    # blocked runs in a row collapse the baseline; switch to max(listings_seen)
    # over the last few runs if that ever bites.
    prior = (
        ScrapeRun.objects.filter(finished_at__isnull=False)
        .exclude(pk=run.pk)
        .order_by("-started_at")
        .first()
    )
    if prior and run.listings_seen < prior.listings_seen / 2:
        logger.warning(
            f"skipping delist sweep: listings_seen={run.listings_seen} is under "
            f"half of prior run's {prior.listings_seen} -- crawl looks blocked"
        )
        return
    Listing.objects.filter(
        source_site="batdongsan",
        is_active=True,
        last_seen_at__lt=run.started_at,
    ).update(is_active=False, delisted_at=run.finished_at)


class Command(BaseCommand):
    help = (
        "Full crawl of the launch batdongsan HCMC scope: SRP pagination and "
        "LDP fetches both via one vanilla local Playwright browser, upsert per "
        "CLAUDE.md §7. Local-only; never deployed to Render."
    )

    def handle(self, *args, **options):
        run = ScrapeRun.objects.create(started_at=timezone.now())
        with sync_playwright() as playwright:
            # headless=False is required: headless Chromium never clears
            # Cloudflare's challenge (403 "Just a moment...", verified live
            # 2026-07-07); a headed vanilla window passes with no evasion.
            # Task Scheduler must run this in a logged-on interactive session.
            browser = playwright.chromium.launch(headless=False)
            page = browser.new_page()
            ldp_urls = crawl_srps(page, run)
            run.listings_seen = len(ldp_urls)
            for url in ldp_urls:
                process_listing(page, url, run)
            browser.close()
        run.finished_at = timezone.now()
        sweep_delistings(run)
        run.save()
        self.stdout.write(
            f"seen={run.listings_seen} inserted={run.inserted} "
            f"updated={run.updated} skipped={run.skipped} errors={run.error_count}"
        )
