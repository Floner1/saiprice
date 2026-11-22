from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from listings import views

urlpatterns = [
    path("", views.ListingListView.as_view(), name="listing-list"),
    path("listing/<int:pk>/", views.ListingDetailView.as_view(), name="listing-detail"),
    path("flagged/", views.AnomalySummaryView.as_view(), name="listing-summary"),
    path("health/", views.PipelineHealthView.as_view(), name="pipeline-health"),
    path("offices/", views.OfficeDashboardView.as_view(), name="office-dashboard"),
    path("districts/", views.DistrictPricesView.as_view(), name="district-prices"),
    path(settings.ADMIN_URL, admin.site.urls),
    path("api/", include("listings.api.urls")),
]
