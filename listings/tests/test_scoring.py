import io
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone

from listings.management.commands import score_listings
from listings.models import Listing, PriceHistory, ScoringRun
from listings.tests.test_models import _make_listing


def _score():
    call_command("score_listings", stdout=io.StringIO())


class LowPhotosRuleTests(TestCase):
    def _scored_listing(self, source_id, **overrides):
        listing = _make_listing(
            source_id=source_id,
            url=f"https://batdongsan.com.vn/{source_id}-pr1",
            **overrides,
        )
        _score()
        listing.refresh_from_db()
        return listing

    def test_flags_listing_with_under_three_photos(self):
        listing = self._scored_listing("lp1", images=["a.jpg", "b.jpg"])
        self.assertTrue(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason,
            {"low_photos": {"triggered": True, "value": 2}},
        )

    def test_empty_gallery_is_zero_photos_and_flagged(self):
        listing = self._scored_listing("lp2", images=[])
        self.assertTrue(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason,
            {"low_photos": {"triggered": True, "value": 0}},
        )

    def test_three_photos_scored_but_not_flagged(self):
        listing = self._scored_listing("lp3", images=["a.jpg", "b.jpg", "c.jpg"])
        self.assertFalse(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason,
            {"low_photos": {"triggered": False, "value": 3}},
        )

    def test_null_images_left_untouched(self):
        listing = self._scored_listing("lp4", images=None)
        self.assertFalse(listing.is_anomaly)
        self.assertIsNone(listing.anomaly_reason)

    def test_inactive_listing_left_untouched(self):
        listing = self._scored_listing("lp5", images=["a.jpg"], is_active=False)
        self.assertFalse(listing.is_anomaly)
        self.assertIsNone(listing.anomaly_reason)

    def test_idempotent_rerun_leaves_state_unchanged(self):
        listing = self._scored_listing("lp6", images=["a.jpg"])
        _score()
        rerun = type(listing).objects.get(pk=listing.pk)
        self.assertTrue(rerun.is_anomaly)
        self.assertEqual(
            rerun.anomaly_reason,
            {"low_photos": {"triggered": True, "value": 1}},
        )

    def test_unflags_listing_that_gained_photos(self):
        listing = self._scored_listing("lp7", images=["a.jpg"])
        self.assertTrue(listing.is_anomaly)
        listing.images = ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]
        listing.save(update_fields=["images"])
        _score()
        listing.refresh_from_db()
        self.assertFalse(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason,
            {"low_photos": {"triggered": False, "value": 4}},
        )

    def test_scoring_does_not_move_last_seen_at(self):
        listing = self._scored_listing("lp8", images=["a.jpg"])
        before = listing.last_seen_at
        _score()
        listing.refresh_from_db()
        self.assertEqual(listing.last_seen_at, before)


class StaleListingRuleTests(TestCase):
    """§12 stale_listing: days_on_market > 90. images stays None in most cases
    so low_photos doesn't run and anomaly_reason isolates the rule under test."""

    def _scored_listing(self, source_id, **overrides):
        listing = _make_listing(
            source_id=source_id,
            url=f"https://batdongsan.com.vn/{source_id}-pr1",
            **overrides,
        )
        _score()
        listing.refresh_from_db()
        return listing

    def _posted(self, days_ago):
        return (timezone.now() - timedelta(days=days_ago)).date()

    def test_flags_listing_over_90_days(self):
        listing = self._scored_listing("sl1", posted_date=self._posted(100))
        self.assertTrue(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason,
            {"stale_listing": {"triggered": True, "value": 100}},
        )

    def test_recent_listing_scored_but_not_flagged(self):
        listing = self._scored_listing("sl2", posted_date=self._posted(30))
        self.assertFalse(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason,
            {"stale_listing": {"triggered": False, "value": 30}},
        )

    def test_exactly_90_days_not_flagged(self):
        listing = self._scored_listing("sl3", posted_date=self._posted(90))
        self.assertFalse(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason,
            {"stale_listing": {"triggered": False, "value": 90}},
        )

    def test_posted_date_null_falls_back_to_earliest_price_history(self):
        listing = _make_listing(
            source_id="sl4",
            url="https://batdongsan.com.vn/sl4-pr1",
            posted_date=None,
        )
        PriceHistory.objects.create(
            listing=listing,
            price=1000,
            observed_at=timezone.now() - timedelta(days=120),
        )
        _score()
        listing.refresh_from_db()
        self.assertTrue(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason,
            {"stale_listing": {"triggered": True, "value": 120}},
        )

    def test_no_posted_date_and_no_history_left_untouched(self):
        listing = self._scored_listing("sl5", posted_date=None)
        self.assertFalse(listing.is_anomaly)
        self.assertIsNone(listing.anomaly_reason)

    def test_combines_with_low_photos_into_two_key_dict(self):
        listing = self._scored_listing(
            "sl6",
            posted_date=self._posted(100),
            images=["a.jpg", "b.jpg", "c.jpg"],
        )
        self.assertTrue(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason,
            {
                "low_photos": {"triggered": False, "value": 3},
                "stale_listing": {"triggered": True, "value": 100},
            },
        )


class AnomalyScoredAtTests(TestCase):
    """anomaly_scored_at dates anomaly_reason's stored values, so a reader can
    tell how far they have drifted from live fields like days_on_market."""

    def test_set_when_a_rule_runs(self):
        before = timezone.now()
        listing = _make_listing(
            source_id="ax1",
            url="https://batdongsan.com.vn/ax1-pr1",
            images=["a.jpg"],
        )
        _score()
        listing.refresh_from_db()
        self.assertIsNotNone(listing.anomaly_scored_at)
        self.assertGreaterEqual(listing.anomaly_scored_at, before)

    def test_left_null_when_no_rule_covers_the_listing(self):
        listing = _make_listing(
            source_id="ax2",
            url="https://batdongsan.com.vn/ax2-pr1",
            images=None,
            posted_date=None,
        )
        _score()
        listing.refresh_from_db()
        self.assertIsNone(listing.anomaly_reason)
        self.assertIsNone(listing.anomaly_scored_at)

    def test_advances_on_rerun(self):
        listing = _make_listing(
            source_id="ax3",
            url="https://batdongsan.com.vn/ax3-pr1",
            images=["a.jpg"],
        )
        _score()
        listing.refresh_from_db()
        first = listing.anomaly_scored_at
        _score()
        listing.refresh_from_db()
        self.assertGreater(listing.anomaly_scored_at, first)

    def test_anomaly_only_row_leaves_predicted_at_null(self):
        # §12: predicted_at marks a run that actually produced a prediction. A
        # row the model skipped must not carry one just because rules ran.
        listing = _make_listing(
            source_id="ax4",
            url="https://batdongsan.com.vn/ax4-pr1",
            images=["a.jpg"],
        )
        _score()
        listing.refresh_from_db()
        self.assertIsNotNone(listing.anomaly_scored_at)
        self.assertIsNone(listing.predicted_at)
        self.assertIsNone(listing.predicted_price)


class PredictionTests(TestCase):
    """score_listings runs the committed model over the rows it can score.

    Population is forced by the model, not chosen: listing_age has no null
    fallback so a null posted_date reaches sklearn as NaN, and the fit was
    sale-only because rent prices are monthly (ml/dataset.py).
    """

    def _eligible(self, source_id, **overrides):
        defaults = dict(
            area_sqm=Decimal("80"),
            district="Quận 7",
            posted_date=(timezone.now() - timedelta(days=14)).date(),
        )
        defaults.update(overrides)
        return _make_listing(
            source_id=source_id,
            url=f"https://alonhadat.com.vn/{source_id}.html",
            **defaults,
        )

    def test_populates_predicted_price_and_predicted_at(self):
        before = timezone.now()
        listing = self._eligible("pr1")
        _score()
        listing.refresh_from_db()
        self.assertIsNotNone(listing.predicted_price)
        self.assertGreater(listing.predicted_price, 0)
        self.assertGreaterEqual(listing.predicted_at, before)

    def test_larger_area_predicts_higher_price(self):
        small = self._eligible("pr2", area_sqm=Decimal("60"))
        large = self._eligible("pr3", area_sqm=Decimal("150"))
        _score()
        small.refresh_from_db()
        large.refresh_from_db()
        self.assertGreater(large.predicted_price, small.predicted_price)

    def test_skips_listing_without_area(self):
        listing = self._eligible("pr4", area_sqm=None)
        _score()
        listing.refresh_from_db()
        self.assertIsNone(listing.predicted_price)
        self.assertIsNone(listing.predicted_at)

    def test_skips_listing_without_posted_date(self):
        listing = self._eligible("pr5", posted_date=None)
        _score()
        listing.refresh_from_db()
        self.assertIsNone(listing.predicted_price)
        self.assertIsNone(listing.predicted_at)

    def test_skips_rent_listing(self):
        listing = self._eligible("pr6", listing_intent="rent")
        _score()
        listing.refresh_from_db()
        self.assertIsNone(listing.predicted_price)
        self.assertIsNone(listing.predicted_at)

    def test_skips_inactive_listing(self):
        listing = self._eligible("pr7", is_active=False)
        _score()
        listing.refresh_from_db()
        self.assertIsNone(listing.predicted_price)
        self.assertIsNone(listing.predicted_at)

    def test_null_district_falls_back_instead_of_failing(self):
        # features.district_median_price fills an unknown district with the
        # train-wide median, so a null district is scoreable, not a skip.
        listing = self._eligible("pr8", district=None)
        _score()
        listing.refresh_from_db()
        self.assertIsNotNone(listing.predicted_price)

    def test_prediction_does_not_move_last_seen_at(self):
        listing = self._eligible("pr9")
        before = listing.last_seen_at
        _score()
        listing.refresh_from_db()
        self.assertEqual(listing.last_seen_at, before)

    def test_skips_listing_past_the_training_area_cap(self):
        # A random forest cannot extrapolate -- past dataset.MAX_AREA_SQM it
        # returns its top leaf, a confident number for a whole building.
        listing = self._eligible("pr10", area_sqm=Decimal("900"))
        _score()
        listing.refresh_from_db()
        self.assertIsNone(listing.predicted_price)
        self.assertIsNone(listing.predicted_at)

    def test_skips_listing_past_the_training_bedroom_cap(self):
        listing = self._eligible("pr11", bedrooms=40)
        _score()
        listing.refresh_from_db()
        self.assertIsNone(listing.predicted_price)
        self.assertIsNone(listing.predicted_at)

    def test_clears_prediction_when_row_leaves_the_population(self):
        # §12: predicted_price is current-state output, overwritten each run.
        # A row that drops out must not keep the last run's number forever.
        listing = self._eligible("pr12")
        _score()
        listing.refresh_from_db()
        self.assertIsNotNone(listing.predicted_price)
        listing.area_sqm = Decimal("900")
        listing.save(update_fields=["area_sqm"])
        _score()
        listing.refresh_from_db()
        self.assertIsNone(listing.predicted_price)
        self.assertIsNone(listing.predicted_at)


class ScoringRunTests(TestCase):
    def test_a_run_row_is_written_with_finished_at_set(self):
        _make_listing(
            source_id="sr1", url="https://alonhadat.com.vn/sr1.html", images=["a.jpg"]
        )
        _score()
        run = ScoringRun.objects.latest("id")
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.scored, 1)

    def test_model_fingerprint_is_recorded(self):
        _make_listing(
            source_id="sr2", url="https://alonhadat.com.vn/sr2.html", images=["a.jpg"]
        )
        _score()
        run = ScoringRun.objects.latest("id")
        self.assertIsNotNone(run.model_fingerprint)
        self.assertEqual(len(run.model_fingerprint), 12)

    def test_empty_accuracy_population_stores_null_metrics(self):
        _make_listing(
            source_id="sr3", url="https://alonhadat.com.vn/sr3.html",
            images=["a.jpg"], price=None,
        )
        _score()
        run = ScoringRun.objects.latest("id")
        self.assertEqual(run.n_compared, 0)
        self.assertIsNone(run.median_ape)
        self.assertIsNone(run.mae_vnd)

    def test_no_failures_stores_null_status_counts(self):
        _make_listing(
            source_id="sr4", url="https://alonhadat.com.vn/sr4.html", images=["a.jpg"]
        )
        _score()
        run = ScoringRun.objects.latest("id")
        self.assertIsNone(run.status_counts)
        self.assertEqual(run.error_count, 0)


class ScoringFailureTests(TestCase):
    def _scorable(self, source_id):
        return _make_listing(
            source_id=source_id,
            url=f"https://alonhadat.com.vn/{source_id}.html",
            images=["a.jpg"],
            area_sqm=Decimal("70"),
            posted_date=timezone.localdate(),
        )

    def test_model_load_failure_is_counted_and_does_not_kill_the_command(self):
        self._scorable("mf1")
        with patch.object(score_listings, "load", side_effect=OSError("no model.pkl")):
            _score()
        run = ScoringRun.objects.latest("id")
        self.assertEqual(run.status_counts, {"model_load_failed": 1})
        self.assertEqual(run.error_count, 1)

    def test_anomaly_rules_still_run_when_the_model_fails(self):
        # This is the point of the change: low_photos and stale_listing do not
        # need the model, and used to die with it.
        listing = self._scorable("mf2")
        with patch.object(score_listings, "load", side_effect=OSError("no model.pkl")):
            _score()
        listing.refresh_from_db()
        self.assertTrue(listing.is_anomaly)
        self.assertEqual(
            listing.anomaly_reason["low_photos"], {"triggered": True, "value": 1}
        )

    def test_inference_failure_is_counted_separately_from_load_failure(self):
        self._scorable("mf3")
        with patch.object(score_listings, "predict", side_effect=ValueError("nan")):
            _score()
        run = ScoringRun.objects.latest("id")
        self.assertEqual(run.status_counts, {"inference_failed": 1})

    def test_a_row_that_fails_to_save_is_counted_and_the_run_continues(self):
        _make_listing(
            source_id="mf4", url="https://alonhadat.com.vn/mf4.html", images=["a.jpg"]
        )
        good = _make_listing(
            source_id="mf5", url="https://alonhadat.com.vn/mf5.html",
            images=["a.jpg", "b.jpg", "c.jpg"],
        )
        original = Listing.save
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DatabaseError("write failed")
            return original(self, *args, **kwargs)

        # assertLogs keeps the expected "listing_save_failed" error out of the
        # suite's console output, where it reads like a live incident
        with patch.object(Listing, "save", flaky):
            with self.assertLogs(
                "listings.management.commands.score_listings", level="ERROR"
            ):
                _score()
        run = ScoringRun.objects.latest("id")
        self.assertEqual(run.status_counts, {"listing_save_failed": 1})
        good.refresh_from_db()
        self.assertIsNotNone(good.anomaly_scored_at)


class ModelFailureDoesNotWipePredictionsTests(TestCase):
    """A caught model failure must degrade, not destroy.

    The null-out branch exists for a row that left the model's population; it
    cannot tell that from "the model never ran", and an empty predictions dict
    looks identical to both.
    """

    def test_load_failure_leaves_existing_predictions_intact(self):
        # area_sqm + posted_date are required for the row to reach the model at
        # all; without them _predictions returns early and load() never runs.
        listing = _make_listing(
            source_id="mw1", url="https://alonhadat.com.vn/mw1.html",
            images=["a.jpg"], area_sqm=Decimal("70"),
            posted_date=timezone.localdate(),
            predicted_price=Decimal("3720083477"),
        )
        with patch.object(score_listings, "load", side_effect=OSError("no model.pkl")):
            _score()
        listing.refresh_from_db()
        self.assertEqual(listing.predicted_price, Decimal("3720083477"))

    def test_inference_failure_leaves_existing_predictions_intact(self):
        listing = _make_listing(
            source_id="mw2", url="https://alonhadat.com.vn/mw2.html",
            images=["a.jpg"], area_sqm=Decimal("70"),
            posted_date=timezone.localdate(),
            predicted_price=Decimal("3720083477"),
        )
        with patch.object(score_listings, "predict", side_effect=ValueError("nan")):
            _score()
        listing.refresh_from_db()
        self.assertEqual(listing.predicted_price, Decimal("3720083477"))

    def test_a_healthy_run_still_clears_a_row_that_left_the_population(self):
        # The null-out branch must keep working when the model did run.
        listing = _make_listing(
            source_id="mw3", url="https://alonhadat.com.vn/mw3.html",
            images=["a.jpg"], posted_date=None,
            predicted_price=Decimal("3720083477"),
        )
        _score()
        listing.refresh_from_db()
        self.assertIsNone(listing.predicted_price)
        self.assertIsNone(listing.predicted_at)
