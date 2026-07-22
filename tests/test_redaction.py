from __future__ import annotations

from research_automation_supervisor.codex_adapter import build_subprocess_environment
from research_automation_supervisor.redaction import REDACTED, redact_json, redact_text


def test_redacts_bearer_and_common_token_forms() -> None:
    source = (
        "Authorization: Bearer abc.def-123\n"
        "sk-exampleSecret ghp_exampleToken github_pat_example xoxb-example xoxp-example"
    )

    rendered = redact_text(source)

    assert "abc.def-123" not in rendered
    assert "sk-exampleSecret" not in rendered
    assert "ghp_exampleToken" not in rendered
    assert "github_pat_example" not in rendered
    assert "xoxb-example" not in rendered
    assert "xoxp-example" not in rendered
    assert rendered.count(REDACTED) == 6


def test_redacts_case_insensitive_secret_assignments() -> None:
    source = 'token=alpha API_KEY: "beta" Password = \'gamma\' cookie:delta ordinary=keep'

    rendered = redact_text(source)

    for secret in ("alpha", "beta", "gamma", "delta"):
        assert secret not in rendered
    assert "ordinary=keep" in rendered


def test_recursive_redaction_preserves_non_string_scalar_types() -> None:
    source = {
        "nested": {
            "access_token": "secret-value",
            "password_count": 3,
            "enabled": True,
            "nothing": None,
        },
        "items": ["ghp_tokenvalue", 17, False, {"Authorization": "Bearer abc"}],
    }

    redacted = redact_json(source)

    assert redacted["nested"]["access_token"] == REDACTED
    assert redacted["nested"]["password_count"] == 3
    assert redacted["nested"]["enabled"] is True
    assert redacted["nested"]["nothing"] is None
    assert redacted["items"][0] == REDACTED
    assert redacted["items"][1] == 17
    assert redacted["items"][2] is False
    assert redacted["items"][3]["Authorization"] == REDACTED


def test_redaction_is_idempotent_and_uses_removed_environment_values() -> None:
    secret = "SENSITIVE_ENV_VALUE_123"
    environment, names, values = build_subprocess_environment(
        {
            "PATH": "/tools",
            "HOME": "/home/example",
            "CODEX_HOME": "/codex/home",
            "Demo_Token": secret,
            "dbPASSWORDbackup": "another-value",
        }
    )

    once = redact_text(f"before {secret} after", values)
    twice = redact_text(once, values)

    assert once == twice == f"before {REDACTED} after"
    assert environment == {
        "PATH": "/tools",
        "HOME": "/home/example",
        "CODEX_HOME": "/codex/home",
    }
    assert names == ("dbPASSWORDbackup", "Demo_Token")
    assert values == (secret, "another-value")
