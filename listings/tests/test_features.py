from datetime import date

import pandas as pd
from django.test import SimpleTestCase

from listings.ml.features import (
    MIN_DISTRICT_ROWS,
    district_median_price,
    fit_district_stats,
    listing_age,
)

# Q1 is thick enough (11 rows) to keep its own median of 2.0. Q7 has a single
# row, so its own median would just be that row's own price -- it must collapse
# to the train-wide median instead. Train-wide median over all 12 = 4.0.
TRAIN = pd.DataFrame(
    {
        "district": ["Q1"] * 11 + ["Q7"],
        "price": [1.0] * 5 + [2.0] + [6.0] * 5 + [10.0],
    }
)

# Row 0 is priced far outside its train district, row 1 is a district train
# never saw. Both exist to prove the test frame cannot move its own statistic.
TEST = pd.DataFrame({"district": ["Q1", "Q9"], "price": [999.0, 5.0]})


class ListingAgeTests(SimpleTestCase):
    def test_days_between_posted_date_and_as_of(self):
        df = pd.DataFrame({"posted_date": [date(2026, 1, 1), date(2026, 6, 30)]})
        self.assertEqual(list(listing_age(df, as_of="2026-07-01")), [181, 1])


class DistrictMedianPriceTests(SimpleTestCase):
    def test_maps_train_median_and_falls_back_for_unseen_district(self):
        out = district_median_price(TEST, fit_district_stats(TRAIN))
        self.assertEqual(out.iloc[0], 2.0)  # Q1 train median, unmoved by 999
        self.assertEqual(out.iloc[1], 4.0)  # Q9 unseen -> train-wide median


class ThinDistrictTests(SimpleTestCase):
    """A district too thin for a meaningful median must not encode its own rows."""

    def test_single_row_district_takes_the_train_wide_median(self):
        stats = fit_district_stats(TRAIN)
        # Q7's only row is priced 10.0. Left alone, its "median" is that row,
        # making the feature a laundered copy of the target for that listing.
        self.assertEqual(stats.loc["Q7", "median_price"], 4.0)
        self.assertNotEqual(stats.loc["Q7", "median_price"], 10.0)

    def test_district_above_the_threshold_keeps_its_own_median(self):
        stats = fit_district_stats(TRAIN)
        self.assertGreaterEqual((TRAIN["district"] == "Q1").sum(), MIN_DISTRICT_ROWS)
        self.assertEqual(stats.loc["Q1", "median_price"], 2.0)

    def test_thin_district_stays_in_the_index(self):
        # Collapsing the value must not drop the row, or callers that count
        # districts (the notebook) would silently under-report.
        self.assertIn("Q7", fit_district_stats(TRAIN).index)


class UnseenDistrictFallbackTests(SimpleTestCase):
    """Q9 is in TEST and absent from TRAIN. Nothing may come back NaN."""

    def setUp(self):
        self.stats = fit_district_stats(TRAIN)

    def test_median_price_uses_train_wide_median_not_a_district_one(self):
        out = district_median_price(TEST, self.stats)
        self.assertFalse(out.isna().any())
        # 4.0 is the train-wide median and is not Q1's median (2.0), so this
        # cannot pass by borrowing the one district row that is in the frame.
        self.assertEqual(out.iloc[1], 4.0)
        self.assertNotEqual(out.iloc[1], self.stats.loc["Q1", "median_price"])

    def test_fallback_ignores_the_frame_it_transforms(self):
        # Same districts, wildly different prices. A fallback derived from the
        # frame being transformed would disagree between these two; one read off
        # the fitted `stats` cannot. This is the assertion that actually bites --
        # it fails against a `fillna(df["price"].median())` implementation.
        other = TEST.assign(price=[1e9, 1e9])
        self.assertEqual(
            district_median_price(other, self.stats).iloc[1],
            district_median_price(TEST, self.stats).iloc[1],
        )
