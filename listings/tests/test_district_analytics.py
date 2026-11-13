from datetime import timedelta
from decimal import Decimal
from itertools import count

from django.test import TestCase
from django.utils import timezone

from listings.district_analytics import district_stats
from listings.models import Listing
from listings.tests.test_models import _make_listing

TY = 10**9
_ids = count()


def _row(price, area=50, district="Quận 7", **overrides):
    n = next(_ids)
    fields = dict(
        source_site="alonhadat",
        source_id=f"d{n}",
        url=f"https://alonhadat.com.vn/d{n}.html",
        price=price,
        area_sqm=area,
        district=district,
    )
    fields.update(overrides)
    return _make_listing(**fields)


def _stats(as_of=None):
    return district_stats(Listing.objects.all(), as_of or timezone.now())


class DistrictStatsTests(TestCase):
    def test_median_and_counts_for_one_group(self):
        for price in (2 * TY, 3 * TY, 10 * TY):
            _row(price)

        (row,) = _stats()

        self.assertEqual(row["district"], "Quận 7")
        self.assertEqual(row["property_type"], "apartment")
        self.assertEqual(row["median_price_per_sqm"], 60_000_000.0)
        self.assertEqual(row["median_price"], 3e9)
        self.assertEqual(row["listing_count"], 3)
        self.assertEqual(row["distinct_count"], 3)

    def test_twin_counts_as_a_listing_but_not_as_a_distinct_row(self):
        for price in (2 * TY, 3 * TY, 10 * TY, 2 * TY):
            _row(price)

        (row,) = _stats()

        self.assertEqual(row["listing_count"], 4)
        self.assertEqual(row["distinct_count"], 3)
        self.assertEqual(row["median_price_per_sqm"], 60_000_000.0)

    def test_excluded_rows(self):
        as_of = timezone.now()
        excluded = {
            "inactive": dict(is_active=False),
            "rent": dict(listing_intent="rent"),
            "no price": dict(price=None),
            "zero price": dict(price=0),
            "no area": dict(area_sqm=None),
            "zero area": dict(area_sqm=0),
            "over 500 m2": dict(area_sqm=Decimal("500.01")),
            "over 10 bedrooms": dict(bedrooms=11),
            "no district": dict(district=None),
            "blank district": dict(district=""),
            "stale": dict(last_seen_at=as_of - timedelta(days=31)),
        }
        for label, overrides in excluded.items():
            with self.subTest(label):
                Listing.objects.all().delete()
                _row(**{"price": 2 * TY, **overrides})
                self.assertEqual(_stats(as_of), [])

    def test_edge_rows_still_count(self):
        as_of = timezone.now()
        _row(2 * TY, area=500)
        _row(3 * TY, bedrooms=10)
        _row(4 * TY, last_seen_at=as_of - timedelta(days=29))

        (row,) = _stats(as_of)

        self.assertEqual(row["listing_count"], 3)

    def test_quartiles_from_ten_distinct_rows(self):
        for k in range(1, 11):
            _row(k * TY, area=100)

        (row,) = _stats()

        self.assertEqual(row["p25_price_per_sqm"], 27_500_000.0)
        self.assertEqual(row["median_price_per_sqm"], 55_000_000.0)
        self.assertEqual(row["p75_price_per_sqm"], 82_500_000.0)

    def test_no_quartiles_below_ten_distinct_rows(self):
        for k in range(1, 10):
            _row(k * TY, area=100)

        (row,) = _stats()

        self.assertIsNone(row["p25_price_per_sqm"])
        self.assertIsNone(row["p75_price_per_sqm"])
        self.assertEqual(row["median_price_per_sqm"], 50_000_000.0)

    def test_property_types_split_into_separate_rows(self):
        _row(2 * TY)
        _row(9 * TY, property_type="villa")

        rows = _stats()

        self.assertEqual(
            sorted(r["property_type"] for r in rows), ["apartment", "villa"]
        )
