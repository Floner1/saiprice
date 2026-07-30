from django.core.management.base import BaseCommand

from listings.ml.train import MODEL_PATH, train


class Command(BaseCommand):
    help = (
        "Fit both CLAUDE.md §13 candidates (linear regression, random forest) on "
        "the cleaned training set, report both models' held-out R2/RMSE/MAE, and "
        "pickle whichever wins to listings/ml/model.pkl."
    )

    def handle(self, *args, **options):
        metrics = train()
        rf = metrics["random_forest"]

        self.stdout.write(f"{'rows_train':12} {metrics['n_train']}")
        self.stdout.write(f"{'rows_test':12} {metrics['n_test']}")
        self.stdout.write("")
        self.stdout.write(f"{'':12}{'linear':>16}{'random_forest':>16}")
        self.stdout.write(f"{'r2':12}{metrics['r2']:16.4f}{rf['r2']:16.4f}")
        self.stdout.write(
            f"{'r2_log':12}{metrics['r2_log']:16.4f}{rf['r2_log']:16.4f}"
            "  (log scale, the fitted one)"
        )
        self.stdout.write(f"{'rmse':12}{metrics['rmse']:16,.0f}{rf['rmse']:16,.0f}")
        self.stdout.write(f"{'mae':12}{metrics['mae']:16,.0f}{rf['mae']:16,.0f}")
        self.stdout.write(
            f"{'median_ape':12}{metrics['median_ape']:16.1%}{rf['median_ape']:16.1%}"
        )
        self.stdout.write("")
        self.stdout.write(f"winner by RMSE (VND):   {metrics['rmse_winner']}")
        # r2 (VND) is not compared here: it's computed from the same VND
        # residuals as rmse, so it can never rank the two models differently.
        # r2_log is the one reported ranking that can actually diverge.
        self.stdout.write(f"winner by R2 (log):     {metrics['r2_winner']}")
        if metrics["disagreement"]:
            self.stdout.write(
                self.style.WARNING(
                    "RMSE (VND) and R2 (log) disagree on the winner -- RMSE "
                    "decided (CLAUDE.md §13)"
                )
            )
        self.stdout.write(f"saved model_type={metrics['winner']} -> {MODEL_PATH}")
