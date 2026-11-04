"""Cursor Subscription (DirectSDK) — Hermes model-provider plugin.

Uses your existing Cursor plan through Cursor's official SDK (``cursor-sdk``): Hermes keeps its own
agent loop, tools, approvals, memory and compaction; Cursor supplies the model (Claude, GPT,
Gemini, Grok, Composer, Auto ...) billed to your Cursor account. Hermes' tools reach the model as
native Cursor custom tools whose callbacks park until Hermes has run them.

Credentials come only through Hermes (credential pool or ``CURSOR_API_KEY``). ``hermes auth add
cursor-subscription-directsdk`` runs Cursor's official browser sign-in and stores the minted key in
Hermes' pool.
"""
from __future__ import annotations

import logging
import sys
import uuid

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
LOGIN_SOURCE = "manual:cursor_browser_login"


# ── credentials (all through Hermes) ───────────────────────────────────────────────────────────

def _current_key():
    """The key Hermes would use for this provider: its credential pool, else CURSOR_API_KEY."""
    try:
        from agent.credential_pool import load_pool
        entry = load_pool(rt.PROVIDER).select()
        if entry is not None and entry.runtime_api_key:
            return entry.runtime_api_key
    except Exception:  # noqa: BLE001
        logger.debug("%s: credential pool lookup failed", rt.PROVIDER, exc_info=True)
    try:
        from agent.secret_scope import get_secret
        return (get_secret("CURSOR_API_KEY") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def sign_in(*, open_browser=True):
    """Install the runtime if needed, run Cursor's browser login, add the key to Hermes' pool."""
    from agent.credential_pool import AUTH_TYPE_API_KEY, PooledCredential, load_pool
    if not rt.runtime_installed():
        rt.install()
    login = rt.browser_login(open_browser=open_browser)
    label = f"cursor-login ({login['email']})" if login.get("email") else "cursor-login"
    pool = load_pool(rt.PROVIDER)
    entry = pool.add_entry(PooledCredential(
        provider=rt.PROVIDER, id=uuid.uuid4().hex[:6], label=label, auth_type=AUTH_TYPE_API_KEY,
        priority=0, source=LOGIN_SOURCE, access_token=login["api_key"], base_url=rt.BASE_URL,
        expires_at_ms=login.get("expires_at_ms")))
    who = f" as {login['email']}" if login.get("email") else ""
    print(f"Signed in to Cursor{who}; credential {entry.id} added to Hermes' pool for {rt.PROVIDER}.\n"
          "The key is named \"Hermes Agent\" in Cursor Dashboard -> API Keys (expires in 90 days; revoke it there).")
    return entry


def _auth_handler(action, args):
    """``hermes auth add`` = browser sign-in. ``--type api-key`` / ``--api-key`` and every other
    action fall through to Hermes' built-in handling."""
    if action != "add":
        return False
    if getattr(args, "api_key", None) or str(getattr(args, "auth_type", "") or "").lower() in ("api-key", "api_key"):
        return False
    sign_in(open_browser=not getattr(args, "no_browser", False))
    return True


def _model_setup_flow(config, current_model, args):
    """``hermes model`` step: runtime install, browser sign-in when no credential exists, live picker."""
    from hermes_cli.model_setup import note_setup_failure, persist_model_selection
    del config, args
    interactive = sys.stdin.isatty()
    if not rt.runtime_installed():
        if not interactive:
            print(f"  {rt.INSTALL_HINT}")
            note_setup_failure("auth")
            return
        rt.install()
    key = _current_key()
    if not key:
        if not interactive:
            print(f"  {rt.LOGGED_OUT_HINT}")
            note_setup_failure("auth")
            return
        print("  Not signed in to Cursor yet. Starting Cursor's browser sign-in ...")
        try:
            sign_in()
        except Exception as exc:  # noqa: BLE001
            print(f"  {exc}")
            note_setup_failure("auth")
            return
        key = _current_key()
    rows = _rows(key) or [{"id": m, "display_name": m} for m in catalog.FALLBACK_MODELS]
    print(f"\n  Models on your Cursor plan ({len(rows)}):")
    for i, row in enumerate(rows, 1):
        mark = "  (current)" if row["id"] == current_model else ""
        print(f"  {i:>3}. {row['id']:<28} {_note(row['id'])}{mark}")
    default = current_model if any(r["id"] == current_model for r in rows) else "composer-2.5"
    choice = input(f"\n  Model number or id [{default}]: ").strip() if interactive else ""
    if not choice:
        selected = default
    elif choice.isdigit() and 1 <= int(choice) <= len(rows):
        selected = rows[int(choice) - 1]["id"]
    else:
        selected = choice
    persist_model_selection(selected, rt.PROVIDER, base_url=rt.BASE_URL)
    print(f"  Default model set to: {selected} (via {rt.DISPLAY_NAME})")


# ── catalog ────────────────────────────────────────────────────────────────────────────────────

def _rows(api_key=None):
    rows = catalog.load_cached(max_age=catalog.CACHE_TTL_S)
    if rows is None and api_key:
        try:
            from .transport import list_models
        except ImportError:
            from transport import list_models
        rows = list_models(api_key)
    return rows or catalog.load_cached()


def _note(model_id):
    window = catalog.context_length(model_id)
    if not window:
        return ""
    return f"{window // 1_000_000}M ctx" if window >= 1_000_000 else f"{window // 1000}K ctx"


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

    def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0):
        rows = _rows(api_key)
        return [row["id"] for row in rows] if rows else None

    def missing_credentials_hint(self):
        return f"Run `hermes auth add {rt.PROVIDER}` to sign in to Cursor in your browser, or set CURSOR_API_KEY."

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
    display_name=rt.DISPLAY_NAME,
    description=f"{rt.DISPLAY_NAME} (your Cursor plan via the official Cursor SDK; Hermes owns tools)",
    signup_url="https://cursor.com/dashboard",
    api_mode="chat_completions",
    auth_type="api_key",
    env_vars=("CURSOR_API_KEY",),
    base_url=rt.BASE_URL,
    supports_health_check=False,
    default_aux_model="composer-2.5",
    fallback_models=catalog.FALLBACK_MODELS,
    model_aliases=dict(ALIASES),
    classify_api_error=classify_api_error,
    auth_handler=_auth_handler,
    model_setup_flow=_model_setup_flow,
)
# Attribute assignment keeps the plugin loadable on hosts predating the field.
profile.supports_streaming = True
register_provider(profile)

if not rt.runtime_present():
    logger.info("%s: %s", rt.DISPLAY_NAME, rt.INSTALL_HINT)
