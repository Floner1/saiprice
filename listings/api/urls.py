from django.urls import path

from listings.api.views import ListingListView

urlpatterns = [
    path("listings/", ListingListView.as_view()),
]
