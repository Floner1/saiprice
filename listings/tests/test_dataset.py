from datetime import date
from itertools import count

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


_BAND_SEQ = count()


def _band(district, per_sqm_millions, area=80):
    """Seed a district with comparable rows at the given prices per m2."""
    for per_sqm in per_sqm_millions:
        _listing(
            f"comp-{next(_BAND_SEQ)}",
            district=district,
            area_sqm=area,
            price=int(per_sqm * 1_000_000) * area,
        )


class PriceImplausibilityTests(TestCase):
    """The confirmed source-side cases, at their real values.

    Every price/area pair below is a real alonhadat row, and each comparable
    band is seeded to the shape the live cleaned set actually shows for that
    district -- Quan 1 median ~118M/m2 against a much higher p90, Quan 8 median
    ~47M/m2. That matters: a rule judging a row against its neighbours is only
    meaningful with real neighbours, and a flattering band would let a
    median-anchored rule pass these tests too.
    """

    def _seed_bands(self):
        # Thu Duc at the real comparable band for the 45 ty listing's area.
        _band(
            "Thành phố Thủ Đức",
            [100, 105, 108, 112, 115, 118, 120, 122, 125, 128, 130, 130],
        )
        _band("Quận 8", [35, 40, 42, 45, 47, 50, 52, 55, 58, 62, 66, 70])
        # Quan 1 is genuinely bimodal: an ordinary tier around its 118M/m2
        # median plus a luxury tier reaching 500M/m2 on its own. Seeded to that
        # real shape, not a flattened one -- a median-anchored rule drops the
        # two genuine keeps below at 3.5x, which is the whole argument for p90.
        _band(
            "Quận 1",
            [55, 65, 70, 78, 85, 92, 100, 108, 115, 120, 128, 140, 155, 175, 210, 280, 350, 500],
        )
        # Live Binh Thanh: median 72.6M/m2, p90 146.9M/m2.
        _band(
            "Quận Bình Thạnh",
            [40, 48, 55, 60, 66, 70, 72, 75, 80, 88, 95, 110, 125, 147],
        )

    def test_confirmed_bad_and_good_prices_land_where_expected(self):
        self._seed_bands()
        typo = _listing(
            "thuduc-typo",
            district="Thành phố Thủ Đức",
            area_sqm=75,
            price=45_000_000_000,
        )
        shifted = _listing(
            "quan8-shifted",
            district="Quận 8",
            area_sqm=49,
            price=33_600_005_000,
        )
        # 1.2B/m2. Already excluded by MAX_AREA_SQM before the price rule sees
        # it -- asserted here because the acceptance case is "gone", not "gone
        # via this particular rule".
        building = _listing(
            "quan1-building",
            district="Quận 1",
            area_sqm=4237,
            price=5_084_400_000_000,
        )
        genuine_a = _listing(
            "quan1-golden-river-a",
            district="Quận 1",
            area_sqm=122,
            price=56_000_000_000,
        )
        genuine_b = _listing(
            "quan1-golden-river-b",
            district="Quận 1",
            area_sqm=113,
            price=55_500_000_000,
        )

        # Live id 1121. The highest-ratio row the rule still keeps on real data
        # (2.73x its district band), so it is what pins the ratio from below:
        # the constant cannot creep down without eating the Landmark 81 tier.
        near_miss = _listing(
            "binhthanh-near-miss",
            district="Quận Bình Thạnh",
            area_sqm=110,
            price=45_000_000_000,
        )

        rows, stats = build_training_rows()
        kept = {row["id"] for row in rows}

        self.assertNotIn(typo.id, kept)
        self.assertNotIn(shifted.id, kept)
        self.assertNotIn(building.id, kept)
        self.assertIn(genuine_a.id, kept)
        self.assertIn(genuine_b.id, kept)
        self.assertIn(near_miss.id, kept)

        self.assertEqual(stats["dropped_price_implausible"], 2)
        self.assertEqual(stats["dropped_whole_building"], 1)

    def test_thin_district_is_judged_at_its_own_price_level(self):
        # Huyện Hóc Môn has three rows live, far too few for a p90, and they are
        # cheap: borrowing the citywide p90 as a level would set the bar at
        # ~390M/m2 and wave through every typo a cheap district can produce.
        # The band is the district's own median scaled by the citywide spread,
        # so the typo goes and the genuinely dearer row stays.
        self._seed_bands()
        _band("Huyện Hóc Môn", [22, 25, 28])
        typo = _listing(
            "hocmon-typo", district="Huyện Hóc Môn", area_sqm=100, price=25_000_000_000
        )
        dearer = _listing(
            "hocmon-dearer", district="Huyện Hóc Môn", area_sqm=118, price=12_000_000_000
        )
        rows, _ = build_training_rows()
        kept = {row["id"] for row in rows}
        self.assertNotIn(typo.id, kept)
        self.assertIn(dearer.id, kept)

    def test_rule_is_skipped_when_there_are_too_few_rows_to_form_a_band(self):
        _listing("clean")
        _, stats = build_training_rows()
        self.assertNotIn("dropped_price_implausible", stats)
