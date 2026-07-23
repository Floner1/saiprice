from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from listings.models import Agent
from listings.tests.test_models import _make_listing


class DashboardListingListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for i in range(1, 22):
            _make_listing(
                source_id=f"v{i}", url=f"https://alonhadat.com.vn/v{i}.html"
            )
        _make_listing(
            source_id="gone", url="https://alonhadat.com.vn/gone.html",
            is_active=False, delisted_at=timezone.now(),
        )

    def test_page_renders_with_result_count(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["paginator"].count, 21)
        self.assertContains(response, "21 results")

    def test_first_page_shows_page_size_listings(self):
        response = self.client.get("/")
        self.assertEqual(len(response.context["page_obj"]), 20)

    def test_page_2_shows_the_remaining_listing(self):
        response = self.client.get("/", {"page": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), 1)
        self.assertEqual(response.context["page_obj"][0].source_id, "v1")

    def test_invalid_page_param_returns_404_not_500(self):
        self.assertEqual(self.client.get("/", {"page": "abc"}).status_code, 404)
        self.assertEqual(self.client.get("/", {"page": "999"}).status_code, 404)

    def test_scraped_title_is_html_escaped(self):
        _make_listing(
            source_id="xss", url="https://alonhadat.com.vn/xss.html",
            title='<script>alert("x")</script>',
        )
        response = self.client.get("/")
        self.assertNotContains(response, '<script>alert("x")</script>')
        self.assertContains(response, "&lt;script&gt;")

    def test_list_links_to_detail(self):
        response = self.client.get("/")
        listing = response.context["page_obj"][0]
        self.assertContains(response, f'href="/listing/{listing.pk}/"')


class DashboardListingDetailTests(TestCase):
    def test_renders_full_info_with_pending_prediction(self):
        listing = _make_listing(
            source_id="d1", url="https://alonhadat.com.vn/d1.html",
            price=8_500_000_000, area_sqm=80, district="Quận 7",
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test listing")
        self.assertContains(response, "8.5 tỷ")
        self.assertContains(response, "Predicted: pending")
        self.assertContains(response, "Quận 7")
        self.assertContains(response, "Not scored yet.")

    def test_renders_prediction_and_anomaly_reason(self):
        listing = _make_listing(
            source_id="d2", url="https://alonhadat.com.vn/d2.html",
            price=8_000_000_000, predicted_price=9_000_000_000,
            is_anomaly=True,
            anomaly_reason={
                "low_photos": {"triggered": True, "value": 1},
                "stale_listing": {"triggered": False, "value": 12},
            },
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertContains(response, "Predicted: 9 tỷ")
        self.assertContains(response, "Flagged as anomaly")
        self.assertContains(response, "low_photos")
        self.assertContains(response, "triggered · value: 1")
        self.assertContains(response, "stale_listing")
        self.assertContains(response, "not triggered · value: 12")

    def test_renders_all_fields_populated(self):
        agent = Agent.objects.create(
            source_site="alonhadat", source_id="ag1", name="Chị Hoa"
        )
        listing = _make_listing(
            source_id="d4", url="https://alonhadat.com.vn/d4.html",
            price=8_000_000_000, predicted_price=7_500_000_000,
            price_per_sqm=100_000_000, area_sqm=80,
            bedrooms=3, bathrooms=2,
            district="Quận 7", ward="Phường Tân Phong",
            address_raw="12 Nguyễn Văn Linh, Phường Tân Phong, Quận 7, TP.HCM",
            posted_date=(timezone.now() - timedelta(days=5)).date(),
            images=["https://img/1.jpg", "https://img/2.jpg"],
            agent=agent, phone_number="0901234567",
            description="Nhà đẹp, sổ hồng riêng.",
            is_anomaly=True,
            anomaly_reason={"low_photos": {"triggered": True, "value": 2}},
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "8 tỷ")
        self.assertContains(response, "Predicted: 7.5 tỷ")
        self.assertContains(response, "100 triệu")
        self.assertContains(response, "Phường Tân Phong")
        self.assertContains(response, "12 Nguyễn Văn Linh")
        self.assertContains(response, "Chị Hoa")
        self.assertContains(response, "0901234567")
        self.assertContains(response, "Nhà đẹp, sổ hồng riêng.")
        self.assertContains(response, "<dd>3</dd>", html=True)
        self.assertContains(response, "<dd>2</dd>", html=True)

    def test_null_price_renders_negotiable(self):
        listing = _make_listing(
            source_id="d5", url="https://alonhadat.com.vn/d5.html", price=None,
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thỏa thuận")

    def test_inactive_listing_404s(self):
        listing = _make_listing(
            source_id="d3", url="https://alonhadat.com.vn/d3.html",
            is_active=False, delisted_at=timezone.now(),
        )
        self.assertEqual(self.client.get(f"/listing/{listing.pk}/").status_code, 404)


class DashboardListingFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _make_listing(
            source_id="f1", url="https://alonhadat.com.vn/f1.html",
            district="Quận 1", property_type="apartment", price=2_000_000_000,
            address_raw="10 Lê Lợi, Quận 1", project_name="Vinhomes Central",
        )
        _make_listing(
            source_id="f2", url="https://alonhadat.com.vn/f2.html",
            district="Quận 7", property_type="house", price=8_000_000_000,
            address_raw="5 Nguyễn Văn Linh, Quận 7", title="Nhà phố Phú Mỹ Hưng",
        )
        _make_listing(
            source_id="f3", url="https://alonhadat.com.vn/f3.html",
            district="Quận 7", property_type="apartment", price=5_000_000_000,
            address_raw="8 Tân Phong, Quận 7",
        )

    def _ids(self, response):
        return [listing.source_id for listing in response.context["page_obj"]]

    def test_no_filter_returns_all_active(self):
        self.assertCountEqual(
            self._ids(self.client.get("/")), ["f1", "f2", "f3"]
        )

    def test_filter_by_district(self):
        self.assertCountEqual(
            self._ids(self.client.get("/", {"district": "Quận 7"})), ["f2", "f3"]
        )

    def test_filter_by_property_type(self):
        self.assertEqual(
            self._ids(self.client.get("/", {"property_type": "house"})), ["f2"]
        )

    def test_filter_by_price_range(self):
        response = self.client.get(
            "/", {"min_price": "3000000000", "max_price": "6000000000"}
        )
        self.assertEqual(self._ids(response), ["f3"])

    def test_filters_combine(self):
        response = self.client.get(
            "/", {"district": "Quận 7", "property_type": "apartment"}
        )
        self.assertEqual(self._ids(response), ["f3"])

    def test_search_matches_address(self):
        self.assertEqual(self._ids(self.client.get("/", {"q": "Lê Lợi"})), ["f1"])

    def test_search_matches_project_name(self):
        self.assertEqual(self._ids(self.client.get("/", {"q": "Vinhomes"})), ["f1"])

    def test_search_matches_title(self):
        self.assertEqual(self._ids(self.client.get("/", {"q": "Phú Mỹ Hưng"})), ["f2"])

    def test_invalid_price_is_ignored_not_500(self):
        response = self.client.get("/", {"min_price": "abc"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["paginator"].count, 3)

    def test_pathological_prices_ignored_not_500(self):
        # nan/inf/huge-exponent construct as valid Decimals but blow up at the
        # DB layer; they must be dropped like any other bad input, not 500.
        for bad in ("nan", "inf", "1e999999"):
            response = self.client.get("/", {"min_price": bad})
            self.assertEqual(response.status_code, 200, bad)
            self.assertEqual(response.context["paginator"].count, 3, bad)

    def test_district_options_listed(self):
        response = self.client.get("/")
        self.assertContains(response, '<option value="Quận 1"')
        self.assertContains(response, '<option value="Quận 7"')

    def test_form_reflects_selected_state(self):
        response = self.client.get("/", {"district": "Quận 7", "q": "Linh"})
        self.assertContains(response, 'value="Linh"')
        self.assertContains(response, '<option value="Quận 7" selected')


class DashboardFilterPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for i in range(1, 22):
            _make_listing(
                source_id=f"p{i}", url=f"https://alonhadat.com.vn/p{i}.html",
                district="Quận 7",
            )

    def test_pagination_links_preserve_filters(self):
        response = self.client.get("/", {"district": "Quận 7"})
        self.assertContains(response, "district=Qu")
        self.assertContains(response, "page=2")

    def test_page_2_with_filter_still_filters(self):
        response = self.client.get("/", {"district": "Quận 7", "page": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["paginator"].count, 21)
        self.assertEqual(len(response.context["page_obj"]), 1)


class DashboardAnomalyBadgeTests(TestCase):
    def test_badge_shown_for_anomaly_listing(self):
        _make_listing(
            source_id="a1", url="https://alonhadat.com.vn/a1.html", is_anomaly=True,
        )
        self.assertContains(self.client.get("/"), "anomaly")

    def test_no_badge_for_normal_listing(self):
        _make_listing(
            source_id="n1", url="https://alonhadat.com.vn/n1.html", is_anomaly=False,
        )
        self.assertNotContains(self.client.get("/"), "anomaly")
