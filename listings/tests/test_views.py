from django.test import TestCase
from django.utils import timezone

from listings.tests.test_models import _make_listing


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
