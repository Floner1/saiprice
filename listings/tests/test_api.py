from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from listings.models import Agent, PriceHistory
from listings.tests.test_models import _make_listing


class ListingFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _make_listing(
            source_id="f1", url="https://batdongsan.com.vn/f1-pr1",
            district_id_source=762, price=5_000_000_000, area_sqm=80,
        )
        _make_listing(
            source_id="f2", url="https://batdongsan.com.vn/f2-pr2",
            district_id_source=762, price=12_000_000_000, area_sqm=200,
        )
        _make_listing(
            source_id="f3", url="https://batdongsan.com.vn/f3-pr3",
            district_id_source=762, price=None, area_sqm=60,
        )
        _make_listing(
            source_id="f4", url="https://batdongsan.com.vn/f4-pr4",
            district_id_source=769, price=6_000_000_000, area_sqm=None,
        )

    def _get_ids(self, **params):
        response = self.client.get("/api/listings/", params)
        self.assertEqual(response.status_code, 200)
        return {row["source_id"] for row in response.json()["results"]}

    def test_price_range_with_zero_matches_returns_empty(self):
        self.assertEqual(self._get_ids(min_price=20_000_000_000), set())

    def test_district_filter_alone(self):
        self.assertEqual(self._get_ids(district_id=762), {"f1", "f2", "f3"})

    def test_combined_price_district_area(self):
        self.assertEqual(
            self._get_ids(
                district_id=762,
                min_price=4_000_000_000,
                max_price=13_000_000_000,
                min_area=100,
            ),
            {"f2"},
        )

    def test_null_price_excluded_from_price_range_not_errored(self):
        self.assertEqual(
            self._get_ids(max_price=15_000_000_000), {"f1", "f2", "f4"}
        )

    def test_null_area_excluded_from_area_range(self):
        self.assertEqual(self._get_ids(max_area=500), {"f1", "f2", "f3"})

    def test_price_display_exposed(self):
        response = self.client.get("/api/listings/", {"district_id": 762})
        rows = {r["source_id"]: r for r in response.json()["results"]}
        self.assertEqual(rows["f1"]["price_display"], "5 tỷ")
        self.assertIsNone(rows["f3"]["price_display"])


class SpecFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.agent = Agent.objects.create(
            source_site="alonhadat", source_id="m1", name="Agent One"
        )
        cls.agent2 = Agent.objects.create(
            source_site="alonhadat", source_id="m2", name="Nguyen Thi Hoa"
        )
        _make_listing(
            source_id="s1", url="https://alonhadat.com.vn/s1.html",
            district="Quận 7", property_type="apartment", listing_intent="sale",
            is_anomaly=True, agent=cls.agent, price=5_000_000_000,
        )
        _make_listing(
            source_id="s2", url="https://alonhadat.com.vn/s2.html",
            district="Quận 7", property_type="house", listing_intent="rent",
            agent=cls.agent2, price=20_000_000,
        )
        _make_listing(
            source_id="s3", url="https://alonhadat.com.vn/s3.html",
            district="Quận 1", property_type="apartment", listing_intent="sale",
            price=9_000_000_000,
        )

    def _get_ids(self, **params):
        response = self.client.get("/api/listings/", params)
        self.assertEqual(response.status_code, 200)
        return {row["source_id"] for row in response.json()["results"]}

    def test_filter_by_district(self):
        self.assertEqual(self._get_ids(district="Quận 7"), {"s1", "s2"})

    def test_filter_by_property_type(self):
        self.assertEqual(self._get_ids(property_type="apartment"), {"s1", "s3"})

    def test_filter_by_listing_intent(self):
        self.assertEqual(self._get_ids(listing_intent="rent"), {"s2"})

    def test_filter_by_is_anomaly(self):
        self.assertEqual(self._get_ids(is_anomaly=True), {"s1"})
        self.assertEqual(self._get_ids(is_anomaly=False), {"s2", "s3"})

    def test_filter_by_agent(self):
        self.assertEqual(self._get_ids(agent=self.agent.pk), {"s1"})

    def test_ordering_by_price(self):
        response = self.client.get("/api/listings/", {"ordering": "price"})
        self.assertEqual(response.status_code, 200)
        ids = [row["source_id"] for row in response.json()["results"]]
        self.assertEqual(ids, ["s2", "s1", "s3"])

    def test_ordering_by_price_descending(self):
        response = self.client.get("/api/listings/", {"ordering": "-price"})
        self.assertEqual(response.status_code, 200)
        ids = [row["source_id"] for row in response.json()["results"]]
        self.assertEqual(ids, ["s3", "s1", "s2"])


class SortByDaysOnMarketTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _make_listing(
            source_id="old", url="https://alonhadat.com.vn/old.html",
            posted_date=(timezone.now() - timedelta(days=30)).date(),
        )
        _make_listing(
            source_id="new", url="https://alonhadat.com.vn/new.html",
            posted_date=(timezone.now() - timedelta(days=5)).date(),
        )
        fallback = _make_listing(
            source_id="fallback", url="https://alonhadat.com.vn/fallback.html",
            posted_date=None,
        )
        PriceHistory.objects.create(
            listing=fallback, price=1000,
            observed_at=timezone.now() - timedelta(days=15),
        )

    def _get_ordered_ids(self, **params):
        response = self.client.get("/api/listings/", params)
        self.assertEqual(response.status_code, 200)
        return [row["source_id"] for row in response.json()["results"]]

    def test_sort_ascending(self):
        self.assertEqual(
            self._get_ordered_ids(sort_by="days_on_market"),
            ["new", "fallback", "old"],
        )

    def test_sort_descending(self):
        self.assertEqual(
            self._get_ordered_ids(sort_by="-days_on_market"),
            ["old", "fallback", "new"],
        )

    def test_sort_combines_with_filters(self):
        self.assertEqual(
            self._get_ordered_ids(sort_by="days_on_market", district="nowhere"),
            [],
        )

    def test_no_params_keeps_default_ordering(self):
        self.assertEqual(
            self._get_ordered_ids(), ["fallback", "new", "old"]
        )

    def test_unknown_sort_by_value_ignored(self):
        self.assertEqual(
            self._get_ordered_ids(sort_by="price"), ["fallback", "new", "old"]
        )


class DaysOnMarketFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.normal = _make_listing(
            source_id="dm1", url="https://alonhadat.com.vn/dm1.html",
            posted_date=(timezone.now() - timedelta(days=10)).date(),
        )
        cls.fallback = _make_listing(
            source_id="dm2", url="https://alonhadat.com.vn/dm2.html",
            posted_date=None,
        )
        PriceHistory.objects.create(
            listing=cls.fallback, price=1000,
            observed_at=timezone.now() - timedelta(days=7),
        )

    def test_list_exposes_days_on_market(self):
        response = self.client.get("/api/listings/")
        self.assertEqual(response.status_code, 200)
        rows = {r["source_id"]: r for r in response.json()["results"]}
        self.assertEqual(rows["dm1"]["days_on_market"], 10)
        self.assertEqual(rows["dm2"]["days_on_market"], 7)

    def test_detail_exposes_days_on_market(self):
        normal = self.client.get(f"/api/listings/{self.normal.pk}/")
        self.assertEqual(normal.json()["days_on_market"], 10)
        fallback = self.client.get(f"/api/listings/{self.fallback.pk}/")
        self.assertEqual(fallback.json()["days_on_market"], 7)


class PaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for i in range(1, 22):
            _make_listing(
                source_id=f"p{i}", url=f"https://alonhadat.com.vn/p{i}.html"
            )

    def test_count_reflects_all_active_listings(self):
        response = self.client.get("/api/listings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 21)

    def test_first_page_has_page_size_results(self):
        body = self.client.get("/api/listings/").json()
        self.assertEqual(len(body["results"]), 20)
        self.assertIsNotNone(body["next"])
        self.assertIsNone(body["previous"])

    def test_page_2_returns_the_remaining_listing(self):
        body = self.client.get("/api/listings/", {"page": 2}).json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["source_id"], "p1")
        self.assertIsNone(body["next"])
        self.assertIsNotNone(body["previous"])

    def test_page_past_the_end_returns_404(self):
        response = self.client.get("/api/listings/", {"page": 3})
        self.assertEqual(response.status_code, 404)


class ListingDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.listing = _make_listing(
            source_id="d1", url="https://alonhadat.com.vn/d1.html",
            district="Quận 3", price=7_000_000_000,
        )

    def test_detail_returns_full_listing(self):
        response = self.client.get(f"/api/listings/{self.listing.pk}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], self.listing.pk)
        self.assertEqual(body["source_id"], "d1")
        self.assertEqual(body["district"], "Quận 3")
        self.assertEqual(body["price"], "7000000000")

    def test_detail_missing_id_returns_404(self):
        response = self.client.get("/api/listings/999999/")
        self.assertEqual(response.status_code, 404)

    def test_delisted_listing_returns_404(self):
        delisted = _make_listing(
            source_id="d2", url="https://alonhadat.com.vn/d2.html",
            is_active=False, delisted_at=timezone.now(),
        )
        response = self.client.get(f"/api/listings/{delisted.pk}/")
        self.assertEqual(response.status_code, 404)
