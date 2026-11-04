"""Claude Subscription DirectSDK (Experimental) — standalone Hermes model-provider registration."""
import logging
import math
import os
import time
from datetime import UTC, datetime

from providers import register_provider
from providers.base import ProviderProfile

# Dual import: the Hermes loader imports this directory as a package; the flat test path does not.
try:
    from .directsdk import ClaudeCodeLoggedOut
    from .model_catalog import ALIASES, MODEL_METADATA, native_model
    from .directsdk_setup import INSTALL_HINT, _resolve
except ImportError:
    from directsdk import ClaudeCodeLoggedOut
    from model_catalog import ALIASES, MODEL_METADATA, native_model
    from directsdk_setup import INSTALL_HINT, _resolve

logger = logging.getLogger(__name__)

# The claude.ai /usage block: the 5-hour session and the weekly caps, plus billed extra usage.
# Same account and the same numbers the Claude Code `/usage` screen shows, not a token estimate.
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Anthropic rate-limits this endpoint hard (a `retry-after` in the many hundreds of seconds is
# routine). A 429 without a usable header backs off for this long — never re-ask on the 2-minute
# picker/statusbar refresh cadence, which would just earn another 429.
_USAGE_RETRY_DEFAULT_S = 900.0
# A reset further ahead than this is a bad stamp, not a real window (the longest is a week).
_USAGE_RESET_MAX_AHEAD_S = 8 * 86_400
_usage_retry_at = 0.0

# Native's own words for a login that is gone for good. ``no usable login`` is the refusal raised
# before any upstream request; ``could not be refreshed`` is native's own diagnosis appended to it.
_DEAD_LOGIN_SIGNATURES = ('no usable login', 'could not be refreshed')
# The transient sibling: another Claude Code process holds the refresh lock (a second CLI, or the
# interactive one mid-rotation). Retrying really can work, so it must NOT take the terminal verdict.
_REFRESH_RACE_SIGNATURE = 'another claude code process is refreshing'


def classify_native_error(error, *, message='', **_):
    """A login this provider cannot use is terminal, not an unclassifiable provider failure.

    Core's pattern tables are HTTP-shaped: they carry no phrase for "the local CLI has no
    credential", so the refusal fell through to the ``unknown`` catch-all and the user read
    ``unavailable (provider failure)`` — the one label that names neither the cause nor the fix —
    while every call burned the full retry budget before the fallback ran. Only this provider knows
    what its own native error means, which is exactly what the profile hook is for
    (``agent.error_classifier._profile_verdict``, scoped to the erroring provider).

    ``ProviderProfile`` declares ``classify_api_error`` as a dataclass FIELD, so the hook must be
    passed at construction (a subclass method of that name is shadowed by the field default).

    The retryable sibling — ``another Claude Code process is refreshing it`` — is deliberately left
    alone: it clears on its own, and a terminal verdict there would send a working login to the
    fallback chain for the duration of someone else's refresh.
    """
    text = ' '.join(str(part) for part in (error, message) if part).lower()
    if _REFRESH_RACE_SIGNATURE in text:
        return None
    if not isinstance(error, ClaudeCodeLoggedOut) and not any(sig in text for sig in _DEAD_LOGIN_SIGNATURES):
        return None
    # ``auth_permanent`` is the built-in terminal-credential verdict (same recovery as ``auth``):
    # abort the retries, take the fallback chain, no credential rotation — there is no pool entry
    # behind this provider and no other credential to rotate to.
    return {'reason': 'auth_permanent', 'retryable': False, 'should_fallback': True,
            'error_context': {'native_login_dead': True}}



def _usage_token() -> str:
    """The plugin's OWN setup token — the login this provider runs on.

    ``CLAUDE_CODE_OAUTH_TOKEN`` is Hermes' pooled credential for its direct Anthropic route and may
    belong to a different account; the provider deliberately ignores it (see ``directsdk_setup``),
    and so must its usage readout or the status bar would show another plan's numbers.
    """
    try:
        from .directsdk_setup import _env
    except ImportError:
        from directsdk_setup import _env
    return str(_env().get("CLAUDE_SUBSCRIPTION_DIRECTSDK_OAUTH_TOKEN") or "").strip()


def _usage_reset(value):
    """ISO / epoch reset → aware datetime, or None for an absent or implausible stamp."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            reset = datetime.fromtimestamp(float(value), tz=UTC)
        elif isinstance(value, str) and value.strip():
            text = value.strip()
            text = text[:-1] + "+00:00" if text.endswith("Z") else text
            reset = datetime.fromisoformat(text)
            reset = reset if reset.tzinfo else reset.replace(tzinfo=UTC)
        else:
            return None
    except (OverflowError, OSError, TypeError, ValueError):
        return None
    ahead = (reset - datetime.now(UTC)).total_seconds()
    return reset if -86_400 <= ahead <= _USAGE_RESET_MAX_AHEAD_S else None


def _usage_window(payload, key: str, label: str, *, scope: str = "account"):
    """One ``{utilization, resets_at}`` block → ``AccountUsageWindow``; None when not reported.

    ``utilization`` is a fraction on the current endpoint (``0.42``) but the shared parser also
    accepts a percent (``42``): scale only values at or below 1, exactly as
    ``agent.account_usage._usage_windows(fraction=True)`` does.
    """
    from agent.account_usage import AccountUsageWindow

    block = payload.get(key) or {}
    used = block.get("utilization") if isinstance(block, dict) else None
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(float(used)):
        return None
    used = float(used)
    if used < 0:
        return None
    if used <= 1:
        used *= 100
    return AccountUsageWindow(label=label, used_percent=used, reset_at=_usage_reset(block.get("resets_at")),
                              scope=scope)


def _fetch_subscription_usage():
    """The account's 5h/weekly allowance from the OAuth usage endpoint, or None.

    Never raises: the provider hook contract is fail-open. The 429 cooldown is module-wide and
    honoured before any request, so the statusbar's background refresh cannot turn a server-side
    rate limit into a request storm.
    """
    global _usage_retry_at
    if time.monotonic() < _usage_retry_at:
        return None
    token = _usage_token()
    if not token:
        return None
    try:
        from agent.anthropic_credentials import _is_oauth_token
    except Exception:  # health: allow BLE001 -- a probe must not depend on an optional core import
        _is_oauth_token = None
    if callable(_is_oauth_token) and not _is_oauth_token(token):
        return None
    try:
        import httpx
    except Exception:  # health: allow BLE001 -- httpx ships with Hermes; fail open without it
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20",
               "User-Agent": "claude-code/2.1.0"}
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(_USAGE_URL, headers=headers)
    except Exception:  # health: allow BLE001 -- network failures read as "unknown", never as a gauge
        return None
    if response.status_code == 429:
        try:
            delay = float(response.headers.get("retry-after") or _USAGE_RETRY_DEFAULT_S)
        except (TypeError, ValueError):
            delay = _USAGE_RETRY_DEFAULT_S
        _usage_retry_at = time.monotonic() + max(delay, 60.0)
        logger.debug("Claude subscription usage: rate limited; retry in %.0fs", max(delay, 60.0))
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json() or {}
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    windows = [w for w in (
        _usage_window(payload, "five_hour", "Current session"),
        _usage_window(payload, "seven_day", "Current week"),
        _usage_window(payload, "seven_day_opus", "Opus week", scope="model"),
        _usage_window(payload, "seven_day_sonnet", "Sonnet week", scope="model"),
    ) if w is not None]
    details = []
    extra = payload.get("extra_usage") or {}
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used, limit = extra.get("used_credits"), extra.get("monthly_limit")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
            details.append(f"Extra usage: ${used / 100.0:.2f} / ${limit / 100.0:.2f} "
                           f"{extra.get('currency') or 'USD'}")
    if not windows and not details:
        return None
    from agent.account_usage import AccountUsageSnapshot

    return AccountUsageSnapshot(
        provider="claude-subscription-directsdk-experimental", source="oauth_usage_api",
        fetched_at=datetime.now(UTC), windows=tuple(windows), details=tuple(details))


class ClaudeOAuthDirectSDKProfile(ProviderProfile):
    model_metadata = MODEL_METADATA

    def get_model_context_length(self, model):
        route = native_model(model)
        pinned = self.model_metadata.get(route, {}).get('context_window')
        # Unpinned: behind the relay native Claude Code runs a plain id within its 200K default, and
        # Hermes' own family guess (claude-opus-5-5 -> 1M before it was pinned) would outgrow that.
        # A [1m] id stays unreported: no Hermes guess exceeds the 1M native budget, nor is it promised.
        return pinned or (None if route.endswith('[1m]') else 200_000)

    def fetch_account_usage(self, *, base_url=None, api_key=None):
        """The subscription account's own 5-hour / weekly windows (the claude.ai usage block).

        Called by ``agent.account_usage.fetch_account_usage`` for this provider slug; the statusbar's
        plan gauge and the model picker both read the snapshot it returns.
        """
        return _fetch_subscription_usage()

    def get_usage_cost(self, model, usage):
        from decimal import Decimal, InvalidOperation
        from agent.usage_pricing import CostResult, format_cost_label

        native = (usage.raw_usage or {}).get('native_cost') or {}
        unknown = CostResult(amount_usd=None, status='unknown', source='none', label='n/a',
                             notes=('native final list-price accounting unavailable; subscription invoice unknown',))
        amount = native.get('total_cost_usd')
        models = native.get('modelUsage') or {}
        if isinstance(amount, bool) or not models or any(row.get('costBasis') != 'list' for row in models.values()):
            return unknown
        try:
            amount = Decimal(str(amount))
        except InvalidOperation:
            return unknown
        if not amount.is_finite() or amount < 0:
            return unknown
        return CostResult(amount_usd=amount, status='estimated', source='provider_cost_api',
                          label=format_cost_label(amount), notes=('native API list-price equivalent; not subscription invoice; extra usage unknown',))

    def create_client(self, **client_kwargs):
        try:
            from .directsdk import Client
        except ImportError:
            from directsdk import Client
        return Client(**client_kwargs)

    def fetch_models(self, **kwargs):
        # No HTTP /models endpoint: the account's own picker (CLI `initialize` handshake) is the
        # live list for /model, the Desktop picker and `hermes model`; None degrades to the catalog.
        rows = self.discover_models(**kwargs)
        return [row["id"] for row in rows] if rows else None

    def setup_status(self, **kwargs):
        try:
            from .directsdk_setup import setup_status
        except ImportError:
            from directsdk_setup import setup_status
        return setup_status(**kwargs)

    def discover_models(self, **kwargs):
        try:
            from .directsdk_setup import discover_models
        except ImportError:
            from directsdk_setup import discover_models
        return discover_models(**kwargs)

    def build_api_kwargs_extras(self, *, reasoning_config=None, **_):
        return ({'reasoning': dict(reasoning_config)} if reasoning_config else {}), {}


profile = ClaudeOAuthDirectSDKProfile(
    name='claude-subscription-directsdk-experimental',
    display_name='Claude Subscription DirectSDK (Experimental)',
    description='Claude Subscription DirectSDK (Experimental) (Claude Pro/Max subscription via your Claude Code login; Hermes owns tools)',
    api_mode='chat_completions',
    auth_type='external_process',
    supports_health_check=False,
    native_reasoning_details_type='claude-subscription-directsdk-experimental.native_assistant',
    env_vars=(),
    base_url='process://claude-subscription-directsdk-experimental',
    process_command='claude',
    process_args=(),
    process_command_env_vars=('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND',),
    default_aux_model='claude-sonnet-5-5[1m]',
    fallback_models=tuple(MODEL_METADATA),
    model_aliases={alias: native_model(alias) for alias in ALIASES},
    classify_api_error=classify_native_error,
    # Zero-output empties here are transient (reasoning_details is a provider-private replay
    # carrier, not visible thinking) and recover on the same provider; subscription retries are free.
    empty_completion_policy='transient',
)
register_provider(profile)

# The provider stays registered when Claude Code is missing so `hermes model` can show the
# install hint; the request path (`directsdk.Client`) refuses with the same message.
if _resolve(None, None) is None:
    logger.warning("%s: %s", profile.display_name, INSTALL_HINT)
