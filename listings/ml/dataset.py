"""Cleaned training set for the price model (CLAUDE.md §13).

Read-only. Nothing here writes to the database, so the scrape, API and
dashboard paths are untouched by any decision in this file.
"""

import statistics

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

# Deliberately no FIXED price_per_sqm cap. Checked 2026-07-27: every surviving
# row above 200M VND/m2 is a genuine District 1 luxury unit (Grand Marina,
# Vinhomes Golden River, Landmark 81, The Marq), so a flat cap would delete the
# real top of the market, not mislabelled buildings. The two caps above already
# exclude the whole-building rows -- id 245 (340 m2, 170 ty) goes out on
# bedrooms=18. The district-relative rule below is the answer to the same
# problem a flat cap could not solve.

# Some alonhadat listings carry a price an order of magnitude off their own
# title -- a seller typo at the source (4,5 ty typed as 45 ty), not a parse
# failure; currency.py and the scraper were checked. The reference is the
# district's 90th percentile price/m2, NOT its median: HCMC districts are
# bimodal, and Quan 1's 118M/m2 median against its 399M/m2 p90 is a genuine
# luxury tier a median-anchored rule cannot tell from a dropped comma. Measured
# on the 1,123-row cleaned set 2026-07-29, the highest defensible row sits at
# 2.73x its district p90 (id 1121, Binh Thanh, 409M/m2) and the lowest
# confirmed-bad at 4.77x (id 1109); 3.5 is near the log-midpoint of that gap.
# Against the median those two groups overlap -- genuine Quan 1 rows reach 4.16x
# their median while the worst bad row is 4.84x -- which is why p90 is the
# anchor.
PRICE_PER_SQM_MAX_RATIO = 3.5

# 11, not 10, and the difference is load-bearing: at exactly 10 values the
# inclusive p90 is (9*second_highest + highest)/10, so the outlier being hunted
# carries 10% of its own band and the effective cutoff slips from 3.5x to 4.85x
# -- past the 4.77x worst known bad row. From 11 values up the p90 lands on the
# second-highest with zero weight on the maximum. Unrelated to
# features.MIN_DISTRICT_ROWS despite the similar name: that one is fitted
# post-split on the train frame, this one runs pre-split on raw rows.
MIN_BAND_ROWS = 11


def _p90(values):
    return statistics.quantiles(values, n=10, method="inclusive")[8]


def _drop_implausible_prices(rows):
    """Drop rows priced past PRICE_PER_SQM_MAX_RATIO x their district band."""
    if len(rows) < MIN_BAND_ROWS:
        return rows, {}

    priced = [(row, row["price"] / row["area_sqm"]) for row in rows]
    by_district = {}
    for row, per_sqm in priced:
        by_district.setdefault(row["district"], []).append(per_sqm)

    # A thin district borrows the citywide SPREAD, not the citywide price level.
    # HCMC's thin districts are its cheap outer ones -- Huyện Cần Giờ has one row
    # in the live set, Huyện Hóc Môn three -- where the citywide p90 sits about
    # 15x their own median and would wave through a textbook 10x typo. Scaling
    # each district's own median by the citywide p90/median ratio keeps the check
    # at the district's real price level, so a cheap district is not left
    # unchecked and an expensive thin one is not mass-flagged.
    all_per_sqm = [per_sqm for _, per_sqm in priced]
    citywide_spread = _p90(all_per_sqm) / statistics.median(all_per_sqm)

    # ponytail: single pass, the band is computed from the population it judges.
    # Holds while bad rows are a small minority of a district (3 of 1,123 live,
    # spread across districts of 45-134 rows). Past roughly 10% of one district
    # the p90 lands on a bad row and the rule silently drops nothing. If a new
    # source lands with systematically shifted prices, refit the band after a
    # first pass instead of widening the ratio.
    bands = {
        district: (
            _p90(values)
            if len(values) >= MIN_BAND_ROWS
            else statistics.median(values) * citywide_spread
        )
        for district, values in by_district.items()
    }
    kept = [
        row
        for row, per_sqm in priced
        if per_sqm <= PRICE_PER_SQM_MAX_RATIO * bands[row["district"]]
    ]
    return kept, {"dropped_price_implausible": len(rows) - len(kept)}


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

    n_deduped = len(rows)

    # Runs last, on already-clean deduped rows, so the band a row is judged
    # against is not itself built from reposts or parse failures.
    rows, implausible = _drop_implausible_prices(rows)

    # `dropped_price_implausible` is absent, not zero, when the set was too thin
    # to band -- "never ran" and "cleared every row" are different facts. Same
    # convention as CLAUDE.md §12's partial anomaly_reason dict.
    stats = {
        "rows_before": total,
        "dropped_not_sale": total - n_sale,
        "dropped_missing_or_nonpositive": n_sale - n_complete,
        "dropped_missing_posted_date": n_complete - n_dated,
        "dropped_whole_building": n_dated - n_units,
        "dropped_duplicate": n_units - n_deduped,
        **implausible,
        "rows_after": len(rows),
    }
    return rows, stats
