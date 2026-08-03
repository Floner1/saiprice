from rest_framework import serializers

from listings.models import Listing


class ListingSerializer(serializers.ModelSerializer):
    price_display = serializers.ReadOnlyField()
    days_on_market = serializers.SerializerMethodField()

    class Meta:
        model = Listing
        # Explicit, not "__all__": a new column on Listing must be added here
        # deliberately before it reaches the public API. Order matches what
        # "__all__" emitted (id, declared fields, model fields, relations) so
        # this stayed a pure refactor.
        fields = [
            "id",
            "price_display",
            "days_on_market",
            "source_site",
            "source_id",
            "url",
            "title",
            "category_id_source",
            "property_type",
            "project_name",
            "project_id_source",
            "listing_intent",
            "is_verified",
            "vip_type",
            "price",
            "price_unit",
            "price_per_sqm",
            "area_sqm",
            "bedrooms",
            "bathrooms",
            "address_raw",
            "district_id_source",
            "ward_id_source",
            "district",
            "ward",
            "specs_raw",
            "description",
            "images",
            "video_url",
            "map_lat",
            "map_lng",
            "posted_date",
            "scraped_at",
            "last_seen_at",
            "is_active",
            "delisted_at",
            "predicted_price",
            "predicted_at",
            "is_anomaly",
            "anomaly_reason",
            "anomaly_scored_at",
            "agent",
        ]

    def get_days_on_market(self, obj):
        delta = getattr(obj, "days_on_market_calc", None)
        return delta.days if delta is not None else None
