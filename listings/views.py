from django.conf import settings
from django.views.generic import ListView

from listings.models import Listing


class ListingListView(ListView):
    # Same page size as the API — one config value, not a second pagination setup.
    queryset = Listing.objects.filter(is_active=True)
    ordering = ["-id"]
    paginate_by = settings.REST_FRAMEWORK["PAGE_SIZE"]
