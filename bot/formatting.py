"""
Single formatting + sending gateway for all bot replies.

Design principle
────────────────
Every outbound message — whether triggered by a command (/jobs, /search),
a natural language query (LangGraph agent), a guardrail block, or a system
error — passes through send_reply().

Formatting is applied exactly once, here, not scattered across handlers.
ParseMode is decided exactly once, here.
The fallback path is handled exactly once, here.

Flow inside send_reply()
────────────────────────
  raw text (from agent / tool / static string)
      │
      ▼
  _to_telegram_markdown()
      • **bold** → *bold*   (Telegram Markdown v1)
      • ### header → *bold*
      • cleans up blank lines
      │
      ▼
  reply_text(parse_mode=MARKDOWN)
      │ success → done
      │ failure (Telegram rejected the markdown — usually a stray special char)
      ▼
  _strip_markdown()
      • removes *asterisks*, _underscores_, `backticks`
      • [label](url) links → "label  url" (preserves both label and URL)
      • result is clean, readable plain text — never raw symbol soup
      │
      ▼
  reply_text(no parse_mode)  ← guaranteed to succeed
"""

from __future__ import annotations

import re

from telegram import Update
from telegram.constants import ParseMode


# ─────────────────────────────────────────
#  Markdown normaliser
# ─────────────────────────────────────────

def _to_telegram_markdown(text: str) -> str:
    """
    Normalise LLM / tool output to Telegram Markdown v1 syntax.

    Telegram Markdown v1 supports: *bold*  _italic_  `code`  [text](url)
    It does NOT support: **double-star bold**, ### headers, ---, ___
    """
    # ### / ## / # headers → *bold*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # **bold** → *bold*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    # __bold__ → *bold*
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    # Horizontal rules (---, ___, ***) → blank line
    text = re.sub(r"^(\s*[-_*]{3,}\s*)$", "", text, flags=re.MULTILINE)
    # Collapse 3+ blank lines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─────────────────────────────────────────
#  Markdown stripper  (safe fallback)
# ─────────────────────────────────────────

def _strip_markdown(text: str) -> str:
    """
    Remove all Telegram Markdown v1 symbols, producing clean plain text.

    Used ONLY as a fallback when parse_mode=MARKDOWN is rejected by Telegram
    (e.g. an unmatched asterisk or parenthesis in job data).

    Crucially: [label](url) links are converted to "label  url" so neither
    the link text nor the URL is lost.
    """
    # [label](url)  →  label  url
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1  \2", text)
    # *bold* and _italic_ → bare text
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_",   r"\1", text)
    # `code` → bare text
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Any remaining stray markers
    text = re.sub(r"[*_`]", "", text)
    return text.strip()


# ─────────────────────────────────────────
#  The single sending gateway
# ─────────────────────────────────────────

async def send_reply(
    update: Update,
    text: str,
    *,
    web_preview: bool = False,
) -> None:
    """
    The one function every handler calls to send a message to the user.

    Parameters
    ----------
    update      : Telegram Update object
    text        : raw text — may contain LLM markdown, plain text, or anything
    web_preview : show Telegram link preview (default False — keeps chat clean)
    """
    formatted = _to_telegram_markdown(text)
    try:
        await update.message.reply_text(
            formatted,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=not web_preview,
        )
    except Exception:
        # Telegram rejected the markdown (stray special character, etc.)
        # Strip all symbols and send as clean plain text — never raw *stars*.
        plain = _strip_markdown(formatted)
        await update.message.reply_text(
            plain,
            disable_web_page_preview=not web_preview,
        )
