"""Claude Subscription DirectSDK (Experimental) — standalone Hermes model-provider registration."""
import logging
import os

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



class ClaudeOAuthDirectSDKProfile(ProviderProfile):
    model_metadata = MODEL_METADATA

    def get_model_context_length(self, model):
        route = native_model(model)
        pinned = self.model_metadata.get(route, {}).get('context_window')
        # Unpinned: behind the relay native Claude Code runs a plain id within its 200K default, and
        # Hermes' own family guess (claude-opus-5-5 -> 1M before it was pinned) would outgrow that.
        # A [1m] id stays unreported: no Hermes guess exceeds the 1M native budget, nor is it promised.
        return pinned or (None if route.endswith('[1m]') else 200_000)

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
    default_aux_model='claude-sonnet-5[1m]',
    fallback_models=tuple(MODEL_METADATA),
    model_aliases={alias: native_model(alias) for alias in ALIASES},
    classify_api_error=classify_native_error,
)
register_provider(profile)

# The provider stays registered when Claude Code is missing so `hermes model` can show the
# install hint; the request path (`directsdk.Client`) refuses with the same message.
if _resolve(None, os.environ) is None:
    logger.warning("%s: %s", profile.display_name, INSTALL_HINT)
