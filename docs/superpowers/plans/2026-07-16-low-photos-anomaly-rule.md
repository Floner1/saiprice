# low_photos Anomaly Rule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `score_listings` management command that recomputes the `low_photos` anomaly flag over active, LDP-enriched listings, idempotently.

**Architecture:** One new management command, one new test file. No `ml/` directory, no stubs for `price_gap`/`stale_listing` (both blocked on the ML model). The rule is a full recompute: every scored listing gets `is_anomaly` and `anomaly_reason` overwritten each run, which makes reruns idempotent and un-flags listings that gain photos.

**Tech Stack:** Django 5.2 management command, Django `TestCase` (project standard per CLAUDE.md §14 — no pytest).

**Confirmed design decisions (Peter, 2026-07-16):**
- `images IS NULL` means "LDP never enriched" (see `scrape_listings._enrich_from_ldp`), not "zero photos" — 62 of 93 active listings are null today. These rows are **skipped entirely**: `is_anomaly`/`anomaly_reason` untouched. Deviates deliberately from §12's literal `len(images or []) < 3`.
- `images == []` means "LDP parsed, gallery empty" (`alonhadat.parse_ldp_extras`) — real zero photos, scored and flagged.
- Threshold `< 3` kept, validated against live data: flags 1 of 31 enriched listings (~3%).
- `anomaly_reason` shape: `{"low_photos": {"triggered": bool, "value": <photo count>}}` — §12's dict shape, but only the rule that actually ran. No faked keys for unbuilt rules.
- `predicted_at` stays null — no prediction happens in this run.
- Scored set: `is_active=True, images__isnull=False`. §12's "with both price and predicted_price populated" qualifier belongs to the price-gap rule; applied literally today it would score zero rows.
- Writes use `save(update_fields=["is_anomaly", "anomaly_reason"])` — `last_seen_at` must never move on a scoring write (models.py:74 comment).

---

### Task 1: score_listings command + tests (TDD)

**Files:**
- Create: `listings/tests/test_scoring.py`
- Create: `listings/management/commands/score_listings.py`

- [ ] **Step 1: Write the failing tests**

`listings/tests/test_scoring.py`:

```python
from django.core.management import call_command
from django.test import TestCase

from listings.tests.test_models import _make_listing


def _score():
    call_command("score_listings")


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
```

- [ ] **Step 2: Run tests, verify they fail with "Unknown command: 'score_listings'"**

Run: `python manage.py test listings.tests.test_scoring -v 2`
Expected: every test errors with `CommandError: Unknown command: 'score_listings'` (command doesn't exist yet).

- [ ] **Step 3: Write the minimal command**

`listings/management/commands/score_listings.py`:

```python
from django.core.management.base import BaseCommand

from listings.models import Listing

LOW_PHOTOS_THRESHOLD = 3


class Command(BaseCommand):
    help = (
        "Recompute anomaly flags over active enriched listings (CLAUDE.md §12). "
        "low_photos only until the ML model exists."
    )

    def handle(self, *args, **options):
        scored = flagged = 0
        # images IS NULL means the LDP was never visited (scrape_listings),
        # not zero photos -- those rows are skipped, not flagged.
        for listing in Listing.objects.filter(is_active=True, images__isnull=False):
            count = len(listing.images)
            triggered = count < LOW_PHOTOS_THRESHOLD
            listing.is_anomaly = triggered
            listing.anomaly_reason = {
                "low_photos": {"triggered": triggered, "value": count}
            }
            # update_fields: a scoring write must never touch last_seen_at
            listing.save(update_fields=["is_anomaly", "anomaly_reason"])
            scored += 1
            flagged += triggered
        self.stdout.write(f"scored={scored} flagged={flagged}")
```

- [ ] **Step 4: Run tests, verify all pass**

Run: `python manage.py test listings.tests.test_scoring -v 2`
Expected: `OK`, 8 tests.

- [ ] **Step 5: Run the full suite (regression check)**

Run: `python manage.py test listings`
Expected: `OK`, no failures anywhere else.

- [ ] **Step 6: Commit**

```bash
git add listings/management/commands/score_listings.py listings/tests/test_scoring.py docs/superpowers/plans/2026-07-16-low-photos-anomaly-rule.md
git commit -m "Add score_listings with the low_photos anomaly rule"
```

(No Claude/Anthropic attribution in the commit message — CLAUDE.md §4.)

---

### Task 2: Verify against the live dev environment

- [ ] **Step 1: Run the command against the dev database**

Run: `python manage.py score_listings`
Expected (from the 2026-07-16 distribution pull): `scored=31 flagged=1` — 31 enriched active listings, exactly one (the 2-photo listing) flagged. 62 null-images rows untouched.

- [ ] **Step 2: Confirm through the live server per the project verify recipe**

Follow the project `verify` skill (build/launch recipe). Check `GET /api/listings/?is_anomaly=true` returns exactly the flagged listing with the one-key `anomaly_reason` dict, and that a re-run of `score_listings` changes nothing (same `scored=31 flagged=1`, same API response).

---

## Self-Review

- **Spec coverage:** §12 low_photos (adapted for null semantics, confirmed by Peter), §14 anomaly-rule tests (triggered + not-triggered), idempotency requirement, ponytail minimum (2 files, no scaffolding). Out of scope confirmed excluded: price_gap, stale_listing, individual_seller, ml/.
- **Placeholder scan:** clean — every step carries its full code, commands, and expected output.
- **Type consistency:** `anomaly_reason` shape identical across command and every test assertion.
