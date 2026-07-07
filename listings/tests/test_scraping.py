from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from listings.management.commands.scrape_batdongsan import sweep_delistings
from listings.models import Listing, ScrapeRun
from listings.scraping.parsers import (
    RequiredFieldMissing,
    parse_ldp,
    parse_srp,
    parse_srp_total_pages,
)

# ponytail: these point at the real sample HTML in testdata/ (gitignored)
# rather than a duplicated fixtures/ copy. If those files move, repoint
# BASE_DIR below or copy them under listings/tests/fixtures/.
BASE_DIR = Path(__file__).resolve().parent.parent.parent / "testdata"


def _read(name):
    return (BASE_DIR / name).read_text(encoding="utf-8")


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
            started_at=now, finished_at=now, listings_seen=listings_seen
        )

    def _stale_active_listing(self):
        return Listing.objects.create(
            source_site="batdongsan",
            source_id="sweep1",
            url="https://batdongsan.com.vn/sweep-pr1",
            title="Test",
            category_id_source=324,
            property_type="apartment",
            listing_intent="sale",
            last_seen_at=timezone.now() - timedelta(days=1),
        )

    def test_blocked_run_skips_sweep(self):
        self._run(1000)
        listing = self._stale_active_listing()
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


class ParseSrpTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.html = _read("srp_sample.txt")

    def test_extracts_listing_urls(self):
        urls = parse_srp(self.html)
        self.assertGreater(len(urls), 0)
        for url in urls:
            self.assertTrue(url.startswith("https://batdongsan.com.vn/"))
            self.assertRegex(url, r"-pr\d+$")

    def test_urls_are_deduplicated(self):
        urls = parse_srp(self.html)
        self.assertEqual(len(urls), len(set(urls)))

    def test_total_pages(self):
        self.assertGreaterEqual(parse_srp_total_pages(self.html), 1)
