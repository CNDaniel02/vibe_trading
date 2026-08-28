from scripts.adapters.errors import summarize_external_error
from scripts.runtime.subprocess_runner import SubprocessJobRunner


def test_external_error_summary_redacts_oauth_secrets() -> None:
    exc = RuntimeError(
        'exchange failed: {"access_token":"access-value",'
        '"refresh_token": "refresh-value", "client_secret":"secret-value"}; '
        'Authorization: Bearer bearer-value'
    )

    summary = summarize_external_error(exc)

    assert "access-value" not in summary
    assert "refresh-value" not in summary
    assert "secret-value" not in summary
    assert "bearer-value" not in summary
    assert summary.count("[REDACTED]") == 4


def test_subprocess_error_redacts_oauth_callback_and_token_body() -> None:
    stderr = (
        "OAuth failed\n"
        "callback=http://127.0.0.1/callback?code=authorization-value"
        '&state=ok payload={"refresh_token":"refresh-value"}'
    )

    summary = SubprocessJobRunner._safe_error(stderr)

    assert "refresh-value" not in summary
    assert "authorization-value" not in summary
    assert "state=ok" in summary
