import pytest

from app.stats import chi_square_sf, srm_test, two_proportion_z_test, wilson_interval

# Reference values computed once with statsmodels 0.14 and scipy, then hardcoded.


@pytest.mark.parametrize(
    ("conversions", "exposures", "low", "high"),
    [
        (50, 1000, 0.03813026239274881, 0.06531382024425081),
        (0, 100, 0.0, 0.03699349820698569),
        (100, 100, 0.9630065017930143, 1.0),
        (1, 3, 0.06149194472039626, 0.7923403991979523),
    ],
)
def test_wilson_interval(conversions: int, exposures: int, low: float, high: float) -> None:
    assert wilson_interval(conversions, exposures) == pytest.approx((low, high), abs=1e-12)


def test_wilson_interval_without_exposures() -> None:
    assert wilson_interval(0, 0) is None


def test_z_test_matches_reference() -> None:
    z, p = two_proportion_z_test(100, 1000, 120, 1000)
    assert z == pytest.approx(1.4293008498232314, rel=1e-12)
    assert p == pytest.approx(0.15291778186394622, rel=1e-12)


def test_z_test_sign_follows_the_variant() -> None:
    z, p = two_proportion_z_test(120, 1000, 100, 1000)
    assert z == pytest.approx(-1.4293008498232314, rel=1e-12)
    assert p == pytest.approx(0.15291778186394622, rel=1e-12)


def test_z_test_keeps_precision_in_the_far_tail() -> None:
    z, p = two_proportion_z_test(500, 5000, 1, 5000)
    assert z == pytest.approx(-22.874032764582353, rel=1e-12)
    assert p == pytest.approx(8.427392907460698e-116, rel=1e-9)


def test_z_test_with_identical_rates() -> None:
    assert two_proportion_z_test(100, 1000, 100, 1000) == (0.0, 1.0)


@pytest.mark.parametrize(
    "counts",
    [
        pytest.param((0, 100, 0, 100), id="no-conversions"),
        pytest.param((100, 100, 100, 100), id="all-converted"),
        pytest.param((0, 0, 10, 100), id="no-control-exposures"),
        pytest.param((10, 100, 0, 0), id="no-variant-exposures"),
    ],
)
def test_z_test_is_undefined_without_variance(counts: tuple[int, int, int, int]) -> None:
    assert two_proportion_z_test(*counts) is None


@pytest.mark.parametrize(
    ("statistic", "df", "expected"),
    [
        (3.841458820694124, 1, 0.04999999999999994),
        (5.991464547107979, 2, 0.05000000000000008),
        (1.0, 1, 0.31731050786291115),
        (10.0, 3, 0.01856613546304323),
        (0.5, 4, 0.9735009788392561),
        (30.0, 9, 0.0004387217709794793),
        (500.0, 1, 9.505397766554137e-111),
        (2.0, 20, 0.9999998885745217),
    ],
)
def test_chi_square_sf(statistic: float, df: int, expected: float) -> None:
    assert chi_square_sf(statistic, df) == pytest.approx(expected, rel=1e-9)


def test_chi_square_sf_at_zero() -> None:
    assert chi_square_sf(0.0, 3) == 1.0


def test_srm_with_two_variants() -> None:
    statistic, p = srm_test([5100, 4900], [50, 50])
    assert statistic == pytest.approx(4.0)
    assert p == pytest.approx(0.045500263896358445, rel=1e-9)


def test_srm_with_three_uneven_variants() -> None:
    statistic, p = srm_test([900, 1000, 3100], [20, 20, 60])
    assert statistic == pytest.approx(13.333333333333334)
    assert p == pytest.approx(0.0012726338013398079, rel=1e-9)


def test_srm_with_skewed_split() -> None:
    statistic, p = srm_test([5300, 4700], [50, 50])
    assert statistic == pytest.approx(36.0)
    assert p == pytest.approx(1.973175290075393e-09, rel=1e-9)


def test_srm_without_data() -> None:
    assert srm_test([0, 0], [50, 50]) is None
