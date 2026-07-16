from django.contrib import admin

from .models import Agent, Listing, PriceHistory, ScrapeRun

admin.site.register(Listing)
admin.site.register(Agent)
admin.site.register(PriceHistory)


@admin.register(ScrapeRun)
class ScrapeRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source_site", "started_at", "finished_at", "listings_seen",
        "inserted", "updated", "skipped", "error_count", "posted_date_nulls",
    )
