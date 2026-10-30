import math

Z_95 = 1.959963984540054
MAX_ITERATIONS = 1000
EPSILON = 1e-15


def wilson_interval(conversions: int, exposures: int) -> tuple[float, float] | None:
    if exposures == 0:
        return None
    rate = conversions / exposures
    z2 = Z_95**2
    denominator = 1 + z2 / exposures
    centre = rate + z2 / (2 * exposures)
    margin = Z_95 * math.sqrt(rate * (1 - rate) / exposures + z2 / (4 * exposures**2))
    return (
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    )


def two_proportion_z_test(
    control_conversions: int,
    control_exposures: int,
    variant_conversions: int,
    variant_exposures: int,
) -> tuple[float, float] | None:
    """Returns (z, two-sided p), or None when the pooled variance is zero."""
    if control_exposures == 0 or variant_exposures == 0:
        return None
    pooled = (control_conversions + variant_conversions) / (control_exposures + variant_exposures)
    variance = pooled * (1 - pooled) * (1 / control_exposures + 1 / variant_exposures)
    if variance == 0:
        return None
    difference = variant_conversions / variant_exposures - control_conversions / control_exposures
    z = difference / math.sqrt(variance)
    return z, math.erfc(abs(z) / math.sqrt(2))


def _lower_gamma_series(a: float, x: float) -> float:
    term = total = 1 / a
    denominator = a
    for _ in range(MAX_ITERATIONS):
        denominator += 1
        term *= x / denominator
        total += term
        if abs(term) < abs(total) * EPSILON:
            break
    return total * math.exp(a * math.log(x) - x - math.lgamma(a))


def _upper_gamma_fraction(a: float, x: float) -> float:
    # Modified Lentz evaluation of the continued fraction for Q(a, x).
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    fraction = d
    for i in range(1, MAX_ITERATIONS):
        an = i * (a - i)
        b += 2
        d = an * d + b
        d = 1 / (d if abs(d) > tiny else tiny)
        c = b + an / c
        c = c if abs(c) > tiny else tiny
        delta = c * d
        fraction *= delta
        if abs(delta - 1) < EPSILON:
            break
    return fraction * math.exp(a * math.log(x) - x - math.lgamma(a))


def chi_square_sf(statistic: float, df: int) -> float:
    """P(X >= statistic), which is the regularized upper incomplete gamma Q(df/2, statistic/2)."""
    if statistic <= 0:
        return 1.0
    a, x = df / 2, statistic / 2
    # The series converges fast below a + 1 and the fraction above it.
    if x < a + 1:
        return max(0.0, 1 - _lower_gamma_series(a, x))
    return _upper_gamma_fraction(a, x)


def srm_test(observed: list[int], weights: list[int]) -> tuple[float, float] | None:
    """Returns (chi-square, p) of observed counts against weights, or None with no data."""
    total = sum(observed)
    if total == 0:
        return None
    weight_total = sum(weights)
    statistic = 0.0
    for count, weight in zip(observed, weights):
        expected = total * weight / weight_total
        statistic += (count - expected) ** 2 / expected
    return statistic, chi_square_sf(statistic, len(observed) - 1)
