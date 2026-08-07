# PA-5C1 V2 Human Review Decisions — SIGNED

> **Status:** Human reviewer `inaeyk` has adopted the renewed decisions and scientific-review text for `case_009` and `case_018`. This signed decision source is not itself the repository receipt bundle; the repository must create and validate the canonical receipts.

## Frozen renewed authority

- New canonical review-packet digest: `4aafec7dd51ab66e8190699a72a9514e46c3228379d0d835e2b1c1220cd34cfb`
- New catalog canonical digest: `f4b52fe2f70baf87ca1ec19dff294490088e5571f45918b112ff00b936abb088`
- V2 review-card ZIP SHA-256: `44c533af720d512ba88cd58ba2f205c96b6f0d9836b69880fc515ea1c811b79f`
- Fixture-author IDs: `research_automation_fixture_team_v1`

## Human signing fields

Complete these only after personally reading and adopting both subject decisions and review texts:

```yaml
reviewer_id: "inaeyk"
reviewer_kind: "human"
issued_at: "2026-08-06T19:36:00+08:00"
global_human_attestation: "I am a human reviewer distinct from the listed fixture authors. I reviewed the exact renewed manifest-bound subjects, independently assessed their scientific authority and auditor-visible blindness, and adopt the decisions and scientific-review text recorded below."
```

Recommended attestation text, to be entered only if true:

> I am a human reviewer distinct from the listed fixture authors. I reviewed the exact renewed manifest-bound subjects, independently assessed their scientific authority and auditor-visible blindness, and adopt the decisions and scientific-review text recorded below.

The repository must generate `receipt_id` and `receipt_sha256`. Do not invent them manually.

## Decision summary

| Renewed receipt | Subject | Decision |
|---|---|---|
| `case_009_v2.json` | `case_009` | **APPROVE** |
| `case_018_v2.json` | `case_018` | **APPROVE** |

## Subject records

### case_009 v2 — APPROVE

```yaml
subject_id: "case_009"
review_generation: "v2"
receipt_target: "examples/physics_auditor/benchmark_v1/scorer_only/review_receipts/case_009_v2.json"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "963050f38aef88d5ad21da951e396bf3eb29e6969b515a99533eb0ef74555b3a"
reviewed_scorer_authority_sha256: "01337f582505303a9c079782813810af8f79234e9e512f8b447bee271855c311"
decision: "approve"
scientific_review: |-
  The repaired pair now tests both the positive-radius spherical Laplacian identity and the regular
  origin branch. For f(r)=r^2, f'(2)=4 and f''(2)=2, so the full expression gives
  2 + 2(4)/2 = 6, while the seeded positive-radius implementation gives only 2. Both variants
  explicitly return 3 f''(0)=6 at r=0 and provide matching origin observations, so the former clean
  variant is now well defined at the required limit. Pass is correct for variant_001, and
  request_repair with high severity is correct for variant_002. The accepted alternatives
  tensor_or_index_error and violated_identity ensure that scientifically accurate descriptions of the
  omitted geometric term are scoreable. The paired title, contract, filenames, oracle, observation
  schema, and origin treatment remain neutral, so the seeded variant is not disclosed.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above for the exact two renewed hashes. This approval supersedes neither the content nor the hash binding of the old `revise` receipt; it authorizes the new `case_009_v2.json` receipt only.

### case_018 v2 — APPROVE

```yaml
subject_id: "case_018"
review_generation: "v2"
receipt_target: "examples/physics_auditor/benchmark_v1/scorer_only/review_receipts/case_018_v2.json"
fixture_author_ids:
  - "research_automation_fixture_team_v1"
reviewed_visible_manifest_sha256: "41220f985ce104490f83f85a527cb71b02097a7d12f56a3c096dfdb57568af1d"
reviewed_scorer_authority_sha256: "c24cc7368212dd2f7e91172739fb47b149842b4b3193154aab7ff91ea7b4a1e0"
decision: "approve"
scientific_review: |-
  The repaired pair now distinguishes independent observations from source-derived values
  mechanically and without revealing which variant is seeded. Variant_001 declares two independent
  samples, supplies an independent zero-input response of 0, and has no source-derived substitute, so
  the evidence requirement is satisfied and pass is correct. Variant_002 declares zero independent
  samples, has no independent zero-input observation, and labels the available zero-input value as
  source-derived, so it lacks the required independent evidence. block_insufficient_evidence with
  high-severity missing_required_evidence is therefore scientifically and mechanically justified.
  The shared schema, null fields, provenance names, and neutral contract make the distinction
  decidable without embedding the private expected route.
reviewer_id: ""
reviewer_kind: ""
issued_at: ""
human_attestation: ""
receipt_id: ""
receipt_sha256: ""
```

**What the human signs:** Adopt `approve` and the scientific-review paragraph above for the exact two renewed hashes. This approval supersedes neither the content nor the hash binding of the old `revise` receipt; it authorizes the new `case_018_v2.json` receipt only.

## Codex import rules

1. Verify this file's SHA-256 and the renewed packet, catalog, subject-manifest, scorer-authority, and fixture-author identities.
2. Refuse import while any global human signing field is blank.
3. Preserve both `approve` decisions and both `scientific_review` blocks exactly.
4. Propagate the completed human identity, reviewer kind, issued time, and attestation into the two exact repository receipts.
5. Generate canonical receipt IDs and self-digests using the repository implementation.
6. Preserve the old `case_009.json` and `case_018.json` revise receipts; they remain bound only to the prior hashes.
7. Create only `case_009_v2.json` and `case_018_v2.json` for these renewed hashes.
8. Run the read-only receipt and renewed-packet validators.
9. Do not modify the repaired fixtures or authorities while importing these approvals.
10. After both receipts validate, PA-5C1 may proceed to its already-authorized qualification tests and independent audits, but no real 41-session benchmark or real GL pilot may run during PA-5C1.
