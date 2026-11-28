import re
from datetime import timedelta
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs

from django.conf import settings
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from listings.models import Agent, Listing, PriceHistory, ScoringRun, ScrapeRun
from listings.tests.test_district_analytics import TY, _fill
from listings.tests.test_models import _make_listing as _make_any_listing


def _make_listing(**overrides):
    # Visitor views show PUBLIC_SOURCES only, so rows here default to a public source.
    return _make_any_listing(**{"source_site": "alonhadat", **overrides})
from listings.views import _page_slots


class DashboardListingListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for i in range(1, 22):
            _make_listing(
                source_id=f"v{i}", url=f"https://alonhadat.com.vn/v{i}.html"
            )
        _make_listing(
            source_id="gone", url="https://alonhadat.com.vn/gone.html",
            is_active=False, delisted_at=timezone.now(),
        )

    def test_page_renders_with_result_count(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["paginator"].count, 21)
        self.assertContains(response, "21 results")

    def test_first_page_shows_page_size_listings(self):
        response = self.client.get("/")
        self.assertEqual(len(response.context["page_obj"]), 20)

    def test_page_2_shows_the_remaining_listing(self):
        response = self.client.get("/", {"page": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), 1)
        self.assertEqual(response.context["page_obj"][0].source_id, "v1")

    def test_invalid_page_param_returns_404_not_500(self):
        self.assertEqual(self.client.get("/", {"page": "abc"}).status_code, 404)
        self.assertEqual(self.client.get("/", {"page": "999"}).status_code, 404)

    def test_scraped_title_is_html_escaped(self):
        _make_listing(
            source_id="xss", url="https://alonhadat.com.vn/xss.html",
            title='<script>alert("x")</script>',
        )
        response = self.client.get("/")
        self.assertNotContains(response, '<script>alert("x")</script>')
        self.assertContains(response, "&lt;script&gt;")

    def test_list_links_to_detail(self):
        response = self.client.get("/")
        listing = response.context["page_obj"][0]
        self.assertContains(response, f'href="/listing/{listing.pk}/"')


class DashboardListingDetailTests(TestCase):
    def test_renders_full_info_with_pending_prediction(self):
        listing = _make_listing(
            source_id="d1", url="https://alonhadat.com.vn/d1.html",
            price=8_500_000_000, area_sqm=80, district="Quận 7",
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test listing")
        self.assertContains(response, "8.5 tỷ")
        self.assertRegex(response.content.decode(), r"Model estimate</p>\s*<p[^>]*>pending</p>")
        self.assertContains(response, "Quận 7")
        self.assertContains(response, "Not scored yet.")

    def test_null_district_renders_a_dash(self):
        listing = _make_listing(
            source_id="d0", url="https://homedy.com/d0-es0", district=None,
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            response.content.decode(), r"<dt[^>]*>District</dt><dd>-</dd>"
        )

    def test_renders_prediction_and_anomaly_reason(self):
        listing = _make_listing(
            source_id="d2", url="https://alonhadat.com.vn/d2.html",
            price=8_000_000_000, predicted_price=9_000_000_000,
            is_anomaly=True,
            anomaly_reason={
                "low_photos": {"triggered": True, "value": 1},
                "stale_listing": {"triggered": False, "value": 12},
            },
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertRegex(response.content.decode(), r"Model estimate</p>\s*<p[^>]*>9 tỷ</p>")
        self.assertContains(response, "Flagged as anomaly")
        self.assertContains(response, "low_photos")
        self.assertContains(response, "triggered · value: 1")
        self.assertContains(response, "stale_listing")
        self.assertContains(response, "not triggered · value: 12")

    def test_renders_all_fields_populated(self):
        agent = Agent.objects.create(
            source_site="alonhadat", source_id="ag1"
        )
        listing = _make_listing(
            source_id="d4", url="https://alonhadat.com.vn/d4.html",
            price=8_000_000_000, predicted_price=7_500_000_000,
            price_per_sqm=100_000_000, area_sqm=80,
            bedrooms=3, bathrooms=2,
            district="Quận 7", ward="Phường Tân Phong",
            address_raw="12 Nguyễn Văn Linh, Phường Tân Phong, Quận 7, TP.HCM",
            posted_date=(timezone.now() - timedelta(days=5)).date(),
            images=["https://img/1.jpg", "https://img/2.jpg"],
            agent=agent,
            description="Nhà đẹp, sổ hồng riêng.",
            is_anomaly=True,
            anomaly_reason={"low_photos": {"triggered": True, "value": 2}},
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "8 tỷ")
        self.assertRegex(response.content.decode(), r"Model estimate</p>\s*<p[^>]*>7\.5 tỷ</p>")
        self.assertContains(response, "100 triệu")
        self.assertContains(response, "Phường Tân Phong")
        self.assertContains(response, "12 Nguyễn Văn Linh")
        self.assertContains(response, "Nhà đẹp, sổ hồng riêng.")
        self.assertContains(response, "<dd>3</dd>", html=True)
        self.assertContains(response, "<dd>2</dd>", html=True)

    def test_detail_shows_no_agent_or_phone(self):
        agent = Agent.objects.create(
            source_site="alonhadat", source_id="ag2"
        )
        listing = _make_listing(
            source_id="d6", url="https://alonhadat.com.vn/d6.html",
            agent=agent,
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertEqual(response.status_code, 200)
        for absent in ("tel:", ">Agent<", ">Phone<"):
            self.assertNotContains(response, absent)

    def test_null_price_renders_negotiable(self):
        listing = _make_listing(
            source_id="d5", url="https://alonhadat.com.vn/d5.html", price=None,
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thỏa thuận")

    def test_inactive_listing_404s(self):
        listing = _make_listing(
            source_id="d3", url="https://alonhadat.com.vn/d3.html",
            is_active=False, delisted_at=timezone.now(),
        )
        self.assertEqual(self.client.get(f"/listing/{listing.pk}/").status_code, 404)


class DashboardListingFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _make_listing(
            source_id="f1", url="https://alonhadat.com.vn/f1.html",
            district="Quận 1", property_type="apartment", price=2_000_000_000,
            address_raw="10 Lê Lợi, Quận 1", project_name="Vinhomes Central",
        )
        _make_listing(
            source_id="f2", url="https://alonhadat.com.vn/f2.html",
            district="Quận 7", property_type="house", price=8_000_000_000,
            address_raw="5 Nguyễn Văn Linh, Quận 7", title="Nhà phố Phú Mỹ Hưng",
        )
        _make_listing(
            source_id="f3", url="https://alonhadat.com.vn/f3.html",
            district="Quận 7", property_type="apartment", price=5_000_000_000,
            address_raw="8 Tân Phong, Quận 7",
        )

    def _ids(self, response):
        return [listing.source_id for listing in response.context["page_obj"]]

    def test_no_filter_returns_all_active(self):
        self.assertCountEqual(
            self._ids(self.client.get("/")), ["f1", "f2", "f3"]
        )

    def test_filter_by_district(self):
        self.assertCountEqual(
            self._ids(self.client.get("/", {"district": "Quận 7"})), ["f2", "f3"]
        )

    def test_filter_by_property_type(self):
        self.assertEqual(
            self._ids(self.client.get("/", {"property_type": "house"})), ["f2"]
        )

    def test_filter_by_price_range(self):
        response = self.client.get(
            "/", {"min_price": "3000000000", "max_price": "6000000000"}
        )
        self.assertEqual(self._ids(response), ["f3"])

    def test_filters_combine(self):
        response = self.client.get(
            "/", {"district": "Quận 7", "property_type": "apartment"}
        )
        self.assertEqual(self._ids(response), ["f3"])

    def test_search_matches_address(self):
        self.assertEqual(self._ids(self.client.get("/", {"q": "Lê Lợi"})), ["f1"])

    def test_search_matches_project_name(self):
        self.assertEqual(self._ids(self.client.get("/", {"q": "Vinhomes"})), ["f1"])

    def test_search_matches_title(self):
        self.assertEqual(self._ids(self.client.get("/", {"q": "Phú Mỹ Hưng"})), ["f2"])

    def test_invalid_price_is_ignored_not_500(self):
        response = self.client.get("/", {"min_price": "abc"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["paginator"].count, 3)

    def test_pathological_prices_ignored_not_500(self):
        # nan/inf/huge-exponent construct as valid Decimals but blow up at the
        # DB layer; they must be dropped like any other bad input, not 500.
        for bad in ("nan", "inf", "1e999999"):
            response = self.client.get("/", {"min_price": bad})
            self.assertEqual(response.status_code, 200, bad)
            self.assertEqual(response.context["paginator"].count, 3, bad)

    def test_district_options_listed(self):
        response = self.client.get("/")
        self.assertContains(response, '<option value="Quận 1"')
        self.assertContains(response, '<option value="Quận 7"')

    def test_form_reflects_selected_state(self):
        response = self.client.get("/", {"district": "Quận 7", "q": "Linh"})
        self.assertContains(response, 'value="Linh"')
        self.assertContains(response, '<option value="Quận 7" selected')

    def test_property_type_option_label_is_capitalised_value_is_not(self):
        response = self.client.get("/")
        self.assertContains(response, '<option value="apartment">Apartment</option>', html=True)


class DashboardFilterPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for i in range(1, 22):
            _make_listing(
                source_id=f"p{i}", url=f"https://alonhadat.com.vn/p{i}.html",
                district="Quận 7",
            )

    def test_pagination_links_preserve_filters(self):
        response = self.client.get("/", {"district": "Quận 7"})
        self.assertContains(response, "district=Qu")
        self.assertContains(response, "page=2")

    def test_page_2_with_filter_still_filters(self):
        response = self.client.get("/", {"district": "Quận 7", "page": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["paginator"].count, 21)
        self.assertEqual(len(response.context["page_obj"]), 1)


class ListingCardTests(TestCase):
    # Rendered standalone, not through the page: the card's contract is that a
    # listing is the only context it needs. A test client GET would hide a
    # dependency on the list view's filter context.
    def _card(self, **overrides):
        listing = _make_listing(
            source_id="c1", url="https://alonhadat.com.vn/c1.html", **overrides
        )
        return render_to_string("listings/_listing_card.html", {"listing": listing})

    def test_renders_every_populated_field(self):
        card = self._card(
            price=3_750_000_000, price_per_sqm=73_530_000, area_sqm=51,
            bedrooms=2, district="Quận Bình Tân",
            posted_date=(timezone.now() - timedelta(days=3)).date(),
        )
        self.assertIn("Apartment for sale", card)
        self.assertIn("3.75 tỷ", card)
        self.assertIn("73.53 triệu/m²", card)
        self.assertIn("51 m²", card)
        self.assertIn("2 beds", card)
        self.assertIn("Quận Bình Tân", card)
        self.assertIn("3 days listed", card)

    def test_links_to_detail(self):
        listing = _make_listing(
            source_id="c2", url="https://alonhadat.com.vn/c2.html"
        )
        card = render_to_string("listings/_listing_card.html", {"listing": listing})
        self.assertIn(f'href="/listing/{listing.pk}/"', card)

    def test_nullable_fields_omitted_not_rendered_empty(self):
        card = self._card(
            price=None, price_per_sqm=None, area_sqm=None, bedrooms=None,
            district=None, posted_date=None,
        )
        self.assertIn("Thỏa thuận", card)
        for absent in ("m²", "bed", "listed", "None"):
            self.assertNotIn(absent, card)

    def test_zero_bedrooms_not_hidden(self):
        self.assertIn("0 beds", self._card(bedrooms=0))

    def test_days_on_market_falls_back_when_posted_date_null(self):
        listing = _make_listing(
            source_id="c3", url="https://alonhadat.com.vn/c3.html", posted_date=None
        )
        PriceHistory.objects.create(
            listing=listing, price=None, price_per_sqm=None,
            observed_at=timezone.now() - timedelta(days=4),
        )
        card = render_to_string("listings/_listing_card.html", {"listing": listing})
        self.assertIn("4 days listed", card)


class ListingCardContactAgentTests(TestCase):
    def _card(self, **overrides):
        listing = _make_listing(
            source_id="k1", url="https://alonhadat.com.vn/k1.html", **overrides
        )
        return listing, render_to_string(
            "listings/_listing_card.html", {"listing": listing}
        )

    def test_list_pages_show_no_agent_or_phone(self):
        # Both pages render the shared card. is_anomaly puts the row on /flagged/.
        agent = Agent.objects.create(
            source_site="alonhadat", source_id="k-ag"
        )
        self._card(agent=agent, is_anomaly=True)
        for path in ("/", "/flagged/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            for absent in ("tel:", "Call"):
                self.assertNotContains(response, absent, msg_prefix=path)

    def test_no_agent_contact_info_renders_no_control(self):
        agent = Agent.objects.create(
            source_site="alonhadat", source_id="k-anon"
        )
        _, card = self._card(agent=agent)
        for absent in ("tel:", "Call", "aria-label"):
            self.assertNotIn(absent, card)


class TemplateCommentTests(TestCase):
    def test_no_comment_delimiters_leak_into_the_page(self):
        # {# #} is single-line only: a newline inside it makes Django print the comment.
        _make_listing(
            source_id="m1", url="https://alonhadat.com.vn/m1.html",
            is_anomaly=True,
        )
        for path in ("/", "/flagged/"):
            self.assertNotRegex(self.client.get(path).content.decode(), r"\{#|#\}", path)


class DashboardAnomalyBadgeTests(TestCase):
    def test_badge_shown_for_anomaly_listing(self):
        _make_listing(
            source_id="a1", url="https://alonhadat.com.vn/a1.html", is_anomaly=True,
            anomaly_reason={
                "price_gap": {"triggered": True, "value": 0.4},
                "low_photos": {"triggered": True, "value": 1},
                "stale_listing": {"triggered": False, "value": 12},
            },
        )
        response = self.client.get("/")
        self.assertContains(response, "Below estimate")
        self.assertContains(response, "Few photos")
        self.assertNotContains(response, "Long on the market")

    def test_no_badge_for_normal_listing(self):
        _make_listing(
            source_id="n1", url="https://alonhadat.com.vn/n1.html", is_anomaly=False,
        )
        response = self.client.get("/")
        for chip in ("Below estimate", "Few photos", "Long on the market"):
            self.assertNotContains(response, chip)


class DashboardAnomalySummaryTests(TestCase):
    def _flag(self, source_id, photos, scored_at, **overrides):
        return _make_listing(
            source_id=source_id,
            url=f"https://alonhadat.com.vn/{source_id}.html",
            is_anomaly=True,
            anomaly_reason={"low_photos": {"triggered": True, "value": photos}},
            anomaly_scored_at=scored_at,
            **overrides,
        )

    def _ids(self, response):
        return [listing.source_id for listing in response.context["listings"]]

    def test_orders_by_fewest_photos_then_most_recently_scored(self):
        now = timezone.now()
        self._flag("s2", 2, now)
        self._flag("s1-old", 1, now - timedelta(days=2))
        self._flag("s0", 0, now - timedelta(days=1))
        self._flag("s1-new", 1, now)
        self.assertEqual(
            self._ids(self.client.get("/flagged/")),
            ["s0", "s1-new", "s1-old", "s2"],
        )

    def test_paginates_at_page_size_and_reports_the_full_count(self):
        for i in range(21):
            self._flag(f"c{i}", i, timezone.now())
        response = self.client.get("/flagged/")
        self.assertEqual(len(response.context["listings"]), 20)
        self.assertContains(response, ">21 flagged")
        self.assertContains(response, "Page 1 of 2")
        self.assertEqual(self._ids(self.client.get("/flagged/", {"page": 2})), ["c20"])

    def test_page_two_shares_no_rows_with_page_one_when_every_row_ties(self):
        # Same photo count and scored time on every row, the shape one scoring
        # run leaves behind, so only the id tiebreaker makes the order stable.
        scored_at = timezone.now()
        for i in range(25):
            self._flag(f"t{i}", 0, scored_at)
        # An UPDATE writes a new row version at the end of the table, so the
        # order Postgres happens to return ties in stops matching id order.
        # On a freshly inserted table the two coincide and this test would
        # pass without the tiebreaker.
        Listing.objects.filter(source_id__in=[f"t{i}" for i in range(0, 25, 2)]).update(
            title="moved"
        )
        first = self._ids(self.client.get("/flagged/"))
        second = self._ids(self.client.get("/flagged/", {"page": 2}))
        self.assertEqual(first + second, [f"t{i}" for i in range(24, -1, -1)])

    def test_renders_the_shared_pagination_bar_with_the_jump_field(self):
        # 8 pages: past the 7 where every number shows and there is no jump.
        _bulk_listings(141, "fb")
        Listing.objects.update(is_anomaly=True)
        bar = _pagination_bar(self.client.get("/flagged/"))
        self.assertEqual(_page_items(bar), ["1", "2", "3", "…", "6", "7", "8"])
        self.assertRegex(bar, r'<a rel="next" href="\?page=2"')
        self.assertIn('pattern="1|2|3|4|5|6|7|8"', bar)

    def test_bad_page_values_404_like_the_listing_page(self):
        self._flag("only", 0, timezone.now())
        for page in ("abc", "0", "99"):
            with self.subTest(page=page):
                self.assertEqual(
                    self.client.get("/flagged/", {"page": page}).status_code, 404
                )

    def test_excludes_unflagged_and_delisted(self):
        self._flag("keep", 1, timezone.now())
        _make_listing(source_id="clean", url="https://alonhadat.com.vn/clean.html")
        self._flag(
            "gone", 0, timezone.now(), is_active=False, delisted_at=timezone.now()
        )
        self.assertEqual(self._ids(self.client.get("/flagged/")), ["keep"])

    def test_renders_photo_count_and_links_to_detail(self):
        listing = self._flag("p1", 0, timezone.now())
        response = self.client.get("/flagged/")
        self.assertContains(response, "0 photos")
        self.assertContains(response, f'href="/listing/{listing.pk}/"')

    def test_row_flagged_by_another_rule_sorts_last_with_no_photo_count(self):
        self._flag("lp", 3, timezone.now())
        _make_listing(
            source_id="other", url="https://alonhadat.com.vn/other.html",
            is_anomaly=True,
            anomaly_reason={"stale_listing": {"triggered": True, "value": 104}},
            anomaly_scored_at=timezone.now(),
        )
        response = self.client.get("/flagged/")
        self.assertEqual(self._ids(response), ["lp", "other"])
        self.assertContains(response, "3 photos")
        self.assertContains(response, ">-</span>")

    def test_nothing_flagged_renders_empty_state(self):
        _make_listing(source_id="q1", url="https://alonhadat.com.vn/q1.html")
        response = self.client.get("/flagged/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ">0 flagged")
        self.assertIsNone(_pagination_bar(response))
        self.assertContains(response, "Nothing flagged.")

    def test_reachable_from_the_listing_page(self):
        self.assertContains(self.client.get("/"), 'href="/flagged/"')


class DetailAnomalyStalenessLabelTests(TestCase):
    def _detail(self, source_id, scored_at):
        listing = _make_listing(
            source_id=source_id,
            url=f"https://alonhadat.com.vn/{source_id}.html",
            posted_date=(timezone.now() - timedelta(days=100)).date(),
            is_anomaly=True,
            anomaly_reason={"stale_listing": {"triggered": True, "value": 97}},
            anomaly_scored_at=scored_at,
        )
        return self.client.get(f"/listing/{listing.pk}/")

    def test_stale_scoring_labelled(self):
        response = self._detail("sx1", timezone.now() - timedelta(days=3))
        self.assertContains(response, "Scored")
        self.assertContains(response, "stale by")

    def test_fresh_scoring_shows_date_without_stale_label(self):
        response = self._detail("sx2", timezone.now())
        self.assertContains(response, "Scored")
        self.assertNotContains(response, "stale by")

    def test_unscored_listing_shows_no_scoring_line(self):
        listing = _make_listing(
            source_id="sx3", url="https://alonhadat.com.vn/sx3.html"
        )
        response = self.client.get(f"/listing/{listing.pk}/")
        self.assertNotContains(response, "Scored")
        self.assertContains(response, "Not scored yet.")


def _login_staff(client):
    client.force_login(User.objects.create_user("staff", is_staff=True))


class PipelineHealthAccessTests(TestCase):
    def test_anonymous_visitor_gets_403_not_a_login_redirect(self):
        # A redirect would reveal the private admin login path.
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 403)

    def test_logged_in_non_staff_gets_403(self):
        self.client.force_login(User.objects.create_user("visitor"))
        self.assertEqual(self.client.get("/health/").status_code, 403)

    def test_staff_gets_the_page(self):
        _login_staff(self.client)
        self.assertEqual(self.client.get("/health/").status_code, 200)


class PipelineHealthViewTests(TestCase):
    def setUp(self):
        _login_staff(self.client)

    def test_renders_with_an_empty_database(self):
        # The page must not 500 before the first run ever happens.
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)

    def test_shows_scrape_volume_for_a_day_with_a_run(self):
        started = timezone.now()
        ScrapeRun.objects.create(
            source_site="alonhadat", started_at=started,
            finished_at=started, listings_seen=799, error_count=1,
        )
        response = self.client.get("/health/")
        self.assertContains(response, "799")

    def test_marks_an_unfinished_old_run_as_aborted(self):
        ScrapeRun.objects.create(
            source_site="alonhadat",
            started_at=timezone.now() - timedelta(hours=8),
        )
        response = self.client.get("/health/")
        self.assertContains(response, "aborted")

    def test_shows_median_ape_as_a_percentage(self):
        now = timezone.now()
        ScoringRun.objects.create(
            started_at=now, finished_at=now,
            median_ape=Decimal("0.2287"), n_compared=700,
            model_fingerprint="abc123abc123",
        )
        response = self.client.get("/health/")
        self.assertContains(response, "22.9")

    def test_labels_the_accuracy_figure_as_in_sample(self):
        # Non-negotiable: this number must never be read as held-out accuracy.
        now = timezone.now()
        ScoringRun.objects.create(
            started_at=now, finished_at=now, median_ape=Decimal("0.2287"),
        )
        response = self.client.get("/health/")
        self.assertContains(response, "in-sample")

    def test_says_lower_is_better_so_a_long_bar_is_not_misread(self):
        now = timezone.now()
        ScoringRun.objects.create(
            started_at=now, finished_at=now, median_ape=Decimal("0.2287"),
        )
        response = self.client.get("/health/")
        self.assertContains(response, "lower is better")

    def test_expands_status_counts_into_readable_rows(self):
        started = timezone.now()
        ScrapeRun.objects.create(
            source_site="alonhadat", started_at=started, finished_at=started,
            listings_seen=10, error_count=1,
            status_counts={"srp_bot_challenge": 1},
        )
        response = self.client.get("/health/")
        self.assertContains(response, "srp_bot_challenge")

    def test_a_day_with_no_run_is_shown_not_omitted(self):
        response = self.client.get("/health/")
        self.assertEqual(len(response.context["scrape_days"]), 30)


class AdminUrlTests(TestCase):
    def test_admin_mounted_at_admin_url_setting(self):
        self.assertEqual(settings.ADMIN_URL, "admin/")
        self.assertEqual(reverse("admin:index"), f"/{settings.ADMIN_URL}")

    def test_anonymous_admin_request_redirects_to_login(self):
        index = reverse("admin:index")
        self.assertRedirects(
            self.client.get(index), f"{reverse('admin:login')}?next={index}"
        )


class MainNavTests(TestCase):
    """base.html's nav: first in body on every page, current link marked."""

    def test_each_page_marks_only_its_own_link_current(self):
        _login_staff(self.client)
        listing = _make_listing()
        current = {
            "/": "Residential",
            f"/listing/{listing.pk}/": "Residential",
            "/flagged/": "Flagged",
            "/health/": "Health",
            "/offices/": "Offices",
            "/districts/": "Districts",
        }
        for path, expected in current.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertRegex(html, r'<body[^>]*>\s*<nav aria-label="Main"')
                # aria-label pins the main nav: the list page has a second
                # nav for pagination.
                nav = re.search(r'<nav aria-label="Main".*?</nav>', html, re.S)
                # The home link holds the logo image, so labels are the link's
                # text with tags stripped.
                links = [(attrs, re.sub(r"<[^>]*>", "", inner))
                         for attrs, inner in re.findall(r"<a ([^>]*)>(.*?)</a>", nav.group(), re.S)]
                self.assertEqual(
                    [(re.search(r'href="([^"]*)"', attrs).group(1), label)
                     for attrs, label in links],
                    [("/", "Residential"), ("/districts/", "Districts"),
                     ("/offices/", "Offices"), ("/", "SaiPrice"),
                     ("/health/", "Health"), ("/flagged/", "Flagged")],
                )
                self.assertEqual(
                    [label for attrs, label in links if 'aria-current="page"' in attrs],
                    [expected],
                )

    def test_health_link_is_hidden_from_non_staff(self):
        for user in (None, User.objects.create_user("visitor")):
            with self.subTest(user=user):
                if user:
                    self.client.force_login(user)
                html = self.client.get("/").content.decode()
                nav = re.search(r'<nav aria-label="Main".*?</nav>', html, re.S).group()
                self.assertNotIn("/health/", nav)
                self.assertIn("Flagged", nav)


class DesktopShellWidthTests(TestCase):
    def test_nav_main_and_footer_drop_max_width_from_lg(self):
        html = self.client.get("/").content.decode()
        nav = re.search(r'<nav aria-label="Main"[^>]*>\s*<div class="([^"]*)"', html)
        main = re.search(r'<main class="([^"]*)"', html)
        footer = re.search(r'<footer[^>]*>\s*<div class="([^"]*)"', html)
        for name, match in (("nav", nav), ("main", main), ("footer", footer)):
            with self.subTest(container=name):
                self.assertIsNotNone(match, name)
                classes = match.group(1).split()
                self.assertIn("max-w-2xl", classes)
                self.assertIn("lg:max-w-none", classes)

    def test_404_content_sits_in_a_centred_column_from_lg(self):
        response = self.client.get("/no-such-page/")
        self.assertEqual(response.status_code, 404)
        main = re.search(r"<main[^>]*>(.*)</main>", response.content.decode(), re.S).group(1)
        class_lists = [c.split() for c in re.findall(r'class="([^"]*)"', main)]
        self.assertTrue(any({"lg:mx-auto", "lg:max-w-2xl"} <= set(c) for c in class_lists))


class DistrictPricesViewTests(TestCase):
    def test_empty_database_says_so_and_draws_no_bar(self):
        response = self.client.get("/districts/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No apartment listings")
        self.assertNotContains(response, 'style="width')

    def test_shows_a_district_with_its_median_per_m2(self):
        _fill("Quận 7", TY)

        response = self.client.get("/districts/")

        self.assertContains(response, "Quận 7")
        self.assertContains(response, "55.0")
        self.assertContains(response, "triệu/m²")
        self.assertContains(response, 'style="width: 92%"')
        self.assertContains(response, "55,000,000 VND per m²")

    def test_states_the_price_floor_beside_the_other_filters(self):
        _fill("Quận 7", TY)

        html = " ".join(self.client.get("/districts/").content.decode().split())

        self.assertIn("under 10 triệu/m²", html)
        self.assertIn("last 30 days", html)

    def test_says_asking_price_never_fair_price(self):
        _fill("Quận 7", TY)

        html = self.client.get("/districts/").content.decode().lower()

        self.assertIn("asking price", html)
        self.assertNotIn("fair price", html)

    def test_thin_district_shows_its_count_and_no_median(self):
        _fill("Quận 9", TY, n=9)

        response = self.client.get("/districts/")

        self.assertContains(response, "Quận 9")
        self.assertContains(response, "Under 10 listings")
        self.assertNotContains(response, "50.0")
        self.assertNotContains(response, 'style="width')

    def test_scraped_district_name_is_escaped(self):
        _fill('"><script>x</script>', TY)

        response = self.client.get("/districts/")

        self.assertNotContains(response, "<script>x")
        self.assertContains(response, "&lt;script&gt;x")

    def test_home_page_links_here(self):
        self.assertContains(self.client.get("/"), 'href="/districts/"')


class PublicSourcesTests(TestCase):
    """Only PUBLIC_SOURCES reach a visitor's listing views. homedy never
    delists, so its rows would sit on the list forever; they still feed
    /districts/."""

    def setUp(self):
        self.alon = _make_listing(
            source_site="alonhadat", source_id="pub1", title="Alonhadat row",
            url="https://alonhadat.com.vn/pub1.html", district="Quận 7",
            is_anomaly=True,
        )
        self.homedy = _make_listing(
            source_site="homedy", source_id="pub2", title="Homedy row",
            url="https://homedy.com/pub2-es2", district="Huyện Củ Chi",
            is_anomaly=True,
        )

    def test_list_page_hides_homedy(self):
        response = self.client.get("/")
        self.assertContains(response, "Alonhadat row")
        self.assertNotContains(response, "Homedy row")

    def test_district_filter_offers_no_homedy_only_district(self):
        response = self.client.get("/")
        self.assertEqual(list(response.context["districts"]), ["Quận 7"])

    def test_detail_page_of_a_homedy_row_is_404(self):
        self.assertEqual(self.client.get(f"/listing/{self.alon.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"/listing/{self.homedy.pk}/").status_code, 404)

    def test_flagged_page_hides_homedy(self):
        response = self.client.get("/flagged/")
        self.assertContains(response, "Alonhadat row")
        self.assertNotContains(response, "Homedy row")

    def test_districts_page_still_counts_homedy(self):
        _fill("Quận 5", TY, source_site="homedy")
        self.assertContains(self.client.get("/districts/"), "Quận 5")


HOSTILE_Q = '"><script>alert(1)</script>'
FILTERS = {
    "district": "Quận 7", "property_type": "apartment",
    "min_price": "1", "max_price": "9000000000", "q": "Listing",
}


def _bulk_listings(n, prefix):
    now = timezone.now()
    Listing.objects.bulk_create(
        Listing(
            source_site="alonhadat", source_id=f"{prefix}{i}",
            url=f"https://alonhadat.com.vn/{prefix}{i}.html", title=f"Listing {i}",
            property_type="apartment", listing_intent="sale", last_seen_at=now,
            district="Quận 7", price=Decimal("2000000000"),
            # q also searches address_raw, so a hostile q still matches every row.
            address_raw=f"{HOSTILE_Q} {i}",
        )
        for i in range(n)
    )


def _pagination_bar(response):
    match = re.search(
        r'<nav aria-label="Pagination".*?</nav>', response.content.decode(), re.S
    )
    return match.group() if match else None


def _page_items(bar):
    # Numbers are link or span text; the "…" is the jump input's placeholder.
    return [n or dots for n, dots in re.findall(r'>(\d+)</(?:a|span)>|placeholder="(…)"', bar)]


def _query(href):
    return parse_qs(unescape(href).lstrip("?"))


class PaginationBarTests(TestCase):
    """41 pages: 810 rows at paginate_by 20, the same shape as the real data."""

    @classmethod
    def setUpTestData(cls):
        _bulk_listings(810, "pb")

    def test_page_items_on_first_middle_and_last_page(self):
        expected = {
            1: ["1", "2", "3", "…", "39", "40", "41"],
            20: ["1", "…", "19", "20", "21", "…", "41"],
            41: ["1", "2", "3", "…", "39", "40", "41"],
        }
        for page, items in expected.items():
            with self.subTest(page=page):
                bar = _pagination_bar(self.client.get("/", {"page": page}))
                self.assertEqual(_page_items(bar), items)

    def test_current_page_is_a_still_underlined_span_not_a_link(self):
        bar = _pagination_bar(self.client.get("/", {"page": 20}))
        current = re.search(r'<span aria-current="page" class="([^"]*)">20</span>', bar)
        self.assertIsNotNone(current)
        self.assertIn("text-accent", current.group(1))
        self.assertIn("after:h-0.5", current.group(1))
        self.assertNotIn("after:scale-x-0", current.group(1))
        self.assertNotIn(20, [
            int(_query(href)["page"][0]) for href in re.findall(r'href="([^"]*)"', bar)
        ])

    def test_unavailable_previous_and_next_keep_their_slot_as_muted_text(self):
        first = _pagination_bar(self.client.get("/", {"page": 1}))
        self.assertNotIn('rel="prev"', first)
        self.assertRegex(first, r'<span class="[^"]*text-muted[^"]*">.*?Previous')
        self.assertRegex(first, r'<a rel="next" href="\?page=2"')
        last = _pagination_bar(self.client.get("/", {"page": 41}))
        self.assertNotIn('rel="next"', last)
        self.assertRegex(last, r'<span class="[^"]*text-muted[^"]*">.*?Next')
        self.assertRegex(last, r'<a rel="prev" href="\?page=40"')

    def test_every_link_keeps_the_active_filters(self):
        bar = _pagination_bar(self.client.get("/", {**FILTERS, "page": 20}))
        hrefs = re.findall(r'href="([^"]*)"', bar)
        self.assertEqual(len(hrefs), 6)  # previous, 1, 19, 21, 41, next
        for href in hrefs:
            with self.subTest(href=href):
                query = _query(href)
                self.assertEqual(len(query.pop("page")), 1)
                self.assertEqual(query, {k: [v] for k, v in FILTERS.items()})

    def test_jump_forms_carry_the_filters_but_not_the_page(self):
        bar = _pagination_bar(self.client.get("/", {**FILTERS, "page": 20}))
        self.assertNotIn("<details", bar)
        hidden = re.findall(r'<input type="hidden" name="([^"]*)" value="([^"]*)">', bar)
        self.assertEqual(sorted(hidden), sorted(list(FILTERS.items()) * 2))
        jumps = re.findall(r'<input type="text" name="page"[^>]*>', bar)
        self.assertEqual(len(jumps), 2)
        pattern = "|".join(str(n) for n in range(1, 42))
        for jump in jumps:
            for attr in (f'pattern="{pattern}"', "required", 'placeholder="…"',
                         'aria-label="Jump to page (1–41)"', 'enterkeyhint="go"',
                         'autocomplete="off"'):
                self.assertIn(attr, jump)
            self.assertNotIn("inputmode", jump)  # iOS's numeric keypad has no Enter key
        self.assertEqual(bar.count(">Pages 1–41 only</span>"), 2)

    def test_hostile_q_is_escaped_in_hrefs_and_hidden_inputs(self):
        response = self.client.get("/", {"q": HOSTILE_Q, "page": 20})
        self.assertNotContains(response, "<script>alert(1)</script>")
        bar = _pagination_bar(response)
        for href in re.findall(r'href="([^"]*)"', bar):
            self.assertEqual(_query(href)["q"], [HOSTILE_Q])
        values = re.findall(r'<input type="hidden" name="q" value="([^"]*)">', bar)
        self.assertEqual(values, ["&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"] * 2)

    def test_hostile_parameter_name_is_escaped(self):
        # Hidden inputs reflect every parameter name, not just the five filters.
        response = self.client.get("/", {HOSTILE_Q: "x", "page": 20})
        self.assertNotContains(response, "<script>alert(1)</script>")
        bar = _pagination_bar(response)
        for href in re.findall(r'href="([^"]*)"', bar):
            self.assertEqual(_query(href)[HOSTILE_Q], ["x"])

    def test_the_ellipsis_centers_near_the_ends_the_current_page_in_the_middle(self):
        self.assertEqual(self.client.get("/", {"page": 2}).context["page_center"], ["…"])
        self.assertEqual(self.client.get("/", {"page": 20}).context["page_center"], [20])

    def test_header_shows_the_page_number_and_no_flagged_link(self):
        response = self.client.get("/", {"page": 20})
        # Same line as the result count, which sits under the heading.
        self.assertRegex(
            response.content.decode(),
            r"</h1>\s*<p[^>]*>\s*<span>810 results</span>\s*<span>Page 20 of 41</span>\s*</p>",
        )
        self.assertNotContains(response, "Flagged listings")

    def test_page_past_the_end_is_still_404(self):
        self.assertEqual(self.client.get("/", {"page": 42}).status_code, 404)


class PageSlotsTests(TestCase):
    def test_three_items_each_side_on_every_page(self):
        for last in (8, 9, 41):
            for page in range(1, last + 1):
                with self.subTest(last=last, page=page):
                    left, center, right = _page_slots(page, last, "…")
                    self.assertEqual((len(left), len(center), len(right)), (3, 1, 3))
                    numbers = [n for n in left + center + right if n != "…"]
                    self.assertEqual(numbers, sorted(set(numbers)))
                    self.assertIn(page, numbers)
                    self.assertEqual((numbers[0], numbers[-1]), (1, last))
                    items = left + center + right
                    for i, item in enumerate(items):
                        if item == "…":
                            # A "…" always stands for at least two hidden pages.
                            before, after = items[i - 1], items[i + 1]
                            if after == "…":
                                after = items[i + 2]
                            self.assertGreaterEqual(after - before, 3)

    def test_the_ellipsis_takes_the_center_near_either_end(self):
        for page in (1, 2, 3, 39, 40, 41):
            with self.subTest(page=page):
                self.assertEqual(_page_slots(page, 41, "…"), ([1, 2, 3], ["…"], [39, 40, 41]))
        self.assertEqual(_page_slots(4, 41, "…"), ([1, 2, 3], [4], [5, "…", 41]))
        self.assertEqual(_page_slots(20, 41, "…"), ([1, "…", 19], [20], [21, "…", 41]))
        self.assertEqual(_page_slots(38, 41, "…"), ([1, "…", 37], [38], [39, 40, 41]))

    def test_seven_pages_or_fewer_show_every_number_in_the_center(self):
        self.assertEqual(_page_slots(2, 7, "…"), ([], [1, 2, 3, 4, 5, 6, 7], []))


class SmallPaginationBarTests(TestCase):
    def test_seven_pages_or_fewer_show_every_number_and_no_jump(self):
        _bulk_listings(130, "sm")
        bar = _pagination_bar(self.client.get("/", {"page": 2}))
        self.assertEqual(_page_items(bar), ["1", "2", "3", "4", "5", "6", "7"])
        self.assertNotIn('name="page"', bar)

    def test_single_page_renders_no_bar(self):
        _bulk_listings(5, "one")
        response = self.client.get("/")
        self.assertIsNone(_pagination_bar(response))
        self.assertNotContains(response, "Page 1 of 1")


class FooterAndLegalPagesTests(TestCase):
    FOOTER_LINKS = ('href="/terms/"', 'href="/privacy/"', 'href="mailto:saipricesupport@gmail.com"')

    def test_legal_pages_render_for_anonymous_visitors_inside_base(self):
        for path, h1 in (("/terms/", "Terms and Conditions"), ("/privacy/", "Privacy Policy")):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f"<h1 class=\"text-2xl tracking-wide\">{h1}</h1>")
                self.assertContains(response, '<nav aria-label="Main"')
                self.assertContains(response, 'lang="en"')

    def test_every_page_shows_the_footer(self):
        _login_staff(self.client)
        listing = _make_listing()
        for path in ("/", f"/listing/{listing.pk}/", "/flagged/", "/offices/",
                     "/districts/", "/health/", "/terms/", "/privacy/"):
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                footer = re.search(r"<footer.*?</footer>", html, re.S)
                self.assertIsNotNone(footer)
                for link in self.FOOTER_LINKS:
                    self.assertIn(link, footer.group())
                self.assertIn("Contact and support:", footer.group())
                self.assertIn(">Terms and Conditions</a>", footer.group())
                self.assertIn(">Privacy Policy</a>", footer.group())

    def test_legal_pages_have_no_generator_credit_or_escaped_markup(self):
        for path in ("/terms/", "/privacy/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotContains(response, "termsfeed")
                self.assertNotContains(response, "created with the help of")
                self.assertNotContains(response, 'class=\\"')

    def test_terms_wording_fixes(self):
        html = self.client.get("/terms/").content.decode()
        self.assertIn(
            "neither the Company nor any of the Company's providers make any "
            "representation or warranty", html
        )
        self.assertIn("refers to: Vietnam", html)
        self.assertNotIn("refers to:  Vietnam", html)


def _text_langs(html):
    """(text, inherited lang) for every text node, in document order."""
    class Parser(HTMLParser):
        VOID = {"meta", "link", "img", "input", "br", "hr", "source", "wbr"}

        def __init__(self):
            super().__init__()
            self.stack, self.texts = [], []

        def handle_starttag(self, tag, attrs):
            if tag not in self.VOID:
                inherited = self.stack[-1] if self.stack else None
                self.stack.append(dict(attrs).get("lang", inherited))

        def handle_endtag(self, tag):
            if tag not in self.VOID:
                self.stack.pop()

        def handle_data(self, data):
            if data.strip():
                self.texts.append((data.strip(), self.stack[-1] if self.stack else None))

    parser = Parser()
    parser.feed(html)
    return parser.texts


class PageLanguageTests(TestCase):
    def _html(self, path):
        return self.client.get(path).content.decode()

    def test_every_page_declares_english_once_on_the_html_element(self):
        _login_staff(self.client)
        listing = _make_listing()
        for path in ("/", f"/listing/{listing.pk}/", "/flagged/", "/districts/",
                     "/offices/", "/health/", "/terms/", "/privacy/", "/no-such-page/"):
            with self.subTest(path=path):
                html = self._html(path)
                self.assertIn('<html lang="en">', html)
                self.assertEqual(html.count('lang="en"'), 1)

    def test_card_title_is_vietnamese_on_list_and_flagged(self):
        listing = _make_listing(
            title="Bán căn hộ Quận 7", is_anomaly=True,
            anomaly_reason={"low_photos": {"triggered": True, "value": 1}},
        )
        for path in ("/", "/flagged/"):
            with self.subTest(path=path):
                self.assertRegex(
                    self._html(path),
                    rf'<a href="/listing/{listing.pk}/"[^>]*lang="vi"[^>]*>Bán căn hộ Quận 7</a>',
                )

    def test_negotiable_price_is_vietnamese_on_card_and_detail(self):
        listing = _make_listing(price=None)
        self.assertRegex(self._html("/"), r'<span[^>]*lang="vi"[^>]*>Thỏa thuận</span>')
        self.assertRegex(
            self._html(f"/listing/{listing.pk}/"), r'<p[^>]*lang="vi"[^>]*>Thỏa thuận</p>'
        )

    def test_priced_listing_price_is_not_marked(self):
        listing = _make_listing(price=8_000_000_000)
        for path in ("/", f"/listing/{listing.pk}/"):
            with self.subTest(path=path):
                self.assertIn(("8 tỷ", "en"), _text_langs(self._html(path)))

    def test_detail_title_description_and_address_are_vietnamese(self):
        listing = _make_listing(
            title="Nhà phố Phú Mỹ Hưng",
            description="Nhà đẹp, sổ hồng riêng.",
            address_raw="12 Nguyễn Văn Linh, Phường Tân Phong, Quận 7, TP.HCM",
        )
        html = self._html(f"/listing/{listing.pk}/")
        self.assertRegex(html, r'<h1[^>]*lang="vi"[^>]*>Nhà phố Phú Mỹ Hưng</h1>')
        self.assertRegex(html, r'<p[^>]*lang="vi"[^>]*>Nhà đẹp, sổ hồng riêng.</p>')
        self.assertRegex(
            html, r'<dd[^>]*lang="vi"[^>]*>12 Nguyễn Văn Linh, Phường Tân Phong, Quận 7, TP.HCM</dd>'
        )

    def test_empty_address_dash_is_not_marked(self):
        listing = _make_listing(address_raw=None)
        texts = _text_langs(self._html(f"/listing/{listing.pk}/"))
        label = [text for text, _ in texts].index("Address")
        self.assertEqual(texts[label + 1], ("-", "en"))

    def test_office_placeholder_phrases_are_vietnamese(self):
        from listings.tests.test_office_analytics import _save
        _save(1, rent=12)
        html = self._html("/offices/")
        self.assertIn('<span lang="vi">Liên hệ</span>', html)
        self.assertIn('<span lang="vi">Đang cập nhật</span>', html)
