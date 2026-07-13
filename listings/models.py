from django.db import models
from django.utils import timezone

SOURCE_SITE_CHOICES = [
    ("alonhadat", "alonhadat"),
    ("homedy", "homedy"),
    ("batdongsan", "batdongsan"),
    ("maisonoffice", "maisonoffice"),
]

SCRAPE_RUN_SOURCE_CHOICES = [
    ("alonhadat", "alonhadat"),
    ("homedy", "homedy"),
    ("batdongsan", "batdongsan"),
]


class Agent(models.Model):
    source_site = models.CharField(max_length=20, choices=SOURCE_SITE_CHOICES)
    source_id = models.CharField(max_length=64)
    name = models.CharField(max_length=255, null=True)

    class Meta:
        unique_together = (("source_site", "source_id"),)


class Listing(models.Model):
    PROPERTY_TYPE_CHOICES = [
        ("apartment", "apartment"),
        ("house", "house"),
        ("land", "land"),
        ("villa", "villa"),
        ("office", "office"),
    ]
    LISTING_INTENT_CHOICES = [
        ("sale", "sale"),
        ("rent", "rent"),
    ]

    source_site = models.CharField(max_length=20, choices=SOURCE_SITE_CHOICES)
    source_id = models.CharField(max_length=64)
    url = models.URLField(max_length=500, unique=True)
    title = models.CharField(max_length=255)
    # nullable since 2026-07-10: batdongsan-specific (pageTrackingData.cateId),
    # no confirmed equivalent on alonhadat/homedy (CLAUDE.md §5.1)
    category_id_source = models.IntegerField(null=True)
    property_type = models.CharField(max_length=20, choices=PROPERTY_TYPE_CHOICES)
    project_name = models.CharField(max_length=255, null=True)
    project_id_source = models.CharField(max_length=64, null=True)
    listing_intent = models.CharField(max_length=10, choices=LISTING_INTENT_CHOICES)
    is_verified = models.BooleanField(default=False)
    vip_type = models.CharField(max_length=50, null=True)
    price = models.DecimalField(max_digits=15, decimal_places=0, null=True)
    price_unit = models.CharField(max_length=50, null=True)
    price_per_sqm = models.DecimalField(max_digits=15, decimal_places=0, null=True)
    area_sqm = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    bedrooms = models.IntegerField(null=True)
    bathrooms = models.IntegerField(null=True)
    address_raw = models.TextField(null=True)
    district_id_source = models.IntegerField(null=True)
    ward_id_source = models.IntegerField(null=True)
    district = models.CharField(max_length=100, null=True)
    ward = models.CharField(max_length=100, null=True)
    specs_raw = models.JSONField(null=True)
    description = models.TextField(null=True)
    images = models.JSONField(null=True)
    video_url = models.URLField(null=True)
    map_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True)
    map_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True)
    agent = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True)
    phone_number = models.CharField(max_length=32, null=True)
    posted_date = models.DateField(null=True)
    scraped_at = models.DateTimeField(auto_now_add=True)
    # not auto_now: only ingestion may set this, ML writes must not (CLAUDE.md §5.1)
    last_seen_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    delisted_at = models.DateTimeField(null=True)
    predicted_price = models.DecimalField(max_digits=15, decimal_places=0, null=True)
    predicted_at = models.DateTimeField(null=True)
    is_anomaly = models.BooleanField(default=False)
    anomaly_reason = models.JSONField(null=True)

    class Meta:
        unique_together = (("source_site", "source_id"),)

    @property
    def days_on_market(self):
        # CLAUDE.md §5.5: fall back to earliest PriceHistory.observed_at when
        # posted_date failed to parse, instead of dropping the anomaly signal.
        if self.posted_date is not None:
            start = self.posted_date
        else:
            earliest = self.pricehistory_set.order_by("observed_at").first()
            if earliest is None:
                return None
            start = earliest.observed_at.date()
        end = (self.delisted_at or timezone.now()).date()
        return (end - start).days


class PriceHistory(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE)
    # nullable per CLAUDE.md §5.3: matches Listing.price -- negotiable
    # ("Thỏa thuận") listings must not raise on their first PriceHistory row
    price = models.DecimalField(max_digits=15, decimal_places=0, null=True)
    price_per_sqm = models.DecimalField(max_digits=15, decimal_places=0, null=True)
    observed_at = models.DateTimeField()


class ScrapeRun(models.Model):
    source_site = models.CharField(max_length=20, choices=SCRAPE_RUN_SOURCE_CHOICES)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    listings_seen = models.IntegerField(default=0)
    inserted = models.IntegerField(default=0)
    updated = models.IntegerField(default=0)
    skipped = models.IntegerField(default=0)
    error_count = models.IntegerField(default=0)
