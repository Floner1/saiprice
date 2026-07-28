"""Cleaned training set for the price model (CLAUDE.md §13).

Read-only. Nothing here writes to the database, so the scrape, API and
dashboard paths are untouched by any decision in this file.
"""

from django.db.models import Q

from listings.models import Listing

# CLAUDE.md §13's minimum feature set. The trainer must read model inputs from
# these names, not from row keys -- rows also carry `id`, which exists only to
# join predictions back to listings and is never a feature.
FEATURES = ("district", "area_sqm", "property_type")
TARGET = "price"

# Domain caps separating a residential unit from a whole building or a project
# transfer. alonhadat files both under its apartment category slug, so
# property_type cannot tell them apart (CLAUDE.md §6, VIP cross-category note).
MAX_AREA_SQM = 500
MAX_BEDROOMS = 10

# Deliberately no price_per_sqm cap. Checked 2026-07-27: every surviving row
# above 200M VND/m2 is a genuine District 1 luxury unit (Grand Marina, Vinhomes
# Golden River, Landmark 81, The Marq), so a third cap would delete the real top
# of the market, not mislabelled buildings. The two caps above already exclude
# the whole-building rows -- id 245 (340 m2, 170 ty) goes out on bedrooms=18.


def build_training_rows(queryset=None):
    """Return (rows, stats): model-ready dicts plus a per-rule drop count."""
    qs = Listing.objects.all() if queryset is None else queryset

    # Rent prices are monthly, sale prices are totals. One target column cannot
    # hold both, and there is nowhere near enough rent data to model separately.
    sale = qs.filter(listing_intent="sale")

    # Target and features are dropped, never imputed: a filled-in price is a
    # fabricated label, and area/district carry most of the signal a fill would
    # be guessing from. A non-positive price or area is a parse failure.
    complete = sale.filter(price__gt=0, area_sqm__gt=0).exclude(
        Q(district__isnull=True) | Q(district="") | Q(property_type="")
    )

    # listing_age is computed from posted_date with no fallback, so a null here
    # reaches the model as NaN and sklearn raises at .fit(). Counted separately
    # from the other drops because §5.4 warns on this field going null fleet-wide
    # -- a spike in this counter and that warning share one root cause.
    dated = complete.filter(posted_date__isnull=False)

    units = dated.exclude(Q(area_sqm__gt=MAX_AREA_SQM) | Q(bedrooms__gt=MAX_BEDROOMS))

    total, n_sale, n_complete, n_dated, n_units = (
        qs.count(),
        sale.count(),
        complete.count(),
        dated.count(),
        units.count(),
    )

    rows, seen = [], set()
    fields = (
        "id",
        "title",
        "price",
        "area_sqm",
        "district",
        "property_type",
        "posted_date",
    )
    for row in units.order_by("id").values(*fields):
        # An exact repost under a fresh source_id stays a distinct Listing by
        # design (CLAUDE.md §2), but duplicate rows leak across a train/test
        # split and inflate the score. Training set only -- no Listing row is
        # merged or modified.
        # ponytail: exact (title, price, area) match. A looser (price, area,
        # district) key catches ~3x more but false-positives on identical
        # floorplans in one building; tighten only if reposts get retitled.
        key = (row["title"], row["price"], row["area_sqm"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "id": row["id"],
                "district": row["district"],
                "property_type": row["property_type"],
                "area_sqm": float(row["area_sqm"]),
                "price": float(row["price"]),
                # Not a FEATURE. Raw input to the listing_age feature, carried
                # through like `id` so the trainer has one source for the set.
                "posted_date": row["posted_date"],
            }
        )

    stats = {
        "rows_before": total,
        "dropped_not_sale": total - n_sale,
        "dropped_missing_or_nonpositive": n_sale - n_complete,
        "dropped_missing_posted_date": n_complete - n_dated,
        "dropped_whole_building": n_dated - n_units,
        "dropped_duplicate": n_units - len(rows),
        "rows_after": len(rows),
    }
    return rows, stats
