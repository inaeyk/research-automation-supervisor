# Bounded locked GL pilot evidence

Source repository: `GL-with-AI`

Source commit: `7d04b5b9882dcd476c1457b8d711ac7b5520b2c1`

- `docs/derivations/stage4AO_A_uniform_gp_background_residual.md` SHA-256 `a8536115e4488cb8b15e85c83b1fbf7b20282fe5d52c08c86380c398328b22df` (locked_derivation)
- `code/BlackStringToy/tests/Stage4AOCGRChomboComparisonBatch4GaugeTest.cpp` SHA-256 `a05754b891bedef09953de004602791dce9eb7117b23cd93802b67aa721a33e4` (test)

The locked test constructs the direct moving-puncture lapse residual -3 lambda and a fixed +3 lambda source. It checks cancellation to 1e-13, unchanged shift/B, zero field Jacobian, and mutation detection for wrong sign and missing factor three.

This snapshot excludes logs, hidden evaluation material, open questions, and publication authority.

