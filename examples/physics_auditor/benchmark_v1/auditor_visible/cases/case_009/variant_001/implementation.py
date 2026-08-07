def radial_laplacian(f_prime: float, f_second: float, r: float) -> float:
    if r == 0.0:
        return 3.0*f_second
    return f_second + 2.0*f_prime/r
