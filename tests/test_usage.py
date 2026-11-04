"""The provider's own subscription usage: the claude.ai 5h/weekly windows.

Hermes reads these through the provider hook (`ProviderProfile.fetch_account_usage`) for the
statusbar's plan gauge and the model picker's usage row. The fetch must use THIS provider's login
(``CLAUDE_SUBSCRIPTION_DIRECTSDK_OAUTH_TOKEN``), parse the endpoint's fraction-or-percent
``utilization`` the same way core does, and honour a 429 ``retry-after`` instead of hammering a
rate-limited endpoint every refresh tick.
"""
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("claude_sub_usage_under_test", REPO_ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, url, headers=None):
        self.calls.append((url, headers))
        return self.response


@pytest.fixture()
def plugin(monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "_usage_token", lambda: "sk-ant-oat-test-token")
    module._usage_retry_at = 0.0
    return module


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """No test may read the machine's real relay capture: that would answer for every fetch."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _install(monkeypatch, response):
    client = _Client(response)
    monkeypatch.setattr("httpx.Client", lambda **_kw: client)
    return client


def test_reads_the_session_and_weekly_windows(monkeypatch, plugin):
    reset = (datetime.now(UTC) + timedelta(days=3)).isoformat().replace("+00:00", "Z")
    client = _install(monkeypatch, _Response(200, {
        "five_hour": {"utilization": 0.12, "resets_at": reset},
        "seven_day": {"utilization": 42, "resets_at": reset},          # percent form
        "seven_day_opus": {"utilization": 0.91, "resets_at": reset},   # model-scoped
        "extra_usage": {"is_enabled": True, "used_credits": 4778, "monthly_limit": 10000},
    }))

    snapshot = plugin._fetch_subscription_usage()

    assert snapshot is not None
    assert snapshot.provider == "claude-subscription-directsdk-experimental"
    assert [(w.label, w.used_percent, w.scope) for w in snapshot.windows] == [
        ("Current session", 12.0, "account"),
        ("Current week", 42.0, "account"),
        ("Opus week", 91.0, "model"),
    ]
    assert snapshot.details == ("Extra usage: $47.78 / $100.00 USD",)
    assert client.calls[0][0] == "https://api.anthropic.com/api/oauth/usage"
    assert client.calls[0][1]["Authorization"] == "Bearer sk-ant-oat-test-token"


def test_a_429_sets_a_cooldown_and_is_not_retried(monkeypatch, plugin):
    _install(monkeypatch, _Response(429, {"error": "rate limited"}, {"retry-after": "899"}))

    assert plugin._fetch_subscription_usage() is None
    assert plugin._usage_retry_at > 0

    # No second request inside the server's own window, even if a refresh tick fires.
    monkeypatch.setattr("httpx.Client", lambda **_kw: pytest.fail("must not re-request during retry-after"))
    assert plugin._fetch_subscription_usage() is None


def test_a_scope_error_rests_the_endpoint(monkeypatch, plugin):
    # `claude setup-token` mints a `user:inference` token; the usage endpoint requires
    # `user:profile` and refuses with 403. Asking again two minutes later cannot change that.
    _install(monkeypatch, _Response(403, {
        "type": "error",
        "error": {"type": "permission_error", "message": "OAuth token does not meet scope requirement user:profile"},
    }))

    assert plugin._fetch_subscription_usage() is None
    assert plugin._usage_retry_at > 0

    monkeypatch.setattr("httpx.Client", lambda **_kw: pytest.fail("must not re-request a scope refusal"))
    assert plugin._fetch_subscription_usage() is None


def test_no_token_means_no_request(monkeypatch, plugin):
    monkeypatch.setattr(plugin, "_usage_token", lambda: "")
    monkeypatch.setattr("httpx.Client", lambda **_kw: pytest.fail("no token must not call the endpoint"))

    assert plugin._fetch_subscription_usage() is None


def _write_capture(root, payload):
    path = root / "cache" / "claude-subscription-directsdk" / "unified-limits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture()
def headers_home(tmp_path, monkeypatch):
    """A private HERMES_HOME: the relay writes there, the hook reads the same path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_inference_headers_paint_the_own_allowance_without_any_request(headers_home, plugin, monkeypatch):
    # The setup token is inference-only: `GET /api/oauth/usage` 403s. But every inference reply
    # carries the account's own 5h/weekly figures, and the relay captures them — so the gauge can
    # show THIS session's Claude numbers with no network call and no profile-scoped credential.
    reset_5h = int((datetime.now(UTC) + timedelta(hours=2)).timestamp())
    reset_7d = int((datetime.now(UTC) + timedelta(days=4)).timestamp())
    _write_capture(headers_home, {
        "captured_at": 1_800_000_000.0,
        "5h-utilization": "0.65", "5h-reset": str(reset_5h),
        "7d-utilization": "0.18", "7d-reset": str(reset_7d),
        "overage-status": "rejected", "overage-disabled-reason": "out_of_credits",
    })
    monkeypatch.setattr(plugin, "_usage_token", lambda: "")
    monkeypatch.setattr("httpx.Client", lambda **_kw: pytest.fail("captured headers must not hit the network"))

    snapshot = plugin._fetch_subscription_usage()

    assert snapshot is not None
    assert snapshot.source == "native_rate_limit_headers"
    assert [(w.label, w.used_percent, w.scope) for w in snapshot.windows] == [
        ("Current session", 65.0, "account"),
        ("Current week", 18.0, "account"),
    ]
    assert snapshot.details == ("Extra usage: not available (out of credits)",)


def test_a_header_window_whose_reset_passed_is_dropped(headers_home, plugin):
    past = int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())
    future = int((datetime.now(UTC) + timedelta(days=4)).timestamp())
    _write_capture(headers_home, {
        "5h-utilization": "0.99", "5h-reset": str(past),
        "7d-utilization": "0.18", "7d-reset": str(future),
    })

    snapshot = plugin._fetch_subscription_usage()

    assert snapshot is not None
    assert [(w.label, w.used_percent) for w in snapshot.windows] == [("Current week", 18.0)]


def test_the_relay_records_the_unified_headers(headers_home):
    sys.path.insert(0, str(REPO_ROOT))
    sys.modules.pop("admission", None)
    import admission

    admission._last_rate_limit = {}
    headers = [
        ("Content-Type", "text/event-stream"),
        ("anthropic-ratelimit-unified-5h-utilization", "0.42"),
        ("anthropic-ratelimit-unified-5h-reset", "1793829600"),
    ]
    admission.capture_rate_limits(headers)

    path = admission.rate_limit_state_path()
    assert path == headers_home / "cache" / "claude-subscription-directsdk" / "unified-limits.json"
    assert json.loads(path.read_text())["5h-utilization"] == "0.42"

    # The same observation on the next reply is not rewritten...
    before = path.stat().st_mtime_ns
    admission.capture_rate_limits(headers)
    assert path.stat().st_mtime_ns == before

    # ...a changed one is, and a reply without unified headers leaves it alone.
    admission.capture_rate_limits([("anthropic-ratelimit-unified-5h-utilization", "0.43")])
    assert json.loads(path.read_text())["5h-utilization"] == "0.43"
    admission.capture_rate_limits([("Server", "cloudflare")])
    assert json.loads(path.read_text())["5h-utilization"] == "0.43"
