"""Structurally different but physically equivalent public synthetic implementation."""


def acceleration(force: float) -> float:
    """Express the unit-mass identity without copying the clean implementation."""
    unit_mass = 1.0
    return force / unit_mass
