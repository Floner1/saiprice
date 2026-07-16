import django_filters
from django.db.models import DateTimeField, DurationField, ExpressionWrapper, F, Min
from django.db.models.functions import Coalesce, Now
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics

from listings.api.serializers import ListingSerializer
from listings.models import Listing

# §5.5's queryset form of the Listing.days_on_market property, including the
# posted_date-null fallback to the earliest PriceHistory.observed_at. Alias
# differs from the property name because annotating over a property fails at
# instance creation. Always annotated (both views) so the serializer can
# expose it; sort_by reuses the same alias.
DAYS_ON_MARKET_CALC = ExpressionWrapper(
    Coalesce(F("delisted_at"), Now())
    - Coalesce(
        F("posted_date"),
        Min("pricehistory__observed_at"),
        output_field=DateTimeField(),
    ),
    output_field=DurationField(),
)


class ListingFilter(django_filters.FilterSet):
    # SQL comparison against NULL is never true, so null-price ("Thỏa thuận")
    # and null-area rows fall out of range filters without extra handling.
    min_price = django_filters.NumberFilter(field_name="price", lookup_expr="gte")
    max_price = django_filters.NumberFilter(field_name="price", lookup_expr="lte")
    min_area = django_filters.NumberFilter(field_name="area_sqm", lookup_expr="gte")
    max_area = django_filters.NumberFilter(field_name="area_sqm", lookup_expr="lte")
    district_id = django_filters.NumberFilter(field_name="district_id_source")

    class Meta:
        model = Listing
        fields = ["district", "property_type", "listing_intent", "is_anomaly", "agent"]


class ListingListView(generics.ListAPIView):
    queryset = Listing.objects.filter(is_active=True).annotate(
        days_on_market_calc=DAYS_ON_MARKET_CALC
    )
    serializer_class = ListingSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ListingFilter
    ordering = ["-id"]

    # Runs after the backends so OrderingFilter's default ["-id"] can't clobber
    # the sort_by ordering.
    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        sort_by = self.request.query_params.get("sort_by")
        if sort_by in ("days_on_market", "-days_on_market"):
            queryset = queryset.order_by(
                sort_by.replace("days_on_market", "days_on_market_calc"), "-id"
            )
        return queryset


class ListingDetailView(generics.RetrieveAPIView):
    queryset = Listing.objects.filter(is_active=True).annotate(
        days_on_market_calc=DAYS_ON_MARKET_CALC
    )
    serializer_class = ListingSerializer
