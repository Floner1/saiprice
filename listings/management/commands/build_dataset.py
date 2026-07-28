from django.core.management.base import BaseCommand

from listings.ml.dataset import FEATURES, TARGET, build_training_rows


class Command(BaseCommand):
    help = (
        "Report the cleaned ML training set (CLAUDE.md §13): row counts before "
        "and after each drop rule, plus a null count per model column. Read-only."
    )

    def handle(self, *args, **options):
        rows, stats = build_training_rows()
        for name, value in stats.items():
            self.stdout.write(f"{name:32} {value}")
        for column in FEATURES + (TARGET,):
            nulls = sum(1 for row in rows if row[column] is None)
            self.stdout.write(f"nulls in {column:23} {nulls}")
