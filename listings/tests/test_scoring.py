import io

from django.core.management import call_command
from django.test import TestCase

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
