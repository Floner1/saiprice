from datetime import date

from listings.ml.dataset import FEATURES, TARGET, build_training_rows
from listings.tests.test_models import _make_listing

from django.test import TestCase


def _listing(source_id, **overrides):
    defaults = dict(
        source_site="alonhadat",
        source_id=source_id,
        url=f"https://alonhadat.com.vn/{source_id}.html",
        title=f"Listing {source_id}",
        price=5_000_000_000,
        area_sqm=70,
        district="Quận 7",
        posted_date=date(2026, 7, 1),
    )
    defaults.update(overrides)
    return _make_listing(**defaults)


class BuildTrainingRowsTests(TestCase):
    def test_keeps_clean_sale_listing(self):
        _listing("clean")
        rows, stats = build_training_rows()
        self.assertEqual(stats["rows_before"], 1)
        self.assertEqual(stats["rows_after"], 1)
        self.assertEqual(rows[0]["area_sqm"], 70.0)
        self.assertEqual(rows[0][TARGET], 5_000_000_000.0)

    def test_drops_each_dirty_row_and_counts_it(self):
        _listing("clean")
        _listing("rent", listing_intent="rent")
        _listing("nullprice", price=None)
        _listing("nullarea", area_sqm=None)
        _listing("nulldistrict", district=None)
        _listing("zeroarea", area_sqm=0)
        _listing("building", area_sqm=4237)
        _listing("rooms", bedrooms=192)

        rows, stats = build_training_rows()
        self.assertEqual(
            stats,
            {
                "rows_before": 8,
                "dropped_not_sale": 1,
                "dropped_missing_or_nonpositive": 4,
                "dropped_missing_posted_date": 0,
                "dropped_whole_building": 2,
                "dropped_duplicate": 0,
                "rows_after": 1,
            },
        )

    def test_drops_listing_with_null_posted_date(self):
        # listing_age has no fallback: a null date becomes NaN and sklearn
        # raises at .fit(). §5.4 exists because a label rename on the source
        # nulls this field fleet-wide, so this is a live failure mode.
        _listing("clean")
        _listing("nodate", posted_date=None)
        rows, stats = build_training_rows()
        self.assertEqual(stats["dropped_missing_posted_date"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual([r["id"] for r in rows], [rows[0]["id"]])

    def test_drops_repost_duplicates_keeping_first_seen(self):
        first = _listing("orig", title="Same unit")
        _listing("repost", title="Same unit")
        rows, stats = build_training_rows()
        self.assertEqual(stats["dropped_duplicate"], 1)
        self.assertEqual([r["id"] for r in rows], [first.id])

    def test_carries_posted_date_for_the_listing_age_feature(self):
        _listing("dated", posted_date=date(2026, 6, 13))
        rows, _ = build_training_rows()
        self.assertEqual(rows[0]["posted_date"], date(2026, 6, 13))

    def test_no_nulls_in_any_model_column(self):
        _listing("clean")
        _listing("nullarea", area_sqm=None)
        rows, _ = build_training_rows()
        # posted_date is not a FEATURE but listing_age is computed from it, so a
        # null here reaches the model just the same.
        for column in FEATURES + (TARGET, "posted_date"):
            self.assertFalse(any(row[column] is None for row in rows), column)
