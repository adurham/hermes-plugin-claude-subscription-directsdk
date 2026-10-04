"""A dead CLI login classifies as a terminal credential failure, not the ``unknown`` catch-all.

The user-visible bug this guards: the refusal reached the fallback banner as
``unavailable (provider failure)`` — a label that names neither the cause nor the fix — and,
because ``unknown`` is retryable, every call spent the full retry budget before the configured
fallback provider ran. Core's pattern tables are HTTP-shaped and carry no phrase for "the local
CLI has no credential", so the provider's own ``classify_api_error`` profile hook is the seam
(``agent/error_classifier._profile_verdict``), exercised here through core's real classifier.
"""
from agent.error_classifier import FailoverReason, classify_api_error
from directsdk import ClaudeCodeLoggedOut
from directsdk_setup import LOGGED_OUT_HINT

PROVIDER = "claude-subscription-directsdk-experimental"
MODEL = "claude-sonnet-5[1m]"


def _classify(profile, message, error=None):
    return classify_api_error(
        error if error is not None else ClaudeCodeLoggedOut(message),
        provider=PROVIDER, model=MODEL, base_url=f"process://{PROVIDER}")


def test_dead_login_is_terminal_auth_with_fallback(profile):
    """The exact live message: terminal verdict, no retries, fallback chain engaged."""
    classified = _classify(profile, f"{LOGGED_OUT_HINT} (native: Failed to authenticate: OAuth session expired and could not be refreshed)")
    assert classified.reason is FailoverReason.auth_permanent
    assert classified.retryable is False
    assert classified.should_fallback is True
    # Nothing to rotate: this provider has no pooled credential of its own.
    assert classified.should_rotate_credential is False


def test_bare_native_text_matches_without_the_exception_type(profile):
    """A wrapped/re-raised string still classifies (the gateway and cron paths wrap errors)."""
    classified = _classify(profile, "Native API error: no usable login in the environment Hermes runs it in")
    assert classified.reason is FailoverReason.auth_permanent


def test_refresh_race_stays_retryable(profile):
    """Another Claude Code process holding the refresh lock clears on its own — never terminal."""
    classified = _classify(
        profile,
        "Native API error: Failed to refresh OAuth token: another Claude Code process is refreshing "
        "it or exited mid-refresh. This is usually transient; retry in a minute")
    assert classified.reason is not FailoverReason.auth_permanent
    assert classified.retryable is True


def test_unrelated_errors_keep_core_classification(profile):
    """The hook is scoped to its own signature and never swallows another verdict."""
    assert profile.classify_api_error(RuntimeError("429 Too Many Requests: rate limit exceeded")) is None
    classified = _classify(profile, "Rate limit exceeded, try again in 30s", error=RuntimeError("Rate limit exceeded, try again in 30s"))
    assert classified.reason is FailoverReason.rate_limit
