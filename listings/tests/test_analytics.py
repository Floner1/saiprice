from decimal import Decimal

from django.test import SimpleTestCase

from listings.analytics import accuracy_metrics


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
