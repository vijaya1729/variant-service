import math
import os
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.assignment import assign, bucket, variant_for_bucket
from app.models import Experiment, Variant

VISITOR_COUNT = 100_000
TOLERANCE_PCT = 1.0


def make_experiment(
    weights: tuple[int, ...] = (50, 50),
    traffic: int = 100,
    status: str = "running",
    salt: str = "salt-a",
) -> Experiment:
    variants = [Variant(key=key, weight=weight) for key, weight in zip("ABCDEF", weights)]
    return Experiment(id="exp", salt=salt, traffic=traffic, variants=variants, status=status)


def percentages(counts: Counter, total: int) -> dict:
    return {key: round(100 * count / total, 2) for key, count in sorted(counts.items())}


@pytest.fixture(scope="module")
def visitors() -> list[str]:
    # Seeded so the printed splits are reproducible and the tests can't flake.
    rng = random.Random(1729)
    return [f"{rng.getrandbits(128):032x}" for _ in range(VISITOR_COUNT)]


def test_bucket_matches_pinned_value() -> None:
    # Computed outside Python with shasum and bc. If this changes, every live
    # experiment reshuffles.
    assert bucket("salt", "enroll", "visitor-1") == 9871


def test_same_input_gives_same_result_across_repeated_calls() -> None:
    experiment = make_experiment()
    results = {assign("visitor-1", experiment).key for _ in range(1000)}
    assert len(results) == 1


def test_bucket_is_stable_across_processes_and_hash_seeds() -> None:
    code = "from app.assignment import bucket; print(bucket('salt', 'variant', 'visitor-1'))"
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("1", "2")
    }
    assert outputs == {str(bucket("salt", "variant", "visitor-1"))}


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "A"), (8999, "A"), (9000, "B"), (9999, "B")],
)
def test_bucket_boundaries_for_90_10(value: int, expected: str) -> None:
    variants = make_experiment(weights=(90, 10)).variants
    assert variant_for_bucket(value, variants).key == expected


@pytest.mark.parametrize("weights", [(50, 50), (90, 10)])
def test_distribution_matches_weights(visitors: list[str], weights: tuple[int, int]) -> None:
    experiment = make_experiment(weights=weights)
    counts = Counter(assign(visitor, experiment).key for visitor in visitors)
    observed = percentages(counts, len(visitors))

    print(f"\ndistribution {weights[0]}/{weights[1]}: {observed}")
    assert observed["A"] == pytest.approx(weights[0], abs=TOLERANCE_PCT)
    assert observed["B"] == pytest.approx(weights[1], abs=TOLERANCE_PCT)


def test_holdback_enrols_traffic_percentage(visitors: list[str]) -> None:
    experiment = make_experiment(traffic=20)
    enrolled = sum(assign(visitor, experiment) is not None for visitor in visitors)
    observed = 100 * enrolled / len(visitors)

    print(f"\nholdback traffic=20: {observed:.2f}% enrolled")
    assert observed == pytest.approx(20, abs=TOLERANCE_PCT)


@pytest.mark.parametrize(("traffic", "expected"), [(0, 0), (100, VISITOR_COUNT)])
def test_traffic_extremes(visitors: list[str], traffic: int, expected: int) -> None:
    experiment = make_experiment(traffic=traffic)
    assert sum(assign(visitor, experiment) is not None for visitor in visitors) == expected


def test_raising_traffic_keeps_enrolled_visitors_on_their_variant(visitors: list[str]) -> None:
    before = make_experiment(traffic=20)
    after = make_experiment(traffic=50)

    enrolled = [visitor for visitor in visitors if assign(visitor, before) is not None]
    assert enrolled
    assert all(assign(visitor, after) == assign(visitor, before) for visitor in enrolled)


def test_experiments_with_different_salts_are_independent(visitors: list[str]) -> None:
    first = make_experiment(salt="salt-a")
    second = make_experiment(salt="salt-b")
    counts = Counter((assign(v, first).key, assign(v, second).key) for v in visitors)
    observed = percentages(counts, len(visitors))

    aa, ab, ba, bb = (counts[cell] for cell in [("A", "A"), ("A", "B"), ("B", "A"), ("B", "B")])
    correlation = (aa * bb - ab * ba) / math.sqrt((aa + ab) * (ba + bb) * (aa + ba) * (ab + bb))

    print(f"\nindependence joint cells: {observed}, correlation: {correlation:.4f}")
    for cell in [("A", "A"), ("A", "B"), ("B", "A"), ("B", "B")]:
        assert observed[cell] == pytest.approx(25, abs=TOLERANCE_PCT)


@pytest.mark.parametrize("status", ["draft", "paused", "stopped"])
def test_non_running_experiment_returns_none(status: str) -> None:
    assert assign("visitor-1", make_experiment(status=status)) is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"weights": (60, 30)}, id="weights-under-100"),
        pytest.param({"weights": (60, 50)}, id="weights-over-100"),
        pytest.param({"weights": (100, 0)}, id="zero-weight"),
        pytest.param({"weights": (110, -10)}, id="negative-weight"),
        pytest.param({"weights": (100,)}, id="single-variant"),
        pytest.param({"traffic": -1}, id="traffic-below-0"),
        pytest.param({"traffic": 101}, id="traffic-above-100"),
        pytest.param({"status": "archived"}, id="unknown-status"),
    ],
)
def test_invalid_config_is_rejected(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        make_experiment(**overrides)


def test_duplicate_variant_keys_are_rejected() -> None:
    variants = [Variant(key="A", weight=50), Variant(key="A", weight=50)]
    with pytest.raises(ValidationError):
        Experiment(id="exp", salt="salt-a", traffic=100, variants=variants, status="running")
