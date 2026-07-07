import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from listings.models import Listing, PriceHistory

BASE_DIR = Path(__file__).resolve().parent.parent.parent / "testdata"


class IngestSavedListingsTests(TestCase):
    def test_mixed_folder_ingests_good_files_and_logs_bad_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            shutil.copy(BASE_DIR / "ldp_apartment.txt", folder / "apartment.html")
            shutil.copy(BASE_DIR / "ldp_land.txt", folder / "land.html")
            (folder / "broken.html").write_text("")

            out, err = StringIO(), StringIO()
            call_command("ingest_saved_listings", str(folder), stdout=out, stderr=err)

            self.assertIn("inserted=2 updated=0 skipped=0 errors=1", out.getvalue())
            self.assertIn("broken.html", err.getvalue())
            self.assertEqual(Listing.objects.count(), 2)
            self.assertEqual(
                set(Listing.objects.values_list("property_type", flat=True)),
                {"apartment", "land"},
            )
            self.assertEqual(PriceHistory.objects.count(), 2)

    def test_second_pass_updates_instead_of_inserting(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            shutil.copy(BASE_DIR / "ldp_apartment.txt", folder / "apartment.html")

            call_command("ingest_saved_listings", str(folder), stdout=StringIO(), stderr=StringIO())
            out = StringIO()
            call_command("ingest_saved_listings", str(folder), stdout=out, stderr=StringIO())

            self.assertIn("inserted=0 updated=1", out.getvalue())
            self.assertEqual(Listing.objects.count(), 1)
            self.assertEqual(PriceHistory.objects.count(), 1)
