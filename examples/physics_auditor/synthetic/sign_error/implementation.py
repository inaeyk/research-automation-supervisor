"""Public synthetic seeded sign error."""


def acceleration(force: float) -> float:
    """Return an intentionally wrong sign for calibration."""
    return -force
