from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from listings.models import Agent, Listing, PriceHistory
from listings.scraping.parsers import ParsedListing
from listings.upsert import upsert


def _make_listing(**overrides):
    defaults = dict(
        source_site="batdongsan",
        source_id="1",
        url="https://batdongsan.com.vn/listing-pr1",
        title="Test listing",
        category_id_source=324,
        property_type="apartment",
        listing_intent="sale",
        last_seen_at=timezone.now(),
    )
    defaults.update(overrides)
    return Listing.objects.create(**defaults)


class UrlFieldLengthTests(TestCase):
    def test_url_max_length_is_500(self):
        field = Listing._meta.get_field("url")
        self.assertEqual(field.max_length, 500)


class ListingUniquenessTests(TestCase):
    def test_source_site_source_id_unique_together_enforced(self):
        _make_listing(source_id="dup", url="https://batdongsan.com.vn/a-pr1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _make_listing(source_id="dup", url="https://batdongsan.com.vn/b-pr1")


class AgentDedupTests(TestCase):
    def test_two_listings_same_agent_reuse_one_agent_row(self):
        agent, created = Agent.objects.update_or_create(
            source_site="batdongsan", source_id="900", defaults={"name": "Tuấn"}
        )
        self.assertTrue(created)

        listing_a = _make_listing(source_id="a", url="https://batdongsan.com.vn/a-pr1", agent=agent)

        agent_again, created_again = Agent.objects.update_or_create(
            source_site="batdongsan", source_id="900", defaults={"name": "Tuấn"}
        )
        listing_b = _make_listing(source_id="b", url="https://batdongsan.com.vn/b-pr1", agent=agent_again)

        self.assertFalse(created_again)
        self.assertEqual(Agent.objects.filter(source_site="batdongsan", source_id="900").count(), 1)
        self.assertEqual(listing_a.agent_id, listing_b.agent_id)


class PriceHistoryUpsertPatternTests(TestCase):
    """Exercises the §7 upsert pattern directly against the ORM: a PriceHistory
    row is inserted only when the incoming price differs from what's stored."""

    def _upsert_pass(self, source_id, url, price):
        existing = Listing.objects.filter(source_site="batdongsan", source_id=source_id).first()
        if existing and existing.price != price:
            PriceHistory.objects.create(
                listing=existing, price=price, observed_at=timezone.now()
            )
        listing, created = Listing.objects.update_or_create(
            source_site="batdongsan",
            source_id=source_id,
            defaults={
                "url": url,
                "title": "Test",
                "category_id_source": 324,
                "property_type": "apartment",
                "listing_intent": "sale",
                "price": price,
                "last_seen_at": timezone.now(),
            },
        )
        if created:
            PriceHistory.objects.create(listing=listing, price=price, observed_at=timezone.now())
        return listing

    def test_first_insert_creates_exactly_one_price_history_row(self):
        listing = self._upsert_pass("1", "https://batdongsan.com.vn/pr1", 1000)
        self.assertEqual(PriceHistory.objects.filter(listing=listing).count(), 1)

    def test_same_price_creates_no_new_row(self):
        listing = self._upsert_pass("2", "https://batdongsan.com.vn/pr2", 1000)
        self._upsert_pass("2", "https://batdongsan.com.vn/pr2", 1000)
        self.assertEqual(PriceHistory.objects.filter(listing=listing).count(), 1)

    def test_price_change_creates_exactly_one_new_row(self):
        listing = self._upsert_pass("3", "https://batdongsan.com.vn/pr3", 1000)
        self._upsert_pass("3", "https://batdongsan.com.vn/pr3", 2000)
        self.assertEqual(PriceHistory.objects.filter(listing=listing).count(), 2)


class NegotiablePriceUpsertTests(TestCase):
    """§5.3: a negotiable ("Thỏa thuận") listing still gets exactly one
    PriceHistory row (with null price) at insert time, so the
    days_on_market posted_date-null fallback has something to compute from."""

    def _parsed(self):
        return ParsedListing(
            source_site="alonhadat",
            source_id="neg1",
            agent_source_id=None,
            agent_name=None,
            expired=False,
            fields={
                "url": "https://alonhadat.com.vn/neg1.html",
                "title": "Negotiable listing",
                "property_type": "house",
                "listing_intent": "sale",
                "price": None,
            },
        )

    def test_null_price_insert_creates_one_price_history_row(self):
        upsert(self._parsed())
        listing = Listing.objects.get(source_site="alonhadat", source_id="neg1")
        history = PriceHistory.objects.filter(listing=listing)
        self.assertEqual(history.count(), 1)
        self.assertIsNone(history.get().price)

    def test_days_on_market_fallback_works_for_negotiable_listing(self):
        upsert(self._parsed())
        listing = Listing.objects.get(source_site="alonhadat", source_id="neg1")
        self.assertIsNone(listing.posted_date)
        self.assertEqual(listing.days_on_market, 0)


class DaysOnMarketTests(TestCase):
    def test_uses_posted_date_when_present_and_still_active(self):
        posted = (timezone.now() - timedelta(days=10)).date()
        listing = _make_listing(
            source_id="dom1",
            url="https://batdongsan.com.vn/dom1-pr1",
            posted_date=posted,
        )
        self.assertEqual(listing.days_on_market, 10)

    def test_uses_delisted_at_as_end_when_delisted(self):
        posted = (timezone.now() - timedelta(days=20)).date()
        delisted = timezone.now() - timedelta(days=5)
        listing = _make_listing(
            source_id="dom2",
            url="https://batdongsan.com.vn/dom2-pr1",
            posted_date=posted,
            delisted_at=delisted,
        )
        self.assertEqual(listing.days_on_market, 15)

    def test_falls_back_to_earliest_price_history_when_posted_date_null(self):
        listing = _make_listing(
            source_id="dom3",
            url="https://batdongsan.com.vn/dom3-pr1",
            posted_date=None,
        )
        observed = timezone.now() - timedelta(days=7)
        PriceHistory.objects.create(listing=listing, price=1000, observed_at=observed)
        self.assertEqual(listing.days_on_market, 7)

    def test_none_when_no_posted_date_and_no_price_history(self):
        listing = _make_listing(
            source_id="dom4",
            url="https://batdongsan.com.vn/dom4-pr1",
            posted_date=None,
        )
        self.assertIsNone(listing.days_on_market)


class ReactivationTests(TestCase):
    """§7: a listing that was delisted, then reappears on a later crawl, must
    have delisted_at cleared by upsert() so days_on_market falls through to
    now() again. Otherwise the stale delisted_at caps the age and silently
    suppresses the stale_listing anomaly rule."""

    def test_reactivation_clears_stale_delisted_at(self):
        posted = (timezone.now() - timedelta(days=95)).date()
        delisted = timezone.now() - timedelta(days=10)
        _make_listing(
            source_site="alonhadat",
            source_id="react1",
            url="https://alonhadat.com.vn/react1.html",
            posted_date=posted,
            is_active=False,
            delisted_at=delisted,
        )

        parsed = ParsedListing(
            source_site="alonhadat",
            source_id="react1",
            agent_source_id=None,
            agent_name=None,
            expired=False,
            fields={
                "url": "https://alonhadat.com.vn/react1.html",
                "title": "Reactivated listing",
                "property_type": "house",
                "listing_intent": "sale",
                "price": 1000,
                "posted_date": posted,
            },
        )
        upsert(parsed)

        listing = Listing.objects.get(source_site="alonhadat", source_id="react1")
        self.assertTrue(listing.is_active)
        self.assertIsNone(listing.delisted_at)
        self.assertGreater(listing.days_on_market, 90)


class PriceDisplayTests(TestCase):
    def _display(self, price, source_id):
        return _make_listing(
            source_id=source_id,
            url=f"https://batdongsan.com.vn/{source_id}-pr1",
            price=price,
        ).price_display

    def test_exactly_one_billion_is_ty(self):
        self.assertEqual(self._display(1_000_000_000, "pd1"), "1 tỷ")

    def test_just_under_one_billion_is_trieu(self):
        self.assertEqual(self._display(999_000_000, "pd2"), "999 triệu")

    def test_two_decimals_kept(self):
        self.assertEqual(self._display(6_280_000_000, "pd3"), "6.28 tỷ")

    def test_trailing_zero_stripped(self):
        self.assertEqual(self._display(8_500_000_000, "pd4"), "8.5 tỷ")

    def test_null_price_is_none(self):
        self.assertIsNone(self._display(None, "pd5"))
