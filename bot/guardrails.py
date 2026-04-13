"""
Lightweight input/output guardrails for the JobAgent Telegram bot.

Deliberately stdlib-only — no ML models, no heavy deps.
Keeps Render's 512MB RAM budget intact while blocking the most common
abuse patterns and preventing internal details from leaking to users.

Input guards
------------
- PromptInjectionGuard : detects attempts to override the system prompt
- LengthGuard          : rejects excessively long inputs

Output guards
-------------
- CLIExposureGuard : strips lines containing internal CLI commands/scripts
- LengthGuard      : truncates near Telegram's 4096-char hard limit
"""

from __future__ import annotations

import re


# ─────────────────────────────────────────
#  Types
# ─────────────────────────────────────────

class GuardrailViolation(Exception):
    """
    Raised by check_input() when a message should be blocked.
    safe_response is a ready-to-send user-friendly reply.
    """
    def __init__(self, reason: str, safe_response: str) -> None:
        super().__init__(reason)
        self.safe_response = safe_response


# ─────────────────────────────────────────
#  Input: injection patterns
# ─────────────────────────────────────────

_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|rules?|prompt|context)",
        r"forget\s+(everything|all|your\s+(previous|prior))",
        r"new\s+(system\s+)?instructions?\s*:",
        r"you\s+are\s+now\s+(?!a?\s*job)",    # "you are now X" but not "you are now a job assistant"
        r"\bDAN\b",
        r"\bjailbreak\b",
        r"<\s*/?system\s*>",
        r"\[INST\].*\[/INST\]",
        r"disregard\s+(all\s+)?(previous|prior|above)",
    ]
]

_MAX_INPUT_CHARS = 2000


# ─────────────────────────────────────────
#  Output: CLI / internal command patterns
# ─────────────────────────────────────────

_CLI_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"uv\s+run\s+\S+",
        r"\bpython\b.*\.py\b",
        r"\brun_scraper\b",
        r"\brun_summarizer\b",
        r"\brun_pipeline\b",
        r"playwright\s+install",
    ]
]

_MAX_OUTPUT_CHARS = 3800   # Telegram hard-caps messages at 4096 chars


# ─────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────

def check_input(text: str) -> None:
    """
    Validate user input before it reaches the agent.
    Raises GuardrailViolation if the input should be blocked.
    Does nothing if the input is acceptable.
    """
    if len(text) > _MAX_INPUT_CHARS:
        raise GuardrailViolation(
            "Input exceeds length limit",
            f"Please keep your message under {_MAX_INPUT_CHARS} characters.",
        )

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            raise GuardrailViolation(
                f"Prompt injection pattern detected: {pattern.pattern}",
                "I can only help with job searches in Japan, Korea, and Thailand.",
            )


def check_output(text: str) -> str:
    """
    Sanitise agent output before it is sent to the user.
    Returns cleaned text — never raises, always returns something safe.
    """
    # Strip individual lines that contain CLI commands
    clean_lines = [
        line for line in text.splitlines()
        if not any(p.search(line) for p in _CLI_PATTERNS)
    ]
    text = "\n".join(clean_lines).strip()

    # Collapse runs of blank lines left behind after stripping
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Truncate near Telegram's hard limit
    if len(text) > _MAX_OUTPUT_CHARS:
        text = (
            text[:_MAX_OUTPUT_CHARS].rstrip()
            + "\n\n_(reply truncated — ask for more details)_"
        )

    return text
