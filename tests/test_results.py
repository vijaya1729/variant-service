import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.models import Experiment, Variant

ADMIN_KEY = "test-admin-key"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}

client = TestClient(app)


@pytest.fixture(autouse=True)
def admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)


@pytest.fixture
def experiment(database: None) -> Experiment:
    # B first, so a test fails if "control" were ever read as alphabetical order.
    variants = [Variant(key="B", weight=50), Variant(key="A", weight=50)]
    experiment = Experiment(
        id="exp", salt="salt", traffic=100, variants=variants, status="running"
    )
    db.create_experiment(experiment)
    return experiment


def seed_exposures(variant_key: str, count: int) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.executemany(
            "insert into exposures (experiment_id, visitor_id, variant_key) values (%s, %s, %s)",
            [("exp", f"{variant_key}-{i}", variant_key) for i in range(count)],
        )


def seed_conversions(variant_key: str, count: int, goal: str = "default") -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            insert into conversions (experiment_id, visitor_id, variant_key, goal)
            values (%s, %s, %s, %s)
            """,
            [("exp", f"{variant_key}-{i}", variant_key, goal) for i in range(count)],
        )


def get_results(**params: str) -> dict:
    response = client.get("/results/exp", params=params, headers=ADMIN_HEADERS)
    assert response.status_code == 200
    return response.json()


def test_results_require_the_admin_key() -> None:
    assert client.get("/results/exp").status_code == 401


def test_unknown_experiment_is_404(database: None) -> None:
    assert client.get("/results/nope", headers=ADMIN_HEADERS).status_code == 404


def test_reports_counts_and_stats_against_the_first_variant(experiment: Experiment) -> None:
    seed_exposures("B", 1000)
    seed_exposures("A", 1000)
    seed_conversions("B", 100)
    seed_conversions("A", 120)

    results = get_results()
    control, variant = results["variants"]

    assert control["key"] == "B"
    assert (control["exposures"], control["conversions"]) == (1000, 100)
    assert control["conversion_rate"] == pytest.approx(0.1)
    assert control["lift"] is None
    assert control["p_value"] is None

    assert variant["key"] == "A"
    assert variant["conversion_rate"] == pytest.approx(0.12)
    assert variant["lift"] == pytest.approx(0.2)
    assert variant["z"] == pytest.approx(1.4293008498232314, rel=1e-12)
    assert variant["p_value"] == pytest.approx(0.15291778186394622, rel=1e-12)
    assert variant["ci_low"] < 0.12 < variant["ci_high"]

    assert results["srm_detected"] is False
    assert results["min_sample_warning"] is False


def test_conversion_without_an_exposure_is_excluded(experiment: Experiment) -> None:
    seed_exposures("B", 10)
    seed_conversions("B", 25)

    control = get_results()["variants"][0]

    assert (control["exposures"], control["conversions"]) == (10, 10)


def test_conversion_arriving_before_its_exposure_is_counted(experiment: Experiment) -> None:
    seed_conversions("B", 5)
    seed_exposures("B", 5)

    assert get_results()["variants"][0]["conversions"] == 5


def test_results_are_scoped_to_the_goal(experiment: Experiment) -> None:
    seed_exposures("B", 10)
    seed_conversions("B", 2)
    seed_conversions("B", 7, goal="purchase")

    assert get_results()["variants"][0]["conversions"] == 2

    purchase = get_results(goal="purchase")
    assert purchase["goal"] == "purchase"
    assert purchase["variants"][0]["conversions"] == 7

    # Filtering by goal must not lose the exposed visitors who never converted on it.
    assert get_results(goal="unused")["variants"][0]["exposures"] == 10


def test_results_with_no_events_are_valid_json(experiment: Experiment) -> None:
    results = get_results()

    assert [variant["exposures"] for variant in results["variants"]] == [0, 0]
    assert results["variants"][1]["conversion_rate"] is None
    assert results["variants"][1]["p_value"] is None
    assert results["srm_p_value"] is None
    assert results["srm_detected"] is False
    assert results["min_sample_warning"] is True
    assert "NaN" not in json.dumps(results)


def test_zero_control_rate_has_no_lift_but_keeps_the_test(experiment: Experiment) -> None:
    seed_exposures("B", 500)
    seed_exposures("A", 500)
    seed_conversions("A", 50)

    variant = get_results()["variants"][1]

    assert variant["lift"] is None
    assert variant["p_value"] < 0.001


def test_srm_fires_on_a_skewed_split(experiment: Experiment) -> None:
    seed_exposures("B", 5300)
    seed_exposures("A", 4700)

    results = get_results()

    assert results["srm_chi_square"] == pytest.approx(36.0)
    assert results["srm_p_value"] == pytest.approx(1.973175290075393e-09, rel=1e-9)
    assert results["srm_detected"] is True


def test_srm_stays_quiet_below_the_threshold(experiment: Experiment) -> None:
    seed_exposures("B", 5100)
    seed_exposures("A", 4900)

    results = get_results()

    assert results["srm_p_value"] == pytest.approx(0.045500263896358445, rel=1e-9)
    assert results["srm_detected"] is False


@pytest.mark.parametrize(("b_exposures", "warning"), [(99, True), (100, False)])
def test_min_sample_warning(b_exposures: int, warning: bool, experiment: Experiment) -> None:
    seed_exposures("B", b_exposures)
    seed_exposures("A", 200)

    assert get_results()["min_sample_warning"] is warning
