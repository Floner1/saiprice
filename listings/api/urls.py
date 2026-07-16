from django.urls import path

from listings.api.views import ListingDetailView, ListingListView

urlpatterns = [
    path("listings/", ListingListView.as_view()),
    path("listings/<int:pk>/", ListingDetailView.as_view()),
]
