"""Engineered features for the price model.

Leakage rule: every district-level statistic comes from `fit_district_stats`,
which is called on the TRAIN frame only. Both frames are then transformed with
that one fitted object, so a test row can never contribute to its own district
median. The functions take `stats` as an argument rather than deriving it
internally precisely so that recomputing on the full frame is not reachable by
accident.

A `price_per_sqm_vs_district_avg` feature lived here until 2026-07-28 and was
removed after review. It was `(price / area_sqm) / district_avg`, so
`ratio * area_sqm * district_avg` reconstructs `price` exactly -- verified on
881/881 train rows. Every other term was already a feature, which left the
target recoverable by multiplication: test R2 went 0.57 -> 0.96 and the fitted
coefficient on log(ratio) was 0.98. It is also uncomputable at scoring time,
where `price` is the unknown being predicted (and is null outright for a
"Thỏa thuận" listing). Do not reintroduce it as a model input. As a dashboard
anomaly stat it would be perfectly sound -- that is a CLAUDE.md §12 decision,
not a feature-engineering one.
"""

import pandas as pd

# Under this many train rows, a district's median is mostly the row being scored
# looking at itself -- Huyện Cần Giờ has exactly one row in the cleaned set. Those
# districts take the train-wide median, so the feature does not degenerate into a
# copy of the target for the thinnest districts. Districts stay in the index
# either way; only the value changes.
MIN_DISTRICT_ROWS = 10


def listing_age(df, as_of=None):
    """Days from `posted_date` to `as_of`. Row-level, no group statistic."""
    as_of = pd.Timestamp.today().normalize() if as_of is None else pd.Timestamp(as_of)
    return (as_of - pd.to_datetime(df["posted_date"])).dt.days


def fit_district_stats(train):
    """Per-district median price, indexed by district. TRAIN FRAME ONLY.

    `train_median_price` is the fallback for a district this frame never saw,
    and the substitute value for one too thin to trust. It rides along as a
    constant column rather than a sentinel row or a second return value, so the
    index stays exactly the set of districts present in train -- callers that
    count districts or diff the per-district column are unaffected.
    """
    grouped = train.groupby("district")["price"]
    train_median = train["price"].median()
    stats = pd.DataFrame(
        {"median_price": grouped.median().mask(grouped.size() < MIN_DISTRICT_ROWS,
                                               train_median)}
    )
    stats["train_median_price"] = train_median
    return stats


def district_median_price(df, stats):
    """Train median price for each row's district, or the train-wide median."""
    # The fallback is read off `stats`, never recomputed from `df`. A test frame
    # therefore cannot influence the value it gets handed for its own district.
    return (
        df["district"]
        .map(stats["median_price"])
        .fillna(stats["train_median_price"].iloc[0])
    )
