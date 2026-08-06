from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from listings.analytics import (
    accuracy_metrics,
    accuracy_trend,
    bar_max,
    run_status,
    scrapes_per_day,
)
from listings.models import ScoringRun, ScrapeRun


def _d(value):
    return Decimal(str(value))


class AccuracyMetricsTests(SimpleTestCase):
    def test_empty_population_returns_nulls_not_zeros(self):
        # A zero would read as a perfect model. Null reads as "not measured".
        self.assertEqual(accuracy_metrics([]), (None, None, 0))

    def test_perfect_predictions_score_zero_error(self):
        mae, median_ape, n = accuracy_metrics([(_d(100), _d(100)), (_d(200), _d(200))])
        self.assertEqual(mae, Decimal("0"))
        self.assertEqual(median_ape, Decimal("0.0000"))
        self.assertEqual(n, 2)

    def test_median_ape_is_the_middle_ratio_not_the_mean(self):
        # ratios 0.10, 0.20, 3.00 -> median 0.20, mean would be ~1.10
        pairs = [(_d(110), _d(100)), (_d(120), _d(100)), (_d(400), _d(100))]
        mae, median_ape, n = accuracy_metrics(pairs)
        self.assertEqual(median_ape, Decimal("0.2000"))
        self.assertEqual(n, 3)

    def test_mae_is_the_mean_absolute_error_in_whole_vnd(self):
        pairs = [(_d(110), _d(100)), (_d(70), _d(100))]
        mae, median_ape, n = accuracy_metrics(pairs)
        self.assertEqual(mae, Decimal("20"))

    def test_under_and_over_prediction_both_count_as_error(self):
        under = accuracy_metrics([(_d(50), _d(100))])
        over = accuracy_metrics([(_d(150), _d(100))])
        self.assertEqual(under[1], over[1])

    def test_null_prediction_rows_are_excluded(self):
        mae, median_ape, n = accuracy_metrics([(None, _d(100)), (_d(110), _d(100))])
        self.assertEqual(n, 1)
        self.assertEqual(median_ape, Decimal("0.1000"))

    def test_zero_and_null_actual_price_rows_are_excluded_not_divided_by(self):
        mae, median_ape, n = accuracy_metrics(
            [(_d(110), _d(0)), (_d(110), None), (_d(110), _d(100))]
        )
        self.assertEqual(n, 1)

    def test_even_population_averages_the_two_middle_ratios(self):
        pairs = [(_d(110), _d(100)), (_d(130), _d(100))]
        self.assertEqual(accuracy_metrics(pairs)[1], Decimal("0.2000"))


class ScrapesPerDayTests(TestCase):
    def _run(self, days_ago, seen, finished=True, errors=0):
        started = timezone.now() - timedelta(days=days_ago)
        return ScrapeRun.objects.create(
            source_site="alonhadat",
            started_at=started,
            finished_at=started + timedelta(minutes=5) if finished else None,
            listings_seen=seen,
            error_count=errors,
        )

    def test_returns_one_entry_per_day_including_days_with_no_run(self):
        self._run(0, 800)
        rows = scrapes_per_day(days=7)
        self.assertEqual(len(rows), 7)
        self.assertEqual([r["status"] for r in rows[:6]], ["no run"] * 6)

    def test_a_day_with_a_healthy_run_reports_its_volume(self):
        self._run(0, 800)
        self.assertEqual(scrapes_per_day(days=3)[-1]["seen"], 800)
        self.assertEqual(scrapes_per_day(days=3)[-1]["status"], "ok")

    def test_two_runs_on_one_day_sum_their_volume(self):
        self._run(0, 400)
        self._run(0, 350)
        row = scrapes_per_day(days=3)[-1]
        self.assertEqual(row["seen"], 750)
        self.assertEqual(row["runs"], 2)

    def test_a_day_of_only_unfinished_runs_reads_aborted_not_zero_volume(self):
        # This is DB reality: ScrapeRun 15/17/18/20 are exactly this shape.
        self._run(0, 0, finished=False)
        self.assertEqual(scrapes_per_day(days=3)[-1]["status"], "aborted")

    def test_a_finished_run_that_saw_nothing_reads_empty_not_aborted(self):
        self._run(0, 0, finished=True)
        self.assertEqual(scrapes_per_day(days=3)[-1]["status"], "empty")

    def test_runs_older_than_the_window_are_excluded(self):
        self._run(40, 900)
        self.assertEqual(sum(r["seen"] for r in scrapes_per_day(days=7)), 0)


class BarMaxTests(SimpleTestCase):
    def test_returns_the_largest_value(self):
        self.assertEqual(bar_max([{"seen": 50}, {"seen": 100}], "seen"), 100)

    def test_all_zero_rows_return_zero(self):
        # widthratio turns a 0 denominator into "0", so no guard is needed here.
        self.assertEqual(bar_max([{"seen": 0}, {"seen": 0}], "seen"), 0)

    def test_empty_input_returns_zero(self):
        self.assertEqual(bar_max([], "seen"), 0)


class RunStatusTests(TestCase):
    def test_unfinished_and_recent_reads_running(self):
        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now()
        )
        self.assertEqual(run_status(run), "running")

    def test_unfinished_and_old_reads_aborted(self):
        run = ScrapeRun.objects.create(
            source_site="alonhadat",
            started_at=timezone.now() - timedelta(hours=7),
        )
        self.assertEqual(run_status(run), "aborted")

    def test_finished_with_errors_says_so(self):
        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now(),
            finished_at=timezone.now(), listings_seen=800, error_count=2,
        )
        self.assertEqual(run_status(run), "ok, 2 errors")

    def test_scoring_run_has_no_listings_seen_and_is_not_called_empty(self):
        run = ScoringRun.objects.create(
            started_at=timezone.now(), finished_at=timezone.now()
        )
        self.assertEqual(run_status(run), "ok")


class AccuracyTrendTests(TestCase):
    def _run(self, minutes_ago, ape, fingerprint="aaaaaaaaaaaa"):
        return ScoringRun.objects.create(
            started_at=timezone.now() - timedelta(minutes=minutes_ago),
            finished_at=timezone.now() - timedelta(minutes=minutes_ago),
            median_ape=Decimal(str(ape)),
            n_compared=700,
            model_fingerprint=fingerprint,
        )

    def test_returns_runs_oldest_first_so_the_chart_reads_left_to_right(self):
        self._run(30, "0.30")
        self._run(10, "0.20")
        rows = accuracy_trend()
        self.assertEqual(
            [r["median_ape"] for r in rows], [Decimal("0.3000"), Decimal("0.2000")]
        )

    def test_runs_without_a_metric_are_excluded(self):
        ScoringRun.objects.create(started_at=timezone.now())
        self.assertEqual(accuracy_trend(), [])

    def test_median_ape_is_exposed_as_a_percentage_for_display(self):
        self._run(10, "0.2287")
        self.assertAlmostEqual(accuracy_trend()[0]["median_ape_pct"], 22.87, places=2)

    def test_a_fingerprint_change_marks_the_run_as_a_new_model(self):
        self._run(30, "0.30", fingerprint="aaaaaaaaaaaa")
        self._run(20, "0.30", fingerprint="aaaaaaaaaaaa")
        self._run(10, "0.25", fingerprint="bbbbbbbbbbbb")
        self.assertEqual(
            [r["new_model"] for r in accuracy_trend()], [False, False, True]
        )


class RunStatusDistinguishesBlockedFromQuietTests(TestCase):
    """The two states this dashboard exists to surface both present as zero
    volume. Checking "empty" first collapses them into one word."""

    def test_a_walled_run_reads_blocked_not_empty(self):
        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now(),
            finished_at=timezone.now(), listings_seen=0, error_count=1,
            status_counts={"srp_bot_challenge": 1},
        )
        self.assertEqual(run_status(run), "blocked")

    def test_a_quiet_finished_run_still_reads_empty(self):
        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now(),
            finished_at=timezone.now(), listings_seen=0, error_count=0,
        )
        self.assertEqual(run_status(run), "empty")

    def test_a_run_aborted_key_reads_aborted_immediately_not_running(self):
        # Definitive evidence beats the 6h elapsed-time heuristic.
        run = ScrapeRun.objects.create(
            source_site="alonhadat", started_at=timezone.now(),
            listings_seen=4, error_count=1,
            status_counts={"run_aborted": 1},
        )
        self.assertEqual(run_status(run), "aborted")

    def test_a_scoring_run_is_never_called_empty_or_blocked(self):
        run = ScoringRun.objects.create(
            started_at=timezone.now(), finished_at=timezone.now(),
        )
        self.assertEqual(run_status(run), "ok")

    def test_a_scoring_run_with_errors_reports_them(self):
        run = ScoringRun.objects.create(
            started_at=timezone.now(), finished_at=timezone.now(),
            error_count=1, status_counts={"model_load_failed": 1},
        )
        self.assertEqual(run_status(run), "ok, 1 error")


class ScrapesPerDayBlockedTests(TestCase):
    def test_a_day_whose_runs_saw_nothing_and_errored_reads_blocked(self):
        started = timezone.now()
        ScrapeRun.objects.create(
            source_site="alonhadat", started_at=started, finished_at=started,
            listings_seen=0, error_count=1,
        )
        self.assertEqual(scrapes_per_day(days=3)[-1]["status"], "blocked")

    def test_a_single_error_is_not_pluralised(self):
        started = timezone.now()
        ScrapeRun.objects.create(
            source_site="alonhadat", started_at=started, finished_at=started,
            listings_seen=800, error_count=1,
        )
        self.assertEqual(scrapes_per_day(days=3)[-1]["status"], "ok, 1 error")
