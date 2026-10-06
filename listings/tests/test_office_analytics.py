from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from listings.models import OfficeBuilding
from listings.office_analytics import (
    MIN_SUMMARY_N,
    axis_top,
    coverage,
    dashboard,
    group_rows,
    place,
)


def _b(rent=None, district="Quận 1", grade="C", floor=None, fee=None):
    """One building as dashboard() reads it from .values()."""
    rent = None if rent is None else Decimal(str(rent))
    return {
        "district": district,
        "grade": grade,
        "rent_min_usd": rent,
        "rent_max_usd": rent,
        "typical_floor_sqm": None if floor is None else Decimal(str(floor)),
        "service_fee_usd": None if fee is None else Decimal(str(fee)),
    }


def _row(rows, key):
    return next(row for row in rows if row["key"] == key)


class GroupRowsTests(SimpleTestCase):
    def test_unpriced_building_counts_toward_total_only(self):
        row = _row(group_rows([_b(12), _b(None)], "district"), "Quận 1")
        self.assertEqual((row["total"], row["priced"]), (2, 1))

    def test_group_with_no_rent_still_gets_a_row(self):
        # Quận Bình Tân today: 1 building, no published rent. Dropping the row
        # would hide that the district exists at all.
        rows = group_rows([_b(None, district="Quận Bình Tân")], "district")
        row = _row(rows, "Quận Bình Tân")
        self.assertEqual(
            (row["total"], row["priced"], row["median"], row["low"]), (1, 0, None, None)
        )

    def test_null_key_is_its_own_row(self):
        rows = group_rows([_b(12, grade="A"), _b(13, grade=None)], "grade")
        self.assertEqual({row["key"] for row in rows}, {"A", None})

    def test_rent_is_the_midpoint_of_the_quoted_range(self):
        building = _b()
        building["rent_min_usd"], building["rent_max_usd"] = Decimal("27"), Decimal("28")
        self.assertEqual(_row(group_rows([building], "district"), "Quận 1")["low"], 27.5)

    def test_under_min_summary_n_has_a_range_but_no_quartiles(self):
        row = _row(group_rows([_b(10), _b(30), _b(20)], "district"), "Quận 1")
        self.assertEqual((row["low"], row["high"]), (10.0, 30.0))
        self.assertIsNone(row["median"])

    def test_ten_rents_get_quartiles(self):
        # Exclusive-method quartiles of 10..19. If MIN_SUMMARY_N is retuned
        # above 10 this fails on purpose: update the case with it.
        self.assertEqual(MIN_SUMMARY_N, 10)
        row = _row(group_rows([_b(r) for r in range(10, 20)], "district"), "Quận 1")
        self.assertEqual((row["p25"], row["median"], row["p75"]), (11.75, 14.5, 17.25))

    def test_bundled_fee_is_counted_not_averaged(self):
        buildings = [_b(fee=0), _b(fee=0), _b(fee=3), _b(fee=5), _b(fee=None)]
        row = _row(group_rows(buildings, "grade"), "C")
        self.assertEqual((row["fee_median"], row["fee_bundled"]), (4.0, 2))

    def test_null_floor_is_excluded_from_the_median(self):
        buildings = [_b(floor=200), _b(floor=None), _b(floor=500)]
        self.assertEqual(_row(group_rows(buildings, "grade"), "C")["floor_median"], 350.0)


def _summary(p25, median, p75):
    return {"p25": p25, "median": median, "p75": p75}


class AxisAndPlaceTests(SimpleTestCase):
    def test_axis_rounds_the_highest_p75_up_to_the_step(self):
        # Grade A's live p75 is 52.6
        rows = [_summary(12, 14, 17), _summary(37.8, 44.5, 52.6)]
        self.assertEqual(axis_top(rows), 60)

    def test_axis_is_none_without_a_summarized_row(self):
        self.assertIsNone(axis_top([{"p75": None}]))
        self.assertIsNone(axis_top([]))

    def test_place_widths_are_differences_of_rounded_edges(self):
        # Edges round to 11, 20, 31. Rounding each width on its own would give
        # 11, 10, 10 and move the median notch one point right.
        row = _summary(10.6, 20.2, 30.6)
        place([row], 100)
        self.assertEqual(row["bar"], {"lead": 11, "low": 9, "high": 11})

    def test_place_skips_rows_without_quartiles(self):
        row = _summary(None, None, None)
        place([row], 60)
        self.assertNotIn("bar", row)


class CoverageTests(SimpleTestCase):
    def test_counts_published_fields_and_bundled_fees(self):
        rows = coverage(
            [
                _b(12, grade=None, floor=200, fee=0),
                _b(None, grade="B", fee=None),
                _b(None, grade="B", fee=3),
            ]
        )
        self.assertEqual(
            {row["label"]: row["count"] for row in rows},
            {"Rent": 1, "Grade": 2, "Typical floor area": 1, "Service fee": 2},
        )
        # Only the 0 is bundled: 3 is a charged fee, None is unpublished.
        self.assertEqual(rows[-1]["bundled"], 1)


def _save(n, rent=None, district="Quận 1", grade="C", is_active=True):
    rent = None if rent is None else Decimal(str(rent))
    return OfficeBuilding.objects.create(
        source_id=str(n),
        url=f"https://maisonoffice.vn/test/{n}/",
        name=f"Building {n}",
        district=district,
        grade=grade,
        rent_min_usd=rent,
        rent_max_usd=rent,
        last_seen_at=timezone.now(),
        is_active=is_active,
    )


class DashboardTests(TestCase):
    def test_small_district_with_high_rents_is_listed_not_ranked(self):
        # The live trap: sorted by median, Huyện Bình Chánh (2 priced) ranks
        # third, above Quận 2 (137).
        for n in range(10):
            _save(n, rent=10 + n)
        _save(100, rent=30, district="Huyện Bình Chánh")
        _save(101, rent=40, district="Huyện Bình Chánh")
        ctx = dashboard()
        self.assertEqual([r["label"] for r in ctx["districts"]], ["Quận 1"])
        self.assertEqual([r["label"] for r in ctx["thin_districts"]], ["Huyện Bình Chánh"])

    def test_ranked_districts_sort_by_median_descending(self):
        for n in range(10):
            _save(n, rent=10 + n)
            _save(100 + n, rent=20 + n, district="Quận 3")
        self.assertEqual(
            [r["label"] for r in dashboard()["districts"]], ["Quận 3", "Quận 1"]
        )

    def test_grades_run_a_b_c_then_not_graded(self):
        for n, grade in enumerate([None, "C", "A", "B"]):
            _save(n, grade=grade)
        self.assertEqual(
            [r["label"] for r in dashboard()["grades"]],
            ["Grade A", "Grade B", "Grade C", "Not graded"],
        )

    def test_inactive_buildings_are_excluded(self):
        _save(1, rent=12)
        _save(2, rent=13, is_active=False)
        self.assertEqual(dashboard()["total"], 1)

    def test_each_chart_gets_its_own_scale(self):
        # Grade A tops out near 50 USD. No district has 10 grade-A buildings,
        # so the district scale follows Quận 3's grade-C rents instead.
        for n in range(10):
            _save(n, rent=40 + n, grade="A", district=f"Quận {n + 4}")
            _save(100 + n, rent=10 + n, district="Quận 3")
        ctx = dashboard()
        self.assertEqual((ctx["grade_axis"], ctx["district_axis"]), (50, 20))

    def test_empty_table_returns_zero_counts_and_no_axis(self):
        ctx = dashboard()
        self.assertEqual(
            (ctx["total"], ctx["priced"], ctx["grade_axis"], ctx["district_axis"]),
            (0, 0, None, None),
        )
        self.assertEqual(ctx["grades"], [])


class OfficeDashboardViewTests(TestCase):
    def test_page_renders_rows_and_never_prints_none(self):
        for n in range(10):
            _save(n, rent=10 + n)
        # Every nullable field null at once: rent, grade, floor, fee, district.
        _save(99, district=None, grade=None)
        response = self.client.get("/offices/")
        self.assertContains(response, "Rent by grade")
        self.assertContains(response, "Quận 1")
        self.assertContains(response, "Not graded")
        self.assertContains(response, "No district")
        self.assertNotContains(response, "None")

    def test_empty_table_renders_the_empty_state(self):
        response = self.client.get("/offices/")
        self.assertContains(response, "No office buildings have been crawled yet.")
        self.assertNotContains(response, "Rent by grade")
