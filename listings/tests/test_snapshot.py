from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from listings.management.commands.snapshot_districts import take_snapshot
from listings.models import DistrictSnapshot, Listing
from listings.tests.test_district_analytics import TY, _row

DAY = date(2026, 11, 12)


class TakeSnapshotTests(TestCase):
    def setUp(self):
        self.as_of = timezone.now()

    def test_writes_one_row_per_group_with_district_stats_numbers(self):
        for price in (2 * TY, 3 * TY, 10 * TY):
            _row(price)
        _row(9 * TY, property_type="villa")

        take_snapshot("alonhadat", DAY, self.as_of)

        row = DistrictSnapshot.objects.get(property_type="apartment")
        self.assertEqual(DistrictSnapshot.objects.count(), 2)
        self.assertEqual(row.snapshot_date, DAY)
        self.assertEqual(row.source_site, "alonhadat")
        self.assertEqual(row.district, "Quận 7")
        self.assertEqual(row.listing_count, 3)
        self.assertEqual(row.distinct_count, 3)
        self.assertEqual(row.median_price_per_sqm, 60_000_000)
        self.assertEqual(row.median_price, 3 * TY)
        self.assertIsNone(row.p25_price_per_sqm)
        self.assertIsNone(row.p75_price_per_sqm)

    def test_stores_quartiles_when_present(self):
        for k in range(1, 11):
            _row(k * TY, area=100)

        take_snapshot("alonhadat", DAY, self.as_of)

        row = DistrictSnapshot.objects.get()
        self.assertEqual(row.p25_price_per_sqm, 27_500_000)
        self.assertEqual(row.p75_price_per_sqm, 82_500_000)

    def test_same_day_rerun_replaces_rows(self):
        _row(2 * TY)
        gone = _row(5 * TY, district="Quận 2")
        take_snapshot("alonhadat", DAY, self.as_of)

        take_snapshot("alonhadat", DAY, self.as_of)
        self.assertEqual(DistrictSnapshot.objects.count(), 2)

        Listing.objects.filter(pk=gone.pk).update(is_active=False)
        take_snapshot("alonhadat", DAY, self.as_of)
        self.assertEqual(
            list(DistrictSnapshot.objects.values_list("district", flat=True)),
            ["Quận 7"],
        )

    def test_new_date_adds_rows_and_keeps_old(self):
        _row(2 * TY)
        take_snapshot("alonhadat", DAY, self.as_of)

        take_snapshot("alonhadat", DAY + timedelta(days=1), self.as_of)

        self.assertEqual(
            sorted(DistrictSnapshot.objects.values_list("snapshot_date", flat=True)),
            [DAY, DAY + timedelta(days=1)],
        )

    def test_one_source_never_touches_another(self):
        _row(2 * TY)
        take_snapshot("alonhadat", DAY, self.as_of)
        _row(4 * TY, source_site="homedy")

        take_snapshot("homedy", DAY, self.as_of)

        alonhadat = DistrictSnapshot.objects.get(source_site="alonhadat")
        homedy = DistrictSnapshot.objects.get(source_site="homedy")
        self.assertEqual(alonhadat.median_price, 2 * TY)
        self.assertEqual(homedy.median_price, 4 * TY)

    def test_source_with_no_listings_clears_that_days_rows(self):
        DistrictSnapshot.objects.create(
            snapshot_date=DAY,
            source_site="homedy",
            district="Quận 7",
            property_type="apartment",
            listing_count=1,
            distinct_count=1,
            median_price_per_sqm=1,
            median_price=1,
        )

        written = take_snapshot("homedy", DAY, self.as_of)

        self.assertEqual(written, 0)
        self.assertFalse(DistrictSnapshot.objects.exists())


class SnapshotCommandTests(TestCase):
    def test_no_listings_writes_nothing(self):
        call_command("snapshot_districts", stdout=StringIO())

        self.assertEqual(DistrictSnapshot.objects.count(), 0)

    def test_dates_the_snapshot_in_ho_chi_minh_time(self):
        late_utc = datetime(2026, 11, 12, 20, 0, tzinfo=dt_timezone.utc)
        _row(2 * TY, last_seen_at=late_utc)
        out = StringIO()

        with patch("django.utils.timezone.now", return_value=late_utc):
            call_command("snapshot_districts", stdout=out)

        row = DistrictSnapshot.objects.get()
        self.assertEqual(row.snapshot_date, date(2026, 11, 13))
        self.assertIn("alonhadat: 1", out.getvalue())
        self.assertIn("homedy: 0", out.getvalue())
