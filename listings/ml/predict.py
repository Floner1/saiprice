"""Load the committed model bundle and score raw listing rows (CLAUDE.md §13).

The inference half of `train.py`. `_design` is imported from there rather than
rebuilt here: fit time and score time must emit the same columns in the same
order, and two copies of that logic drifting apart is the failure `features.py`
guards against on the leakage side.
"""

import math
import pickle
from functools import cache
from pathlib import Path

import numpy as np

MODEL_PATH = Path(__file__).resolve().parent / "model.pkl"


@cache
def load():
    """The pickled {model, district_stats, model_type} bundle.

    Cached because it is 7.5 MB of fitted trees and the caller scores one row
    at a time. No path argument on purpose: `predict` could not forward one, so
    an override here would silently disagree with the model it scores against.
    """
    return pickle.loads(MODEL_PATH.read_bytes())


def predict(frame, as_of=None):
    """Predicted price in whole VND, one per row of `frame`.

    `frame` holds raw listing columns -- district, property_type, area_sqm,
    posted_date -- not model features; `_design` derives those. The fit was on
    log(price), so exp() is the back-transform, which also makes each value a
    conditional median rather than a mean (train.py's module docstring).
    """
    # ponytail: imported here, not at module scope, because train.py reaches
    # dataset.py -> listings.models, which would make this module unimportable
    # outside a configured Django process. Nothing below touches the ORM.
    from listings.ml.train import _design

    bundle = load()
    X = _design(
        frame,
        bundle["district_stats"],
        as_of,
        columns=bundle["model"].feature_names_in_,
    )
    return np.exp(bundle["model"].predict(X))


if __name__ == "__main__":  # python -m listings.ml.predict
    import os

    import django
    import pandas as pd

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saiprice.settings")
    django.setup()

    bundle = load()
    assert sorted(bundle) == ["district_stats", "model", "model_type"], sorted(bundle)
    assert bundle["model_type"] == "random_forest", bundle["model_type"]

    # Pinned against the committed artifact, computed 2026-08-02 from
    # train._design plus the bundle directly, independently of this module.
    # Not an accuracy claim -- it fails if the design matrix, the fitted column
    # order, the exp() back-transform or as_of handling drift, which is what a
    # loader can actually get wrong. The held-out RMSE this model won on is not
    # stored in the bundle, and the split behind it is unreconstructable: the
    # cleaned set was 1,226 rows at fit time and keeps growing, so a fixed
    # random_state no longer selects the same test rows.
    #
    # posted_date sits 14 days before as_of deliberately. Every fitted
    # listing_age split threshold is under ~50 (the training set topped out at
    # 50 days), so an older sample saturates and the prediction stops moving --
    # verified, a sample at age 48 scored identically at age 1266, which left
    # as_of drift undetectable.
    rows = pd.DataFrame(
        [
            {
                "district": "Quận 7",
                "property_type": "apartment",
                "area_sqm": area,
                "posted_date": pd.Timestamp("2026-07-15"),
            }
            for area in (70.0, 150.0)
        ]
    )
    small, large = predict(rows, as_of=pd.Timestamp("2026-07-29"))
    assert math.isclose(small, 4_302_344_966.893654, rel_tol=1e-9), small
    assert math.isclose(large, 12_022_580_496.960297, rel_tol=1e-9), large
    print(f"ok  70sqm={small:,.0f} VND  150sqm={large:,.0f} VND")
