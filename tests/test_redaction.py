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


def test_sensitive_composites_redact_every_descendant_string() -> None:
    source = {
        "token": {
            "value": "plain-secret",
            "nested": {"label": "also-secret", "count": 3, "enabled": True},
            "items": ["list-secret", 17, None, {"deep": "deep-secret"}],
        },
        "credentials": [
            {"username": "owned-string", "attempts": 2},
            "direct-list-string",
            False,
        ],
    }

    redacted = redact_json(source)

    assert redacted == {
        "token": {
            "value": REDACTED,
            "nested": {"label": REDACTED, "count": 3, "enabled": True},
            "items": [REDACTED, 17, None, {"deep": REDACTED}],
        },
        "credentials": [
            {"username": REDACTED, "attempts": 2},
            REDACTED,
            False,
        ],
    }


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


def test_placeholder_overlapping_values_are_idempotent() -> None:
    for sensitive_value in (
        "prefix<REDACTED>suffix",
        "x<REDACTED>",
        "<REDACTED>suffix",
        "REDACTED",
        "<RED",
        "ACTED>",
        REDACTED,
    ):
        source = f"secret={sensitive_value}; existing={REDACTED}"
        once = redact_text(source, (sensitive_value,))
        twice = redact_text(once, (sensitive_value,))
        third = redact_text(twice, (sensitive_value,))

        assert once == twice == third
        if REDACTED in sensitive_value and sensitive_value != REDACTED:
            assert sensitive_value not in once
        assert f"existing={REDACTED}" in once
        assert "<<REDACTED>>" not in third


def test_overlapping_sensitive_literals_are_merged_without_partial_disclosure() -> None:
    source = f"value=abcdefg existing={REDACTED}"

    once = redact_text(source, ("abcde", "cdefg", "abc"))
    twice = redact_text(once, ("abcde", "cdefg", "abc"))

    assert once == twice == f"value={REDACTED} existing={REDACTED}"
    assert "ab" not in once
    assert "fg" not in once


def test_recursive_json_redaction_is_idempotent_with_existing_placeholders() -> None:
    source = {
        "ordinary": [REDACTED, "REDACTED", {"text": "already <REDACTED>"}],
        "session": {"nested": [REDACTED, "raw", 1, True, None]},
    }

    once = redact_json(source, ("REDACTED",))
    twice = redact_json(once, ("REDACTED",))

    assert once == twice
    assert once["session"] == {"nested": [REDACTED, REDACTED, 1, True, None]}
