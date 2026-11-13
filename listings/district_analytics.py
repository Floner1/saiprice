"""Median asking price per m2 by district, for the daily snapshot and /districts/.

Read-only. Built on asking prices, never sale prices.
"""

import statistics
from collections import defaultdict
from datetime import timedelta

from django.db.models import Q

from listings.ml.dataset import MAX_AREA_SQM, MAX_BEDROOMS

# A run that ends on an srp_ code skips the delisting sweep, and recent runs
# all have, so is_active alone keeps listings that left the market weeks ago.
# Seen in the last FRESH_DAYS is the stand-in.
FRESH_DAYS = 30

# Below this many distinct rows, quartiles are a few numbers posing as a
# distribution. Same threshold as the office dashboard.
MIN_SUMMARY_N = 10

MARKET_SOURCES = ("alonhadat", "homedy")


def district_stats(queryset, as_of):
    """One dict per (district, property_type) over the listings on the market.

    A repost with the same price, area and district counts in listing_count
    but only once in distinct_count and in every median, so a project posted
    fifty times at one price does not set its district's figure alone.
    """
    rows = (
        queryset.filter(
            is_active=True,
            listing_intent="sale",
            price__gt=0,
            area_sqm__gt=0,
            area_sqm__lte=MAX_AREA_SQM,
            last_seen_at__gte=as_of - timedelta(days=FRESH_DAYS),
        )
        .exclude(bedrooms__gt=MAX_BEDROOMS)
        .exclude(Q(district__isnull=True) | Q(district=""))
        .values_list("district", "property_type", "price", "area_sqm")
    )

    groups = defaultdict(list)
    for district, property_type, price, area in rows:
        groups[district, property_type].append((price, area))

    out = []
    for (district, property_type), listed in groups.items():
        pairs = set(listed)
        per_sqm = [float(price / area) for price, area in pairs]
        p25, _, p75 = (
            statistics.quantiles(per_sqm, n=4)
            if len(pairs) >= MIN_SUMMARY_N
            else (None, None, None)
        )
        out.append(
            {
                "district": district,
                "property_type": property_type,
                "listing_count": len(listed),
                "distinct_count": len(pairs),
                "median_price_per_sqm": statistics.median(per_sqm),
                "p25_price_per_sqm": p25,
                "p75_price_per_sqm": p75,
                "median_price": float(statistics.median(p for p, _ in pairs)),
            }
        )
    return out
