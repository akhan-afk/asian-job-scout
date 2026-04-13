"""
Telegram command and message handlers.

All outbound messages go through send_reply() from bot.formatting — the
single gateway that normalises markdown and handles the safe fallback.
No ParseMode decisions are made here.
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from telegram import Update
from telegram.ext import ContextTypes

from bot.formatting import send_reply
from bot.guardrails import GuardrailViolation, check_input, check_output
from bot.observability import get_trace_handler

load_dotenv()

MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest")


# ─────────────────────────────────────────
#  Static messages
# ─────────────────────────────────────────

WELCOME_MESSAGE = """\
*Welcome to JobAgent!*

I help you find and understand jobs in Asia (Japan, Korea & Thailand).

*COMMANDS*
/jobs — Show the latest job summaries
/search <keyword> — Search jobs by keyword (e.g. /search engineer)
/subscribe — Get notified when new job listings are added
/unsubscribe — Stop job alert notifications
/clear — Reset conversation history
/help — Show this guide

*THINGS YOU CAN ASK ME*
- Jobs in Japan
- Backend roles in Korea
- Any remote roles in Thailand?
- What does [company name] do?
- What is a good salary for a software engineer in Tokyo?
- How do I write a Japanese-style CV?

_I only answer career and job-related questions._"""


# ─────────────────────────────────────────
#  Summaries helpers
# ─────────────────────────────────────────

def get_latest_summaries(region: str | None = None) -> list[dict]:
    """Return the most recent job summaries regardless of date."""
    from bot.tools import _get_summaries
    return _get_summaries(region)


def format_summary_message(s: dict) -> str:
    url  = s.get("url", "")
    body = s.get("body", "")
    if url:
        return f"{body}\n\n[View listing]({url})"
    return body


# ─────────────────────────────────────────
#  Command handlers
# ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, WELCOME_MESSAGE)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reply(update, WELCOME_MESSAGE)


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    summaries = get_latest_summaries()

    if not summaries:
        await send_reply(
            update,
            "No job listings are in the database yet. "
            "The pipeline hasn't been run — try again later or ask me about a specific region.",
        )
        return

    data_date = summaries[0].get("data_date", "")
    date_note = f" (data from {data_date})" if data_date else ""
    await send_reply(update, f"Found *{len(summaries)}* job listings{date_note}:")

    for s in summaries:
        await send_reply(update, format_summary_message(s), web_preview=False)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = " ".join(context.args).strip().lower() if context.args else ""

    if not keyword:
        await send_reply(
            update,
            "Please provide a keyword. Example: `/search engineer`",
        )
        return

    summaries = get_latest_summaries()
    matches   = [s for s in summaries if keyword in s.get("body", "").lower()]

    if not matches:
        if not summaries:
            await send_reply(
                update,
                "No job listings are in the database yet. Try again after the pipeline runs.",
            )
        else:
            await send_reply(update, f'No jobs found matching *{keyword}*.')
        return

    await send_reply(update, f'Found *{len(matches)}* job(s) matching *{keyword}*:')

    for s in matches:
        await send_reply(update, format_summary_message(s), web_preview=False)


# ─────────────────────────────────────────
#  Subscription handlers
# ─────────────────────────────────────────

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        from db.client import get_users_collection
        get_users_collection().update_one(
            {"user_id": user.id},
            {"$set": {
                "user_id":       user.id,
                "username":      user.username,
                "first_name":    user.first_name,
                "subscribed":    True,
                "regions":       ["japan", "korea", "thailand"],
                "subscribed_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        await send_reply(
            update,
            "You're subscribed to job alerts! "
            "I'll message you whenever new listings are scraped. "
            "Use /unsubscribe to stop at any time.",
        )
    except Exception as exc:
        await send_reply(update, f"Subscription failed: {exc}")


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        from db.client import get_users_collection
        result = get_users_collection().update_one(
            {"user_id": user.id},
            {"$set": {"subscribed": False}},
        )
        msg = (
            "You weren't subscribed."
            if result.matched_count == 0
            else "You've been unsubscribed from job alerts."
        )
        await send_reply(update, msg)
    except Exception as exc:
        await send_reply(update, f"Error: {exc}")


# ─────────────────────────────────────────
#  Clear conversation history
# ─────────────────────────────────────────

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the LangGraph checkpoint for this user so they start fresh."""
    user_id = str(update.effective_user.id)
    try:
        from db.client import get_db
        db      = get_db()
        deleted = 0
        for col_name in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            result   = db[col_name].delete_many({"thread_id": user_id})
            deleted += result.deleted_count

        await send_reply(
            update,
            "Conversation history cleared. Starting fresh — what would you like to know?",
        )
        print(f"[clear] Removed {deleted} checkpoint docs for user {user_id}")
    except Exception as exc:
        await send_reply(update, "Couldn't clear history. Please try again.")
        print(f"[error] cmd_clear: {exc}")


# ─────────────────────────────────────────
#  Message handler — LangGraph agent
# ─────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    user_name = update.effective_user.first_name
    user_text = update.message.text

    print(f"[{user_name} | {user_id}] {user_text}")

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    # ── Input guardrails ──────────────────────────────────────────────────────
    try:
        check_input(user_text)
    except GuardrailViolation as gv:
        print(f"[guardrail:input] {gv}")
        await send_reply(update, gv.safe_response)
        return

    agent = context.bot_data.get("agent")
    if agent is None:
        await send_reply(update, "Agent is not initialised yet. Please try again in a moment.")
        return

    # ── Agent invocation (with Langfuse tracing if configured) ───────────────
    trace_handler = get_trace_handler(user_id)
    callbacks     = [trace_handler] if trace_handler else []

    try:
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_text)]},
            config={
                "configurable": {"thread_id": str(user_id)},
                "callbacks":    callbacks,
            },
        )
        reply = result["messages"][-1].content or ""
    except Exception as exc:
        reply = "Sorry, something went wrong. Please try again."
        print(f"[error] agent: {exc}")
    finally:
        if trace_handler is not None:
            try:
                trace_handler.flush()
            except Exception:
                pass

    # ── Output guardrails ─────────────────────────────────────────────────────
    reply = check_output(reply)

    # ── Send through the single formatting gateway ────────────────────────────
    await send_reply(update, reply)

    print(f"[bot → {user_name}] {reply[:80]}{'...' if len(reply) > 80 else ''}")
