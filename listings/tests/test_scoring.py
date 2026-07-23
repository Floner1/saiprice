import io
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from listings.models import PriceHistory
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
