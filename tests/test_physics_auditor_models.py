from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from research_automation_supervisor.physics_auditor_models import (
    PHYSICS_AUDITOR_CODEX_ROLE_POLICY_V1,
    PhysicsAuditorExecutionConfigV1,
)


def _config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "backend": "codex_cli",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "timeout_seconds": 300,
        "max_stdout_bytes": 1048576,
        "max_stderr_bytes": 1048576,
        "sandbox_policy": "read_only",
        "approval_policy": "never",
        "network_policy": "disabled_by_codex_policy_not_kernel_enforced",
        "output_schema_id": "physics_audit_report_v1",
        "prompt_template_version": "physics_auditor_prompt_v1",
        "session_policy": "fresh_ephemeral",
        "structured_output_policy": "strict",
        "trusted_executable": None,
        "environment_allowlist_profile": "codex_cli_minimal_v1",
    }


def test_execution_config_is_strict_codex_only_and_read_only() -> None:
    config = PhysicsAuditorExecutionConfigV1.model_validate(_config())

    assert config.backend == "codex_cli"
    assert config.sandbox_policy == "read_only"
    assert config.session_policy == "fresh_ephemeral"
    assert config.structured_output_policy == "strict"
    assert len(config.canonical_sha256()) == 64

    for field, value in (
        ("backend", "direct_api"),
        ("sandbox_policy", "workspace_write"),
        ("session_policy", "resume"),
        ("approval_policy", "full-auto"),
    ):
        candidate = {**_config(), field: value}
        with pytest.raises(ValidationError):
            PhysicsAuditorExecutionConfigV1.model_validate(candidate)

    with pytest.raises(ValidationError):
        PhysicsAuditorExecutionConfigV1.model_validate({**_config(), "command": "codex --yolo"})


def test_distinct_role_policy_forbids_resume_yolo_writes_and_oracle_execution() -> None:
    policy = PHYSICS_AUDITOR_CODEX_ROLE_POLICY_V1

    assert policy.adapter_role == "auditor"
    assert policy.sandbox == "read-only"
    assert policy.approval == "never"
    assert policy.ephemeral is True
    assert policy.resume_allowed is False
    assert policy.danger_full_access_allowed is False
    assert policy.oracle_execution_surface == "none"
    assert hashlib.sha256(policy.to_canonical_json()).hexdigest() == policy.canonical_sha256()
