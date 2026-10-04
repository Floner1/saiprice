import io
import unicodedata
from decimal import Decimal
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from listings.management.commands import scrape_offices
from listings.management.commands.scrape_offices import save_building
from listings.models import OfficeBuilding, OfficeRentHistory
from listings.scraping.parsers import RequiredFieldMissing
from listings.scraping.sites import maisonoffice

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?><?xml-stylesheet type="text/xsl" href="//maisonoffice.vn/main-sitemap.xsl"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://maisonoffice.vn/post-sitemap1.xml</loc><lastmod>2026-10-01T10:50:01+00:00</lastmod></sitemap>
<sitemap><loc>https://maisonoffice.vn/van_phong_hcm-sitemap1.xml</loc><lastmod>2026-10-01T08:22:50+00:00</lastmod></sitemap>
<sitemap><loc>https://maisonoffice.vn/vp_dien_tich_hcm-sitemap1.xml</loc><lastmod>2025-10-31T09:55:34+00:00</lastmod></sitemap>
</sitemapindex>"""


class SitemapTests(SimpleTestCase):
    def test_parses_loc_and_lastmod_from_an_index(self):
        self.assertEqual(
            maisonoffice.parse_sitemap(SITEMAP_INDEX.encode()),
            [
                ("https://maisonoffice.vn/post-sitemap1.xml", "2026-10-01T10:50:01+00:00"),
                ("https://maisonoffice.vn/van_phong_hcm-sitemap1.xml", "2026-10-01T08:22:50+00:00"),
                ("https://maisonoffice.vn/vp_dien_tich_hcm-sitemap1.xml", "2025-10-31T09:55:34+00:00"),
            ],
        )

    def test_only_hcm_office_sitemaps_match(self):
        locs = [loc for loc, _ in maisonoffice.parse_sitemap(SITEMAP_INDEX.encode())]
        self.assertEqual(
            [loc for loc in locs if maisonoffice.HCM_SITEMAP.search(loc)],
            ["https://maisonoffice.vn/van_phong_hcm-sitemap1.xml"],
        )


class DistrictTests(SimpleTestCase):
    def test_building_urls_yield_their_district_slug(self):
        base = "https://maisonoffice.vn/cho-thue-van-phong-tphcm"
        self.assertEqual(maisonoffice.district_slug(f"{base}/quan-1/the-one-sai-gon/"), "quan-1")
        # 5 live slugs contain an extra "/" -- still buildings
        self.assertEqual(
            maisonoffice.district_slug(f"{base}/quan-tan-binh/biet-thu-149/33-bvt/"),
            "quan-tan-binh",
        )

    def test_non_building_urls_yield_none(self):
        self.assertIsNone(maisonoffice.district_slug("https://maisonoffice.vn/van_phong_hcm/"))
        self.assertIsNone(
            maisonoffice.district_slug(
                "https://maisonoffice.vn/van-phong-hcm/quan-binh-thanh/toa-nha-aurora/"
            )
        )

    def test_slugs_map_to_residential_naming(self):
        cases = {
            "quan-1": "Quận 1",
            "quan-12": "Quận 12",
            "quan-2": "Quận 2",
            "quan-binh-thanh": "Quận Bình Thạnh",
            "tp-thu-duc": "Thành phố Thủ Đức",
            "huyen-binh-chanh": "Huyện Bình Chánh",
            "binh-chanh": "Huyện Bình Chánh",
        }
        for slug, name in cases.items():
            with self.subTest(slug=slug):
                self.assertEqual(maisonoffice.district_name(slug), name)

    def test_unknown_slug_maps_to_none(self):
        self.assertIsNone(maisonoffice.district_name("huyen-can-gio"))


class ParseUsdPerSqmTests(SimpleTestCase):
    def test_ranges_and_single_values(self):
        cases = {
            "27 - 28 usd/m2/tháng": (Decimal("27"), Decimal("28")),
            "14 – 15 USD/m²/tháng": (Decimal("14"), Decimal("15")),
            "6 usd/m2/tháng": (Decimal("6"), Decimal("6")),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(maisonoffice.parse_usd_per_sqm(text), expected)

    def test_comma_and_dot_are_both_decimal_points(self):
        # parse_vnd reads "." as a thousands separator; here "3.8" must stay 3.8
        self.assertEqual(maisonoffice.parse_usd_per_sqm("3.8 usd/m2/tháng")[0], Decimal("3.8"))
        self.assertEqual(maisonoffice.parse_usd_per_sqm("5,5 usd/m2/tháng")[0], Decimal("5.5"))
        self.assertEqual(maisonoffice.parse_usd_per_sqm("2,17 usd/m2/tháng")[0], Decimal("2.17"))

    def test_non_usd_per_sqm_and_empty_give_none(self):
        for text in ("Liên hệ", "", None, "250.000 vnd/m2/tháng", "1500 usd/tháng"):
            with self.subTest(text=text):
                self.assertEqual(maisonoffice.parse_usd_per_sqm(text), (None, None))


# Trimmed from the live The One Sài Gòn page (2026-10-01): same selectors,
# same label/value markup, plus a decoy "Giá thuê" row in the editor copy that
# parsing must not pick up.
BUILDING_HTML = """<html><body class="wp-singular single single-van_phong_hcm postid-30648 wp-custom-logo">
<aside class="building-detail-head">
<h1>The One Sài Gòn</h1>
<div class="building-meta"><p>136-138  Lê Thị Hồng Gấm, Phường Bến Thành, (Quận 1) TP.HCM</p></div>
</aside>
<div class="detail-content">
<div id="thong_so" class="box-item tab-item thong_so"><div class="tab-content"><ul>
<li><div class="info"><strong>Hạng tòa nhà </strong><span>Hạng B</span></div></li>
<li><div class="info"><strong>Tầng điển hình </strong><span>Diện tích 627m2/sàn</span></div></li>
</ul></div></div>
<div id="chi_tiet_gia" class="box-item tab-item thong_so"><div class="box-item tab-content gia_thue"><ul>
<li><div class="info"><strong>Giá thuê </strong><span>27 - 28 usd/m2/tháng</span></div></li>
<li><div class="info"><strong>Phí dịch vụ </strong><span>5,5 usd/m2/tháng</span></div></li>
<li><div class="info"><strong>Đỗ ô tô </strong><span>2.500.000 vnd/xe/tháng</span></div></li>
</ul></div></div>
</div>
<div class="fulltext"><table><tr><td>Giá thuê</td><td>$99 – $99/ m2</td></tr></table></div>
</body></html>"""
BUILDING_URL = "https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/the-one-sai-gon/"


class ParseBuildingTests(SimpleTestCase):
    def test_full_page(self):
        fields, notes = maisonoffice.parse_building(BUILDING_HTML, BUILDING_URL)
        self.assertEqual(
            fields,
            {
                "source_id": "30648",
                "url": BUILDING_URL,
                "name": "The One Sài Gòn",
                "district": "Quận 1",
                "ward": "Phường Bến Thành",
                "address_raw": "136-138 Lê Thị Hồng Gấm, Phường Bến Thành, (Quận 1) TP.HCM",
                "grade": "B",
                "rent_min_usd": Decimal("27"),
                "rent_max_usd": Decimal("28"),
                "service_fee_usd": Decimal("5.5"),
                "typical_floor_sqm": Decimal("627"),
                "specs_raw": {
                    "Hạng tòa nhà": "Hạng B",
                    "Tầng điển hình": "Diện tích 627m2/sàn",
                    "Giá thuê": "27 - 28 usd/m2/tháng",
                    "Phí dịch vụ": "5,5 usd/m2/tháng",
                    "Đỗ ô tô": "2.500.000 vnd/xe/tháng",
                },
            },
        )
        self.assertEqual(notes, [])

    def test_contact_for_price_and_missing_grade_are_plain_nulls(self):
        html = BUILDING_HTML.replace("27 - 28 usd/m2/tháng", "Liên hệ").replace(
            "<li><div class=\"info\"><strong>Hạng tòa nhà </strong><span>Hạng B</span></div></li>", ""
        )
        fields, notes = maisonoffice.parse_building(html, BUILDING_URL)
        self.assertEqual((fields["rent_min_usd"], fields["rent_max_usd"]), (None, None))
        self.assertIsNone(fields["grade"])
        self.assertEqual(notes, [])

    def test_unexpected_units_are_nulled_and_noted(self):
        html = (
            BUILDING_HTML.replace("27 - 28 usd/m2/tháng", "250.000 vnd/m2/tháng")
            .replace("5,5 usd/m2/tháng", "100.000 vnd/m2/tháng")
            .replace("Diện tích 627m2/sàn", "3 tầng")
            .replace("Hạng B", "Hạng S")
        )
        fields, notes = maisonoffice.parse_building(html, BUILDING_URL)
        self.assertIsNone(fields["rent_min_usd"])
        self.assertIsNone(fields["service_fee_usd"])
        self.assertIsNone(fields["typical_floor_sqm"])
        self.assertIsNone(fields["grade"])
        self.assertEqual(
            sorted(notes),
            [
                "floor_area_unrecognized",
                "grade_unrecognized",
                "rent_not_usd_per_sqm",
                "service_fee_not_usd_per_sqm",
            ],
        )

    def test_bundled_fee_is_zero_not_null(self):
        # "included in the rent" states a fact -- no separate charge -- so it
        # is stored as 0, unlike "Liên hệ", which is missing data.
        html = BUILDING_HTML.replace("5,5 usd/m2/tháng", "Đã bao gồm trong giá thuê")
        fields, notes = maisonoffice.parse_building(html, BUILDING_URL)
        self.assertEqual(fields["service_fee_usd"], Decimal("0"))
        self.assertEqual(notes, [])

    def test_pending_update_nulls_every_field_without_a_note(self):
        html = BUILDING_HTML
        for value in ("27 - 28 usd/m2/tháng", "5,5 usd/m2/tháng", "Diện tích 627m2/sàn", "Hạng B"):
            html = html.replace(value, "Đang cập nhật")
        fields, notes = maisonoffice.parse_building(html, BUILDING_URL)
        for field in ("rent_min_usd", "rent_max_usd", "service_fee_usd", "typical_floor_sqm", "grade"):
            with self.subTest(field=field):
                self.assertIsNone(fields[field])
        self.assertEqual(notes, [])

    def test_abbreviated_ward_is_expanded(self):
        html = BUILDING_HTML.replace(
            "136-138  Lê Thị Hồng Gấm, Phường Bến Thành, (Quận 1) TP.HCM",
            "Lô C1-2, đường D1, Khu Công Nghệ Cao, P. Tăng Nhơn Phú, (Thủ Đức) TP.HCM",
        )
        fields, _ = maisonoffice.parse_building(html, BUILDING_URL)
        self.assertEqual(fields["ward"], "Phường Tăng Nhơn Phú")

    def test_address_without_a_ward_gives_null_not_the_city(self):
        # "TP.HCM" contains "P." -- an unanchored abbreviation match turns
        # this address into ward "Phường HCM".
        html = BUILDING_HTML.replace(
            "136-138  Lê Thị Hồng Gấm, Phường Bến Thành, (Quận 1) TP.HCM",
            "Lô C1-2, đường D1, Khu Công Nghệ Cao, (Thủ Đức) TP.HCM",
        )
        fields, _ = maisonoffice.parse_building(html, BUILDING_URL)
        self.assertIsNone(fields["ward"])

    def test_unmapped_district_is_noted(self):
        url = BUILDING_URL.replace("/quan-1/", "/huyen-can-gio/")
        fields, notes = maisonoffice.parse_building(BUILDING_HTML, url)
        self.assertIsNone(fields["district"])
        self.assertEqual(notes, ["unmapped_district"])

    def test_missing_identity_raises(self):
        with self.assertRaises(RequiredFieldMissing) as ctx:
            maisonoffice.parse_building(BUILDING_HTML.replace("postid-30648", ""), BUILDING_URL)
        self.assertEqual(ctx.exception.field, "source_id")
        with self.assertRaises(RequiredFieldMissing) as ctx:
            maisonoffice.parse_building(BUILDING_HTML.replace("The One Sài Gòn", ""), BUILDING_URL)
        self.assertEqual(ctx.exception.field, "name")


HCM_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://maisonoffice.vn/van_phong_hcm/</loc><lastmod>2026-09-28T03:30:00+00:00</lastmod></url>
<url><loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/a/</loc><lastmod>2026-09-28T03:30:00+00:00</lastmod></url>
<url><loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/b/</loc><lastmod>2026-09-27T03:30:00+00:00</lastmod></url>
<url><loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/a/</loc><lastmod>2026-09-28T03:30:00+00:00</lastmod></url>
<url><loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-3/c/</loc><lastmod>2026-09-26T03:30:00+00:00</lastmod></url>
<url><loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-7/d/</loc><lastmod>2026-09-26T03:30:00+00:00</lastmod></url>
</urlset>"""


def _fake_site(hcm_sitemap=HCM_SITEMAP_XML):
    pages = {
        maisonoffice.SITEMAP_INDEX: SITEMAP_INDEX,
        "https://maisonoffice.vn/van_phong_hcm-sitemap1.xml": hcm_sitemap,
        "https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/a/": BUILDING_HTML,
        "https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-3/c/": BUILDING_HTML.replace(
            "postid-30648", "postid-11"
        ),
    }
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        body = pages[url]
        return Mock(text=body, content=body.encode()), None

    return fake_fetch, fetched


class ScrapeOfficesCommandTests(SimpleTestCase):
    def test_capped_districts_fetch_only_the_sample(self):
        fake_fetch, fetched = _fake_site()
        out = io.StringIO()
        with patch.object(scrape_offices, "fetch", side_effect=fake_fetch):
            call_command(
                "scrape_offices", "--districts", "quan-1,quan-3", "--per-district", "1",
                "--dry-run", stdout=out, stderr=io.StringIO(),
            )
        # index, the one HCM sitemap, then one building per wanted district:
        # no other sitemap, no archive URL, no duplicate, no quan-7
        self.assertEqual(
            fetched,
            [
                maisonoffice.SITEMAP_INDEX,
                "https://maisonoffice.vn/van_phong_hcm-sitemap1.xml",
                "https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/a/",
                "https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-3/c/",
            ],
        )
        lines = out.getvalue().splitlines()
        self.assertIn("30648 | Quận 1 | B | rent 27-28 | fee 5.5 | floor 627", lines[0])
        # same page body as quan-1: the district comes from the URL, never the page
        self.assertTrue(lines[1].startswith("11 | Quận 3 |"))
        self.assertIn("'parsed': 2", lines[-1])


def _fields(**overrides):
    fields, _ = maisonoffice.parse_building(BUILDING_HTML, BUILDING_URL)
    fields["source_modified_at"] = None
    return {**fields, **overrides}


class SaveBuildingTests(TestCase):
    def test_first_save_inserts_building_and_one_history_row(self):
        self.assertTrue(save_building(_fields()))
        building = OfficeBuilding.objects.get(source_id="30648")
        self.assertEqual(building.district, "Quận 1")
        self.assertEqual(
            list(OfficeRentHistory.objects.values_list("rent_min_usd", "rent_max_usd")),
            [(Decimal("27"), Decimal("28"))],
        )

    def test_unchanged_rent_adds_no_history_and_reactivates(self):
        save_building(_fields())
        OfficeBuilding.objects.update(is_active=False, delisted_at=timezone.now())
        self.assertFalse(save_building(_fields()))
        building = OfficeBuilding.objects.get()
        self.assertTrue(building.is_active)
        self.assertIsNone(building.delisted_at)
        self.assertEqual(OfficeRentHistory.objects.count(), 1)

    def test_changed_rent_adds_a_history_row(self):
        save_building(_fields())
        save_building(_fields(rent_min_usd=Decimal("29"), rent_max_usd=Decimal("30")))
        self.assertEqual(
            list(
                OfficeRentHistory.objects.order_by("id").values_list(
                    "rent_min_usd", "rent_max_usd"
                )
            ),
            [(Decimal("27"), Decimal("28")), (Decimal("29"), Decimal("30"))],
        )


class ScrapeOfficesSaveTests(TestCase):
    def _run(self, *extra, sitemap=HCM_SITEMAP_XML):
        fake_fetch, _ = _fake_site(sitemap)
        out, self.err = io.StringIO(), io.StringIO()
        with patch.object(scrape_offices, "fetch", side_effect=fake_fetch):
            call_command(
                "scrape_offices", "--districts", "quan-1,quan-3", "--per-district", "1",
                *extra, stdout=out, stderr=self.err,
            )
        return out.getvalue()

    def test_malformed_lastmod_is_nulled_and_logged_not_fatal(self):
        good = (
            "<loc>https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/a/</loc>"
            "<lastmod>2026-09-28T03:30:00+00:00</lastmod>"
        )
        sitemap = HCM_SITEMAP_XML.replace(good, good.replace("2026-09-28T03:30:00+00:00", "28/09/2026"), 1)
        out = self._run(sitemap=sitemap)
        self.assertIn("'inserted': 2", out)
        self.assertIn("'bad_lastmod': 1", out)
        self.assertIsNone(OfficeBuilding.objects.get(source_id="30648").source_modified_at)
        self.assertIsNotNone(OfficeBuilding.objects.get(source_id="11").source_modified_at)
        self.assertIn("28/09/2026", self.err.getvalue())

    def test_save_error_is_counted_and_logged_not_fatal(self):
        # WordPress re-creating a post at a slug an older post used: the new
        # post id collides with an existing row on the unique url.
        OfficeBuilding.objects.create(
            source_id="999",
            url="https://maisonoffice.vn/cho-thue-van-phong-tphcm/quan-1/a/",
            name="older post",
            last_seen_at=timezone.now(),
        )
        out = self._run()
        self.assertIn("'save_exception': 1", out)
        self.assertIn("'inserted': 1", out)
        self.assertTrue(OfficeBuilding.objects.filter(source_id="11").exists())
        self.assertIn("quan-1/a/", self.err.getvalue())

    def test_default_run_saves(self):
        out = self._run()
        self.assertEqual(
            sorted(OfficeBuilding.objects.values_list("source_id", "district")),
            [("11", "Quận 3"), ("30648", "Quận 1")],
        )
        self.assertIn("'inserted': 2", out)

    def test_dry_run_writes_nothing(self):
        self._run("--dry-run")
        self.assertEqual(OfficeBuilding.objects.count(), 0)


def _floor(text):
    html = BUILDING_HTML.replace("Diện tích 627m2/sàn", text)
    return maisonoffice.parse_building(html, BUILDING_URL)[0]["typical_floor_sqm"]


class FloorAreaTests(SimpleTestCase):
    def test_separator_before_exactly_three_digits_is_thousands(self):
        # The site mixes conventions: "1,163" and "1.597" are thousands,
        # "167.75" is a decimal (live rows 29595, 139139, 72396).
        cases = {
            "167.75 m2": Decimal("167.75"),
            "1.597 m2": Decimal("1597"),
            "1,163 m2": Decimal("1163"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_floor(text), expected)

    def test_tower_and_block_labels_are_not_read_as_the_area(self):
        # Live row 30132 stored 1 m2: the "1" came from "Tháp 1".
        cases = {
            "Tháp 1: 411 m2, Tháp 2: 1066 m2": Decimal("411"),
            "Tower A: 1.597 m2/sàn": Decimal("1597"),
            "Khối A: 402 m²; Khối B: 285 m²": Decimal("402"),
            "Block 2: 500 m2": Decimal("500"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_floor(text), expected)


class UnicodeAndWardTests(SimpleTestCase):
    def test_decomposed_page_parses_like_composed(self):
        # 13 live addresses arrived as NFD text; one lost its ward to it.
        nfd = unicodedata.normalize("NFD", BUILDING_HTML)
        self.assertEqual(
            maisonoffice.parse_building(nfd, BUILDING_URL),
            maisonoffice.parse_building(BUILDING_HTML, BUILDING_URL),
        )

    def test_lowercase_ward_prefix_is_found_and_capitalised(self):
        html = BUILDING_HTML.replace(
            "136-138  Lê Thị Hồng Gấm, Phường Bến Thành, (Quận 1) TP.HCM",
            "50 Hoàng Trọng Mậu, KĐT Him Lam, phường Tân Hưng, (Quận 7) TP.HCM",
        )
        fields, _ = maisonoffice.parse_building(html, BUILDING_URL)
        self.assertEqual(fields["ward"], "Phường Tân Hưng")


def _fee(text):
    html = BUILDING_HTML.replace("5,5 usd/m2/tháng", text)
    fields, notes = maisonoffice.parse_building(html, BUILDING_URL)
    return fields["service_fee_usd"], notes


def _rent(text):
    html = BUILDING_HTML.replace("27 - 28 usd/m2/tháng", text)
    fields, notes = maisonoffice.parse_building(html, BUILDING_URL)
    return (fields["rent_min_usd"], fields["rent_max_usd"]), notes


class VocabularyTests(SimpleTestCase):
    def test_not_included_fee_stays_null_never_zero(self):
        # "Chưa bao gồm" means NOT included: the fee exists, amount unknown.
        # It contains "bao gồm", so the bundled-fee rule must not make it 0.
        self.assertEqual(_fee("Chưa bao gồm Phí dịch vụ và VAT"), (None, []))

    def test_no_figure_phrases_null_without_a_note(self):
        for text in ("Cập nhật", "Thỏa thuận"):
            with self.subTest(fee=text):
                self.assertEqual(_fee(text), (None, []))
        self.assertEqual(_rent("Cập nhật"), ((None, None), []))

    def test_sqm_reads_as_square_metres(self):
        self.assertEqual(
            _rent("12 - 13 usd/sqm/month"), ((Decimal("12"), Decimal("13")), [])
        )

    def test_bundled_fee_spellings_read_as_zero(self):
        # "boa" is a live typo of "bao"; the second form drops "Đã".
        for text in ("Đã boa gồm trong giá thuê", "Bao gồm trong giá thuê"):
            with self.subTest(text=text):
                self.assertEqual(_fee(text), (Decimal("0"), []))
