from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import F, Q
from django.views.generic import DetailView, ListView, TemplateView

from listings.analytics import accuracy_trend, bar_max, recent_runs, scrapes_per_day
from listings.models import Listing, ScoringRun, ScrapeRun


def _to_decimal(value):
    # GET params are strings; drop anything that can't be a real price so a bad
    # filter skips instead of 500ing. InvalidOperation catches junk ("abc");
    # the finite/magnitude check catches nan/inf and exponents that construct as
    # valid Decimals but overflow the price column (max_digits=15) at query time.
    if not value:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite() or abs(number) >= Decimal("1e15"):
        return None
    return number


class ListingListView(ListView):
    # Same page size as the API — one config value, not a second pagination setup.
    # select_related: the card's contact control reads agent.name, which is one
    # query per row without it.
    queryset = Listing.objects.filter(is_active=True).select_related("agent")
    ordering = ["-id"]
    paginate_by = settings.REST_FRAMEWORK["PAGE_SIZE"]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.GET
        if district := params.get("district"):
            qs = qs.filter(district=district)
        if property_type := params.get("property_type"):
            qs = qs.filter(property_type=property_type)
        if (min_price := _to_decimal(params.get("min_price"))) is not None:
            qs = qs.filter(price__gte=min_price)
        if (max_price := _to_decimal(params.get("max_price"))) is not None:
            qs = qs.filter(price__lte=max_price)
        if q := params.get("q", "").strip():
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(address_raw__icontains=q)
                | Q(project_name__icontains=q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["districts"] = (
            Listing.objects.filter(is_active=True)
            .exclude(district__isnull=True)
            .exclude(district="")
            .values_list("district", flat=True)
            .distinct()
            .order_by("district")
        )
        ctx["property_types"] = Listing.PROPERTY_TYPE_CHOICES
        return ctx


class ListingDetailView(DetailView):
    # Same is_active scoping as the API detail view.
    queryset = Listing.objects.filter(is_active=True)


class AnomalySummaryView(ListView):
    template_name = "listings/listing_summary.html"
    context_object_name = "listings"
    # The ranking value is a JSON key, not a column: §12 writes one key per rule
    # that ran, so a row flagged by some other rule has no low_photos key and
    # resolves to SQL NULL. nulls_last is explicit rather than inherited from
    # the backend's default ASC placement.
    queryset = (
        Listing.objects.filter(is_active=True, is_anomaly=True)
        .select_related("agent")
        .order_by(
            F("anomaly_reason__low_photos__value").asc(nulls_last=True),
            "-anomaly_scored_at",
        )[:10]
    )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # The page shows 10 of however many there are; without the total it
        # reads as "10 flagged", which is a different claim.
        ctx["flagged_count"] = Listing.objects.filter(
            is_active=True, is_anomaly=True
        ).count()
        return ctx


class PipelineHealthView(TemplateView):
    template_name = "listings/pipeline_health.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["scrape_days"] = scrapes_per_day()
        ctx["scrape_max"] = bar_max(ctx["scrape_days"], "seen")
        ctx["accuracy_runs"] = accuracy_trend()
        ctx["accuracy_max"] = bar_max(ctx["accuracy_runs"], "median_ape_pct")
        ctx["recent_scrapes"] = recent_runs(ScrapeRun)
        ctx["recent_scorings"] = recent_runs(ScoringRun)
        return ctx
