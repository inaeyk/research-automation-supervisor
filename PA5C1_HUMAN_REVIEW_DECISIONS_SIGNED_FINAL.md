# PA-5C1 Human Review Decisions — SIGNED

> **Status:** Human reviewer `inaeyk` has adopted the subject decisions and scientific-review text recorded in this file. This signed decision source is not itself the repository receipt bundle; Codex must convert it into the exact receipt schema and validate the canonical receipt digests.

## Frozen authority

- Raw review-packet SHA-256: `cf42b305dffda08f9b5c52dde8d329e8c0a8d20454dc9168813913ee5afe5873`
- Canonical semantic review-packet digest: `34fb40cabd94f55a5f1712d9f48bb68d57d3907c4409b1bac6f5e31c56c2423e`
- Catalog canonical digest: `8bfaa4c0987e70bfda4efd48a449905a5a694391b3eae810c3bae6615ae816d6`
- Labeled-review ZIP SHA-256: `b1aa7e3cb38f305c308423d6f682fb53b2d9035a58264089a72c02a6611bf993`
- Fixture-author IDs: `research_automation_fixture_team_v1`

## Decision summary

- `approve`: 29 subjects
- `revise`: 2 subjects
- `remove`: 0 subjects

Subjects requiring revision before PA-5C1 qualification:

- `case_009`
- `case_018`

## Human signing fields

Completed by the human reviewer:

```yaml
reviewer_id: "inaeyk"
reviewer_kind: "human"  # repository-valid human reviewer kind
issued_at: "2026-08-06T17:19:00+08:00"
global_human_attestation: "I am a human reviewer distinct from the listed fixture authors. I reviewed the exact manifest-bound subject, independently assessed its scientific authority and auditor-visible blindness, and adopt the decision and scientific-review text recorded for that subject."
```

Recommended attestation text, to be entered only if true:

> I am a human reviewer distinct from the listed fixture authors. I reviewed the exact manifest-bound subject, independently assessed its scientific authority and auditor-visible blindness, and adopt the decision and scientific-review text recorded for that subject.

The repository must create a separate receipt for every subject. `receipt_id` and `receipt_sha256` must be generated and validated by the repository; they must not be invented manually.

## Codex import instructions

Codex should:

1. Verify the packet, catalog, subject manifest, and scorer-authority hashes against the frozen authority above.
2. Refuse to import while the global human signing fields are blank.
3. Treat every subject record below as the proposed human decision authority; do not rewrite its decision or scientific-review text.
4. Convert the records into the exact repository receipt schema only after the human reviewer fills and explicitly adopts the signing fields.
5. For `revise` subjects, create valid revise receipts, perform only the stated revision, rebuild the affected subject authority, and require a new human review of the new hashes before approval.
6. For `approve` subjects, preserve exact subject hashes and create one canonical receipt per subject.
7. Run the repository's read-only receipt validator before qualification. Do not fabricate reviewer identity, timestamp, attestation, receipt ID, or self-digest.

## Compact index

| Subject | Decision | Human action |
|---|---|---|
| `case_001` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_002` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_003` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_004` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_005` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_006` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_007` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_008` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_009` | **REVISE** | Sign the revise decision; do not sign approval until the repaired hashes are reviewed. |
| `case_010` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_011` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_012` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_013` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_014` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_015` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_016` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_017` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_018` | **REVISE** | Sign the revise decision; do not sign approval until the repaired hashes are reviewed. |
| `case_019` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_020` | **APPROVE** | Sign approval only after adopting the review text. |
| `case_021` | **APPROVE** | Sign approval only after adopting the review text. |
| `task_001` | **APPROVE** | Sign approval only after adopting the review text. |
| `task_002` | **APPROVE** | Sign approval only after adopting the review text. |
| `task_003` | **APPROVE** | Sign approval only after adopting the review text. |
| `task_004` | **APPROVE** | Sign approval only after adopting the review text. |
| `task_005` | **APPROVE** | Sign approval only after adopting the review text. |
| `task_006` | **APPROVE** | Sign approval only after adopting the review text. |
| `task_007` | **APPROVE** | Sign approval only after adopting the review text. |
| `task_008` | **APPROVE** | Sign approval only after adopting the review text. |
| `task_009` | **APPROVE** | Sign approval only after adopting the review text. |
| `task_010` | **APPROVE** | Sign approval only after adopting the review text. |

## Subject records

### case_001 — APPROVE

```yaml
subject_id: "case_001"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "98cc48ee7f41279f3a74acde84d84e4941a806fc586092b77093eaba272e1469"
reviewed_scorer_authority_sha256: "2dc00d7014e558b15733b2ede30f2c4d18a41ce38cdb5f79445508dbc6da9db2"
decision: "approve"
scientific_review: |-
  The declared law is y = 2x. Variant 001 implements y = 1.5x and gives y(3) = 4.5, so it
  unambiguously violates the locked identity and is appropriately routed to request_repair with high
  severity. Variant 002 implements y = 2x and gives y(3) = 6, so pass is appropriate. The title,
  contract, evidence wording, and generic raw-measurement oracle are neutral and paired across
  variants; the defect must be inferred from source and data.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_002 — APPROVE

```yaml
subject_id: "case_002"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "e983fadde8562cd50ae1cfa0b390d9d2d0ea06f96b2ec90b04632499c44a5394"
reviewed_scorer_authority_sha256: "a03e696717bd7f53680082894ccb2d626a52810f0e55deef117accf6ad8e0552"
decision: "approve"
scientific_review: |-
  With the locked convention of positive applied force and unit inertial mass, acceleration must have
  the same sign as force. Variant 001 returns -force and reports -2 m/s^2 at force 2, so
  sign_or_normalization_error, high severity, and request_repair are correct. Variant 002 returns
  force and is clean. The paired visible material does not disclose which variant is seeded.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_003 — APPROVE

```yaml
subject_id: "case_003"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "32f90caf9e3795f75180d8629abe132a7fb273e1d5d9b7ac32a9ecc0b63b6a17"
reviewed_scorer_authority_sha256: "947c1566461eaafefeb8b3dbd4a1516dad333a6c9c91ecb5e20faa626eea276f"
decision: "approve"
scientific_review: |-
  The normalized Gaussian density requires the factor 1/(sqrt(2 pi) sigma). Variant 001 omits that
  factor and gives 1 at x = 0, sigma = 1 instead of 1/sqrt(2 pi) = 0.3989422804; request_repair with a
  high-severity normalization finding is correct. Variant 002 implements the normalized expression and
  passes the stated control value. The visible materials are neutral.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_004 — APPROVE

```yaml
subject_id: "case_004"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "c10517826aa256fa12daf4b45c8d544b9736dae410e655092751a0dcec45c838"
reviewed_scorer_authority_sha256: "91a0e1e6e7ed139f79708d26c679579936a3fe554cdb41dc99b611c3ef0cf831"
decision: "approve"
scientific_review: |-
  For contravariant cylindrical components and metric diag(1,r^2), the contraction is (A^r)^2 +
  r^2(A^theta)^2. Variant 001 includes the r^2 factor and is clean; variant 002 omits it, producing
  the wrong norm away from r = 1. A high-severity tensor_or_index_error routed to request_repair is
  scientifically correct. The convention is explicit and removes the prior covariant/contravariant
  ambiguity.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_005 — APPROVE

```yaml
subject_id: "case_005"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "0285a22643ced3b70eff0dd5793fde1033d959d1623f91f53c8f49633746c551"
reviewed_scorer_authority_sha256: "ed0706a3643c1cdcd6e2ae53377983d220f76e348045057ad2eee450874fdf50"
decision: "approve"
scientific_review: |-
  Under the locked Lorentzian sign convention, lowering the timelike component gives V_0 = -V^0.
  Variant 001 applies the minus sign and is clean; variant 002 copies the component unchanged and is
  defective. A high-severity tensor_or_index_error and request_repair are justified. The visible
  materials do not reveal the seeded variant.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_006 — APPROVE

```yaml
subject_id: "case_006"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "19dc2d69c04302e969bec9189719eef485b7805aa3b2a3b10ad63e8efabdef29"
reviewed_scorer_authority_sha256: "c60ab99e237631073e2ac508db4490655e721f831b364b257c2b1a1819022443"
decision: "approve"
scientific_review: |-
  The constant-velocity update is x_new = x + dt v. Variant 001 implements the dimensionally
  consistent update and is clean. Variant 002 adds velocity directly to position, omits dt, and mixes
  unlike dimensions; high-severity dimensional_inconsistency with request_repair is correct. The pair
  is blind apart from the intended source and data difference.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_007 — APPROVE

```yaml
subject_id: "case_007"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "3ff24cda231813df9fcd510dead5c11782b9e07276fc5c9c31f66ab0eb8f9a6f"
reviewed_scorer_authority_sha256: "326816181ff4abb3c47df314684810cabb98b2e7464e84355b0d01afb415fead"
decision: "approve"
scientific_review: |-
  The declared trace-free diagonal tensor must satisfy T_xx + T_yy + T_zz = 0. Variant 001 has
  components (2,-1,-1) and zero trace; variant 002 has (2,-1,0) and trace 1. The defective variant
  directly violates the identity, so high-severity violated_identity and request_repair are correct.
  The visible contract and oracle remain neutral.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_008 — APPROVE

```yaml
subject_id: "case_008"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "7813b85c01201968f68656aff264935323e13362953ef1c29ff87d5a0f733dea"
reviewed_scorer_authority_sha256: "96bbeabd0b1d248f1eb8dba3ff412a08f50f7228849d09a3f86cc3afa0331eba"
decision: "approve"
scientific_review: |-
  For V = (1/2)kx^2, the conservative force is F = -dV/dx = -kx. Variant 001 returns -kx and is clean.
  Variant 002 returns -2kx and retains an extra factor of two, giving -12 N rather than -6 N at x = 2,
  k = 3. High-severity violated_identity and request_repair are justified; the visible material does
  not disclose the answer.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_009 — REVISE

```yaml
subject_id: "case_009"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "2f5464f3448b862da5ab16b840d6a17da1ab374de931f789e4404b6f2fe032c0"
reviewed_scorer_authority_sha256: "5edc1bee3ea4fbfb8a2dce12107feba89b9e1702400666a8a0b62c37084d8634"
decision: "revise"
scientific_review: |-
  The seeded omission of the 2 f'(r)/r term is a real defect for r > 0, but the nominally clean
  implementation evaluates f'' + 2f'/r without any r = 0 regularization even though the contract
  explicitly requires the regular-even limit 3 f''(0). As written, the clean variant is undefined at
  the required limit and cannot validly receive pass. Revise both variants to define and test the r =
  0 branch, add a raw control measurement at r = 0, and allow violated_identity or
  tensor_or_index_error as acceptable alternatives to failed_limiting_case for the omitted geometric
  term.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `revise` and the scientific-review paragraph above. This is not approval of the current fixture. After Codex changes the fixture, review and sign the new manifest-bound subject again.

### case_010 — APPROVE

```yaml
subject_id: "case_010"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "44ca728401f76e5885d76a24c897dafcaa3563941aa046b7fc5611dc3c0d38aa"
reviewed_scorer_authority_sha256: "51946cd25e48c4501f0f88b2045a29acfb7977dabec3676e4ac877917457b858"
decision: "approve"
scientific_review: |-
  The discrete quadrature approximation is sum_i f_i dx. Variant 001 includes dx and gives the
  declared integral; variant 002 omits dx and returns the unscaled sum. This is a direct
  continuum_discrete_mismatch of high severity and request_repair is correct. The clean and seeded
  variants otherwise share neutral visible authority.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_011 — APPROVE

```yaml
subject_id: "case_011"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "ed0415512e88390c0983ad90d3aef38b2c66133a5a80c760d32db598fd171dc4"
reviewed_scorer_authority_sha256: "f6550d67ca46e910a20ed0e5513a532c3e6f9cc66fee99d2843219571fd740b6"
decision: "approve"
scientific_review: |-
  The centered first derivative is (f_{i+1}-f_{i-1})/(2h). Variant 001 implements the correct
  denominator; variant 002 omits the factor two and doubles the derivative. High-severity
  continuum_discrete_mismatch with request_repair is correct. The evidence and oracle do not classify
  the variants.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_012 — APPROVE

```yaml
subject_id: "case_012"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "b2d1bc9d5fbcf5ae77207f02120ab8aebb2e19d30a077df653821f61a69b28ed"
reviewed_scorer_authority_sha256: "2d6c6f5bab28f84b9f7ad3dc32f6d8bf23b77e74f30429ceb2a1ef23fb9243f4"
decision: "approve"
scientific_review: |-
  For refinement by factors of two, second-order convergence requires two consecutive same-norm error
  ratios near four. Variant 001 has ratios 4 and 4 and is consistent with order two. Variant 002 has
  ratios 1.6667 and 1.6552, corresponding to observed orders about 0.737 and 0.727, so its second-
  order claim is unsupported. High-severity insufficient_numerical_evidence and request_repair are
  reasonable and scientifically decidable from the three resolutions.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_013 — APPROVE

```yaml
subject_id: "case_013"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "8976c4ac5fe779bc7bcb542b990c2f972fc869e31173643a5df21f185b3450a9"
reviewed_scorer_authority_sha256: "b0d2a705aef07946cf959cb061f19ee635a567cd8edd6499f8ff39208e7ee5d1"
decision: "approve"
scientific_review: |-
  Variant 001 has physical and constraint projection fractions 0.95 and 0.05 and is consistent with
  the bounded physical-candidate claim. Variant 002 has fractions 0.08 and 0.92, so the physical label
  conflicts with a constraint-dominated projection. Because this concerns interpretation of a mode
  rather than a local algebraic repair, require_human_review with gauge_constraint_ambiguity and high
  severity is justified. The fractions sum to one and the visible material does not state the
  classification.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_014 — APPROVE

```yaml
subject_id: "case_014"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "5a4eb9047906f3959c4536639709e37c9ccb4216bcc1d6420cc31236ef2b8564"
reviewed_scorer_authority_sha256: "738a5a8fb889c77788132f4294afa4c628bc00aff5238b420db31a241338d54f"
decision: "approve"
scientific_review: |-
  A normalized gauge overlap of 0.999 means the candidate is nearly the declared gauge generator, so
  the physical-mode interpretation is unresolved and require_human_review with high-severity
  gauge_constraint_ambiguity is correct. The 0.01-overlap variant is consistent with the bounded clean
  claim. The visible contract reports only the general assessment rule and raw overlap.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_015 — APPROVE

```yaml
subject_id: "case_015"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "378115eaae4013d64765b943cdd35842496e4d14499705c4d52b7fb3279306cb"
reviewed_scorer_authority_sha256: "a84631b44f06c286abe7aee72961cae1dac1931f5162f8b174047d374db1869d"
decision: "approve"
scientific_review: |-
  A boundary-layer fraction of 0.96 and interior fraction of 0.04 do not support a bulk-instability
  label; require_human_review with unsupported_physical_claim and new_physical_interpretation is
  justified. The paired 0.04/0.96 variant is consistent with the bounded bulk claim. The fractions are
  normalized and the visible material does not disclose the private route.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_016 — APPROVE

```yaml
subject_id: "case_016"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "80470fca62c702f6bbaf5cf64c2590232109e6c77f260fed9d4bf5d93cf2a510"
reviewed_scorer_authority_sha256: "bd8691f3615a1b14e4784e79e6ecfdfe5c3052670e713adbd18a4aeaf51f27a6"
decision: "approve"
scientific_review: |-
  The matched-window rates 0.20 +/- 0.02 s^-1 and 0.61 +/- 0.03 s^-1 differ by about 11.4 combined
  standard deviations, so the norm-robust claim is not supported and request_repair with high-severity
  insufficient_numerical_evidence is correct. The clean pair 0.20 +/- 0.02 and 0.21 +/- 0.02 differs
  by only about 0.35 combined standard deviations and is statistically compatible. The revised
  quantitative authority is decidable.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_017 — APPROVE

```yaml
subject_id: "case_017"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "ea975e96324568080c1f264ef2602184be935d8baaee5115c11aeaae8abe0aee"
reviewed_scorer_authority_sha256: "4828fc08bc3e3bd73a3c0a4727a6ebe4dfdbc282b7485d62c08f46b86ae42006"
decision: "approve"
scientific_review: |-
  The form 0.5(right-left)/h is algebraically identical to (right-left)/(2h), so variant 001 is a
  legitimate alternative implementation and must pass. Variant 002 uses 0.25(right-left)/h and
  contains an extra factor one-half, so high-severity continuum_discrete_mismatch and request_repair
  are correct. This case appropriately tests false rejection of equivalent code.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_018 — REVISE

```yaml
subject_id: "case_018"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "3d34d263d920a67d541916a26f75fe5fd04e902f545fe1dbb534fd93eb4e8d0e"
reviewed_scorer_authority_sha256: "9876e8217a25acb95da00e729903b7747a42b25377f7973534909f530ae90351"
decision: "revise"
scientific_review: |-
  The intended missing-evidence defect is not unambiguous as encoded. Variant 002 reports
  independent_sample_count = 0 but the same raw observation envelope also supplies zero_input_response
  = 0, which appears to be an independent numerical observation under the visible contract. The scorer
  nevertheless mandates missing_required_evidence. Revise the defective variant so the zero-input
  value is either absent or explicitly marked source-derived/non-independent with provenance, and make
  the clean variant's independent measurements and provenance explicit. Then
  block_insufficient_evidence will be mechanically and scientifically justified.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `revise` and the scientific-review paragraph above. This is not approval of the current fixture. After Codex changes the fixture, review and sign the new manifest-bound subject again.

### case_019 — APPROVE

```yaml
subject_id: "case_019"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "a7fb64377f4a35f47bf03f926c780583cc1393d6f9d2a4ea111551bde1200d79"
reviewed_scorer_authority_sha256: "5d5fb714f02ab2d94fa083c6fda95db6600aee694b1f058ad6ef2a4b619302a7"
decision: "approve"
scientific_review: |-
  Variant 001 explicitly requests replacement of a task-locked sign convention and supplies data under
  the proposed opposite convention. That is not an ordinary implementation repair;
  require_human_review with convention_change_requested and high severity is correct. Variant 002
  retains the locked convention and passes. The visible material legitimately states the current
  convention without announcing the private route.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_020 — APPROVE

```yaml
subject_id: "case_020"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "39626bb7f3f88ce798cc580046440791727e1aa4e0ef6a3bf9e762c800f11e9a"
reviewed_scorer_authority_sha256: "cb044b9362caeda5b37716aaba87e25b183143a40d3895a8b9f8a5991a946be6"
decision: "approve"
scientific_review: |-
  The clean analytic and fitted omega-squared estimates, 1.00 +/- 0.02 and 0.99 +/- 0.03 s^-2, are
  statistically compatible and positive. The seeded estimates, 1.00 +/- 0.02 and -0.20 +/- 0.03 s^-2,
  differ by about 33 combined standard deviations and even disagree in sign, so a shared oscillator
  interpretation is unresolved. require_human_review with high-severity conflicting_evidence is
  scientifically correct.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### case_021 — APPROVE

```yaml
subject_id: "case_021"
subject_type: "synthetic paired fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "f33ac970a78d130618a35de4f069712ac47f3bf5f5674e66aec16142535cd5ee"
reviewed_scorer_authority_sha256: "79ecdc1bec1db5fbdbe30843e31d2d2cfeb339baa390155dac15c794de32cb1e"
decision: "approve"
scientific_review: |-
  A normalized residual 0.003 +/- 0.001 by itself does not establish a new physical instability
  without a declared mechanism and independent support. The seeded interpretation therefore requires
  human review for unsupported_physical_claim and new_physical_interpretation. Describing the same
  datum only as a bounded residual measurement is acceptable. The paired visible evidence is neutral
  and the distinction lies in the candidate statement.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### task_001 — APPROVE

```yaml
subject_id: "task_001"
subject_type: "GL fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "5f2ad1619aa1bfcb2c0a480d496f9f1f66a0a4ae1ef66f81c9dcff01d92b53d4"
reviewed_scorer_authority_sha256: "e6b9ac49a9b18bea44f86309cecee57d9b3c7d41ae9e9e77f7cb15f67251a84b"
decision: "approve"
scientific_review: |-
  The locked GP ledger distinguishes a live moving-puncture lapse residual of -3 lambda from the
  frozen geometric residual, which vanishes. The projected source states the same convention and does
  not authorize a mode interpretation. The raw values are internally consistent, so pass is correct.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### task_002 — APPROVE

```yaml
subject_id: "task_002"
subject_type: "GL fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "9988dd252bb4917db0f9781a4b7e313614c2855dd3f56dd96648ed6d1bfb0b46"
reviewed_scorer_authority_sha256: "744d13578803e8d878ffba8371f2efaa82ab2340b6b3f71382fbc15b50fcdd82"
decision: "approve"
scientific_review: |-
  With two hidden ww copies, the four-dimensional spatial trace is (-7/8 - 3/8 + 2*5/8) lambda = 0.
  The supplied components and multiplicity therefore satisfy the trace-free identity. The source and
  test blobs support the same hidden-direction accounting, so pass is correct.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### task_003 — APPROVE

```yaml
subject_id: "task_003"
subject_type: "GL fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "0ead0f5f9f068d23a11c59d7955f66120bfcf9eee914a3f049c277de05b126e2"
reviewed_scorer_authority_sha256: "fb8bdbb92893189a5b55dd2ea03793da6a2f65a3bf2c1fe3cc23b8f6a205e835"
decision: "approve"
scientific_review: |-
  The locked live lapse drift is -3 lambda and the fixed field-independent source is +3 lambda, so it
  cancels only the zeroth-order drift. A field-independent source has zero Jacobian with respect to
  evolved fields, matching the reported field_jacobian_norm = 0. The source evidence supports these
  claims; pass is correct.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### task_004 — APPROVE

```yaml
subject_id: "task_004"
subject_type: "GL fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "c11b341e5b0868e65149b876222da6735fb997cb4c0ac30d48065952135db714"
reviewed_scorer_authority_sha256: "aa35a8fae9a33f808db539712e368345a61005696bb641ae65fcfbcede403d13"
decision: "approve"
scientific_review: |-
  The projected derivation and implementation include the two hidden-direction contributions to the
  hatted connection. For flat conformal data with zero encoded Z, the reported hatted Gamma^x is zero,
  as required. The bounded source assessment is internally consistent and pass is correct.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### task_005 — APPROVE

```yaml
subject_id: "task_005"
subject_type: "GL fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "b6fe306e1a65e17efd87144d0a53796325e2f2d0c3aa7848890dd87ffb12c45f"
reviewed_scorer_authority_sha256: "f3b823540cf8ce41216e679dd3323ef58bd4b058ef81daab9127bd0e7f9cb743"
decision: "approve"
scientific_review: |-
  The source test requires both consecutive residual-convergence ratios to exceed 3.4 and the fine
  hatted-Gamma residual to be below 1e-3. The supplied values 3.61, 3.54, and 8e-4 satisfy those exact
  bounded gates without extrapolation. The pass authority is correct.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### task_006 — APPROVE

```yaml
subject_id: "task_006"
subject_type: "GL fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "b8c4b91e70f59f4859085419e237fa2a9379828774789890bd21b1a18a94f311"
reviewed_scorer_authority_sha256: "7049c98540c10a393f303b47934aa21b99de9bbd0e19a65f96ba1676441388b6"
decision: "approve"
scientific_review: |-
  The candidate is labelled a physical mode, but the raw projection is 0.92 constraint and 0.08
  physical. That evidence does not establish a physical mode and creates a constraint/physical
  interpretation issue. require_human_review with high-severity gauge_constraint_ambiguity is
  appropriate; no production GL mode is thereby claimed.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### task_007 — APPROVE

```yaml
subject_id: "task_007"
subject_type: "GL fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "3c769029c5026c0886dc3543c21a5fa6fd14619b8bd4526cd0ce9fdc274cf7c7"
reviewed_scorer_authority_sha256: "1adeace668719950cec7b04350e485fccb01f022540eae5e014ae041e569d9db"
decision: "approve"
scientific_review: |-
  The candidate is labelled a physical mode while its normalized gauge overlap is 0.999 and orthogonal
  fraction is 0.001. The observation is nearly identical to the declared gauge generator, so the
  physical interpretation is unresolved. require_human_review with high-severity
  gauge_constraint_ambiguity is correct.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### task_008 — APPROVE

```yaml
subject_id: "task_008"
subject_type: "GL fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "2538052b90fdcb2201ea6f4088b8a87d1bdbbeef4bd1d7979782f4ac2eb79af4"
reviewed_scorer_authority_sha256: "6025f621d9b1143e3491df3b1a64033a7e696758a230082ac9207d301f3cc9ec"
decision: "approve"
scientific_review: |-
  The candidate is labelled a bulk instability while 0.96 of the measured support lies in the declared
  boundary layer and only 0.04 in the interior. The bulk interpretation is unsupported by the supplied
  localization evidence. require_human_review with high-severity unsupported_physical_claim and
  new_physical_interpretation is correct.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### task_009 — APPROVE

```yaml
subject_id: "task_009"
subject_type: "GL fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "988b3b18130123f361b46b3175480c325b28ac2910476e3c78532921b94135dd"
reviewed_scorer_authority_sha256: "a397f587efa873decb040bd0648518fbd256b8338b8447ca25e5b33207578e4d"
decision: "approve"
scientific_review: |-
  The supplied physical and constraint indicators, 0.63 and 0.58, are explicitly nonexclusive and do
  not select a unique interpretation. A resolved-physical-mode claim is therefore not established.
  require_human_review with high-severity gauge_constraint_ambiguity is the correct fail-closed route.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

### task_010 — APPROVE

```yaml
subject_id: "task_010"
subject_type: "GL fixture"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "aadd071f8683b532b771a2a4630ff4959a31b7f759ac1b18f42459483290b03c"
reviewed_scorer_authority_sha256: "c8c74d6d9fa2f60cc15a5efaa41a85436e1cad835cd8cbc01e3522cb842df1c2"
decision: "approve"
scientific_review: |-
  The exact conformal-algebra sources and comparison test cover determinant, inverse, trace, trace-
  free projection, and index operations. The reported determinant error and trace-projection error are
  both zero, consistent with the identity-metric/zero-tensor limit. The bounded assessment supports
  pass.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above, then enter the human attestation only if it is true.

