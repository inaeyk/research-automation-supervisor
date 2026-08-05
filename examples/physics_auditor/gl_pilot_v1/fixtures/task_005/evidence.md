# Bounded locked GL pilot evidence

Source repository: `GL-with-AI`

Source commit: `7d04b5b9882dcd476c1457b8d711ac7b5520b2c1`

- `code/BlackStringToy/Stage4AOGPDiscretePreflight.hpp` SHA-256 `1c298b88e4b52c6d2778ab5dfd6347847b5527dc351edfb22bfb56b82e513f97` (implementation)
- `code/BlackStringToy/tests/Stage4AOBDiscreteOperatorPreflightTest.cpp` SHA-256 `5e4928d4c74f57a9e94e1ee71a6eb242fa54ad5e67841a357438fd349d3d2221` (test)

The locked test uses the unmodified discrete RHS, not the GP-holding source. It checks 256/512/1024, two ratios above 3.4, three lapse target errors below 1e-12, and a fine hat_Gamma^x residual below 1e-3. This is a local preflight, not a completed spectral operator claim.

This snapshot excludes logs, hidden evaluation material, open questions, and publication authority.

