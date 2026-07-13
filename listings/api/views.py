import django_filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics

from listings.api.serializers import ListingSerializer
from listings.models import Listing


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
        fields = ["min_price", "max_price", "min_area", "max_area", "district_id"]


class ListingListView(generics.ListAPIView):
    queryset = Listing.objects.filter(is_active=True)
    serializer_class = ListingSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = ListingFilter
