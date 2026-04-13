"""
Langfuse observability integration for the JobAgent LangGraph agent.

Langfuse is open-source (MIT/AGPL) LLM observability with a native
LangChain callback integration. It tracks every LLM call, tool invocation,
token count, latency, and error — grouped by user and session.

Free cloud setup (one-time, ~2 minutes)
----------------------------------------
1. Sign up at https://cloud.langfuse.com  (email only, no credit card)
2. Create a project → Settings → API Keys → copy Public + Secret keys
3. Add to Render environment variables (or .env for local dev):
       LANGFUSE_PUBLIC_KEY = pk-lf-...
       LANGFUSE_SECRET_KEY = sk-lf-...
       LANGFUSE_HOST       = https://cloud.langfuse.com   ← optional (default)

If the keys are absent, get_trace_handler() returns None and the bot runs
normally with observability silently disabled. No code changes needed to
enable/disable — just set or unset the environment variables.

Self-hosting
------------
Deploy Langfuse on Railway / Render / any Docker host and point
LANGFUSE_HOST at your instance. The same free cloud API keys won't work
for a self-hosted instance — generate new ones in your self-hosted UI.
"""

from __future__ import annotations

import os

# Cached after first check so we don't read env vars on every request
_langfuse_enabled: bool | None = None


def _is_enabled() -> bool:
    global _langfuse_enabled
    if _langfuse_enabled is None:
        has_keys = bool(
            os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
        )
        if has_keys:
            try:
                import langfuse  # noqa: F401  — verify package is installed
                _langfuse_enabled = True
                print("[observability] Langfuse enabled — traces will appear at cloud.langfuse.com")
            except ImportError:
                _langfuse_enabled = False
                print(
                    "[observability] LANGFUSE keys found but 'langfuse' package not installed. "
                    "Run: pip install langfuse"
                )
        else:
            _langfuse_enabled = False
            print(
                "[observability] Langfuse not configured "
                "(set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY to enable)"
            )
    return _langfuse_enabled


def get_trace_handler(user_id: str | int):
    """
    Return a LangfuseCallbackHandler scoped to this user + session,
    or None if Langfuse is not configured / installed.

    Usage in agent invocation:
        handler = get_trace_handler(user_id)
        callbacks = [handler] if handler else []
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=text)]},
            config={
                "configurable": {"thread_id": str(user_id)},
                "callbacks": callbacks,
            },
        )

    What gets traced per message
    ----------------------------
    - Full LangGraph trace (graph execution)
    - Each LLM call: model, prompt, response, token counts, latency
    - Each tool call: name, input args, output
    - Errors with full stack traces
    - User ID and session ID for per-user filtering in the dashboard
    """
    if not _is_enabled():
        return None

    try:
        from langfuse.callback import CallbackHandler
        return CallbackHandler(
            user_id=str(user_id),
            session_id=str(user_id),
            tags=["telegram", "jobagent"],
        )
    except Exception as exc:
        print(f"[observability] Failed to create Langfuse handler: {exc}")
        return None
