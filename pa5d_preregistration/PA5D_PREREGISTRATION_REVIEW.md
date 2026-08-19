# PA-5D0 calibration preregistration review

Status: **HUMAN_AUTHORITY_REQUIRED**. This is a prospective draft, not an approved preregistration receipt. PA-5D1 must not start.

## Decisions required

A human must select one exact 41-run schedule, approve the exact already-qualified PA-3 configuration, approve the one-shot execution and metric/GL-scoring policies, and approve or replace every proposed numeric performance threshold. The catalog-zero critical metric may only be approved as NO_GATE; revising it requires a new review. No proposed scientific choice is active before those exact decisions are rebound.

| Decision | Subject | Allowed values |
| --- | --- | --- |
| select_benchmark_schedule_v1 | Exact 41-session schedule | schedule_maximum_variant_coverage_v1; schedule_balanced_single_repeat_v1 |
| approve_pa3_qualified_model_configuration_v1 | Exact PA-3 model and execution configuration | approve_exact_qualified_pa3_configuration |
| approve_one_shot_calibration_execution_policy_v1 | One-shot benchmark and GL execution lifecycle | approve_one_shot_calibration_execution_policy_v1 |
| approve_metric_and_gl_scoring_policy_v1 | Derived metric, per-category, pair/repeat, token, and GL scoring policy | approve_metric_and_gl_scoring_policy_v1 |
| approve_threshold_critical_defect_recognition_rate | Performance threshold critical_defect_recognition_rate: NO_GATE None | approve_proposed |
| approve_threshold_defect_category_recognition_rate | Performance threshold defect_category_recognition_rate: >= 0.9 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_false_pass_rate | Performance threshold false_pass_rate: == 0.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_clean_pass_rate | Performance threshold clean_pass_rate: >= 0.9 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_false_critical_finding_rate | Performance threshold false_critical_finding_rate: == 0.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_repair_routing_rate | Performance threshold repair_routing_rate: >= 0.9 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_human_escalation_rate | Performance threshold human_escalation_rate: == 1.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_gl_expected_pass_route_rate | Performance threshold gl_expected_pass_route_rate: == 1.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_insufficient_evidence_routing_rate | Performance threshold insufficient_evidence_routing_rate: == 1.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_malformed_report_rate | Performance threshold malformed_report_rate: == 0.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_infrastructure_failure_rate | Performance threshold infrastructure_failure_rate: <= 0.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_severity_correctness_rate | Performance threshold severity_correctness_rate: >= 0.9 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_required_category_recognition_rate | Performance threshold required_category_recognition_rate: >= 0.9 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_acceptable_alternative_recognition_rate | Performance threshold acceptable_alternative_recognition_rate: >= 0.9 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_forbidden_category_violation_rate | Performance threshold forbidden_category_violation_rate: == 0.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_forbidden_route_violation_rate | Performance threshold forbidden_route_violation_rate: == 0.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_evidence_validity_rate | Performance threshold evidence_validity_rate: == 1.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_route_consistency_rate | Performance threshold route_consistency_rate: >= 0.95 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_finding_category_consistency_rate | Performance threshold finding_category_consistency_rate: >= 0.95 | approve_proposed; replace_with_explicit_operator_value_and_rationale |
| approve_threshold_repeat_consistency_rate | Performance threshold repeat_consistency_rate: == 1.0 | approve_proposed; replace_with_explicit_operator_value_and_rationale |

## Benchmark contents

Catalog: `physics_benchmark_blind_authority_v1`; 21 paired subjects, 42 qualified variants (21 clean and 21 defective). The table is scorer-side review authority and must remain inaccessible to the Auditor.

| Case | Variant | Label | Expected route | Required categories | Acceptable alternatives |
| --- | --- | --- | --- | --- | --- |
| case_001 | variant_001 | defective | request_repair | violated_identity | none |
| case_001 | variant_002 | clean | pass | none | none |
| case_002 | variant_001 | defective | request_repair | sign_or_normalization_error | none |
| case_002 | variant_002 | clean | pass | none | none |
| case_003 | variant_001 | defective | request_repair | sign_or_normalization_error | none |
| case_003 | variant_002 | clean | pass | none | none |
| case_004 | variant_001 | clean | pass | none | none |
| case_004 | variant_002 | defective | request_repair | tensor_or_index_error | none |
| case_005 | variant_001 | clean | pass | none | none |
| case_005 | variant_002 | defective | request_repair | tensor_or_index_error | none |
| case_006 | variant_001 | clean | pass | none | none |
| case_006 | variant_002 | defective | request_repair | dimensional_inconsistency | none |
| case_007 | variant_001 | clean | pass | none | none |
| case_007 | variant_002 | defective | request_repair | violated_identity | none |
| case_008 | variant_001 | clean | pass | none | none |
| case_008 | variant_002 | defective | request_repair | violated_identity | none |
| case_009 | variant_001 | clean | pass | none | none |
| case_009 | variant_002 | defective | request_repair | failed_limiting_case | tensor_or_index_error, violated_identity |
| case_010 | variant_001 | clean | pass | none | none |
| case_010 | variant_002 | defective | request_repair | continuum_discrete_mismatch | none |
| case_011 | variant_001 | clean | pass | none | none |
| case_011 | variant_002 | defective | request_repair | continuum_discrete_mismatch | none |
| case_012 | variant_001 | clean | pass | none | none |
| case_012 | variant_002 | defective | request_repair | insufficient_numerical_evidence | none |
| case_013 | variant_001 | clean | pass | none | none |
| case_013 | variant_002 | defective | require_human_review | gauge_constraint_ambiguity | none |
| case_014 | variant_001 | defective | require_human_review | gauge_constraint_ambiguity | none |
| case_014 | variant_002 | clean | pass | none | none |
| case_015 | variant_001 | defective | require_human_review | new_physical_interpretation, unsupported_physical_claim | none |
| case_015 | variant_002 | clean | pass | none | none |
| case_016 | variant_001 | defective | request_repair | insufficient_numerical_evidence | none |
| case_016 | variant_002 | clean | pass | none | none |
| case_017 | variant_001 | clean | pass | none | none |
| case_017 | variant_002 | defective | request_repair | continuum_discrete_mismatch | none |
| case_018 | variant_001 | clean | pass | none | none |
| case_018 | variant_002 | defective | block_insufficient_evidence | missing_required_evidence | none |
| case_019 | variant_001 | defective | require_human_review | convention_change_requested | none |
| case_019 | variant_002 | clean | pass | none | none |
| case_020 | variant_001 | clean | pass | none | none |
| case_020 | variant_002 | defective | require_human_review | conflicting_evidence | none |
| case_021 | variant_001 | defective | require_human_review | new_physical_interpretation, unsupported_physical_claim | none |
| case_021 | variant_002 | clean | pass | none | none |

## Exact 41-run schedule alternatives

### schedule_maximum_variant_coverage_v1

Status: **HUMAN_AUTHORITY_REQUIRED — candidate not selected**.

Neutral rule: Enumerate all 42 catalog variants; omit the unique variant with the greatest SHA-256 rank of catalog_hash|schedule_id|case/variant; run every remainder once.

Tradeoff: Maximizes distinct catalog-variant coverage (41 of 42); no exact variant is repeated, so repeat consistency is not applicable.

Omitted variants: `case_005/variant_002`. Distinct variants: 41; exact repeats: 0. Ordering is `(case_id, variant_id, repetition_id)`.

| Effective denominator | Exact count |
| --- | --- |
| clean runs | 21 |
| defective runs | 20 |
| expected pass | 21 |
| expected repair | 13 |
| expected human escalation | 6 |
| expected insufficient evidence | 1 |
| complete variant pairs | 20 |
| exact repeat pairs | 0 |
| route-consistency misses allowed by proposed >=0.95 | 1 |

| # | Case | Variant | Rep | Execution ID | Fresh PA-3 action root |
| --- | --- | --- | --- | --- | --- |
| 1 | case_001 | variant_001 | 1 | benchmark-ca04694b700c0d665590c768b5bebdac | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_001-pa5d-0c6dc27744d4006072c65f0b9848e08e/physics-auditor/benchmark-ca04694b700c0d665590c768b5bebdac` |
| 2 | case_001 | variant_002 | 1 | benchmark-d637c23913313dbc5d6d61a073d87918 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_001-pa5d-bababbc5c8adeb78597e6d859cab4a28/physics-auditor/benchmark-d637c23913313dbc5d6d61a073d87918` |
| 3 | case_002 | variant_001 | 1 | benchmark-c2e9e4a4b6f9754128ed6415013588b6 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_002-pa5d-8216356e20ce810b51baa2f5039bb8f3/physics-auditor/benchmark-c2e9e4a4b6f9754128ed6415013588b6` |
| 4 | case_002 | variant_002 | 1 | benchmark-8c5bcf4c689533332c301caf6b948a3a | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_002-pa5d-e99e64dbd1fb0b83a0233d33d6226c3a/physics-auditor/benchmark-8c5bcf4c689533332c301caf6b948a3a` |
| 5 | case_003 | variant_001 | 1 | benchmark-6c431703764969ec6cb6421abe94c1b2 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_003-pa5d-248689de4b49c3a12464a3172846fc9e/physics-auditor/benchmark-6c431703764969ec6cb6421abe94c1b2` |
| 6 | case_003 | variant_002 | 1 | benchmark-5f76629a26a5ec2ae49a9599d2904470 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_003-pa5d-8ff6c3de25fcc4ca65a403431fcf65e9/physics-auditor/benchmark-5f76629a26a5ec2ae49a9599d2904470` |
| 7 | case_004 | variant_001 | 1 | benchmark-bde5beec30757d2720f158671fd1e841 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_004-pa5d-71146d48a2ee388917f73607971ba423/physics-auditor/benchmark-bde5beec30757d2720f158671fd1e841` |
| 8 | case_004 | variant_002 | 1 | benchmark-77bbb5a84a7d6a56c9abeeb781885dbc | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_004-pa5d-1d421085e2b1e948bcb31acb824cf041/physics-auditor/benchmark-77bbb5a84a7d6a56c9abeeb781885dbc` |
| 9 | case_005 | variant_001 | 1 | benchmark-05bf430fb35342c2325ded783097126e | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_005-pa5d-ac389655db670d37f6270f488f47c434/physics-auditor/benchmark-05bf430fb35342c2325ded783097126e` |
| 10 | case_006 | variant_001 | 1 | benchmark-d5f2c7a1c4249c946d55c9fc1b14519a | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_006-pa5d-ca2969d95124afe925ee6749a412dc5b/physics-auditor/benchmark-d5f2c7a1c4249c946d55c9fc1b14519a` |
| 11 | case_006 | variant_002 | 1 | benchmark-6c53f23711e7d63767b74e25ec184e1f | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_006-pa5d-17aae4ee6d2684e7a13bfaafdac72383/physics-auditor/benchmark-6c53f23711e7d63767b74e25ec184e1f` |
| 12 | case_007 | variant_001 | 1 | benchmark-a446cd9646da4e6df977df732c276e35 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_007-pa5d-6bee76e1ad79f9d4ba9822ebffcab969/physics-auditor/benchmark-a446cd9646da4e6df977df732c276e35` |
| 13 | case_007 | variant_002 | 1 | benchmark-c9234bbbbfa56017d07acd9ec7c94c6a | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_007-pa5d-a9590f5a849d0b3bc9071a5b5b9f70d9/physics-auditor/benchmark-c9234bbbbfa56017d07acd9ec7c94c6a` |
| 14 | case_008 | variant_001 | 1 | benchmark-adecc5fd4726a4bcfecd95b3bb94bd03 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_008-pa5d-4f844bc42a112d33b8bac9e760fa8960/physics-auditor/benchmark-adecc5fd4726a4bcfecd95b3bb94bd03` |
| 15 | case_008 | variant_002 | 1 | benchmark-33147df35fcbc42deaa332ae35721c7c | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_008-pa5d-c652d35d21a6732a36b5ff7d8690a9a6/physics-auditor/benchmark-33147df35fcbc42deaa332ae35721c7c` |
| 16 | case_009 | variant_001 | 1 | benchmark-eea61c7dbcc7c667d9f64ace5053a880 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_009-pa5d-dbbc4e8e06d81caece275aa1d1f6af78/physics-auditor/benchmark-eea61c7dbcc7c667d9f64ace5053a880` |
| 17 | case_009 | variant_002 | 1 | benchmark-2f3c0f606116ed563f6c115c89921eb1 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_009-pa5d-af5f2e9b3ebdf3a42a9bf45b114bfe4e/physics-auditor/benchmark-2f3c0f606116ed563f6c115c89921eb1` |
| 18 | case_010 | variant_001 | 1 | benchmark-4800824f7f809b2c2d07ad600d3680b7 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_010-pa5d-4faaaba62753e8ddba36ddbb04cf9356/physics-auditor/benchmark-4800824f7f809b2c2d07ad600d3680b7` |
| 19 | case_010 | variant_002 | 1 | benchmark-97457d2ee89e46e6dc06e290bf521a45 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_010-pa5d-5e1345315c947d94dd9a849ca740cc19/physics-auditor/benchmark-97457d2ee89e46e6dc06e290bf521a45` |
| 20 | case_011 | variant_001 | 1 | benchmark-81f758404c8772c6093bc0e64472d325 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_011-pa5d-b117201dcb5475a35611655f00ad9466/physics-auditor/benchmark-81f758404c8772c6093bc0e64472d325` |
| 21 | case_011 | variant_002 | 1 | benchmark-6c1abd0e85abea0609e75563aa6d1e33 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_011-pa5d-959ad3104d40c616ccd90ac049a32d38/physics-auditor/benchmark-6c1abd0e85abea0609e75563aa6d1e33` |
| 22 | case_012 | variant_001 | 1 | benchmark-b20eabf89ace21604b4156ae682c9a3b | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_012-pa5d-1b795a36168f086b0467452b16173767/physics-auditor/benchmark-b20eabf89ace21604b4156ae682c9a3b` |
| 23 | case_012 | variant_002 | 1 | benchmark-74d2806d5904c97cb885ccac2063673c | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_012-pa5d-f742d34ee083017b2c0ab43c0404fddb/physics-auditor/benchmark-74d2806d5904c97cb885ccac2063673c` |
| 24 | case_013 | variant_001 | 1 | benchmark-899985b84c0423fce9246ee670f77b76 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_013-pa5d-fe8163ab1932607ef272386525ce2ec6/physics-auditor/benchmark-899985b84c0423fce9246ee670f77b76` |
| 25 | case_013 | variant_002 | 1 | benchmark-037e0eb5b248b44c4b4bb2d8d8dddaf0 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_013-pa5d-259632b41f42590d593d30ef6a15b7b7/physics-auditor/benchmark-037e0eb5b248b44c4b4bb2d8d8dddaf0` |
| 26 | case_014 | variant_001 | 1 | benchmark-e3cbafc6d7e3c6b41802f26864d9e279 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_014-pa5d-124cc71319a8e4822ee3ce315d7dafb2/physics-auditor/benchmark-e3cbafc6d7e3c6b41802f26864d9e279` |
| 27 | case_014 | variant_002 | 1 | benchmark-3d63224732ad38009a4ea73ac77cec58 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_014-pa5d-9ac0c48f4d5a99b632e6f4dbe2e0c5cd/physics-auditor/benchmark-3d63224732ad38009a4ea73ac77cec58` |
| 28 | case_015 | variant_001 | 1 | benchmark-485b6df8bced5818e419f7422cd9a2cf | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_015-pa5d-c4965d1f67cf9d5c1a115cb2976794f3/physics-auditor/benchmark-485b6df8bced5818e419f7422cd9a2cf` |
| 29 | case_015 | variant_002 | 1 | benchmark-eba2ffc1a009719bc6d8eab5c9cab957 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_015-pa5d-53df42d19b8883829092f85b837801d7/physics-auditor/benchmark-eba2ffc1a009719bc6d8eab5c9cab957` |
| 30 | case_016 | variant_001 | 1 | benchmark-7f0c0014e1ff06b316f4500db235c27f | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_016-pa5d-2f76a5ad0f0d38c28a021ec67e7fbf2e/physics-auditor/benchmark-7f0c0014e1ff06b316f4500db235c27f` |
| 31 | case_016 | variant_002 | 1 | benchmark-19311f40c7c4b444d64e94d53c2891c8 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_016-pa5d-483654d1b3cfd00ab182597bb6344d05/physics-auditor/benchmark-19311f40c7c4b444d64e94d53c2891c8` |
| 32 | case_017 | variant_001 | 1 | benchmark-b87d57844290c610c80e07254675202b | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_017-pa5d-f34f6ac515f15f9550eca525dcc9ceb2/physics-auditor/benchmark-b87d57844290c610c80e07254675202b` |
| 33 | case_017 | variant_002 | 1 | benchmark-cb86dc6c3175e4de7c246afa4c888bca | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_017-pa5d-5df402ea96b60e5ec40d708b612017b2/physics-auditor/benchmark-cb86dc6c3175e4de7c246afa4c888bca` |
| 34 | case_018 | variant_001 | 1 | benchmark-59db35db159ab75acd4e09e96e87744b | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_018-pa5d-6dc68c6539854c334fe807f44e3765f9/physics-auditor/benchmark-59db35db159ab75acd4e09e96e87744b` |
| 35 | case_018 | variant_002 | 1 | benchmark-3f75bdd835e699ce550cbebf37724313 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_018-pa5d-91d9d52825c925b3a0e1cd379d7bfaea/physics-auditor/benchmark-3f75bdd835e699ce550cbebf37724313` |
| 36 | case_019 | variant_001 | 1 | benchmark-75f391c4924dc87e115ba66275de9f3e | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_019-pa5d-11c9fc8fb256d267b69be308f01db9de/physics-auditor/benchmark-75f391c4924dc87e115ba66275de9f3e` |
| 37 | case_019 | variant_002 | 1 | benchmark-3948f0887e79cc5fec77777a9ec89866 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_019-pa5d-834b8ef33a1cf0a16aec4d677d0647ba/physics-auditor/benchmark-3948f0887e79cc5fec77777a9ec89866` |
| 38 | case_020 | variant_001 | 1 | benchmark-642b66fb59eff82bbdfd4fdc4882c5ab | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_020-pa5d-2b3ab5e91091911cbd24244b092646d5/physics-auditor/benchmark-642b66fb59eff82bbdfd4fdc4882c5ab` |
| 39 | case_020 | variant_002 | 1 | benchmark-4e1944c552e2c836428cb96b21e1134a | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_020-pa5d-22cda8cda1ffba195f4dcbf4f45f2564/physics-auditor/benchmark-4e1944c552e2c836428cb96b21e1134a` |
| 40 | case_021 | variant_001 | 1 | benchmark-559e40fa18e4ff61cde0ab110b8b1d2b | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_021-pa5d-3a45dc94d83e23809b0b282d6ee98f94/physics-auditor/benchmark-559e40fa18e4ff61cde0ab110b8b1d2b` |
| 41 | case_021 | variant_002 | 1 | benchmark-af54c3ddd2a0afdf2f82fe8dcea66c34 | `runs/pa5d1-preregistered-v1/benchmark-066da7ec4384ab1a6d94b82e/children/case_021-pa5d-9672dfeb7cdbf1e119138465f71e0796/physics-auditor/benchmark-af54c3ddd2a0afdf2f82fe8dcea66c34` |

### schedule_balanced_single_repeat_v1

Status: **HUMAN_AUTHORITY_REQUIRED — candidate not selected**.

Neutral rule: SHA-256-rank clean and defective variants separately to omit one of each, then SHA-256-rank the 40 remaining variants and repeat the greatest-ranked variant.

Tradeoff: Retains 40 distinct variants with balanced omission of one clean and one defective variant, and adds one exact repeat to make repeat consistency observable once.

Omitted variants: `case_014/variant_001, case_021/variant_002`. Distinct variants: 40; exact repeats: 1. Ordering is `(case_id, variant_id, repetition_id)`.

| Effective denominator | Exact count |
| --- | --- |
| clean runs | 20 |
| defective runs | 21 |
| expected pass | 20 |
| expected repair | 14 |
| expected human escalation | 6 |
| expected insufficient evidence | 1 |
| complete variant pairs | 19 |
| exact repeat pairs | 1 |
| route-consistency misses allowed by proposed >=0.95 | 0 |

| # | Case | Variant | Rep | Execution ID | Fresh PA-3 action root |
| --- | --- | --- | --- | --- | --- |
| 1 | case_001 | variant_001 | 1 | benchmark-fee53793c2dfa7ba751b1fd2b31786e7 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_001-pa5d-be710250e6ec9dc6df72c4f4215a523a/physics-auditor/benchmark-fee53793c2dfa7ba751b1fd2b31786e7` |
| 2 | case_001 | variant_002 | 1 | benchmark-1bc6f59ee55539823b03db71a7553300 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_001-pa5d-373d383367109f82162b482f5a203ce5/physics-auditor/benchmark-1bc6f59ee55539823b03db71a7553300` |
| 3 | case_002 | variant_001 | 1 | benchmark-16f184112ca70b4b8033f10ccc8e32b1 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_002-pa5d-8ef9a7786a71cd4c38ceab3c85b35f65/physics-auditor/benchmark-16f184112ca70b4b8033f10ccc8e32b1` |
| 4 | case_002 | variant_002 | 1 | benchmark-e02545f240932d97bb7feade0f812f96 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_002-pa5d-68e3ab6db31c78a7c6351bb091431e5d/physics-auditor/benchmark-e02545f240932d97bb7feade0f812f96` |
| 5 | case_003 | variant_001 | 1 | benchmark-9fdfc3525df6b6434c7a36198da35091 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_003-pa5d-531eed0348f8f6a5f8d0ac9f70af1ac5/physics-auditor/benchmark-9fdfc3525df6b6434c7a36198da35091` |
| 6 | case_003 | variant_002 | 1 | benchmark-776a278d3fb348d18f3ad84f5a86c29f | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_003-pa5d-94d221d45039f9f91f14de53cee846fb/physics-auditor/benchmark-776a278d3fb348d18f3ad84f5a86c29f` |
| 7 | case_004 | variant_001 | 1 | benchmark-cfdc1cfeb5dea713fe3934977c4ee1ee | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_004-pa5d-afae9b1df8818929eccf94673c994ee0/physics-auditor/benchmark-cfdc1cfeb5dea713fe3934977c4ee1ee` |
| 8 | case_004 | variant_002 | 1 | benchmark-0741f558356e95f5d29005c93eaa3ef7 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_004-pa5d-01123b0b44f6854678d0fcf020a76ab4/physics-auditor/benchmark-0741f558356e95f5d29005c93eaa3ef7` |
| 9 | case_005 | variant_001 | 1 | benchmark-3f82c9d423ef45fac13504aa01a96ea7 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_005-pa5d-59e71ba0c15fef18e30aeda4c828d08c/physics-auditor/benchmark-3f82c9d423ef45fac13504aa01a96ea7` |
| 10 | case_005 | variant_002 | 1 | benchmark-0256069ec098251ac5b023ddeda3f2f4 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_005-pa5d-5122c0ec26c78cdb3b3e153897733b24/physics-auditor/benchmark-0256069ec098251ac5b023ddeda3f2f4` |
| 11 | case_006 | variant_001 | 1 | benchmark-cc5d1b063377b5b1e188366064f32818 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_006-pa5d-d3d61cde4758ce727501768b68e27476/physics-auditor/benchmark-cc5d1b063377b5b1e188366064f32818` |
| 12 | case_006 | variant_002 | 1 | benchmark-22990de32a3e6b6f143a9f4ebb6c6589 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_006-pa5d-520b40041387f3efdb6e3778fc87eb59/physics-auditor/benchmark-22990de32a3e6b6f143a9f4ebb6c6589` |
| 13 | case_007 | variant_001 | 1 | benchmark-07e181d5f1f378ed66a7ea968de8e00f | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_007-pa5d-cfe11a9a748a82401f4271329a338872/physics-auditor/benchmark-07e181d5f1f378ed66a7ea968de8e00f` |
| 14 | case_007 | variant_002 | 1 | benchmark-049f6c1e6104625aded547b9331288f5 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_007-pa5d-11a4ce1337c9566634661bc44ed36089/physics-auditor/benchmark-049f6c1e6104625aded547b9331288f5` |
| 15 | case_008 | variant_001 | 1 | benchmark-20493cd39ce7b762a915b06ab49e5de9 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_008-pa5d-d776b8e3d4d967c3a3a6f23b0bfb5bb2/physics-auditor/benchmark-20493cd39ce7b762a915b06ab49e5de9` |
| 16 | case_008 | variant_002 | 1 | benchmark-3e56f9dfa05856f688f7e551e51d56b8 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_008-pa5d-41c959a4be66cb1605fa0f316fb7c62a/physics-auditor/benchmark-3e56f9dfa05856f688f7e551e51d56b8` |
| 17 | case_009 | variant_001 | 1 | benchmark-544615bf6b03366e967b053515835108 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_009-pa5d-289a376641e7d77dfd2398d6c70efd08/physics-auditor/benchmark-544615bf6b03366e967b053515835108` |
| 18 | case_009 | variant_002 | 1 | benchmark-051276105bd8aef37ebf2ff3caf29953 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_009-pa5d-f68ecf6e527e586f48bd46e40e134f67/physics-auditor/benchmark-051276105bd8aef37ebf2ff3caf29953` |
| 19 | case_010 | variant_001 | 1 | benchmark-e2a7ab9c1d5075fbd57da47fe3fc9f35 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_010-pa5d-cd21a2bfe039ce9c98317be91838e592/physics-auditor/benchmark-e2a7ab9c1d5075fbd57da47fe3fc9f35` |
| 20 | case_010 | variant_002 | 1 | benchmark-a6df49539725a8a5b5cf3fcdf4419a5c | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_010-pa5d-374ab5f99d425ae19100185e36f85620/physics-auditor/benchmark-a6df49539725a8a5b5cf3fcdf4419a5c` |
| 21 | case_011 | variant_001 | 1 | benchmark-0f53c0c5ad70a7dfc5fd2de2c6529d91 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_011-pa5d-8a03758d23934556142312418f288ea8/physics-auditor/benchmark-0f53c0c5ad70a7dfc5fd2de2c6529d91` |
| 22 | case_011 | variant_002 | 1 | benchmark-84285a6e5b0f84ecf417fcb17b85e3af | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_011-pa5d-b58f5a2c6c476149dc0d71778d3b676d/physics-auditor/benchmark-84285a6e5b0f84ecf417fcb17b85e3af` |
| 23 | case_012 | variant_001 | 1 | benchmark-65078b20a861e9d0ca2a1fab92a7749d | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_012-pa5d-d9c4fca5798c77c7f4b1789e3913b081/physics-auditor/benchmark-65078b20a861e9d0ca2a1fab92a7749d` |
| 24 | case_012 | variant_002 | 1 | benchmark-5a808d792e8081bcd3315a0ecdcc9b3a | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_012-pa5d-ad9c8360802cc1713e66f174298883e7/physics-auditor/benchmark-5a808d792e8081bcd3315a0ecdcc9b3a` |
| 25 | case_013 | variant_001 | 1 | benchmark-5e43a12432a19efd6b4cd8fae4324e47 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_013-pa5d-387f19ec6e537f0fcb6043a42ad86889/physics-auditor/benchmark-5e43a12432a19efd6b4cd8fae4324e47` |
| 26 | case_013 | variant_002 | 1 | benchmark-eadf5f835a49fc9e457d1ee11770c869 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_013-pa5d-3473accfe24b93247f1ca292cf687b79/physics-auditor/benchmark-eadf5f835a49fc9e457d1ee11770c869` |
| 27 | case_014 | variant_002 | 1 | benchmark-2519426808508c8056ce6b9adc744443 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_014-pa5d-efcf548972217d8bce025c6d00e8f8be/physics-auditor/benchmark-2519426808508c8056ce6b9adc744443` |
| 28 | case_015 | variant_001 | 1 | benchmark-40e1c9345ceb5fb1c74417250fef81c0 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_015-pa5d-0528f35d5593aa533e480bbb436a3ed4/physics-auditor/benchmark-40e1c9345ceb5fb1c74417250fef81c0` |
| 29 | case_015 | variant_002 | 1 | benchmark-524417197fcba35bbff4f35a757b2b1e | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_015-pa5d-42ad5e9c4d4d7e1573e4bd119775a178/physics-auditor/benchmark-524417197fcba35bbff4f35a757b2b1e` |
| 30 | case_016 | variant_001 | 1 | benchmark-8380be1e45f879e42b15b1b5e3649df6 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_016-pa5d-1483075874e584ac02c4ed729eff06fc/physics-auditor/benchmark-8380be1e45f879e42b15b1b5e3649df6` |
| 31 | case_016 | variant_002 | 1 | benchmark-f457052cea8c0fdfc12bc657aeef55d2 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_016-pa5d-21163d27aadfabc468e175225874134b/physics-auditor/benchmark-f457052cea8c0fdfc12bc657aeef55d2` |
| 32 | case_017 | variant_001 | 1 | benchmark-c3a24aef5513cb07f276ce00173502e1 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_017-pa5d-4e01ef1980f97624610c101131c33291/physics-auditor/benchmark-c3a24aef5513cb07f276ce00173502e1` |
| 33 | case_017 | variant_002 | 1 | benchmark-2e218bcab88a0dc859d49eeb87650892 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_017-pa5d-62e519b638dc2285068ce67664eb07c1/physics-auditor/benchmark-2e218bcab88a0dc859d49eeb87650892` |
| 34 | case_018 | variant_001 | 1 | benchmark-78789b75a3d8b01d9a941f5f3e5cda1d | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_018-pa5d-e23c919dd20c75c6606129e67708d446/physics-auditor/benchmark-78789b75a3d8b01d9a941f5f3e5cda1d` |
| 35 | case_018 | variant_002 | 1 | benchmark-a33891a7f6ec8bd02db548bde098785e | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_018-pa5d-ee79c9e769474a9ff54de8537ad97498/physics-auditor/benchmark-a33891a7f6ec8bd02db548bde098785e` |
| 36 | case_019 | variant_001 | 1 | benchmark-ed3d8d7ac2fec60c120db606e1d12f31 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_019-pa5d-f854344c346deec78a2271a70d269a67/physics-auditor/benchmark-ed3d8d7ac2fec60c120db606e1d12f31` |
| 37 | case_019 | variant_002 | 1 | benchmark-e809a78efbc86972dc199d86bd447732 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_019-pa5d-8e9bc53f26a8bcbfa1ab8b44b6005a18/physics-auditor/benchmark-e809a78efbc86972dc199d86bd447732` |
| 38 | case_020 | variant_001 | 1 | benchmark-ab72c934fc503d3152273833fb55db56 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_020-pa5d-b238c98aa10423f03a204c111c1ca8f1/physics-auditor/benchmark-ab72c934fc503d3152273833fb55db56` |
| 39 | case_020 | variant_002 | 1 | benchmark-a6d0465603155a8b3386700998ad7c8d | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_020-pa5d-7df060961872b618b80c4e81c215f37a/physics-auditor/benchmark-a6d0465603155a8b3386700998ad7c8d` |
| 40 | case_021 | variant_001 | 1 | benchmark-8d1a8d447a99a681d9e2c52ca24271d3 | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_021-pa5d-d8c9d1d39cd261a13ef896e3abb413ec/physics-auditor/benchmark-8d1a8d447a99a681d9e2c52ca24271d3` |
| 41 | case_021 | variant_001 | 2 | benchmark-216df5dc168a1bcf0d5609631e334aed | `runs/pa5d1-preregistered-v1/benchmark-4729e2067dddfb64a3dff628/children/case_021-pa5d-f2a98a819a496f9891af26e7aad635e2/physics-auditor/benchmark-216df5dc168a1bcf0d5609631e334aed` |

## Exact model and PA-3 configuration

Status: **HUMAN_AUTHORITY_REQUIRED** even though the proposed bytes are the already-qualified PA-3 configuration. The human selection makes its use in this scientific calibration prospective and explicit.

| Field | Bound value |
| --- | --- |
| approval_policy | "never" |
| backend | "codex_cli" |
| environment_allowlist_profile | "codex_cli_minimal_v1" |
| max_stderr_bytes | 1048576 |
| max_stdout_bytes | 1048576 |
| model | "gpt-5.6-sol" |
| network_policy | "disabled_by_codex_policy_not_kernel_enforced" |
| output_schema_id | "physics_audit_report_v1" |
| prompt_template_version | "physics_auditor_prompt_v2" |
| reasoning_effort | "high" |
| sandbox_policy | "read_only" |
| schema_version | 1 |
| session_policy | "fresh_ephemeral" |
| structured_output_policy | "strict" |
| timeout_seconds | 300 |
| trusted_executable | null |
| role_policy_sha256 | 3ef14f5e708536189df54acf88aa0c90f0aa356188e5f83c392744e9b4e2b7e6 |
| bubblewrap_policy_sha256 | d762a628b90ede5647a2475114e04aadc744f977bbaa7d97dc5834830188b560 |
| codex_cli_version | codex-cli 0.146.0 |
| codex_executable_sha256 | 2e863156ed35ecc5253b1e2f907a9143077b9f7cb51942070c61996471ff6e04 |
| bubblewrap_version | bubblewrap 0.11.1 |
| bubblewrap_backend_identity_sha256 | e7ffcfcfc7611c7cc6bb5913841e6385684429b8ab9b0c4c7b7c506fd94eaedc |
| runtime_identity_drift_allowed | False |
| prompt_template_sha256 | dcc15eac412efd0b8a1628adc05f21765d744ba1265bd8296aeac0c0cd477c6c |
| output_schema_sha256 | 82ffc2fe49e3929678368733c6200933d072c27abcd548d65cb52dbe62121297 |
| rendered_prompt_policy | qualified_renderer_over_exact_run_inputs_then_bind_rendered_sha256_before_launch |
| resume_allowed | False |
| yolo_or_full_access_allowed | False |

Each benchmark run and GL task gets exactly one fresh ephemeral provider session. No resume, session reuse, yolo/full-access inheritance, source-worktree mount, scorer mount, or unverified PA-2/PA-3 evidence is allowed.
The operator may resolve the executable by path, but launch must fail closed unless the resolved Codex executable and Bubblewrap backend match the exact version and SHA-256 identities above; runtime drift is not permitted.

## One-shot execution and scoring policies

Status: **HUMAN_AUTHORITY_REQUIRED**. The proposal requires dedicated PA-5D1 one-shot adapters to qualify before launch. Each coordinate produces exactly one PA-3 session and one verified proof; a route is an observation, not permission to run an ordinary PA-4 Worker/repair loop. Non-pass routes are persisted and scored without repair or human scientific override. Ambiguous infrastructure invalidates the calibration unless the identical action is provably resumable.

The proposed derived-metric policy fixes declared-category assignment, applies per-category gates to every nonzero declared category, uses PA-5C2 tri-state failure eligibility independently for each criterion without a global malformed or infrastructure override, defines exact pair/repeat comparisons, nearest-rank p95, and token combination without double-counting reasoning tokens. Benchmark and GL are separate cohorts: each applicable threshold must pass in each cohort, with no cross-domain pooling. The GL proposal derives separate route/category/severity/evidence/malformed/infrastructure criteria from each locked task. Both policies require explicit human approval.

## Normative threshold table

Structural gates are inherited qualified authority. Every performance value is only a proposal and is highlighted as HUMAN_AUTHORITY_REQUIRED. Duration and token usage are descriptive and have no acceptance gate.

Important denominator facts: the catalog contains no critical-minimum benchmark variant, so `critical_defect_recognition_rate` is not applicable. Only case_009/variant_002 declares acceptable alternatives; both schedules include it. Repeat consistency is observable only in the balanced single-repeat schedule.

| Metric | Operator | Value | Gate | Application | Authority status | Rationale |
| --- | --- | --- | --- | --- | --- | --- |
| answer_key_exposure_count | == | 0 | structural_hard_gate | global | QUALIFIED_PRE_OUTCOME | PA-5C1 blindness authority |
| session_reuse_count | == | 0 | structural_hard_gate | global | QUALIFIED_PRE_OUTCOME | PA-3 fresh-ephemeral and PA-5C structural authority |
| duplicate_external_action_count | == | 0 | structural_hard_gate | global | QUALIFIED_PRE_OUTCOME | PA-5A/PA-5C3 exactly-once recovery authority |
| unverified_pa2_pa3_evidence_accepted_count | == | 0 | structural_hard_gate | global | QUALIFIED_PRE_OUTCOME | PA-5C1/PA-5C2 proof closure |
| run_identity_bijection | == | exact | structural_hard_gate | global | QUALIFIED_PRE_OUTCOME | PA-5C2 expected/observed identity equality |
| malformed_report_fail_closed | == | True | structural_hard_gate | global | QUALIFIED_PRE_OUTCOME | PA-3/PA-5C2 strict report boundary |
| scorer_accessible_to_auditor | == | False | structural_hard_gate | global | QUALIFIED_PRE_OUTCOME | PA-5C1 disjoint namespace |
| scientific_authority_mutation_count | == | 0 | structural_hard_gate | global | QUALIFIED_PRE_OUTCOME | PA-5C3 frozen manifest and PA-5C4 Start authority |
| post_outcome_scientific_change_count | == | 0 | structural_hard_gate | global | QUALIFIED_PRE_OUTCOME | Prospective calibration immutability |
| critical_defect_recognition_rate | NO_GATE | None | performance_gate | global | **HUMAN_AUTHORITY_REQUIRED** | The qualified catalog has zero critical-minimum variants; metric is N/A and the proposal is explicitly no acceptance gate. This value is proposed, not authorized. |
| defect_category_recognition_rate | >= | 0.9 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | Proposed broad category-quality floor. This value is proposed, not authorized. |
| false_pass_rate | == | 0.0 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | A known defective fixture should not pass. This value is proposed, not authorized. |
| clean_pass_rate | >= | 0.9 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | Proposed clean-fixture specificity floor. This value is proposed, not authorized. |
| false_critical_finding_rate | == | 0.0 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | A clean fixture should not receive a critical finding. This value is proposed, not authorized. |
| repair_routing_rate | >= | 0.9 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | Proposed repair-route quality floor. This value is proposed, not authorized. |
| human_escalation_rate | == | 1.0 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | All frozen human-gate cases should escalate. This value is proposed, not authorized. |
| gl_expected_pass_route_rate | == | 1.0 | performance_gate | global | **HUMAN_AUTHORITY_REQUIRED** | All locked expected-pass GL tasks should pass. This value is proposed, not authorized. |
| insufficient_evidence_routing_rate | == | 1.0 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | Missing authority must fail closed. This value is proposed, not authorized. |
| malformed_report_rate | == | 0.0 | performance_gate | global | **HUMAN_AUTHORITY_REQUIRED** | Proposed output-quality gate, separate from fail-closed handling. This value is proposed, not authorized. |
| infrastructure_failure_rate | <= | 0.0 | performance_gate | global | **HUMAN_AUTHORITY_REQUIRED** | Proposed requirement for a complete interpretable calibration. This value is proposed, not authorized. |
| severity_correctness_rate | >= | 0.9 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | Proposed severity-quality floor. This value is proposed, not authorized. |
| required_category_recognition_rate | >= | 0.9 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | Proposed required-category floor. This value is proposed, not authorized. |
| acceptable_alternative_recognition_rate | >= | 0.9 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | Proposed approved-alternative floor. This value is proposed, not authorized. |
| forbidden_category_violation_rate | == | 0.0 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | Forbidden categories should never appear. This value is proposed, not authorized. |
| forbidden_route_violation_rate | == | 0.0 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | Forbidden deterministic routes should never occur. This value is proposed, not authorized. |
| evidence_validity_rate | == | 1.0 | performance_gate | aggregate_and_each_nonzero_declared_category | **HUMAN_AUTHORITY_REQUIRED** | Every accepted scientific claim must cite verified evidence. This value is proposed, not authorized. |
| route_consistency_rate | >= | 0.95 | performance_gate | global | **HUMAN_AUTHORITY_REQUIRED** | Proposed paired-route consistency floor. This value is proposed, not authorized. |
| finding_category_consistency_rate | >= | 0.95 | performance_gate | global | **HUMAN_AUTHORITY_REQUIRED** | Proposed paired-category consistency floor. This value is proposed, not authorized. |
| repeat_consistency_rate | == | 1.0 | performance_gate | global | **HUMAN_AUTHORITY_REQUIRED** | If repeats are selected, their semantic outputs should agree. This value is proposed, not authorized. |
| duration_seconds | NO_GATE | None | descriptive | not_applicable_descriptive | NOT_AN_ACCEPTANCE_GATE | Observational resource metric; report exactly where exposed without acceptance tuning. |
| authoritative_input_tokens | NO_GATE | None | descriptive | not_applicable_descriptive | NOT_AN_ACCEPTANCE_GATE | Observational resource metric; report exactly where exposed without acceptance tuning. |
| authoritative_output_tokens | NO_GATE | None | descriptive | not_applicable_descriptive | NOT_AN_ACCEPTANCE_GATE | Observational resource metric; report exactly where exposed without acceptance tuning. |
| authoritative_combined_tokens | NO_GATE | None | descriptive | not_applicable_descriptive | NOT_AN_ACCEPTANCE_GATE | Observational resource metric; report exactly where exposed without acceptance tuning. |

## Exact ten-task GL pilot

Locked source commit: `7d04b5b9882dcd476c1457b8d711ac7b5520b2c1`. Task order is the qualified catalog order. The pilot uses the exact same proposed PA-3 configuration and one fresh action/session per task. This authorizes no production GL-mode claim.

| # | Task | Expected route | Required categories | Visible manifest | Locked source blobs | Fresh action root |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | task_001 | pass | none | 5f2ad1619aa1bfcb2c0a480d496f9f1f66a0a4ae1ef66f81c9dcff01d92b53d4 | docs/derivations/stage4AO_A_uniform_gp_background_residual.md (20934 B, a8536115e4488cb8b15e85c83b1fbf7b20282fe5d52c08c86380c398328b22df) | `runs/pa5d1-preregistered-v1/gl-b6ba044097ed7d565518a26a/01-task_001-ebe1b49f6f4d3cb0` |
| 2 | task_002 | pass | none | 9988dd252bb4917db0f9781a4b7e313614c2855dd3f56dd96648ed6d1bfb0b46 | code/BlackStringToy/ConformalCartoonAlgebra.hpp (5581 B, c3a074745720c9bc9ca6c76294552b4b45439e9a3fb3d37ff4ac2f3f6d8a5e26)<br>code/BlackStringToy/tests/Stage4ALTraceFreeCurvatureLapseBlockTest.cpp (12177 B, ad20357be843f50859a086f433b64ae7fce68755c9f25c19ae7795349d5944b6)<br>docs/derivations/stage4AO_A_uniform_gp_background_residual.md (20934 B, a8536115e4488cb8b15e85c83b1fbf7b20282fe5d52c08c86380c398328b22df) | `runs/pa5d1-preregistered-v1/gl-b6ba044097ed7d565518a26a/02-task_002-d7ec922683e877a5` |
| 3 | task_003 | pass | none | 0ead0f5f9f068d23a11c59d7955f66120bfcf9eee914a3f049c277de05b126e2 | code/BlackStringToy/tests/Stage4AOCGRChomboComparisonBatch4GaugeTest.cpp (12514 B, a05754b891bedef09953de004602791dce9eb7117b23cd93802b67aa721a33e4)<br>docs/derivations/stage4AO_A_uniform_gp_background_residual.md (20934 B, a8536115e4488cb8b15e85c83b1fbf7b20282fe5d52c08c86380c398328b22df) | `runs/pa5d1-preregistered-v1/gl-b6ba044097ed7d565518a26a/03-task_003-4c69156b64a7ce2c` |
| 4 | task_004 | pass | none | c11b341e5b0868e65149b876222da6735fb997cb4c0ac30d48065952135db714 | code/BlackStringToy/CartoonHatGammaX.hpp (8877 B, bd02989289435745f186bc9e14ce52e58ebb206d09c3df1a0605fcd867f22e3d)<br>code/BlackStringToy/tests/Stage4ANHatGammaXTest.cpp (10178 B, 72a6e8bcc1ae7c640bc11612751b7e8f57047a7a76f1657eab805be89ac39b68)<br>docs/derivations/stage4AM_hatGammaX_derivation.md (15946 B, 331424a902d1e7f67d6cc4aae04f9f15f2668b5c1cc711eede7277e1e07aa95d) | `runs/pa5d1-preregistered-v1/gl-b6ba044097ed7d565518a26a/04-task_004-1cacec1f0d07b464` |
| 5 | task_005 | pass | none | b6fe306e1a65e17efd87144d0a53796325e2f2d0c3aa7848890dd87ffb12c45f | code/BlackStringToy/Stage4AOGPDiscretePreflight.hpp (20889 B, 1c298b88e4b52c6d2778ab5dfd6347847b5527dc351edfb22bfb56b82e513f97)<br>code/BlackStringToy/tests/Stage4AOBDiscreteOperatorPreflightTest.cpp (7198 B, 5e4928d4c74f57a9e94e1ee71a6eb242fa54ad5e67841a357438fd349d3d2221) | `runs/pa5d1-preregistered-v1/gl-b6ba044097ed7d565518a26a/05-task_005-cf08822ecedbad55` |
| 6 | task_006 | require_human_review | gauge_constraint_ambiguity | b8c4b91e70f59f4859085419e237fa2a9379828774789890bd21b1a18a94f311 | docs/derivations/stage4AO_C_frozen_gauge_spectral_gate.md (153719 B, 2e3bad6782ba242d468cba82df92019e2dcad061fe7f8b6597a773b20ba9323b) | `runs/pa5d1-preregistered-v1/gl-b6ba044097ed7d565518a26a/06-task_006-99c496550513a2c5` |
| 7 | task_007 | require_human_review | gauge_constraint_ambiguity | 3c769029c5026c0886dc3543c21a5fa6fd14619b8bd4526cd0ce9fdc274cf7c7 | docs/derivations/stage4AO_C_frozen_gauge_spectral_gate.md (153719 B, 2e3bad6782ba242d468cba82df92019e2dcad061fe7f8b6597a773b20ba9323b) | `runs/pa5d1-preregistered-v1/gl-b6ba044097ed7d565518a26a/07-task_007-a1cae8513acb9644` |
| 8 | task_008 | require_human_review | new_physical_interpretation, unsupported_physical_claim | 2538052b90fdcb2201ea6f4088b8a87d1bdbbeef4bd1d7979782f4ac2eb79af4 | docs/derivations/stage4AO_C_frozen_gauge_spectral_gate.md (153719 B, 2e3bad6782ba242d468cba82df92019e2dcad061fe7f8b6597a773b20ba9323b) | `runs/pa5d1-preregistered-v1/gl-b6ba044097ed7d565518a26a/08-task_008-18210c0648471017` |
| 9 | task_009 | require_human_review | gauge_constraint_ambiguity | 988b3b18130123f361b46b3175480c325b28ac2910476e3c78532921b94135dd | docs/derivations/stage4AO_C_frozen_gauge_spectral_gate.md (153719 B, 2e3bad6782ba242d468cba82df92019e2dcad061fe7f8b6597a773b20ba9323b) | `runs/pa5d1-preregistered-v1/gl-b6ba044097ed7d565518a26a/09-task_009-d7b13351d606cdf5` |
| 10 | task_010 | pass | none | aadd071f8683b532b771a2a4630ff4959a31b7f759ac1b18f42459483290b03c | code/BlackStringToy/ConformalCartoonAlgebra.hpp (5581 B, c3a074745720c9bc9ca6c76294552b4b45439e9a3fb3d37ff4ac2f3f6d8a5e26)<br>code/BlackStringToy/tests/Stage4AOCGRChomboComparisonBatch1Test.cpp (45961 B, 1100e029143dd0c5f37fd0732a761d6688d21dbd253c886ef86b0721bb77979d) | `runs/pa5d1-preregistered-v1/gl-b6ba044097ed7d565518a26a/10-task_010-57292842a10504a8` |

## Failure, recovery, and hard-stop policy

Only PA-5A/PA-5C3 `auto_resume` and `finish_finalization` may continue an identical already-authorized action. Ambiguous launch state, stale/reused process identity, missing proof, changed authority, malformed report, or unverified evidence fails closed. Recovery may repair infrastructure only; every scientific input stays frozen.

Any post-outcome prompt, threshold, metric, fixture, expected route, model/config, schedule, repetition, root, source, or scoring change invalidates the entire calibration. It cannot be patched or continued under this preregistration.

## Contamination statement

Invalidated PA-5B outputs were not used to derive schedule, thresholds, prompt wording, expected routes, model configuration, repetitions, or scoring policy. PA-5B appears only in the machine-readable contamination register as historical non-authority. The alternatives above are derived solely from the current qualified catalog and explicit SHA-256 ranking rules.

## Canonical authority hashes

| Authority | SHA-256 |
| --- | --- |
| PA-5D0 review draft | 89057f629ef8ba808e69628dc83b09ccb8cadf5a3e85ffde018d33d83c7d46a1 |
| Qualified catalog canonical | f4b52fe2f70baf87ca1ec19dff294490088e5571f45918b112ff00b936abb088 |
| Qualified catalog file | 01e621c06c47cb253be3d03e367b30b976717d4ba7ad2d575d12ab9135995372 |
| Qualified PA-5C1 fixture qualification | 1fdb54d40ae2828225be35966ab7844ca3114fb6e3f6d730089f0a987576056b |
| Qualified PA-5C1 scorer root | 4a4ea8e3ea95563381571d1745e8825b8f7a51280827d14a221324ea52436f79 |
| Exact model config canonical | 9e930328a244aba56f5f096da4a5972817f6ccd028af797fdc52169666d1470b |
| GL expected child set | b3126dbc5b0d11d4e2c2592d135bdb5fa4f8779bd6009f2355a6064d512bef44 |
| Schedule child set: schedule_maximum_variant_coverage_v1 | d408ee1278cdfa1641616d1c13c8b294a3a23b1034dae6f95eed25bae661d134 |
| Schedule child set: schedule_balanced_single_repeat_v1 | aee369954b494a9bc81a44edcd1f033bf98c5a08578a309d07039bae77833d9f |
| Candidate authority: schedule_maximum_variant_coverage_v1 | 211c63f49b9c325accd369a770ca9597781cfce539fd6bda1c4964bd31bc5603 |
| Candidate authority: schedule_balanced_single_repeat_v1 | 6701478c87af2090558a6ea69f2ff49a6e98eb2de8b856f99fe1e5ed950269dc |

No final approved preregistration receipt exists. Benchmark sessions launched: **0**. GL-pilot sessions launched: **0**.
