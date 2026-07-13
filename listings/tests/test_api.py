from django.test import TestCase

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
