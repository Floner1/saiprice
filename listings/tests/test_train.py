import math
import pickle
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np
import pandas as pd
from django.test import SimpleTestCase, TestCase
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

from listings.ml.dataset import build_training_rows
from listings.ml.features import fit_district_stats
from listings.ml.train import MODEL_PATH, RANDOM_STATE, TEST_SIZE, _design, train
from listings.tests.test_models import _make_listing

SAMPLE = pd.DataFrame(
    [
        {
            "district": "Quận 7",
            "property_type": "apartment",
            "area_sqm": 70.0,
            "posted_date": date(2026, 6, 1),
        }
    ]
)


def _predict(bundle, frame, as_of="2026-07-29"):
    X = _design(
        frame,
        bundle["district_stats"],
        pd.Timestamp(as_of),
        columns=bundle["model"].feature_names_in_,
    )
    return np.exp(bundle["model"].predict(X))


class PickledModelTests(SimpleTestCase):
    """The committed artifact (CLAUDE.md §13) must load and score a raw row."""

    def setUp(self):
        self.bundle = pickle.loads(Path(MODEL_PATH).read_bytes())

    def test_predicts_a_plausible_vnd_price_for_one_row(self):
        price = float(_predict(self.bundle, SAMPLE)[0])
        self.assertTrue(math.isfinite(price))
        # Four orders of magnitude wide, so this is a sanity bound, not an
        # accuracy claim. It still fails hard if the model was fitted on raw
        # VND instead of log(price) -- exp of a billion overflows to inf.
        self.assertGreater(price, 1e8)
        self.assertLess(price, 1e12)

    def test_prediction_responds_to_area(self):
        """Regression: a huge-magnitude feature must not swamp the solver.

        district_median_price sits at ~4.5e9 and area_sqm at ~80. Handed to
        LinearRegression unscaled, that is a condition number of 4.4e10, and
        lstsq truncates the three small singular values -- every coefficient
        but district_median_price comes back at ~1e-19 and the model predicts
        one constant per district. The tell is area-only train R2 (0.367)
        beating the full model's (0.172), which OLS cannot do.
        """
        pair = pd.DataFrame(
            [{**SAMPLE.iloc[0].to_dict(), "area_sqm": a} for a in (50.0, 150.0)]
        )
        small, large = _predict(self.bundle, pair)
        self.assertGreater(large, small * 1.5)

    def test_score_frame_missing_a_category_is_not_encoded_as_the_baseline(self):
        # get_dummies on a one-row frame emits only that row's category, so
        # score time must reindex against the fitted columns. A frame holding
        # only a non-baseline type must not collapse onto the dropped one.
        types = [c for c in self.bundle["model"].feature_names_in_
                 if c.startswith("property_type_")]
        self.assertTrue(types, "no property_type dummies in the fitted model")
        one = SAMPLE.assign(property_type=types[0].removeprefix("property_type_"))
        X = _design(
            one,
            self.bundle["district_stats"],
            pd.Timestamp("2026-07-29"),
            columns=self.bundle["model"].feature_names_in_,
        )
        self.assertEqual(X[types[0]].iloc[0], 1.0)


class TrainTests(TestCase):
    def setUp(self):
        for i in range(30):
            _make_listing(
                source_id=f"t{i}",
                url=f"https://alonhadat.com.vn/{i}.html",
                title=f"Unit {i}",
                property_type="apartment" if i % 2 else "house",
                price=3_000_000_000 + i * 100_000_000,
                area_sqm=50 + i,
                district="Quận 7",
                posted_date=date(2026, 6, 1),
            )

    def _train(self):
        """Train into a temp path so the committed model.pkl is never touched."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.pkl"
            metrics = train(model_path=path)
            return metrics, pickle.loads(path.read_bytes())

    def test_writes_a_loadable_bundle_and_reports_vnd_metrics(self):
        metrics, bundle = self._train()

        self.assertEqual(metrics["n_test"], round(30 * TEST_SIZE))
        self.assertEqual(metrics["n_train"], 30 - metrics["n_test"])
        # RMSE/MAE are back-transformed to VND, so they sit on the price scale,
        # not log's single digits.
        self.assertGreater(metrics["rmse"], 1e6)
        self.assertGreater(metrics["mae"], 1e6)
        self.assertTrue(math.isfinite(metrics["r2"]))
        self.assertGreater(float(_predict(bundle, SAMPLE)[0]), 0)

    def test_district_stats_are_fitted_on_the_train_split_only(self):
        _, bundle = self._train()
        df = pd.DataFrame(build_training_rows()[0])
        expected, _ = train_test_split(
            df, test_size=TEST_SIZE, random_state=RANDOM_STATE
        )
        pd.testing.assert_frame_equal(
            bundle["district_stats"], fit_district_stats(expected)
        )

    def test_raw_district_is_not_a_model_input(self):
        columns = self._train()[1]["model"].feature_names_in_

        self.assertNotIn("district", columns)
        self.assertIn("district_median_price", columns)
        # One-hot district would show up as one column per district name.
        self.assertEqual(
            [c for c in columns if c.startswith("district")],
            ["district_median_price"],
        )


class RandomForestTests(TestCase):
    """CLAUDE.md §13's other candidate. Same split, same target, same features
    as linear regression -- these tests exist to catch the split silently
    diverging between the two models, not to re-test linear regression.
    """

    def setUp(self):
        for i in range(30):
            _make_listing(
                source_id=f"rf{i}",
                url=f"https://alonhadat.com.vn/rf{i}.html",
                title=f"RF unit {i}",
                property_type="apartment" if i % 2 else "house",
                price=3_000_000_000 + i * 100_000_000,
                area_sqm=50 + i,
                district="Quận 7",
                posted_date=date(2026, 6, 1),
            )

    def _train(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.pkl"
            metrics = train(model_path=path)
            return metrics, pickle.loads(path.read_bytes())

    def test_random_forest_metrics_are_finite(self):
        rf = self._train()[0]["random_forest"]
        for key in ("r2", "r2_log", "rmse", "mae", "median_ape"):
            self.assertTrue(math.isfinite(rf[key]), f"{key} not finite: {rf[key]}")
        self.assertGreater(rf["rmse"], 0)

    def test_random_forest_prediction_responds_to_area(self):
        """Same check as PickledModelTests.test_prediction_responds_to_area,
        run against a freshly fit RandomForestRegressor directly -- the
        committed model.pkl may hold either candidate, so this can't rely on
        the pickled bundle to exercise random forest specifically.
        """
        as_of = pd.Timestamp("2026-07-29")
        df = pd.DataFrame(build_training_rows()[0])
        train_df, _ = train_test_split(
            df, test_size=TEST_SIZE, random_state=RANDOM_STATE
        )
        stats = fit_district_stats(train_df)
        X_train = _design(train_df, stats, as_of)

        model = RandomForestRegressor(random_state=RANDOM_STATE)
        model.fit(X_train, np.log(train_df["price"]))

        pair = pd.DataFrame(
            [{**SAMPLE.iloc[0].to_dict(), "area_sqm": a} for a in (50.0, 150.0)]
        )
        X_pair = _design(pair, stats, as_of, columns=X_train.columns)
        small, large = np.exp(model.predict(X_pair))
        self.assertGreater(large, small)

    def test_linear_and_random_forest_share_train_test_row_ids(self):
        """The exact assumption that broke quietly today (CLAUDE.md dataset.py
        row-count change silently shifting a fixed-random_state split): assert
        the split `train()` actually used matches one fresh, independent
        reconstruction with the same params, not just that both models report
        the same `n_train`/`n_test` counts.
        """
        metrics, _ = self._train()

        df = pd.DataFrame(build_training_rows()[0])
        expected_train, expected_test = train_test_split(
            df, test_size=TEST_SIZE, random_state=RANDOM_STATE
        )
        self.assertEqual(
            sorted(metrics["train_ids"]), sorted(expected_train["id"].tolist())
        )
        self.assertEqual(
            sorted(metrics["test_ids"]), sorted(expected_test["id"].tolist())
        )

    def test_build_training_rows_and_split_called_exactly_once(self):
        """Guards the actual mechanism: two models must consume one split, not
        each call build_training_rows()/train_test_split() on their own --
        that's how the two could silently diverge.
        """
        with mock.patch(
            "listings.ml.train.build_training_rows", wraps=build_training_rows
        ) as rows_spy, mock.patch(
            "listings.ml.train.train_test_split", wraps=train_test_split
        ) as split_spy:
            self._train()

        self.assertEqual(rows_spy.call_count, 1)
        self.assertEqual(split_spy.call_count, 1)

    # Winner selection: CLAUDE.md §13 ships whichever wins on RMSE/R2, and a
    # disagreement between those two rankings must be reported, not hidden.

    def test_winner_is_the_lower_rmse_model(self):
        metrics, _ = self._train()
        lower_rmse = (
            "linear" if metrics["rmse"] <= metrics["random_forest"]["rmse"] else "random_forest"
        )
        self.assertEqual(metrics["rmse_winner"], lower_rmse)
        self.assertEqual(metrics["winner"], metrics["rmse_winner"])

    def test_disagreement_flag_matches_the_two_rankings(self):
        """r2_winner ranks by r2_log, not r2 (VND): r2 and rmse are computed
        from the same VND residuals, so ranking by one always ranks by the
        other identically (R2 = 1 - SS_res/SS_tot, RMSE = sqrt(SS_res/n),
        same SS_res/SS_tot/n for both models) -- a VND-r2-based ranking could
        never disagree with the RMSE-based one, making this flag dead code.
        r2_log is the one reported ranking that can actually diverge.
        """
        metrics, _ = self._train()
        higher_r2_log = (
            "linear"
            if metrics["r2_log"] >= metrics["random_forest"]["r2_log"]
            else "random_forest"
        )
        self.assertEqual(metrics["r2_winner"], higher_r2_log)
        self.assertEqual(
            metrics["disagreement"], metrics["rmse_winner"] != metrics["r2_winner"]
        )

    def test_pickled_bundle_records_which_model_type_won(self):
        metrics, bundle = self._train()
        self.assertEqual(bundle["model_type"], metrics["winner"])
        if metrics["winner"] == "random_forest":
            self.assertIsInstance(bundle["model"], RandomForestRegressor)
        else:
            self.assertIsInstance(bundle["model"], Pipeline)
