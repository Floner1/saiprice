from rest_framework import serializers

from listings.models import Listing


class ListingSerializer(serializers.ModelSerializer):
    price_display = serializers.ReadOnlyField()
    days_on_market = serializers.SerializerMethodField()

    class Meta:
        model = Listing
        fields = "__all__"

    def get_days_on_market(self, obj):
        delta = getattr(obj, "days_on_market_calc", None)
        return delta.days if delta is not None else None
