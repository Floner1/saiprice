from django.contrib import admin

from .models import Agent, Listing, PriceHistory, ScrapeRun

admin.site.register(Listing)
admin.site.register(Agent)
admin.site.register(PriceHistory)
admin.site.register(ScrapeRun)
