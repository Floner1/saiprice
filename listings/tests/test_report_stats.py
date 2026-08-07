from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from listings.tests.test_models import _make_listing


def _listing(**overrides):
    defaults = dict(
        source_site="alonhadat",
        # _make_listing defaults to a batdongsan row; category_id_source is
        # batdongsan-only (CLAUDE.md §5.1) and must not ride along to alonhadat.
        category_id_source=None,
        source_id="1",
        url="https://alonhadat.com.vn/listing-1",
        price=4_000_000_000,
        price_per_sqm=60_000_000,
        area_sqm=70,
        district="Quận 7",
    )
    defaults.update(overrides)
    return _make_listing(**defaults)


class ReportStatsTests(TestCase):
    def test_writes_a_timestamped_snapshot_file(self):
        _listing(source_id="1", url="https://alonhadat.com.vn/listing-1")
        _listing(source_id="2", url="https://alonhadat.com.vn/listing-2", is_active=False)

        # Fixed clock so the filename is deterministic, not minute-of-the-
        # real-run (two runs in the same wall-clock minute would otherwise
        # collide on one filename).
        fixed_now = timezone.make_aware(timezone.datetime(2026, 1, 1, 9, 0))
        out_path = Path(settings.BASE_DIR) / "stats_reports" / "2026-01-01_0900_stats.txt"
        self.addCleanup(out_path.unlink, missing_ok=True)

        with mock.patch("listings.management.commands.report_stats.timezone.now", return_value=fixed_now):
            call_command("report_stats")

        self.assertTrue(out_path.exists())
        content = out_path.read_text(encoding="utf-8")
        self.assertIn("active: 1  inactive: 1", content)
