#!/usr/bin/env python3
# ruff: noqa: E501
"""Build deterministic PA-5C1 fixture bytes, never human-review receipts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from research_automation_supervisor.physics_benchmark_blindness import (
    build_fixture_review_packet,
    load_blind_fixture_catalog,
)

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "examples/physics_auditor/benchmark_v1"
VISIBLE = BENCHMARK / "auditor_visible"
SCORER = BENCHMARK / "scorer_only"
GL_SOURCE = ROOT.parent / "GL-with-AI"
GL_COMMIT = "7d04b5b9882dcd476c1457b8d711ac7b5520b2c1"

ORACLE = '''#!/usr/bin/python3
"""Generic raw-measurement normalizer for a PA-5C1 visible fixture."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: raw_measurement_oracle.py OBSERVATIONS_JSON")
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if set(value) != {"measurements", "schema_version"} or value["schema_version"] != 1:
        raise SystemExit("invalid raw measurement envelope")
    rows = value["measurements"]
    if not isinstance(rows, list) or not rows:
        raise SystemExit("raw measurements are unavailable")
    for row in rows:
        if set(row) != {"name", "uncertainty", "unit", "value"}:
            raise SystemExit("invalid raw measurement row")
        if not isinstance(row["name"], str) or not isinstance(row["unit"], str):
            raise SystemExit("invalid raw measurement metadata")
        if isinstance(row["value"], bool) or isinstance(row["uncertainty"], bool):
            raise SystemExit("raw scalar fields cannot be boolean")
    print(json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
'''

EVIDENCE = """# Visible fixture evidence

Inspect the declared source and the raw scalar measurements. Apply only the physical
identity and limiting case in `contract.yaml`. The measurements carry no classification.
"""


def measurement(
    name: str, value: float | int | None, unit: str, uncertainty: float | int | None = None
) -> dict[str, Any]:
    return {"name": name, "uncertainty": uncertainty, "unit": unit, "value": value}


CASES: tuple[dict[str, Any], ...] = (
    {
        "convention": "Unit scalar response",
        "identity": "For the declared response law, y = 2 x.",
        "limit": "At x = 0, y = 0.",
        "clean_source": "def response(x: float) -> float:\n    return 2.0 * x\n",
        "defect_source": "def response(x: float) -> float:\n    return 1.5 * x\n",
        "clean_measurements": [measurement("response_at_x3", 6.0, "1")],
        "defect_measurements": [measurement("response_at_x3", 4.5, "1")],
        "diagnosis": "The response slope is 1.5 instead of the declared value 2.",
        "route": "request_repair",
        "categories": ["violated_identity"],
    },
    {
        "convention": "Positive force and unit inertial mass",
        "identity": "Acceleration equals the signed applied force.",
        "limit": "Zero applied force gives zero acceleration.",
        "clean_source": "def acceleration(force: float) -> float:\n    return force\n",
        "defect_source": "def acceleration(force: float) -> float:\n    return -force\n",
        "clean_measurements": [measurement("acceleration_at_force_2", 2.0, "m/s^2")],
        "defect_measurements": [measurement("acceleration_at_force_2", -2.0, "m/s^2")],
        "diagnosis": "The implementation reverses the locked force sign.",
        "route": "request_repair",
        "categories": ["sign_or_normalization_error"],
    },
    {
        "convention": "Unit-normalized Gaussian density",
        "identity": "The density is exp(-x^2/(2 sigma^2))/(sqrt(2 pi) sigma) for sigma > 0.",
        "limit": "At x = 0 and sigma = 1, the density is 1/sqrt(2 pi).",
        "clean_source": "import math\n\ndef density(x: float, sigma: float) -> float:\n    return math.exp(-(x*x)/(2*sigma*sigma))/(math.sqrt(2*math.pi)*sigma)\n",
        "defect_source": "import math\n\ndef density(x: float, sigma: float) -> float:\n    return math.exp(-(x*x)/(2*sigma*sigma))\n",
        "clean_measurements": [measurement("density_at_origin", 0.3989422804014327, "1")],
        "defect_measurements": [measurement("density_at_origin", 1.0, "1")],
        "diagnosis": "The Gaussian normalization factor is omitted.",
        "route": "request_repair",
        "categories": ["sign_or_normalization_error"],
    },
    {
        "convention": "Contravariant cylindrical components with metric diag(1,r^2)",
        "identity": "For (A^r,A^theta), A_i A^i = (A^r)^2 + r^2 (A^theta)^2.",
        "limit": "At r = 1, the contraction is (A^r)^2 + (A^theta)^2.",
        "clean_source": "def vector_norm_sq(a_r: float, a_theta: float, r: float) -> float:\n    return a_r*a_r + r*r*a_theta*a_theta\n",
        "defect_source": "def vector_norm_sq(a_r: float, a_theta: float, r: float) -> float:\n    return a_r*a_r + a_theta*a_theta\n",
        "clean_measurements": [measurement("norm_sq_at_r2", 4.0, "1")],
        "defect_measurements": [measurement("norm_sq_at_r2", 1.0, "1")],
        "diagnosis": "The cylindrical metric factor r squared is omitted.",
        "route": "request_repair",
        "categories": ["tensor_or_index_error"],
    },
    {
        "convention": "Lorentzian signature (-,+)",
        "identity": "Lowering the timelike component gives V_0 = -V^0.",
        "limit": "V^0 = 0 gives V_0 = 0.",
        "clean_source": "def lower_time(v_up_0: float) -> float:\n    return -v_up_0\n",
        "defect_source": "def lower_time(v_up_0: float) -> float:\n    return v_up_0\n",
        "clean_measurements": [measurement("lowered_component", -2.0, "1")],
        "defect_measurements": [measurement("lowered_component", 2.0, "1")],
        "diagnosis": "The timelike component is copied without the lowering sign.",
        "route": "request_repair",
        "categories": ["tensor_or_index_error"],
    },
    {
        "convention": "Position in metres, velocity in metres per second, and time in seconds",
        "identity": "The constant-velocity update is x_new = x + dt v.",
        "limit": "At dt = 0, position is unchanged.",
        "clean_source": "def advance(x: float, velocity: float, dt: float) -> float:\n    return x + dt*velocity\n",
        "defect_source": "def advance(x: float, velocity: float, dt: float) -> float:\n    return x + velocity\n",
        "clean_measurements": [measurement("position_after_step", 1.75, "m")],
        "defect_measurements": [measurement("position_after_step", 4.0, "m")],
        "diagnosis": "The update omits the time increment and adds unlike dimensions.",
        "route": "request_repair",
        "categories": ["dimensional_inconsistency"],
    },
    {
        "convention": "Euclidean metric for a diagonal three-tensor",
        "identity": "A tensor declared trace-free has T_xx + T_yy + T_zz = 0.",
        "limit": "The zero tensor has zero trace.",
        "clean_source": "COMPONENTS = (2.0, -1.0, -1.0)\n\ndef trace() -> float:\n    return sum(COMPONENTS)\n",
        "defect_source": "COMPONENTS = (2.0, -1.0, 0.0)\n\ndef trace() -> float:\n    return sum(COMPONENTS)\n",
        "clean_measurements": [measurement("metric_trace", 0.0, "1")],
        "defect_measurements": [measurement("metric_trace", 1.0, "1")],
        "diagnosis": "The declared trace-free tensor has nonzero metric trace.",
        "route": "request_repair",
        "categories": ["violated_identity"],
    },
    {
        "convention": "Harmonic potential V(x)=k x^2/2 with k>0",
        "identity": "The conservative force is F(x) = -dV/dx = -k x.",
        "limit": "At x = 0, the force vanishes.",
        "clean_source": "def force(x: float, stiffness: float) -> float:\n    return -stiffness*x\n",
        "defect_source": "def force(x: float, stiffness: float) -> float:\n    return -2.0*stiffness*x\n",
        "clean_measurements": [measurement("force_at_x2_k3", -6.0, "N")],
        "defect_measurements": [measurement("force_at_x2_k3", -12.0, "N")],
        "diagnosis": "Differentiation of the half-quadratic potential retains an extra factor two.",
        "route": "request_repair",
        "categories": ["violated_identity"],
    },
    {
        "convention": "Spherically symmetric Euclidean scalar Laplacian for r>0",
        "identity": "The radial Laplacian is f''(r) + 2 f'(r)/r.",
        "limit": "For a regular even field, the r to zero limit is 3 f''(0).",
        "clean_source": "def radial_laplacian(f_prime: float, f_second: float, r: float) -> float:\n    if r == 0.0:\n        return 3.0*f_second\n    return f_second + 2.0*f_prime/r\n",
        "defect_source": "def radial_laplacian(f_prime: float, f_second: float, r: float) -> float:\n    if r == 0.0:\n        return 3.0*f_second\n    return f_second\n",
        "clean_measurements": [
            measurement("laplacian_of_r_squared_at_r0", 6.0, "1"),
            measurement("laplacian_of_r_squared_at_r2", 6.0, "1"),
        ],
        "defect_measurements": [
            measurement("laplacian_of_r_squared_at_r0", 6.0, "1"),
            measurement("laplacian_of_r_squared_at_r2", 2.0, "1"),
        ],
        "diagnosis": "The spherical-coordinate 2 f-prime over r contribution is omitted.",
        "route": "request_repair",
        "categories": ["failed_limiting_case"],
        "acceptable_alternative_categories": [
            "tensor_or_index_error",
            "violated_identity",
        ],
    },
    {
        "convention": "Uniform-cell midpoint quadrature",
        "identity": "The discrete approximation to integral f dx is sum_i f_i dx.",
        "limit": "The integral of a zero field is zero.",
        "clean_source": "def integrate(values: list[float], dx: float) -> float:\n    return sum(values)*dx\n",
        "defect_source": "def integrate(values: list[float], dx: float) -> float:\n    return sum(values)\n",
        "clean_measurements": [measurement("integral", 1.0, "1")],
        "defect_measurements": [measurement("integral", 4.0, "1")],
        "diagnosis": "The cell width is omitted from the quadrature.",
        "route": "request_repair",
        "categories": ["continuum_discrete_mismatch"],
    },
    {
        "convention": "Second-order centered first derivative",
        "identity": "D f_i = (f_(i+1)-f_(i-1))/(2 h).",
        "limit": "The centered derivative of a constant is zero.",
        "clean_source": "def centered(left: float, right: float, h: float) -> float:\n    return (right-left)/(2.0*h)\n",
        "defect_source": "def centered(left: float, right: float, h: float) -> float:\n    return (right-left)/h\n",
        "clean_measurements": [measurement("derivative", 1.0, "1")],
        "defect_measurements": [measurement("derivative", 2.0, "1")],
        "diagnosis": "The centered-stencil denominator omits a factor two.",
        "route": "request_repair",
        "categories": ["continuum_discrete_mismatch"],
    },
    {
        "convention": "Uniform factor-two refinement with a common L2 error norm",
        "identity": "Second-order convergence requires two consecutive error ratios near four and consistent observed orders.",
        "limit": "Each refinement must reduce the same declared error norm.",
        "clean_source": 'RESOLUTIONS = (64, 128, 256)\nERRORS = (1.6e-3, 4.0e-4, 1.0e-4)\nCLAIM = "second-order convergence"\n',
        "defect_source": 'RESOLUTIONS = (64, 128, 256)\nERRORS = (1.6e-3, 9.6e-4, 5.8e-4)\nCLAIM = "second-order convergence"\n',
        "clean_measurements": [
            measurement("coarse_medium_ratio", 4.0, "1"),
            measurement("medium_fine_ratio", 4.0, "1"),
        ],
        "defect_measurements": [
            measurement("coarse_medium_ratio", 1.6666666667, "1"),
            measurement("medium_fine_ratio", 1.6551724138, "1"),
        ],
        "diagnosis": "Three same-norm resolutions yield order near 0.74, not second order.",
        "route": "request_repair",
        "categories": ["insufficient_numerical_evidence"],
    },
    {
        "convention": "Orthogonal physical and constraint diagnostic subspaces",
        "identity": "A physical-mode claim must report its physical and constraint projection fractions under the declared inner product.",
        "limit": "The two declared projection fractions sum to one within tolerance.",
        "clean_source": 'PHYSICAL_FRACTION = 0.95\nCONSTRAINT_FRACTION = 0.05\nCLAIM = "physical candidate"\n',
        "defect_source": 'PHYSICAL_FRACTION = 0.08\nCONSTRAINT_FRACTION = 0.92\nCLAIM = "physical candidate"\n',
        "clean_measurements": [
            measurement("physical_fraction", 0.95, "1"),
            measurement("constraint_fraction", 0.05, "1"),
        ],
        "defect_measurements": [
            measurement("physical_fraction", 0.08, "1"),
            measurement("constraint_fraction", 0.92, "1"),
        ],
        "diagnosis": "The physical label conflicts with a constraint-dominated projection.",
        "route": "require_human_review",
        "categories": ["gauge_constraint_ambiguity"],
    },
    {
        "convention": "Normalized overlap with a frozen pure-gauge generator",
        "identity": "A physical-mode claim must account for the declared normalized gauge overlap.",
        "limit": "The normalized overlap lies between zero and one.",
        "clean_source": 'GAUGE_OVERLAP = 0.01\nCLAIM = "physical candidate"\n',
        "defect_source": 'GAUGE_OVERLAP = 0.999\nCLAIM = "physical candidate"\n',
        "clean_measurements": [measurement("gauge_overlap", 0.01, "1")],
        "defect_measurements": [measurement("gauge_overlap", 0.999, "1")],
        "diagnosis": "The physical label is unresolved because the candidate nearly equals the gauge generator.",
        "route": "require_human_review",
        "categories": ["gauge_constraint_ambiguity"],
    },
    {
        "convention": "Energy fraction in a fixed three-cell boundary layer",
        "identity": "A bulk-instability claim must account for the declared boundary-layer energy fraction.",
        "limit": "Boundary and interior fractions sum to one within tolerance.",
        "clean_source": 'BOUNDARY_FRACTION = 0.04\nINTERIOR_FRACTION = 0.96\nCLAIM = "bulk instability"\n',
        "defect_source": 'BOUNDARY_FRACTION = 0.96\nINTERIOR_FRACTION = 0.04\nCLAIM = "bulk instability"\n',
        "clean_measurements": [
            measurement("boundary_fraction", 0.04, "1"),
            measurement("interior_fraction", 0.96, "1"),
        ],
        "defect_measurements": [
            measurement("boundary_fraction", 0.96, "1"),
            measurement("interior_fraction", 0.04, "1"),
        ],
        "diagnosis": "The claimed bulk behavior is dominated by the declared boundary layer.",
        "route": "require_human_review",
        "categories": ["new_physical_interpretation", "unsupported_physical_claim"],
    },
    {
        "convention": "Matched-window exponential fits of normalized L2 and Linf amplitudes",
        "identity": "A norm-robust growth claim requires statistically compatible fitted exponents from both norms over the same time window.",
        "limit": "A time-independent signal has zero fitted exponent in both norms.",
        "clean_source": 'L2_RATE = 0.20\nLINF_RATE = 0.21\nL2_SIGMA = 0.02\nLINF_SIGMA = 0.02\nCLAIM = "norm-robust growth"\n',
        "defect_source": 'L2_RATE = 0.20\nLINF_RATE = 0.61\nL2_SIGMA = 0.02\nLINF_SIGMA = 0.03\nCLAIM = "norm-robust growth"\n',
        "clean_measurements": [
            measurement("l2_growth_rate", 0.20, "1/s", 0.02),
            measurement("linf_growth_rate", 0.21, "1/s", 0.02),
        ],
        "defect_measurements": [
            measurement("l2_growth_rate", 0.20, "1/s", 0.02),
            measurement("linf_growth_rate", 0.61, "1/s", 0.03),
        ],
        "diagnosis": "Matched-window growth exponents disagree by more than ten combined standard deviations.",
        "route": "request_repair",
        "categories": ["insufficient_numerical_evidence"],
    },
    {
        "convention": "Algebraically equivalent centered derivative forms",
        "identity": "Any expression equal to (right-left)/(2 h) satisfies the declared stencil.",
        "limit": "Constant left and right values give zero.",
        "clean_source": "def centered(left: float, right: float, h: float) -> float:\n    return 0.5*(right-left)/h\n",
        "defect_source": "def centered(left: float, right: float, h: float) -> float:\n    return 0.25*(right-left)/h\n",
        "clean_measurements": [measurement("derivative", 1.0, "1")],
        "defect_measurements": [measurement("derivative", 0.5, "1")],
        "diagnosis": "The alternative form has an extra factor one-half.",
        "route": "request_repair",
        "categories": ["continuum_discrete_mismatch"],
    },
    {
        "convention": "Explicit independent and source-derived provenance for a scalar response",
        "identity": "The declared response law requires at least one raw measurement explicitly marked independent in addition to source inspection.",
        "limit": "The zero-input response must carry explicit provenance; a source-derived value is not an independent observation.",
        "clean_source": "def response(x: float) -> float:\n    return 2.0*x\n\nINDEPENDENT_SAMPLE_COUNT = 2\nINDEPENDENT_ZERO_INPUT_RESPONSE = 0.0\nSOURCE_DERIVED_ZERO_INPUT_RESPONSE = None\n",
        "defect_source": "def response(x: float) -> float:\n    return 2.0*x\n\nINDEPENDENT_SAMPLE_COUNT = 0\nINDEPENDENT_ZERO_INPUT_RESPONSE = None\nSOURCE_DERIVED_ZERO_INPUT_RESPONSE = 0.0\n",
        "clean_measurements": [
            measurement("independent_sample_count", 2, "count"),
            measurement("independent_zero_input_response", 0.0, "1"),
            measurement("source_derived_zero_input_response", None, "1"),
        ],
        "defect_measurements": [
            measurement("independent_sample_count", 0, "count"),
            measurement("independent_zero_input_response", None, "1"),
            measurement("source_derived_zero_input_response", 0.0, "1"),
        ],
        "diagnosis": "Required independent observations are unavailable; the zero-input value is explicitly source-derived.",
        "route": "block_insufficient_evidence",
        "categories": ["missing_required_evidence"],
    },
    {
        "convention": "Positive force convention fixed by the task contract",
        "identity": "The implementation and any requested convention change must be assessed against the currently locked sign.",
        "limit": "Zero force maps to zero acceleration under either sign convention.",
        "clean_source": 'REQUEST = "retain the declared positive-force convention"\n',
        "defect_source": 'REQUEST = "replace the declared sign convention with negative acceleration"\n',
        "clean_measurements": [measurement("acceleration_at_force_2", 2.0, "m/s^2")],
        "defect_measurements": [measurement("acceleration_at_force_2", -2.0, "m/s^2")],
        "diagnosis": "The candidate requests a change to locked physical convention authority.",
        "route": "require_human_review",
        "categories": ["convention_change_requested"],
    },
    {
        "convention": "Oscillator equation q-double-dot + omega-squared q = 0",
        "identity": "Independent analytic and fitted omega-squared estimates must be statistically compatible before a shared oscillator interpretation is asserted.",
        "limit": "A positive omega-squared corresponds to bounded sinusoidal solutions under the declared equation.",
        "clean_source": "ANALYTIC_OMEGA_SQ = 1.00\nFITTED_OMEGA_SQ = 0.99\n",
        "defect_source": "ANALYTIC_OMEGA_SQ = 1.00\nFITTED_OMEGA_SQ = -0.20\n",
        "clean_measurements": [
            measurement("analytic_omega_squared", 1.00, "1/s^2", 0.02),
            measurement("fitted_omega_squared", 0.99, "1/s^2", 0.03),
        ],
        "defect_measurements": [
            measurement("analytic_omega_squared", 1.00, "1/s^2", 0.02),
            measurement("fitted_omega_squared", -0.20, "1/s^2", 0.03),
        ],
        "diagnosis": "Independent omega-squared estimates disagree in sign by many combined uncertainties.",
        "route": "require_human_review",
        "categories": ["conflicting_evidence"],
    },
    {
        "convention": "Dimensionless residual under a fixed normalization",
        "identity": "Any physical claim attached to the residual requires a declared mechanism and independent supporting evidence.",
        "limit": "A zero residual supplies no evidence for a nonzero effect.",
        "clean_source": 'RESIDUAL = 0.003\nSTATEMENT = "bounded residual measurement"\n',
        "defect_source": 'RESIDUAL = 0.003\nSTATEMENT = "new physical instability"\n',
        "clean_measurements": [measurement("normalized_residual", 0.003, "1", 0.001)],
        "defect_measurements": [measurement("normalized_residual", 0.003, "1", 0.001)],
        "diagnosis": "A small residual is promoted to a new physical interpretation without mechanism or independent support.",
        "route": "require_human_review",
        "categories": ["new_physical_interpretation", "unsupported_physical_claim"],
    },
)

GL_SOURCES: tuple[tuple[tuple[str, str], ...], ...] = (
    (("docs/derivations/stage4AO_A_uniform_gp_background_residual.md", "locked_derivation"),),
    (
        ("code/BlackStringToy/ConformalCartoonAlgebra.hpp", "implementation"),
        ("code/BlackStringToy/tests/Stage4ALTraceFreeCurvatureLapseBlockTest.cpp", "test"),
        ("docs/derivations/stage4AO_A_uniform_gp_background_residual.md", "locked_derivation"),
    ),
    (
        ("code/BlackStringToy/tests/Stage4AOCGRChomboComparisonBatch4GaugeTest.cpp", "test"),
        ("docs/derivations/stage4AO_A_uniform_gp_background_residual.md", "locked_derivation"),
    ),
    (
        ("code/BlackStringToy/CartoonHatGammaX.hpp", "implementation"),
        ("code/BlackStringToy/tests/Stage4ANHatGammaXTest.cpp", "test"),
        ("docs/derivations/stage4AM_hatGammaX_derivation.md", "locked_derivation"),
    ),
    (
        ("code/BlackStringToy/Stage4AOGPDiscretePreflight.hpp", "implementation"),
        ("code/BlackStringToy/tests/Stage4AOBDiscreteOperatorPreflightTest.cpp", "test"),
    ),
    (("docs/derivations/stage4AO_C_frozen_gauge_spectral_gate.md", "locked_derivation"),),
    (("docs/derivations/stage4AO_C_frozen_gauge_spectral_gate.md", "locked_derivation"),),
    (("docs/derivations/stage4AO_C_frozen_gauge_spectral_gate.md", "locked_derivation"),),
    (("docs/derivations/stage4AO_C_frozen_gauge_spectral_gate.md", "locked_derivation"),),
    (
        ("code/BlackStringToy/ConformalCartoonAlgebra.hpp", "implementation"),
        ("code/BlackStringToy/tests/Stage4AOCGRChomboComparisonBatch1Test.cpp", "test"),
    ),
)

GL_IDENTITIES = (
    "Assess the exact background ledger and distinguish the live-lapse and frozen geometric residual quantities.",
    "Assess the four-dimensional spatial trace using two hidden ww copies.",
    "Assess the fixed field-independent lapse source and its evolved-field derivatives.",
    "Assess the full hidden-direction contraction and the declared hatted connection definition.",
    "Assess the three-resolution discrete preflight using the exact implementation and test blobs.",
    "Assess the candidate statement against the exact spectral-gate source and raw projection measurements.",
    "Assess the candidate statement against the exact spectral-gate source and raw overlap measurements.",
    "Assess the candidate statement against the exact spectral-gate source and raw localization measurements.",
    "Assess the supplied statement using both declared indicators and the exact spectral-gate source.",
    "Assess determinant, inverse, trace, projection, and index operations in the exact conformal algebra blobs.",
)

GL_LIMITS = (
    "Report the declared uniform-background residual values without adding a mode interpretation.",
    "The zero conformal extrinsic curvature has zero trace.",
    "The field-independent source has zero derivative with respect to evolved fields.",
    "Flat conformal data with zero encoded Z gives a zero hatted connection.",
    "Report the two consecutive refinement ratios and fine residual without extrapolation.",
    "Report the normalized projection fractions without converting them to a class label.",
    "Report the normalized overlap without converting it to a class label.",
    "Report the boundary and interior fractions without converting them to a class label.",
    "Report both indicators without selecting an interpretation not present in source authority.",
    "Identity conformal metric and zero tensor give zero determinant-safe trace.",
)

GL_OBSERVATIONS = (
    [
        measurement("live_lapse_residual", -3.0, "lambda"),
        measurement("frozen_geometric_residual", 0.0, "lambda"),
    ],
    [
        measurement("a_xx", -0.875, "lambda"),
        measurement("a_zz", -0.375, "lambda"),
        measurement("a_ww", 0.625, "lambda"),
        measurement("hidden_ww_multiplicity", 2, "count"),
    ],
    [
        measurement("lapse_drift", -3.0, "lambda"),
        measurement("fixed_source", 3.0, "lambda"),
        measurement("field_jacobian_norm", 0.0, "1"),
    ],
    [measurement("hidden_multiplicity", 2, "count"), measurement("flat_hat_gamma_x", 0.0, "1")],
    [
        measurement("coarse_medium_ratio", 3.61, "1"),
        measurement("medium_fine_ratio", 3.54, "1"),
        measurement("fine_hat_gamma_residual", 0.0008, "1"),
    ],
    [
        measurement("constraint_projection_fraction", 0.92, "1"),
        measurement("physical_projection_fraction", 0.08, "1"),
    ],
    [measurement("gauge_overlap", 0.999, "1"), measurement("orthogonal_fraction", 0.001, "1")],
    [
        measurement("boundary_layer_fraction", 0.96, "1"),
        measurement("interior_fraction", 0.04, "1"),
    ],
    [measurement("physical_indicator", 0.63, "1"), measurement("constraint_indicator", 0.58, "1")],
    [measurement("determinant_error", 0.0, "1"), measurement("trace_projection_error", 0.0, "1")],
)

GL_SOURCES_TEXT = (
    'STATEMENT = "bounded background-ledger assessment"\n',
    'STATEMENT = "bounded conformal-trace assessment"\n',
    'STATEMENT = "bounded lapse-source assessment"\n',
    'STATEMENT = "bounded hatted-connection assessment"\n',
    'STATEMENT = "bounded discrete-preflight assessment"\n',
    'STATEMENT = "physical mode"\n',
    'STATEMENT = "physical mode"\n',
    'STATEMENT = "bulk instability"\n',
    'STATEMENT = "resolved physical mode"\n',
    'STATEMENT = "bounded conformal-algebra assessment"\n',
)

GL_EXPECTED = (
    ("pass", "The exact locked background ledger is internally consistent.", []),
    ("pass", "The exact hidden-multiplicity trace calculation is consistent.", []),
    ("pass", "The exact lapse source cancels only the declared drift.", []),
    ("pass", "The exact hatted-connection implementation is consistent.", []),
    ("pass", "The exact preflight satisfies its bounded numerical gates.", []),
    (
        "require_human_review",
        "The physical label is not established by the constraint-heavy projection.",
        ["gauge_constraint_ambiguity"],
    ),
    (
        "require_human_review",
        "The physical label is not established by the near-unit gauge overlap.",
        ["gauge_constraint_ambiguity"],
    ),
    (
        "require_human_review",
        "The bulk claim is not established by a boundary-localized observation.",
        ["new_physical_interpretation", "unsupported_physical_claim"],
    ),
    (
        "require_human_review",
        "The two nonexclusive indicators do not select a unique interpretation.",
        ["gauge_constraint_ambiguity"],
    ),
    ("pass", "The exact bounded conformal algebra sources satisfy the declared checks.", []),
)


def contract(
    *, convention: str, identity: str, limit: str, source_paths: tuple[tuple[str, str], ...] = ()
) -> str:
    evidence = [
        ("fixture_title", "document", "title.txt"),
        ("case_note", "document", "evidence.md"),
        ("candidate_source", "artifact", "implementation.py"),
        ("raw_measurements", "numerical", "observations.json"),
    ]
    for index, (path, role) in enumerate(source_paths, start=1):
        kind = "derivation" if role == "locked_derivation" else "artifact"
        evidence.append((f"source_{index:03d}", kind, f"source/{path}"))
    evidence_yaml = "\n".join(
        f"  - id: {item_id}\n    kind: {kind}\n    description: Declared auditor-visible evidence.\n    path: {path}\n    required_for: [primary_identity]"
        for item_id, kind, path in evidence
    )
    return f"""schema_version: 1
profile: physics_implementation
conventions:
  - id: declared_convention
    value: {convention}
    authority: task_locked
assumptions:
  - id: bounded_scope
    statement: This is a bounded fixture assessment and authorizes no discovery or publication claim.
required_identities:
  - id: primary_identity
    statement: {identity}
    required_evidence_kinds: [document, numerical]
    oracle_ids: [raw_measurement_oracle]
limiting_cases:
  - id: control_limit
    statement: {limit}
    required_evidence_kinds: [numerical]
    oracle_ids: [raw_measurement_oracle]
evidence:
{evidence_yaml}
oracles:
  - id: raw_measurement_oracle
    kind: numerical
    reference: raw_measurement_oracle
    statement: Normalize and return declared scalar measurements without classification.
    check_ids: [primary_identity, control_limit]
    required: true
forbidden_claims:
  - id: no_discovery_claim
    statement: Do not present this bounded fixture as a new physical discovery.
human_gate:
  required_for:
    - convention_change
    - new_physical_interpretation
    - unresolved_gauge_constraint_ambiguity
audit_policy:
  schema_version: 1
  insufficient_required_evidence: block
  conflicting_required_evidence: human_review
  medium_severity: request_repair
  low_severity: allow_pass
  informational_severity: allow_pass
auditor_role_ref: physics_auditor
"""


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def git_blob(path: str) -> bytes:
    completed = subprocess.run(
        ("/usr/bin/git", "-C", GL_SOURCE, "show", f"{GL_COMMIT}:{path}"),
        capture_output=True,
        check=True,
    )
    return completed.stdout


def main() -> None:
    pairs: list[dict[str, Any]] = []
    for index, item in enumerate(CASES, start=1):
        case_id = f"case_{index:03d}"
        pair_id = f"pair_{index:03d}"
        clean_variant = (
            "variant_001"
            if hashlib.sha256(case_id.encode()).digest()[0] % 2 == 0
            else "variant_002"
        )
        variants: list[dict[str, Any]] = []
        for variant_id in ("variant_001", "variant_002"):
            is_clean = variant_id == clean_variant
            visible_root = f"examples/physics_auditor/benchmark_v1/auditor_visible/cases/{case_id}/{variant_id}"
            directory = ROOT / visible_root
            write_text(directory / "title.txt", f"Benchmark fixture {index:03d}\n")
            write_text(
                directory / "contract.yaml",
                contract(
                    convention=item["convention"], identity=item["identity"], limit=item["limit"]
                ),
            )
            write_text(directory / "evidence.md", EVIDENCE)
            write_text(
                directory / "implementation.py",
                item["clean_source"] if is_clean else item["defect_source"],
            )
            write_json(
                directory / "observations.json",
                {
                    "measurements": item["clean_measurements"]
                    if is_clean
                    else item["defect_measurements"],
                    "schema_version": 1,
                },
            )
            write_text(directory / "raw_measurement_oracle.py", ORACLE)
            variants.append(
                {
                    **(
                        {
                            "acceptable_alternative_categories": item[
                                "acceptable_alternative_categories"
                            ]
                        }
                        if not is_clean and "acceptable_alternative_categories" in item
                        else {}
                    ),
                    "diagnosis": "The declared physical identity and raw observations are consistent."
                    if is_clean
                    else item["diagnosis"],
                    "expected_route": "pass" if is_clean else item["route"],
                    "fixture_label": "clean" if is_clean else "defective",
                    "forbidden_categories": ["report_integrity_error"],
                    "minimum_severity": None if is_clean else "high",
                    "required_categories": [] if is_clean else item["categories"],
                    "variant_id": variant_id,
                    "visible_root": visible_root,
                }
            )
        pairs.append(
            {
                "case_id": case_id,
                "contract_file": "contract.yaml",
                "oracle_files": ["raw_measurement_oracle.py"],
                "pair_id": pair_id,
                "raw_observation_files": ["observations.json"],
                "receipt_path": (
                    f"examples/physics_auditor/benchmark_v1/scorer_only/review_receipts/{case_id}_v2.json"
                    if case_id in {"case_009", "case_018"}
                    else f"examples/physics_auditor/benchmark_v1/scorer_only/review_receipts/{case_id}.json"
                ),
                "schema_version": 1,
                "title_file": "title.txt",
                "variable_files": ["implementation.py", "observations.json"],
                "variants": variants,
            }
        )

    gl_tasks: list[dict[str, Any]] = []
    for index, sources in enumerate(GL_SOURCES, start=1):
        task_id = f"task_{index:03d}"
        visible_root = f"examples/physics_auditor/benchmark_v1/auditor_visible/gl/{task_id}"
        directory = ROOT / visible_root
        write_text(directory / "title.txt", f"GL fixture {index:03d}\n")
        write_text(
            directory / "contract.yaml",
            contract(
                convention=f"Exact source snapshot {GL_COMMIT}",
                identity=GL_IDENTITIES[index - 1],
                limit=GL_LIMITS[index - 1],
                source_paths=sources,
            ),
        )
        write_text(directory / "evidence.md", EVIDENCE)
        write_text(directory / "implementation.py", GL_SOURCES_TEXT[index - 1])
        write_json(
            directory / "observations.json",
            {"measurements": GL_OBSERVATIONS[index - 1], "schema_version": 1},
        )
        write_text(directory / "raw_measurement_oracle.py", ORACLE)
        route, interpretation, categories = GL_EXPECTED[index - 1]
        source_blobs = []
        for path, role in sources:
            raw = git_blob(path)
            source_blobs.append(
                {
                    "byte_length": len(raw),
                    "path": path,
                    "role": role,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        gl_tasks.append(
            {
                "contract_file": "contract.yaml",
                "expected_interpretation": interpretation,
                "expected_route": route,
                "forbidden_categories": ["report_integrity_error"],
                "minimum_severity": None if route == "pass" else "high",
                "oracle_files": ["raw_measurement_oracle.py"],
                "receipt_path": f"examples/physics_auditor/benchmark_v1/scorer_only/review_receipts/{task_id}.json",
                "required_categories": categories,
                "schema_version": 1,
                "source_blobs": source_blobs,
                "task_id": task_id,
                "title_file": "title.txt",
                "visible_root": visible_root,
            }
        )

    SCORER.mkdir(parents=True, exist_ok=True)
    catalog_path = SCORER / "catalog.json"
    write_json(
        catalog_path,
        {
            "auditor_visible_root": "examples/physics_auditor/benchmark_v1/auditor_visible",
            "catalog_id": "physics_benchmark_blind_authority_v1",
            "fixture_author_ids": ["research_automation_fixture_team_v1"],
            "gl_source_commit": GL_COMMIT,
            "gl_tasks": gl_tasks,
            "pairs": pairs,
            "schema_version": 1,
            "scorer_only_root": "examples/physics_auditor/benchmark_v1/scorer_only",
        },
    )
    catalog = load_blind_fixture_catalog(catalog_path)
    review_packet = build_fixture_review_packet(
        catalog,
        repository_root=ROOT,
        source_repository_root=GL_SOURCE,
    )
    write_json(SCORER / "review-packet.json", review_packet.model_dump(mode="json"))


if __name__ == "__main__":
    main()
