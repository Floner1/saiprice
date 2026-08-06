from django.contrib import admin

from .models import Agent, Listing, PriceHistory, ScoringRun, ScrapeRun

admin.site.register(Listing)
admin.site.register(Agent)
admin.site.register(PriceHistory)


@admin.register(ScrapeRun)
class ScrapeRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source_site", "started_at", "finished_at", "listings_seen",
        "inserted", "updated", "skipped", "error_count", "posted_date_nulls",
        "status_counts",
    )


@admin.register(ScoringRun)
class ScoringRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "started_at", "finished_at", "predicted", "scored", "flagged",
        "n_compared", "median_ape", "mae_vnd", "model_fingerprint",
        "error_count", "status_counts",
    )
