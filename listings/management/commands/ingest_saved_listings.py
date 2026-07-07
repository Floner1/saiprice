from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from listings.scraping.parsers import parse_ldp
from listings.upsert import upsert


class Command(BaseCommand):
    help = (
        "Ingest manually saved batdongsan LDP HTML files from a folder through "
        "the same parse_ldp + upsert path the crawler used. Performs no "
        "delisting and never flips is_active: each folder is a partial sample "
        "of the market, so absence from a batch means nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "folder",
            help="folder of saved LDP HTML files; every file is read regardless of name",
        )

    def handle(self, *args, **options):
        folder = Path(options["folder"])
        if not folder.is_dir():
            raise CommandError(f"not a folder: {folder}")

        inserted = updated = skipped = errors = 0
        for path in sorted(p for p in folder.iterdir() if p.is_file()):
            try:
                html = path.read_text(encoding="utf-8", errors="replace")
                parsed = parse_ldp(html, path.resolve().as_uri())
                if parsed.expired:
                    # skipped, not delisted: this command must never flip
                    # is_active (CLAUDE.md §6), so an expired page is refused
                    # rather than ingested as active or marked delisted
                    self.stderr.write(f"skipped {path.name}: expired")
                    skipped += 1
                    continue
                if upsert(parsed):
                    inserted += 1
                else:
                    updated += 1
            except Exception as exc:
                self.stderr.write(f"error {path.name}: {exc}")
                errors += 1

        self.stdout.write(
            f"inserted={inserted} updated={updated} skipped={skipped} errors={errors}"
        )
