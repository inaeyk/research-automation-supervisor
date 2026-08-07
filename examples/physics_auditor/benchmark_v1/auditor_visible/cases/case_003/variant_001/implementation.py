import math

def density(x: float, sigma: float) -> float:
    return math.exp(-(x*x)/(2*sigma*sigma))
