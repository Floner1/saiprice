"""Price model candidates (CLAUDE.md §13): linear regression and random forest.

Target is log(price): raw VND is right-skewed (measured skew 4.675 -> 0.836 on
the cleaned set), and a linear fit on it is dominated by the luxury tail. The
transform lives here, not in `dataset.py`, so the cleaned set stays a plain
description of the data and the random-forest half can pick its own target.

Reported metrics are back-transformed to VND. exp() of a least-squares fit on
logs estimates the conditional *median* price, not the mean, so R2/RMSE/MAE
below are honest error against real prices but are not the quantities the fit
minimised.
"""

import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score, root_mean_squared_error
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from listings.ml.dataset import build_training_rows
from listings.ml.features import district_median_price, fit_district_stats, listing_age

# Must match notebooks/feature_engineering.ipynb so this fit lands on the same
# split the leak audit was run against.
RANDOM_STATE = 42
TEST_SIZE = 0.2

MODEL_PATH = Path(__file__).resolve().parent / "model.pkl"


def _design(frame, stats, as_of, columns=None):
    """Model matrix. Pass the fitted `columns` whenever `frame` is not train."""
    numeric = pd.DataFrame(
        {
            "area_sqm": frame["area_sqm"],
            "listing_age": listing_age(frame, as_of),
            "district_median_price": district_median_price(frame, stats),
        },
        index=frame.index,
    )
    # dtype is load-bearing: get_dummies defaults to bool, and a bool column
    # concatenated here makes the frame object-dtyped, which sklearn rejects.
    dummies = pd.get_dummies(
        frame["property_type"], prefix="property_type", dtype="float64"
    )
    X = pd.concat([numeric, dummies], axis=1)
    if columns is None:
        # Dummy variable trap: the full set sums to 1 in every row and is
        # collinear with the intercept. Which category goes is arbitrary.
        return X.drop(columns=sorted(dummies.columns)[0])
    # A non-train frame only carries the categories it happens to contain, so
    # its own dummies are not the fitted set. Reindexing drops the baseline
    # column and zero-fills the rest -- all-zero is the baseline's encoding.
    return X.reindex(columns=columns, fill_value=0.0)


def _score(model, X_test, actual):
    """Held-out metrics on the VND scale, plus r2 on the scale the fit optimises."""
    log_predicted = model.predict(X_test)
    predicted = np.exp(log_predicted)
    return {
        "r2": r2_score(actual, predicted),
        "rmse": root_mean_squared_error(actual, predicted),
        "mae": mean_absolute_error(actual, predicted),
        "r2_log": r2_score(np.log(actual), log_predicted),
        "median_ape": float(np.median(np.abs(predicted - actual) / actual)),
    }


def train(model_path=MODEL_PATH, as_of=None):
    """Fit both CLAUDE.md §13 candidates on one shared split, pickle whichever
    wins on held-out RMSE/R2, return both models' metrics plus the verdict.
    """
    as_of = pd.Timestamp.now() if as_of is None else pd.Timestamp(as_of)
    df = pd.DataFrame(build_training_rows()[0])
    train_df, test_df = train_test_split(
        df, test_size=TEST_SIZE, random_state=RANDOM_STATE
    )

    # Train frame only. Fitting on `df` would put each test row's own price
    # into the district median it is scored against (features.py docstring).
    stats = fit_district_stats(train_df)

    X_train = _design(train_df, stats, as_of)
    X_test = _design(test_df, stats, as_of, columns=X_train.columns)
    y_train_log = np.log(train_df["price"])
    actual = test_df["price"].to_numpy()

    # StandardScaler is load-bearing, not hygiene. district_median_price is
    # ~4.5e9 next to area_sqm's ~80, a condition number of 4.4e10 on the
    # centered matrix; unscaled, lstsq inside LinearRegression truncates the
    # three small singular values and returns ~1e-19 for every coefficient but
    # that one, so the model degenerates to one constant per district.
    linear_model = make_pipeline(StandardScaler(), LinearRegression())
    linear_model.fit(X_train, y_train_log)
    linear_metrics = _score(linear_model, X_test, actual)

    # Tree splits are scale-invariant, so no StandardScaler here.
    rf_model = RandomForestRegressor(random_state=RANDOM_STATE)
    rf_model.fit(X_train, y_train_log)
    rf_metrics = _score(rf_model, X_test, actual)

    # CLAUDE.md §13: ship whichever wins on held-out RMSE/R2 -- RMSE decides.
    # r2_winner deliberately ranks by r2_log, not r2 (VND): r2 and rmse here
    # are both computed from the same VND actual/predicted pair, so
    # R2 = 1 - SS_res/SS_tot and RMSE = sqrt(SS_res/n) share SS_res, SS_tot and
    # n between the two models -- lower RMSE and higher r2 (VND) can never
    # rank two models differently, which would make "flag disagreement" dead
    # code. r2_log is a genuinely independent ranking (exp() back-transform
    # doesn't preserve relative squared error between VND and log scale), so
    # it is the one that can actually disagree with VND RMSE.
    rmse_winner = "linear" if linear_metrics["rmse"] <= rf_metrics["rmse"] else "random_forest"
    r2_winner = "linear" if linear_metrics["r2_log"] >= rf_metrics["r2_log"] else "random_forest"
    winner = rmse_winner
    winning_model = linear_model if winner == "linear" else rf_model

    # feature_names_in_ carries the fitted column order, so scoring code needs
    # nothing from this bundle beyond the model, the district stats, and which
    # model type it is.
    Path(model_path).write_bytes(
        pickle.dumps(
            {"model": winning_model, "district_stats": stats, "model_type": winner}
        )
    )
    return {
        # Back-compat top level: unchanged shape/values for existing callers
        # that read linear regression's own metrics directly off `metrics`.
        **linear_metrics,
        "n_train": len(train_df),
        "n_test": len(test_df),
        "train_ids": train_df["id"].tolist(),
        "test_ids": test_df["id"].tolist(),
        "random_forest": rf_metrics,
        "winner": winner,
        "rmse_winner": rmse_winner,
        "r2_winner": r2_winner,
        "disagreement": rmse_winner != r2_winner,
    }
