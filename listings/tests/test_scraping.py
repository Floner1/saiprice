import io
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from requests.exceptions import Timeout

from listings.management.commands import scrape_listings
from listings.management.commands.scrape_listings import sweep_delistings
from listings.models import Listing, ScrapeRun
from listings.scraping import client
from listings.scraping.parsers import ParsedListing, RequiredFieldMissing, parse_ldp
from listings.scraping.sites import alonhadat
from listings.tests.test_models import _make_listing

# ponytail: real sample HTML in testdata/ rather than a duplicated fixtures/
# copy. testdata/ is ignored except for the four files these tests read, which
# are committed -- see .gitignore. Adding a fixture means un-ignoring it there.
BASE_DIR = Path(__file__).resolve().parent.parent.parent / "testdata"


def _read(name):
    return (BASE_DIR / name).read_text(encoding="utf-8")


def _item(source_id="111"):
    return ParsedListing(
        source_site="alonhadat",
        source_id=source_id,
        agent_source_id=None,
        agent_name=None,
        expired=False,
        fields={
            "url": f"https://alonhadat.com.vn/x-{source_id}.html",
            "title": "t",
            "property_type": "apartment",
            "listing_intent": "sale",
        },
    )


class ParseLdpApartmentTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.parsed = parse_ldp(
            _read("ldp_apartment.txt"),
            "https://batdongsan.com.vn/fallback-url-pr46002296",
        )

    def test_identity_fields(self):
        self.assertEqual(self.parsed.source_site, "batdongsan")
        self.assertEqual(self.parsed.source_id, "46002296")

    def test_required_fields(self):
        f = self.parsed.fields
        self.assertEqual(f["property_type"], "apartment")
        self.assertEqual(f["listing_intent"], "sale")
        self.assertEqual(f["category_id_source"], 324)
        self.assertIn("pr46002296", f["url"])
        self.assertTrue(f["title"])

    def test_price_and_area(self):
        f = self.parsed.fields
        self.assertEqual(f["price"], Decimal("6280000000"))
        self.assertEqual(f["price_unit"], "tỷ")
        self.assertEqual(f["area_sqm"], Decimal("71.5"))
        self.assertEqual(f["price_per_sqm"], (f["price"] / f["area_sqm"]).quantize(Decimal("1")))

    def test_bedrooms_bathrooms(self):
        f = self.parsed.fields
        self.assertEqual(f["bedrooms"], 3)
        self.assertEqual(f["bathrooms"], 2)

    def test_address_district_ward(self):
        f = self.parsed.fields
        self.assertEqual(f["district"], "Quận 7")
        self.assertEqual(f["ward"], "Phường Tân Phong")
        self.assertEqual(f["district_id_source"], 59)
        self.assertEqual(f["ward_id_source"], 8773)

    def test_map_coords(self):
        f = self.parsed.fields
        self.assertEqual(f["map_lat"], Decimal("10.7336549238947"))
        self.assertEqual(f["map_lng"], Decimal("106.705968203542"))

    def test_agent(self):
        self.assertEqual(self.parsed.agent_source_id, "3490868")
        self.assertEqual(self.parsed.agent_name, "Tuấn")

    def test_expired_flag(self):
        self.assertFalse(self.parsed.expired)

    def test_phone_number_always_null(self):
        self.assertIsNone(self.parsed.fields["phone_number"])

    def test_posted_date(self):
        self.assertEqual(self.parsed.fields["posted_date"], date(2026, 7, 4))

    def test_images_and_video(self):
        f = self.parsed.fields
        self.assertGreaterEqual(len(f["images"]), 3)
        self.assertTrue(f["video_url"].endswith(".mp4"))

    def test_specs_raw_stored_as_is(self):
        specs = self.parsed.fields["specs_raw"]
        self.assertEqual(specs["Diện tích"], "71,5 m²")
        self.assertEqual(specs["Số phòng ngủ"], "3 phòng")

    def test_project_fields(self):
        f = self.parsed.fields
        self.assertEqual(f["project_name"], "Sky Garden 3")
        self.assertEqual(f["project_id_source"], "488")


class ParseLdpImagesSemanticsTests(SimpleTestCase):
    """images=[] means the gallery parsed with zero photos (scored by
    low_photos); images=None means the gallery container itself is missing
    (markup change / partial save) -- a §8 nullable parse failure, excluded
    from scoring. A video-only listing is the reachable real case for []."""

    def _parse_apartment_without(self, selector):
        soup = BeautifulSoup(_read("ldp_apartment.txt"), "html.parser")
        for el in soup.select(selector):
            el.decompose()
        return parse_ldp(str(soup), "https://batdongsan.com.vn/fallback-url-pr46002296")

    def test_empty_gallery_parses_to_empty_list_not_none(self):
        parsed = self._parse_apartment_without(
            ".re__media-preview .swiper-slide[data-filter='image']"
        )
        self.assertEqual(parsed.fields["images"], [])

    def test_missing_gallery_container_parses_to_none(self):
        parsed = self._parse_apartment_without(".re__media-preview")
        self.assertIsNone(parsed.fields["images"])


class ParseLdpLandTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.parsed = parse_ldp(
            _read("ldp_land.txt"),
            "https://batdongsan.com.vn/fallback-url-pr45990744",
        )

    def test_property_type_and_intent(self):
        f = self.parsed.fields
        self.assertEqual(f["property_type"], "land")
        self.assertEqual(f["listing_intent"], "rent")

    def test_land_has_no_bedrooms_bathrooms(self):
        f = self.parsed.fields
        self.assertIsNone(f["bedrooms"])
        self.assertIsNone(f["bathrooms"])

    def test_no_project_for_projectid_zero(self):
        f = self.parsed.fields
        self.assertIsNone(f["project_name"])
        self.assertIsNone(f["project_id_source"])

    def test_address_with_non_quan_district_label(self):
        # ponytail: district label isn't always "Quận" (e.g. "Thành phố");
        # parser takes the second-to-last comma segment either way.
        f = self.parsed.fields
        self.assertEqual(f["district"], "Thành phố Thuận An")
        self.assertEqual(f["ward"], "Phường Thuận Giao")


class ParseLdpRequiredFieldTests(SimpleTestCase):
    def test_unmapped_category_id_raises_required_field_missing(self):
        html = """
        <html><head>
        <link rel="canonical" href="https://batdongsan.com.vn/test-pr1"></link>
        <script>
        window.pageTrackingData = {
            ...JSON.parse('{"pageTrackingType":"LDP","products":[{"intent":"Sale","pageType":"LDP","productId":1,"projectId":0,"vipType":0,"verified":false,"expired":false,"cateId":999999,"cityCode":"SG","districtId":1,"wardId":1,"streetId":1,"pageId":1,"createByUser":1,"productType":1}]}'),
        };
        </script>
        </head><body><h1 class="re__pr-title">Test listing</h1></body></html>
        """
        with self.assertRaises(RequiredFieldMissing):
            parse_ldp(html, "https://batdongsan.com.vn/test-pr1")

    def test_missing_title_raises_required_field_missing(self):
        html = """
        <html><head>
        <script>
        window.pageTrackingData = {
            ...JSON.parse('{"pageTrackingType":"LDP","products":[{"intent":"Sale","pageType":"LDP","productId":1,"projectId":0,"vipType":0,"verified":false,"expired":false,"cateId":324,"cityCode":"SG","districtId":1,"wardId":1,"streetId":1,"pageId":1,"createByUser":1,"productType":1}]}'),
        };
        </script>
        </head><body></body></html>
        """
        with self.assertRaises(RequiredFieldMissing):
            parse_ldp(html, "https://batdongsan.com.vn/test-pr1")


class SweepDelistingsGuardTests(TestCase):
    def _run(self, listings_seen):
        now = timezone.now()
        return ScrapeRun.objects.create(
            source_site="alonhadat",
            started_at=now,
            finished_at=now,
            listings_seen=listings_seen,
        )

    def _stale_active_listing(self):
        return Listing.objects.create(
            source_site="alonhadat",
            source_id="sweep1",
            url="https://alonhadat.com.vn/sweep-1.html",
            title="Test",
            property_type="apartment",
            listing_intent="sale",
            last_seen_at=timezone.now() - timedelta(days=1),
        )

    def test_blocked_run_skips_sweep(self):
        self._run(1000)
        listing = self._stale_active_listing()
        # assertLogs also keeps the expected "crawl looks blocked" warning out
        # of the suite's console output, where it reads like a live incident
        with self.assertLogs(
            "listings.management.commands.scrape_listings", level="WARNING"
        ):
            sweep_delistings(self._run(3))
        listing.refresh_from_db()
        self.assertTrue(listing.is_active)
        self.assertIsNone(listing.delisted_at)

    def test_run_at_half_prior_count_sweeps(self):
        self._run(1000)
        listing = self._stale_active_listing()
        sweep_delistings(self._run(500))
        listing.refresh_from_db()
        self.assertFalse(listing.is_active)
        self.assertIsNotNone(listing.delisted_at)

    def test_first_run_with_no_prior_sweeps_unconditionally(self):
        listing = self._stale_active_listing()
        sweep_delistings(self._run(3))
        listing.refresh_from_db()
        self.assertFalse(listing.is_active)


class ParseAlonhadatSrpTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.listings, cls.skips = alonhadat.parse_srp(
            _read("alonhadat_srp_apartment.txt"), "apartment", "sale"
        )

    def test_all_cards_parse_with_required_fields(self):
        self.assertEqual(len(self.listings), 20)
        self.assertEqual(self.skips, [])
        for item in self.listings:
            self.assertEqual(item.source_site, "alonhadat")
            self.assertTrue(item.source_id.isdigit())
            self.assertTrue(item.fields["url"].startswith("https://alonhadat.com.vn/"))
            self.assertTrue(item.fields["title"])
            self.assertEqual(item.fields["property_type"], "apartment")
            self.assertEqual(item.fields["listing_intent"], "sale")

    def test_first_card_known_values(self):
        f = self.listings[0].fields
        self.assertEqual(f["price"], Decimal("40000000000"))
        self.assertEqual(f["price_unit"], "tỷ")
        self.assertEqual(f["area_sqm"], Decimal("90"))
        self.assertEqual(f["bedrooms"], 16)
        self.assertEqual(f["posted_date"], date(2026, 7, 10))
        self.assertEqual(f["district"], "Quận 5")
        self.assertEqual(f["ward"], "Phường Chợ Quán")
        self.assertEqual(f["vip_type"], "vip-2")
        self.assertEqual(f["price_per_sqm"], (f["price"] / f["area_sqm"]).quantize(Decimal("1")))

    def test_non_vip_card_has_null_vip_type(self):
        self.assertIsNone(self.listings[12].fields["vip_type"])

    def test_agent_memberid_on_every_card(self):
        for item in self.listings:
            self.assertTrue(item.agent_source_id)
            self.assertIsNone(item.agent_name)

    def test_missing_id_in_href_is_skipped_not_dropped(self):
        html = (
            '<article class="property-item"><a itemprop="url" href="/no-id-here.html">'
            '<h3 itemprop="name">t</h3></a></article>'
        )
        listings, skips = alonhadat.parse_srp(html, "apartment", "sale")
        self.assertEqual(listings, [])
        self.assertEqual(skips, [("/no-id-here.html", "source_id")])


class ParseAlonhadatLdpExtrasTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.extras = alonhadat.parse_ldp_extras(_read("alonhadat_ldp_apartment.txt"))

    def test_breadcrumb_confirms_property_type_and_intent(self):
        self.assertEqual(self.extras["property_type"], "apartment")
        self.assertEqual(self.extras["listing_intent"], "sale")

    def test_images_absolute_deduped_complete(self):
        self.assertEqual(len(self.extras["images"]), 5)
        for url in self.extras["images"]:
            self.assertTrue(url.startswith("https://alonhadat.com.vn/files/"))

    def test_rent_mirror_slug(self):
        html = (
            "<div itemscope itemtype='https://schema.org/BreadcrumbList'>"
            "<a href='/cho-thue-nha'>x</a></div>"
        )
        extras = alonhadat.parse_ldp_extras(html)
        self.assertEqual(extras["property_type"], "house")
        self.assertEqual(extras["listing_intent"], "rent")

    def test_unmapped_slug_and_missing_anchor(self):
        html = (
            "<div itemscope itemtype='https://schema.org/BreadcrumbList'>"
            "<a href='/can-ban-van-phong'>x</a></div>"
        )
        extras = alonhadat.parse_ldp_extras(html)
        self.assertIsNone(extras["property_type"])
        self.assertIsNone(extras["listing_intent"])
        self.assertIsNone(extras["images"])


class ParseAlonhadatLdpImagesSemanticsTests(SimpleTestCase):
    """Same null-vs-[] contract as batdongsan's parse_ldp: [] means the LDP
    parsed (article.property anchor present) with zero gallery photos --
    enrichment done, scored by low_photos; None means the anchor itself is
    missing (markup redesign / partial page), so images stays null, the
    enrichment gate retries next run, and score_listings skips the row."""

    def _parse_sample_without(self, selector):
        soup = BeautifulSoup(_read("alonhadat_ldp_apartment.txt"), "html.parser")
        for el in soup.select(selector):
            el.decompose()
        return alonhadat.parse_ldp_extras(str(soup))

    def test_empty_gallery_with_anchor_parses_to_empty_list(self):
        extras = self._parse_sample_without("article.property section.images img")
        self.assertEqual(extras["images"], [])

    def test_missing_structural_anchor_parses_images_to_none(self):
        extras = self._parse_sample_without("article.property")
        self.assertIsNone(extras["images"])


class EnrichFromLdpTests(TestCase):
    def _run_enrich(self, item, fetch_response):
        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now()
        )
        with patch.object(
            scrape_listings, "fetch", return_value=fetch_response
        ) as mock_fetch:
            # captured stderr keeps the expected "ldp fetch failed" line out
            # of the suite's console output, where it reads like a live incident
            scrape_listings.Command(stderr=io.StringIO())._enrich_from_ldp(item, run)
        return run, mock_fetch

    def test_new_listing_gets_breadcrumb_type_and_images(self):
        ldp_html = (
            "<div itemscope itemtype='https://schema.org/BreadcrumbList'>"
            "<a href='/can-ban-nha'>x</a></div>"
            "<article class='property'><section class='images'>"
            "<ul class='image-list'><li><img src='/files/a.jpg'></li></ul>"
            "</section></article>"
        )
        item = _item()
        run, mock_fetch = self._run_enrich(item, (Mock(text=ldp_html), None))
        mock_fetch.assert_called_once()
        self.assertEqual(item.fields["property_type"], "house")
        self.assertEqual(item.fields["images"], ["https://alonhadat.com.vn/files/a.jpg"])
        self.assertEqual(run.error_count, 0)

    def test_existing_row_with_images_skips_fetch_and_keeps_stored_type(self):
        Listing.objects.create(
            source_site="alonhadat",
            source_id="111",
            url="https://alonhadat.com.vn/x-111.html",
            title="t",
            property_type="house",
            listing_intent="sale",
            images=["https://alonhadat.com.vn/files/a.jpg"],
            last_seen_at=timezone.now(),
        )
        item = _item()
        run, mock_fetch = self._run_enrich(item, (None, "fetch_gave_up"))
        mock_fetch.assert_not_called()
        self.assertNotIn("property_type", item.fields)
        self.assertNotIn("listing_intent", item.fields)

    def test_existing_row_with_empty_gallery_counts_as_done_and_skips_fetch(self):
        Listing.objects.create(
            source_site="alonhadat",
            source_id="111",
            url="https://alonhadat.com.vn/x-111.html",
            title="t",
            property_type="house",
            listing_intent="sale",
            images=[],
            last_seen_at=timezone.now(),
        )
        item = _item()
        run, mock_fetch = self._run_enrich(item, (None, "fetch_gave_up"))
        mock_fetch.assert_not_called()

    def test_anchor_missing_ldp_leaves_images_null_for_retry_and_notes_it(self):
        Listing.objects.create(
            source_site="alonhadat",
            source_id="111",
            url="https://alonhadat.com.vn/x-111.html",
            title="t",
            property_type="house",
            listing_intent="sale",
            images=None,
            last_seen_at=timezone.now(),
        )
        item = _item()
        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now()
        )
        stderr = io.StringIO()
        with patch.object(
            scrape_listings,
            "fetch",
            return_value=(Mock(text="<html><body>redesigned</body></html>"), None),
        ) as mock_fetch:
            scrape_listings.Command(stderr=stderr)._enrich_from_ldp(item, run)
        mock_fetch.assert_called_once()
        self.assertIn("images", item.fields)
        self.assertIsNone(item.fields["images"])
        self.assertEqual(run.error_count, 0)
        self.assertIn("article.property", stderr.getvalue())

    def test_existing_row_without_images_refetches_but_failed_fetch_is_error(self):
        Listing.objects.create(
            source_site="alonhadat",
            source_id="111",
            url="https://alonhadat.com.vn/x-111.html",
            title="t",
            property_type="house",
            listing_intent="sale",
            images=None,
            last_seen_at=timezone.now(),
        )
        item = _item()
        run, mock_fetch = self._run_enrich(item, (None, "fetch_gave_up"))
        mock_fetch.assert_called_once()
        self.assertEqual(run.error_count, 1)
        self.assertNotIn("property_type", item.fields)
        self.assertNotIn("images", item.fields)


class LdpBudgetTests(TestCase):
    LDP_HTML = (
        "<div itemscope itemtype='https://schema.org/BreadcrumbList'>"
        "<a href='/can-ban-can-ho-chung-cu'>x</a></div>"
        "<article class='property'><section class='images'>"
        "<ul class='image-list'><li><img src='/files/a.jpg'></li></ul>"
        "</section></article>"
    )

    def _command_with_budget(self, budget):
        cmd = scrape_listings.Command()
        cmd.ldp_budget = budget
        return cmd

    def test_default_flag_values(self):
        parser = scrape_listings.Command().create_parser("manage.py", "scrape_listings")
        opts = parser.parse_args(["--source", "alonhadat"])
        self.assertEqual(opts.max_ldp_visits, 20)
        self.assertFalse(opts.no_ldp_enrich)

    def test_cap_stops_fetches_but_capped_items_keep_srp_fields(self):
        cmd = self._command_with_budget(1)
        run = ScrapeRun.objects.create(source_site="alonhadat", started_at=timezone.now())
        first, second = _item("111"), _item("222")
        with patch.object(
            scrape_listings, "fetch", return_value=(Mock(text=self.LDP_HTML), None)
        ) as mock_fetch:
            cmd._enrich_from_ldp(first, run)
            cmd._enrich_from_ldp(second, run)
        mock_fetch.assert_called_once_with(first.fields["url"])
        self.assertIn("images", first.fields)
        self.assertNotIn("images", second.fields)
        self.assertEqual(second.fields["property_type"], "apartment")
        self.assertEqual(second.fields["listing_intent"], "sale")
        self.assertEqual(run.error_count, 0)

    def test_already_enriched_rows_do_not_burn_budget(self):
        Listing.objects.create(
            source_site="alonhadat",
            source_id="111",
            url="https://alonhadat.com.vn/x-111.html",
            title="t",
            property_type="house",
            listing_intent="sale",
            images=["https://alonhadat.com.vn/files/a.jpg"],
            last_seen_at=timezone.now(),
        )
        cmd = self._command_with_budget(1)
        run = ScrapeRun.objects.create(source_site="alonhadat", started_at=timezone.now())
        with patch.object(
            scrape_listings, "fetch", return_value=(Mock(text=self.LDP_HTML), None)
        ) as mock_fetch:
            cmd._enrich_from_ldp(_item("111"), run)
            cmd._enrich_from_ldp(_item("222"), run)
        mock_fetch.assert_called_once_with("https://alonhadat.com.vn/x-222.html")

    def test_capped_existing_row_still_pops_provisional_srp_type(self):
        Listing.objects.create(
            source_site="alonhadat",
            source_id="111",
            url="https://alonhadat.com.vn/x-111.html",
            title="t",
            property_type="house",
            listing_intent="sale",
            images=None,
            last_seen_at=timezone.now(),
        )
        cmd = self._command_with_budget(0)
        run = ScrapeRun.objects.create(source_site="alonhadat", started_at=timezone.now())
        item = _item("111")
        with patch.object(scrape_listings, "fetch") as mock_fetch:
            cmd._enrich_from_ldp(item, run)
        mock_fetch.assert_not_called()
        self.assertNotIn("property_type", item.fields)
        self.assertNotIn("listing_intent", item.fields)
        self.assertEqual(run.error_count, 0)


class NoLdpEnrichHandleTests(TestCase):
    def test_srp_only_crawl_fires_zero_ldp_requests(self):
        srp = Mock(text=_read("alonhadat_srp_apartment.txt"))
        out = io.StringIO()
        with patch.object(scrape_listings, "fetch", return_value=(srp, None)) as mock_fetch:
            call_command(
                "scrape_listings",
                "--source", "alonhadat",
                "--no-ldp-enrich",
                "--max-ldp-visits", "5",
                stdout=out,
            )
        self.assertEqual(Listing.objects.count(), 20)
        self.assertEqual(Listing.objects.filter(images__isnull=True).count(), 20)
        for call in mock_fetch.call_args_list:
            self.assertIn("/can-ban-can-ho-chung-cu", call.args[0])
            self.assertFalse(call.args[0].endswith(".html"))
        self.assertIn("ldp_visits=0", out.getvalue())


class PostedDateNullCheckTests(TestCase):
    # Two valid cards with no [itemprop='datePosted'] at all: parses clean,
    # posted_date null on both, 100% null rate -> the break warning fires.
    SRP_HTML = (
        '<article class="property-item"><a itemprop="url" href="/x-111.html">'
        '<h3 itemprop="name">t</h3></a></article>'
        '<article class="property-item"><a itemprop="url" href="/x-222.html">'
        '<h3 itemprop="name">t</h3></a></article>'
    )

    def test_all_null_run_counts_and_warns(self):
        srp = Mock(text=self.SRP_HTML)
        with patch.object(scrape_listings, "fetch", return_value=(srp, None)):
            with self.assertLogs(
                "listings.management.commands.scrape_listings", level="WARNING"
            ) as logs:
                call_command(
                    "scrape_listings", "--source", "alonhadat", "--no-ldp-enrich",
                    stdout=io.StringIO(),
                )
        run = ScrapeRun.objects.latest("started_at")
        self.assertEqual(run.listings_seen, 2)
        self.assertEqual(run.posted_date_nulls, 2)
        self.assertTrue(any("posted_date null rate 100%" in line for line in logs.output))


class FetchBotChallengeTests(SimpleTestCase):
    def test_challenge_redirect_returns_none_without_retry(self):
        challenge = Mock(
            status_code=200,
            url="https://alonhadat.com.vn/xac-thuc-nguoi-dung.html?url=/x-1.html",
        )
        with patch.object(client.session, "get", return_value=challenge) as get:
            # assertLogs keeps the expected "bot challenge served" error out
            # of the suite's console output, where it reads like a live incident
            with self.assertLogs("listings.scraping.client", level="ERROR"):
                self.assertEqual(
                    client.fetch("https://alonhadat.com.vn/x-1.html"),
                    (None, "bot_challenge"),
                )
            self.assertEqual(get.call_count, 1)


class FetchErrorCodeTests(SimpleTestCase):
    def _response(self, status_code=200, url="https://alonhadat.com.vn/x-1.html"):
        return Mock(status_code=status_code, url=url, headers={})

    def test_success_returns_response_and_no_error(self):
        response = self._response()
        with patch.object(client.session, "get", return_value=response):
            with patch.object(client.time, "sleep"):
                got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIs(got, response)
        self.assertIsNone(error)

    def test_bot_challenge_redirect_reports_bot_challenge(self):
        response = self._response(
            url="https://alonhadat.com.vn/xac-thuc-nguoi-dung.html"
        )
        with patch.object(client.session, "get", return_value=response):
            with patch.object(client.time, "sleep"):
                with self.assertLogs("listings.scraping.client", level="ERROR"):
                    got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIsNone(got)
        self.assertEqual(error, "bot_challenge")

    def test_404_reports_http_404_not_a_generic_error(self):
        with patch.object(client.session, "get", return_value=self._response(404)):
            with patch.object(client.time, "sleep"):
                with self.assertLogs("listings.scraping.client", level="INFO"):
                    got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIsNone(got)
        self.assertEqual(error, "http_404")

    def test_other_non_200_reports_http_error(self):
        with patch.object(client.session, "get", return_value=self._response(403)):
            with patch.object(client.time, "sleep"):
                with self.assertLogs("listings.scraping.client", level="WARNING"):
                    got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIsNone(got)
        self.assertEqual(error, "http_error")

    def test_repeated_timeout_reports_fetch_gave_up_after_three_attempts(self):
        with patch.object(client.session, "get", side_effect=Timeout) as get:
            with patch.object(client.time, "sleep"):
                with self.assertLogs("listings.scraping.client", level="ERROR"):
                    got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIsNone(got)
        self.assertEqual(error, "fetch_gave_up")
        self.assertEqual(get.call_count, 3)

    def test_persistent_500_reports_fetch_gave_up(self):
        with patch.object(
            client.session, "get", return_value=self._response(500)
        ) as get:
            with patch.object(client.time, "sleep"):
                with self.assertLogs("listings.scraping.client", level="ERROR"):
                    got, error = client.fetch("https://alonhadat.com.vn/x-1.html")
        self.assertIsNone(got)
        self.assertEqual(error, "fetch_gave_up")
        self.assertEqual(get.call_count, 3)


class SoftGonePlaceholderTests(SimpleTestCase):
    REAL_LDP = """
      <div itemtype="http://schema.org/BreadcrumbList">
        <a href="/can-ban-can-ho-chung-cu">Căn hộ</a>
      </div>
      <article class="property">
        <h1 itemprop="name">Bán căn hộ Quận 7</h1>
        <section class="images"><ul class="image-list">
          <li><img src="/img/a.jpg"></li>
        </ul></section>
      </article>
    """
    SOFT_GONE = """
      <article class="property">
        <h1 itemprop="name">Trang chủ</h1>
      </article>
    """

    def test_real_ldp_is_not_soft_gone(self):
        extras = alonhadat.parse_ldp_extras(self.REAL_LDP)
        self.assertFalse(extras["soft_gone"])
        self.assertEqual(extras["images"], ["https://alonhadat.com.vn/img/a.jpg"])

    def test_placeholder_shell_is_soft_gone(self):
        extras = alonhadat.parse_ldp_extras(self.SOFT_GONE)
        self.assertTrue(extras["soft_gone"])

    def test_soft_gone_leaves_images_null_not_empty(self):
        # [] would mark enrichment done and mass-flag low_photos; null keeps
        # the row retry-eligible.
        extras = alonhadat.parse_ldp_extras(self.SOFT_GONE)
        self.assertIsNone(extras["images"])

    def test_page_without_the_anchor_is_not_soft_gone(self):
        extras = alonhadat.parse_ldp_extras("<html><body>nothing</body></html>")
        self.assertFalse(extras["soft_gone"])
        self.assertIsNone(extras["images"])


class ScrapeRunStatusCountsTests(TestCase):
    SRP_HTML = """
      <article class="property-item">
        <a itemprop="url" href="/x-777.html" data-memberid="m1">link</a>
        <span itemprop="name">Bán căn hộ</span>
      </article>
    """

    def _run(self, fetch_side_effect):
        with patch.object(scrape_listings, "fetch", side_effect=fetch_side_effect):
            call_command(
                "scrape_listings", "--source", "alonhadat", "--pages", "1",
                stdout=io.StringIO(), stderr=io.StringIO(),
            )
        return ScrapeRun.objects.latest("id")

    def test_srp_bot_challenge_is_counted_by_name(self):
        run = self._run([(None, "bot_challenge")])
        self.assertEqual(run.status_counts, {"srp_bot_challenge": 1})
        self.assertEqual(run.error_count, 1)

    def test_srp_timeout_is_counted_separately_from_the_wall(self):
        run = self._run([(None, "fetch_gave_up")])
        self.assertEqual(run.status_counts, {"srp_fetch_gave_up": 1})

    def test_ldp_404_is_counted_but_is_not_an_error(self):
        run = self._run([(Mock(text=self.SRP_HTML), None), (None, "http_404")])
        self.assertEqual(run.status_counts.get("ldp_404"), 1)
        self.assertEqual(run.error_count, 0)

    def test_ldp_404_does_not_delist_a_listing_seen_on_the_srp(self):
        self._run([(Mock(text=self.SRP_HTML), None), (None, "http_404")])
        listing = Listing.objects.get(source_site="alonhadat", source_id="777")
        self.assertTrue(listing.is_active)

    def test_soft_gone_ldp_is_counted_and_leaves_images_null(self):
        soft_gone = (
            '<article class="property"><h1 itemprop="name">Trang chủ</h1></article>'
        )
        run = self._run(
            [(Mock(text=self.SRP_HTML), None), (Mock(text=soft_gone), None)]
        )
        self.assertEqual(run.status_counts.get("ldp_soft_gone"), 1)
        self.assertEqual(run.error_count, 0)
        listing = Listing.objects.get(source_site="alonhadat", source_id="777")
        self.assertIsNone(listing.images)

    def test_no_anchor_is_counted_but_is_not_an_error(self):
        run = self._run(
            [
                (Mock(text=self.SRP_HTML), None),
                (Mock(text="<html><body>redesigned</body></html>"), None),
            ]
        )
        self.assertEqual(run.status_counts.get("ldp_no_anchor"), 1)
        self.assertEqual(run.error_count, 0)

    def test_no_failures_stores_null_not_an_empty_dict(self):
        real_ldp = (
            "<div itemscope itemtype='https://schema.org/BreadcrumbList'>"
            "<a href='/can-ban-can-ho-chung-cu'>x</a></div>"
            '<article class="property"><h1 itemprop="name">Real</h1>'
            "<section class='images'><ul class='image-list'>"
            "<li><img src='/files/a.jpg'></li></ul></section></article>"
        )
        run = self._run([(Mock(text=self.SRP_HTML), None), (Mock(text=real_ldp), None)])
        self.assertIsNone(run.status_counts)
        self.assertEqual(run.error_count, 0)


class ScrapeRunAbortTests(TestCase):
    def _abort(self):
        with patch.object(
            scrape_listings.alonhadat, "parse_srp", side_effect=RuntimeError("boom")
        ):
            with patch.object(
                scrape_listings, "fetch", return_value=(Mock(text="<html></html>"), None)
            ):
                with self.assertRaises(RuntimeError):
                    call_command(
                        "scrape_listings", "--source", "alonhadat",
                        stdout=io.StringIO(), stderr=io.StringIO(),
                    )

    def test_counters_survive_an_exception_mid_run(self):
        self._abort()
        run = ScrapeRun.objects.latest("id")
        self.assertEqual(run.status_counts, {"run_aborted": 1})
        self.assertEqual(run.error_count, 1)

    def test_aborted_run_leaves_finished_at_null(self):
        # finished_at means "completed". Stamping it would also let the
        # aborted run's partial count become sweep_delistings' baseline.
        self._abort()
        self.assertIsNone(ScrapeRun.objects.latest("id").finished_at)

    def test_aborted_run_is_not_used_as_the_next_sweep_baseline(self):
        healthy = ScrapeRun.objects.create(
            source_site="alonhadat",
            started_at=timezone.now() - timedelta(days=2),
            finished_at=timezone.now() - timedelta(days=2),
            listings_seen=800,
        )
        self._abort()
        aborted = ScrapeRun.objects.latest("id")
        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now(),
            finished_at=timezone.now(), listings_seen=3,
        )
        prior = (
            ScrapeRun.objects.filter(
                source_site="alonhadat", finished_at__isnull=False
            )
            .exclude(pk=run.pk)
            .order_by("-started_at")
            .first()
        )
        self.assertNotEqual(prior.pk, aborted.pk)
        self.assertEqual(prior.pk, healthy.pk)

    def test_aborted_run_does_not_sweep_delistings(self):
        stale = _make_listing(
            source_site="alonhadat", source_id="old",
            url="https://alonhadat.com.vn/old-1.html",
            last_seen_at=timezone.now() - timedelta(days=3),
        )
        self._abort()
        stale.refresh_from_db()
        self.assertTrue(stale.is_active)


class SweepZeroSeenGuardTests(TestCase):
    def test_a_run_that_saw_nothing_never_sweeps(self):
        # Two consecutive blocked runs: `0 < 0/2` is False, so the ratio guard
        # alone lets the sweep through and delists the whole active table.
        ScrapeRun.objects.create(
            source_site="alonhadat",
            started_at=timezone.now() - timedelta(days=1),
            finished_at=timezone.now() - timedelta(days=1),
            listings_seen=0,
        )
        live = _make_listing(
            source_site="alonhadat", source_id="keep",
            url="https://alonhadat.com.vn/keep-1.html",
            last_seen_at=timezone.now() - timedelta(days=3),
        )
        blocked = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now(),
            finished_at=timezone.now(), listings_seen=0,
            error_count=1, status_counts={"srp_bot_challenge": 1},
        )
        with self.assertLogs(
            "listings.management.commands.scrape_listings", level="WARNING"
        ):
            sweep_delistings(blocked)
        live.refresh_from_db()
        self.assertTrue(live.is_active)
