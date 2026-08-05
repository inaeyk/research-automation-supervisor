"""Public synthetic candidate evidence for one opaque PA-5B case."""

import math


def density(x: float, sigma: float) -> float:
    return math.exp(-(x*x)/(2.0*sigma*sigma))

