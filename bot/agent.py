"""
LangGraph ReAct agent for the JobAgent Telegram bot.

Architecture
────────────
  user message
      │
      ▼
  ┌──────────────────────────────────────────────────┐
  │  StateGraph (AgentState)                         │
  │                                                  │
  │  START                                           │
  │    → _should_summarize()                         │
  │       ├─ "summarize" ─► summarize_node           │
  │       └─ "agent"     ─► agent_node (direct)      │
  │                                                  │
  │  summarize_node  (llm_plain, no tools)           │
  │    • condenses messages[:-KEEP] into 1 summary   │
  │    • uses RemoveMessage to discard old entries   │
  │    → agent_node                                  │
  │                                                  │
  │  agent_node  (llm + tools, ChatMistralAI)        │
  │    • applies intent-routing hint to last message │
  │    • calls Mistral                               │
  │    → tool_calls? ─► ToolNode ─► back            │
  │    → done?       ─► END                         │
  └──────────────────────────────────────────────────┘
      │
      ▼
  MongoDBSaver  ←→  Atlas: jobagent.checkpoints
  (thread_id = str(telegram user_id))

Conversation summarization
──────────────────────────
When the persisted message count exceeds SUMMARIZE_THRESHOLD (20),
the summarize_node fires before the agent.  It:
  1. Takes all messages except the last MESSAGES_TO_KEEP (6)
  2. Asks Mistral (no tools) for a 2-3 sentence summary
  3. Removes the old messages via RemoveMessage
  4. Inserts a single "[Summary of earlier conversation]: …" message
Result: the agent always sees [system, summary?, last-6] — never an
unbounded history that confuses smaller models.

Intent routing
──────────────
mistral-small-latest is a capable but small model that sometimes
responds with a greeting instead of calling list_jobs when it sees
"Jobs in Japan" or "Show me Korea roles".

_apply_routing_hint() detects regional / job-query patterns and appends
a direct instruction — e.g. "[MUST call list_jobs(region='japan')]" —
to the last human message just before the LLM invocation.  This hint is
NEVER saved to the checkpoint, so the user never sees it and the
conversation history stays clean.
"""

from __future__ import annotations

import os
import re
from typing import Annotated

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_mistralai import ChatMistralAI
from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from typing_extensions import TypedDict

from bot.tools import ALL_TOOLS

load_dotenv()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_MODEL   = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
MONGODB_URI     = os.getenv("MONGODB_URI", "")


# ─────────────────────────────────────────
#  System prompt
# ─────────────────────────────────────────

_SYSTEM_PROMPT_BASE = (
    "You are a job search assistant specialising in the Asian job market — "
    "particularly Japan, South Korea, and Thailand. "
    "You help users find jobs, understand listings, explain company cultures, "
    "decode job requirements, and give career advice for working in these countries.\n\n"

    "TOOL USAGE — follow these rules strictly:\n"
    "- When a user asks about jobs, listings, roles, or openings in ANY region, "
    "you MUST call list_jobs or search_jobs FIRST before composing your reply. "
    "Never answer job queries from memory or training data.\n"
    "- If the tool returns no data, tell the user clearly that no listings are "
    "in the database yet and to check back later.\n"
    "- Never mention CLI commands, script names, or technical instructions. "
    "Tell users that data updates happen automatically in the background.\n\n"

    "SUBSCRIPTIONS — you manage this bot's notification system. "
    "When a user asks about their subscription status or job alerts, "
    "call check_subscription_status with their Telegram ID (provided below). "
    "For subscribe/unsubscribe actions, tell them to use /subscribe or /unsubscribe.\n\n"

    "SCOPE — only answer questions about jobs, careers, working in Asia, "
    "and this bot's notification subscription feature. "
    "For anything else (weather, news, trivia), politely redirect.\n\n"

    "FORMATTING — replies are sent via Telegram:\n"
    "- *bold* for emphasis (single asterisks only)\n"
    "- _italic_ for secondary info\n"
    "- `code` for technical terms or URLs\n"
    "- - for bullet points\n"
    "- No markdown headers (###, ##, #) — ever\n"
    "- No **double asterisks**\n"
    "- Keep job listings as clean bullet lists, not walls of text\n\n"

    "Current user's Telegram ID: {user_id}"
)


# ─────────────────────────────────────────
#  State
# ─────────────────────────────────────────

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


# ─────────────────────────────────────────
#  LLM instances
# ─────────────────────────────────────────

# llm_plain: no tools bound — used exclusively for conversation summarization
# so it can never accidentally invoke scraper / search tools mid-summary.
llm_plain = ChatMistralAI(api_key=MISTRAL_API_KEY, model=MISTRAL_MODEL)

# llm: full agent with tool-calling enabled
llm = llm_plain.bind_tools(ALL_TOOLS)


# ─────────────────────────────────────────
#  Intent routing
# ─────────────────────────────────────────

_REGION_HINTS: dict[str, list[str]] = {
    "japan":    ["japan", "japanese", "tokyo", "osaka"],
    "korea":    ["korea", "korean", "seoul", "south korea"],
    "thailand": ["thailand", "thai", "bangkok"],
}

_JOB_TERMS_RE = re.compile(
    r"\b(job|jobs|work|career|careers|role|roles|position|positions|"
    r"opening|openings|listing|listings|hiring|vacancy|vacancies|"
    r"opportunity|opportunities)\b",
    re.IGNORECASE,
)


def _apply_routing_hint(text: str) -> str:
    """
    Detect regional / job-query patterns and append a direct tool-call
    instruction to the message text, e.g.:
        "Jobs in Japan"
        → "Jobs in Japan\n[MUST call list_jobs(region='japan') now.]"

    The hint is applied only inside agent_node's local copy of the message
    list — it is NEVER written back to the LangGraph state / MongoDB
    checkpoint, so users never see it and history stays clean.
    """
    lower = text.lower()

    detected_region = next(
        (region for region, kws in _REGION_HINTS.items() if any(k in lower for k in kws)),
        None,
    )
    is_job_query = bool(_JOB_TERMS_RE.search(lower))

    # "Jobs in Japan" / "Show me Korea roles" / "Any openings in Thailand?"
    if detected_region and is_job_query:
        return (
            f"{text}\n"
            f"[MUST call list_jobs(region=\"{detected_region}\") now to answer this.]"
        )

    # Short regional reference — e.g. "In Japan?" / "Thailand?" — likely job-related
    if detected_region and len(text.split()) <= 6:
        return (
            f"{text}\n"
            f"[MUST call list_jobs(region=\"{detected_region}\") now to answer this.]"
        )

    # Generic job query without a specific region — e.g. "Show me some jobs"
    if is_job_query:
        return (
            f"{text}\n"
            "[MUST call list_jobs or search_jobs now to answer this.]"
        )

    return text


# ─────────────────────────────────────────
#  Conversation summarization
# ─────────────────────────────────────────

_SUMMARIZE_THRESHOLD = 20   # trigger when history exceeds this many messages
_MESSAGES_TO_KEEP    = 6    # always preserve the most recent N messages intact


def _should_summarize(state: AgentState) -> str:
    """Routing function: decides whether to run summarization before the agent."""
    return "summarize" if len(state["messages"]) > _SUMMARIZE_THRESHOLD else "agent"


async def summarize_node(state: AgentState, config: RunnableConfig) -> dict:
    """
    Condense old messages into a brief summary to keep context manageable.

    Flow:
      1. Split messages into to_summarize (older) and to_keep (recent 6)
      2. Call llm_plain to produce a 2-3 sentence summary
      3. Return RemoveMessage objects for all old messages + the summary
         as a new HumanMessage — LangGraph's add_messages reducer handles
         the removal and insertion atomically.
    """
    messages     = state["messages"]
    to_summarize = messages[:-_MESSAGES_TO_KEEP]
    # to_keep    = messages[-_MESSAGES_TO_KEEP:]  # implicitly kept

    # Build a readable transcript (skip empty messages)
    transcript_lines: list[str] = []
    for m in to_summarize:
        if isinstance(m, SystemMessage):
            continue
        role    = "User" if isinstance(m, HumanMessage) else "Assistant"
        content = (getattr(m, "content", "") or "")[:300].strip()
        if content:
            transcript_lines.append(f"{role}: {content}")

    if not transcript_lines:
        return {}  # nothing to summarize — leave state unchanged

    summary_prompt = (
        "Summarise this conversation in 2-3 sentences. "
        "Capture: which job regions the user is interested in, "
        "any role types or keywords mentioned, specific preferences, "
        "and any important context.\n\n"
        + "\n".join(transcript_lines)
    )

    # Remove old messages regardless — if the LLM call fails we still trim
    removes = [
        RemoveMessage(id=m.id)
        for m in to_summarize
        if getattr(m, "id", None) is not None
    ]

    try:
        summary_resp = await llm_plain.ainvoke([HumanMessage(content=summary_prompt)], config)
        summary_text = summary_resp.content or ""
    except Exception as exc:
        print(f"[agent] Summarization failed (trimming silently): {exc}")
        return {"messages": removes}

    summary_msg = HumanMessage(
        content=f"[Summary of earlier conversation]: {summary_text}"
    )
    return {"messages": removes + [summary_msg]}


# ─────────────────────────────────────────
#  Agent node
# ─────────────────────────────────────────

async def agent_node(state: AgentState, config: RunnableConfig) -> dict:
    """
    Core reasoning step: prepend system prompt, apply intent-routing hint
    to the last user message, then call the LLM.

    Both the system prompt injection and the routing hint operate on a
    local copy of the message list — they are NOT returned in the state
    dict, so they are never persisted to the MongoDB checkpoint.
    """
    messages = list(state["messages"])

    # Prepend system prompt if not already present in this invocation's context
    if not any(isinstance(m, SystemMessage) for m in messages):
        thread_id = config.get("configurable", {}).get("thread_id", "unknown")
        system    = _SYSTEM_PROMPT_BASE.format(user_id=thread_id)
        messages  = [SystemMessage(content=system)] + messages

    # Apply intent routing hint to the last HumanMessage (local copy only)
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            augmented = _apply_routing_hint(messages[i].content)
            if augmented != messages[i].content:
                messages = (
                    messages[:i]
                    + [HumanMessage(content=augmented)]
                    + messages[i + 1 :]
                )
            break

    response = await llm.ainvoke(messages, config)
    return {"messages": [response]}


# ─────────────────────────────────────────
#  Graph
# ─────────────────────────────────────────

def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("summarize", summarize_node)
    graph.add_node("agent",     agent_node)
    graph.add_node("tools",     ToolNode(ALL_TOOLS))

    # Route from START: summarize if history is long, else go straight to agent
    graph.add_conditional_edges(
        START,
        _should_summarize,
        {"summarize": "summarize", "agent": "agent"},
    )
    graph.add_edge("summarize", "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")

    return graph


# ─────────────────────────────────────────
#  Public factory
# ─────────────────────────────────────────

def create_agent(mongo_client):
    """
    Compile the StateGraph with a MongoDBSaver checkpointer.

    Usage:
        agent = create_agent(MongoClient(MONGODB_URI))
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_text)]},
            config={"configurable": {"thread_id": str(user_id)}},
        )
        reply = result["messages"][-1].content
    """
    checkpointer = MongoDBSaver(mongo_client, db_name="jobagent")
    graph        = build_graph()
    return graph.compile(checkpointer=checkpointer)
