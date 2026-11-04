"""Cursor Subscription (Experimental) — Hermes model-provider plugin.

Uses your existing Cursor plan through Cursor's official SDK (``cursor-sdk``): Hermes keeps its own
agent loop, tools, approvals, memory and compaction; Cursor supplies the model (Claude, GPT,
Gemini, Grok, Composer, Auto ...) billed to your Cursor account.
"""
from __future__ import annotations

import logging

from providers import register_provider
from providers.base import ProviderProfile

# Dual import: Hermes imports this directory as a package; the flat test path does not.
try:
    from . import cursor_catalog as catalog
    from . import cursor_runtime as rt
except ImportError:
    import cursor_catalog as catalog
    import cursor_runtime as rt

logger = logging.getLogger(__name__)

ALIASES = catalog.ALIASES


class CursorSubscriptionProfile(ProviderProfile):
    def get_model_context_length(self, model):
        # Never None: Hermes' family-substring guess (e.g. 1M for any claude-opus-5*) can exceed
        # the window Cursor actually serves for the default variant.
        return catalog.context_length(ALIASES.get(model, model)) or catalog.DEFAULT_CONTEXT

    def get_usage_cost(self, model, usage):
        from agent.usage_pricing import CostResult
        return CostResult(amount_usd=None, status="unknown", source="none", label="n/a",
                          notes=("billed to your Cursor plan; see cursor.com/dashboard/usage (SDK tag)",))

    def create_client(self, **client_kwargs):
        try:
            from .transport import Client
        except ImportError:
            from transport import Client
        return Client(**client_kwargs)

    def setup_status(self, **kwargs):
        return rt.setup_status()

    def discover_models(self, **kwargs):
        rows = catalog.load_cached(max_age=catalog.CACHE_TTL_S)
        if rows is None:
            try:
                from .transport import list_models
            except ImportError:
                from transport import list_models
            rows = list_models() or catalog.load_cached()
        if not rows:
            return None
        out = []
        for row in rows:
            window = catalog.context_length(row["id"])
            note = f"{window // 1000}K ctx" if window and window < 1_000_000 else (
                f"{window // 1_000_000}M ctx" if window else "")
            out.append({"id": row["id"], "label": row.get("display_name") or row["id"], "note": note})
        return out

    def fetch_models(self, **kwargs):
        rows = self.discover_models()
        return [row["id"] for row in rows] if rows else None

    def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
        extra = {"reasoning": dict(reasoning_config)} if reasoning_config else {}
        # Auxiliary calls (titles, compression summaries) arrive without the chat path's session_id:
        # the client runs those as throwaway one-shot agents.
        auxiliary = context.get("task") is not None or "session_id" not in context
        return extra, ({"hermes_auxiliary": True} if auxiliary else {})


def classify_api_error(error, **_):
    code = getattr(error, "code", None)
    if code in ("timeout", "cancelled"):
        return {"reason": "timeout"}
    return None


profile = CursorSubscriptionProfile(
    name=rt.PROVIDER,
    display_name="Cursor Subscription (Experimental)",
    description="Cursor Subscription (Experimental) (your Cursor plan via the official Cursor SDK; Hermes owns tools)",
    signup_url="https://cursor.com/dashboard",
    api_mode="chat_completions",
    auth_type="external_process",
    supports_health_check=False,
    env_vars=(),
    base_url="process://cursor-subscription",
    process_command=str(rt.LAUNCHER),
    process_args=(),
    process_command_env_vars=("CURSOR_SUBSCRIPTION_COMMAND",),
    default_aux_model="composer-2.5",
    fallback_models=catalog.FALLBACK_MODELS,
    model_aliases=dict(ALIASES),
    classify_api_error=classify_api_error,
)
# Attribute assignment keeps the plugin loadable on hosts predating the field.
profile.supports_streaming = True
register_provider(profile)

if not rt.runtime_present():
    logger.info("%s: %s", profile.display_name, rt.INSTALL_HINT)
